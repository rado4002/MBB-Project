"""Browser-authenticated, minimized operator conversation reads."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from pydantic import UUID4
from sqlalchemy import TIMESTAMP, String, cast, exists, func, literal, select, true, tuple_, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.internal_note import InternalNote
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.modules.m4_conversation.ownership import (
    ConversationNotFound as OwnershipConversationNotFound,
    IdempotencyConflict as OwnershipIdempotencyConflict,
    IdempotencyInProgress as OwnershipIdempotencyInProgress,
    OwnershipConflict,
    OwnershipSnapshot,
    OwnershipTransitionUnavailable,
    ReturnToAIDisabled,
    ReturnToAIUnavailable,
    transition_ownership,
)
from app.modules.m4_conversation.internal_notes import (
    InternalNoteConversationNotFound,
    InternalNoteIdempotencyConflict,
    InternalNoteResult,
    InternalNoteUnavailable,
    create_internal_note,
)
from app.modules.m4_conversation.operator_replies import (
    OperatorReplyResult,
    ReplyAcceptanceUnavailable,
    ReplyChannelUnsupported,
    ReplyConversationNotFound,
    ReplyIdempotencyConflict,
    ReplyNotEligible,
    ReplyOwnershipConflict,
    ReplyOwnershipVersionConflict,
    accept_operator_reply,
    mark_operator_reply_accepted,
)
from app.modules.m8_maps.operator_escalation import (
    ConversationNotFound,
    EscalationAlreadyOpen,
    IdempotencyConflict,
    IdempotencyInProgress,
    OperatorEscalationResult,
    OperatorEscalationUnavailable,
    create_operator_escalation,
)
from app.request_ids import normalize_or_generate_request_id
from app.schemas.common import ConversationStatus, Language
from app.schemas.operator_conversations import (
    OperatorConversationDetail,
    OperatorConversationOwnership,
    OperatorConversationQueueItem,
    OperatorConversationQueueResponse,
    OperatorCustomerSummary,
    OperatorLatestMessage,
    OperatorLeadSummary,
    OperatorHumanOwner,
    OperatorInternalNoteCreate,
    OperatorInternalNoteItem,
    OperatorMessageHistoryResponse,
    OperatorMessageItem,
    OperatorMessageMedia,
    OperatorOpenEscalation,
    OperatorReplyCreate,
    OperatorTimelineMessageItem,
    OperatorTimelineResponse,
    OperatorOwnershipTransitionRequest,
    OperatorOwnershipTransitionResponse,
)
from app.schemas.operator_escalations import (
    OperatorEscalationActor,
    OperatorEscalationCreate,
    OperatorEscalationResponse,
)

router = APIRouter(
    prefix="/operator/conversations",
    tags=["operator-conversations"],
)
log = structlog.get_logger(__name__)

_OPEN_ESCALATION_STATUSES = ("open", "in_progress")
_CURSOR_VERSION = 1
_CURSOR_SIGNATURE_BYTES = hashlib.sha256().digest_size
_MAX_CURSOR_LENGTH = 1024
_PRODUCT_INTEREST_LIMIT = 5
_PRODUCT_INTEREST_CHARACTER_LIMIT = 80
_PREVIEW_CHARACTER_LIMIT = 120

_MEDIA_PLACEHOLDERS = {
    "french": {
        "voice_note": "[Message vocal]",
        "image": "[Image]",
    },
    "lingala": {
        "voice_note": "[Nsango ya mongongo]",
        "image": "[Elilingi]",
    },
    "swahili": {
        "voice_note": "[Ujumbe wa sauti]",
        "image": "[Picha]",
    },
}


def _operator_access_predicate(principal: BrowserPrincipal):
    """Temporary all-conversation policy, isolated for later row-level scoping."""
    return literal(principal.account.role).in_(("administrator", "operator"))


def _open_escalation_exists():
    return exists(
        select(EscalationTicket.ticket_id).where(
            EscalationTicket.conversation_id == Conversation.conversation_id,
            EscalationTicket.status.in_(_OPEN_ESCALATION_STATUSES),
        )
    )


def _masked_phone_expression():
    # The complete phone remains inside PostgreSQL and is never selected into Python.
    return func.concat("***", func.right(Customer.phone_number, 4))


def _service_unavailable(exc: Exception) -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="SERVICE_UNAVAILABLE",
        message="The conversation service is temporarily unavailable.",
    )


def _validation_error() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status.HTTP_400_BAD_REQUEST,
        code="VALIDATION_ERROR",
        message="The pagination cursor is invalid.",
    )


def _cursor_secret(principal: BrowserPrincipal) -> bytes:
    secret = principal.session.state.settings.browser_session_hmac_secret.encode()
    if len(secret) < 32:
        raise _service_unavailable(RuntimeError("cursor signing unavailable"))
    return secret


def _encode_cursor(
    *,
    kind: Literal["conversation", "message", "timeline"],
    occurred_at: datetime,
    item_id: UUID,
    principal: BrowserPrincipal,
    conversation_id: UUID | None = None,
    item_kind: Literal["message", "internal_note"] | None = None,
) -> str:
    normalized = occurred_at.astimezone(timezone.utc)
    payload: dict[str, Any] = {
        "v": _CURSOR_VERSION,
        "k": kind,
        "t": normalized.isoformat(timespec="microseconds"),
        "i": str(item_id),
    }
    if conversation_id is not None:
        payload["c"] = str(conversation_id)
    if kind == "timeline":
        if item_kind not in {"message", "internal_note"}:
            raise _service_unavailable(RuntimeError("timeline cursor kind is unavailable"))
        payload["x"] = item_kind
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(
        _cursor_secret(principal),
        b"operator-read-cursor:v1:" + encoded,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(encoded + signature).rstrip(b"=").decode()


def _decode_cursor(
    token: str,
    *,
    kind: Literal["conversation", "message", "timeline"],
    principal: BrowserPrincipal,
    conversation_id: UUID | None = None,
) -> tuple[datetime, UUID] | tuple[datetime, str, UUID]:
    if not token or len(token) > _MAX_CURSOR_LENGTH or not token.isascii():
        raise _validation_error()
    try:
        padding = "=" * (-len(token) % 4)
        combined = base64.b64decode(
            token + padding,
            altchars=b"-_",
            validate=True,
        )
        canonical = base64.urlsafe_b64encode(combined).rstrip(b"=").decode()
        if not hmac.compare_digest(token, canonical):
            raise ValueError("non-canonical cursor encoding")
        if len(combined) <= _CURSOR_SIGNATURE_BYTES:
            raise ValueError("short cursor")
        encoded = combined[:-_CURSOR_SIGNATURE_BYTES]
        signature = combined[-_CURSOR_SIGNATURE_BYTES:]
        expected = hmac.new(
            _cursor_secret(principal),
            b"operator-read-cursor:v1:" + encoded,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid cursor signature")
        payload = json.loads(encoded)
        if (
            not isinstance(payload, dict)
            or payload.get("v") != _CURSOR_VERSION
            or payload.get("k") != kind
        ):
            raise ValueError("invalid cursor purpose")
        if conversation_id is not None and payload.get("c") != str(conversation_id):
            raise ValueError("cursor belongs to another conversation")
        occurred_at = datetime.fromisoformat(payload["t"])
        if occurred_at.tzinfo is None:
            raise ValueError("cursor timestamp has no timezone")
        item_id = UUID(payload["i"])
        item_kind = payload.get("x")
        if kind == "timeline" and item_kind not in {"message", "internal_note"}:
            raise ValueError("invalid timeline item kind")
    except BrowserAuthError:
        raise
    except (
        binascii.Error,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise _validation_error() from exc
    if kind == "timeline":
        return occurred_at.astimezone(timezone.utc), item_kind, item_id
    return occurred_at.astimezone(timezone.utc), item_id


def _preview(content: str, content_type: str, language: str) -> str:
    if content_type == "text":
        return content[:_PREVIEW_CHARACTER_LIMIT]
    placeholders = _MEDIA_PLACEHOLDERS.get(language, _MEDIA_PLACEHOLDERS["french"])
    return placeholders.get(content_type, "[Contenu non textuel]")


def _display_interests(value: list[str] | None) -> list[str]:
    return [
        interest[:_PRODUCT_INTEREST_CHARACTER_LIMIT]
        for interest in (value or [])[:_PRODUCT_INTEREST_LIMIT]
        if isinstance(interest, str)
    ]


def _operator_error(
    *,
    status_code: int,
    code: str,
    message: str,
) -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status_code,
        code=code,
        message=message,
    )


def _escalation_response(
    result: OperatorEscalationResult,
) -> OperatorEscalationResponse:
    ticket = result.ticket
    if (
        ticket.operator_reason is None
        or ticket.escalation_type is None
        or ticket.created_by_account_id is None
        or ticket.source != "operator_browser"
    ):
        raise OperatorEscalationUnavailable(
            "operator escalation result is incomplete"
        )
    return OperatorEscalationResponse(
        escalation_id=ticket.ticket_id,
        conversation_id=ticket.conversation_id,
        status="open",
        reason=ticket.operator_reason,
        type=ticket.escalation_type,
        priority=ticket.priority,
        source=ticket.source,
        created_at=ticket.created_at,
        created_by=OperatorEscalationActor(
            account_id=ticket.created_by_account_id,
            display_name=result.actor_display_name,
        ),
    )


def _ownership_response(
    ownership: OwnershipSnapshot,
) -> OperatorConversationOwnership:
    human_owner = None
    if (
        ownership.human_owner_account_id is not None
        and ownership.human_owner_display_name is not None
    ):
        human_owner = OperatorHumanOwner(
            account_id=ownership.human_owner_account_id,
            display_name=ownership.human_owner_display_name,
        )
    return OperatorConversationOwnership(
        owner_type=ownership.owner_type,
        human_owner=human_owner,
        ai_execution_state=ownership.ai_execution_state,
        version=ownership.version,
        updated_at=ownership.updated_at,
    )


def _ownership_from_row(row: Any) -> OperatorConversationOwnership:
    return _ownership_response(
        OwnershipSnapshot(
            conversation_id=row["conversation_id"],
            owner_type=row["owner_type"],
            human_owner_account_id=row["human_owner_account_id"],
            human_owner_display_name=row["human_owner_display_name"],
            ai_execution_state=row["ai_execution_state"],
            version=row["ownership_version"],
            updated_at=row["ownership_updated_at"],
        )
    )


def _operator_message_item(
    *,
    message_id: UUID,
    occurred_at: datetime,
    direction: str,
    content_type: str,
    content: str,
    language: str,
    operator_author_account_id: UUID | None,
    author_display_name: str | None,
    delivery_state: str | None,
    delivery_state_timestamp: datetime | None,
) -> OperatorMessageItem:
    is_text = content_type == "text"
    operator_author = None
    if operator_author_account_id is not None and author_display_name is not None:
        operator_author = OperatorHumanOwner(
            account_id=operator_author_account_id,
            display_name=author_display_name,
        )
    return OperatorMessageItem(
        message_id=message_id,
        occurred_at=occurred_at,
        direction=direction,
        sender_type=(
            "customer"
            if direction == "inbound"
            else "operator" if operator_author is not None else "unknown"
        ),
        operator_author=operator_author,
        delivery_state=delivery_state,
        delivery_state_timestamp=delivery_state_timestamp,
        content_type=content_type,
        text=content if is_text else None,
        media=(
            None
            if is_text
            else OperatorMessageMedia(kind=content_type, available=False)
        ),
        language=language,
    )


def _reply_response(result: OperatorReplyResult) -> OperatorMessageItem:
    message = result.message
    response = _operator_message_item(
        message_id=message.message_id,
        occurred_at=message.timestamp,
        direction=message.direction,
        content_type=message.content_type,
        content=message.content,
        language=message.language,
        operator_author_account_id=message.operator_author_account_id,
        author_display_name=message.author_display_name,
        delivery_state=message.delivery_state,
        delivery_state_timestamp=message.delivery_state_timestamp,
    )
    # POST replies report the durable acceptance result. Later history reads
    # expose any subsequent worker transition to sent, failed, or uncertain.
    return response.model_copy(
        update={
            "delivery_state": "accepted",
            "delivery_state_timestamp": message.timestamp,
        }
    )


def _internal_note_response(
    result: InternalNoteResult,
) -> OperatorInternalNoteItem:
    note = result.note
    return OperatorInternalNoteItem(
        note_id=note.note_id,
        occurred_at=note.created_at,
        author=OperatorHumanOwner(
            account_id=note.author_account_id,
            display_name=note.author_display_name,
        ),
        text=note.content,
    )


def _timeline_message_item(row: Any) -> OperatorTimelineMessageItem:
    message = _operator_message_item(
        message_id=row["item_id"],
        occurred_at=row["occurred_at"],
        direction=row["direction"],
        content_type=row["content_type"],
        content=row["content"],
        language=row["language"],
        operator_author_account_id=row["author_account_id"],
        author_display_name=row["author_display_name"],
        delivery_state=row["delivery_state"],
        delivery_state_timestamp=row["delivery_state_timestamp"],
    )
    return OperatorTimelineMessageItem(**message.model_dump())


def _timeline_note_item(row: Any) -> OperatorInternalNoteItem:
    return OperatorInternalNoteItem(
        note_id=row["item_id"],
        occurred_at=row["occurred_at"],
        author=OperatorHumanOwner(
            account_id=row["author_account_id"],
            display_name=row["author_display_name"],
        ),
        text=row["content"],
    )


def _publish_operator_reply(message_id: UUID) -> None:
    from app.tasks.celery_app import celery_app

    celery_app.send_task(
        "operator_replies.deliver",
        kwargs={"message_id": str(message_id)},
        queue="default",
        task_id=f"operator-reply-{message_id}",
    )


@router.get("", response_model=OperatorConversationQueueResponse)
async def list_operator_conversations(
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 25,
    cursor: Annotated[str | None, Query(max_length=_MAX_CURSOR_LENGTH)] = None,
    conversation_status: Annotated[
        ConversationStatus | None, Query(alias="status")
    ] = None,
    escalation_state: Annotated[
        Literal["open", "none"] | None, Query()
    ] = None,
    language: Annotated[Language | None, Query()] = None,
) -> OperatorConversationQueueResponse:
    latest_message = (
        select(
            Message.content.label("latest_content"),
            Message.content_type.label("latest_content_type"),
            Message.direction.label("latest_direction"),
            Message.timestamp.label("latest_occurred_at"),
        )
        .where(Message.conversation_id == Conversation.conversation_id)
        .order_by(Message.timestamp.desc(), Message.message_id.desc())
        .limit(1)
        .lateral("latest_message")
    )
    open_escalation = _open_escalation_exists()
    statement = (
        select(
            Conversation.conversation_id,
            Conversation.last_message_time,
            Conversation.language_detected,
            Conversation.status,
            Conversation.message_count,
            Conversation.owner_type,
            Conversation.human_owner_account_id,
            Conversation.ai_execution_state,
            Conversation.ownership_version,
            Conversation.ownership_updated_at,
            Customer.name.label("customer_display_name"),
            _masked_phone_expression().label("customer_phone_masked"),
            latest_message.c.latest_content,
            latest_message.c.latest_content_type,
            latest_message.c.latest_direction,
            latest_message.c.latest_occurred_at,
            open_escalation.label("has_open_escalation"),
            OperatorAccount.display_name.label("human_owner_display_name"),
        )
        .join(Customer, Customer.phone_number == Conversation.customer_id)
        .outerjoin(
            OperatorAccount,
            OperatorAccount.account_id == Conversation.human_owner_account_id,
        )
        .outerjoin(latest_message, true())
        .where(_operator_access_predicate(principal))
        .order_by(
            Conversation.last_message_time.desc(),
            Conversation.conversation_id.desc(),
        )
        .limit(limit + 1)
    )
    if conversation_status is not None:
        statement = statement.where(Conversation.status == conversation_status.value)
    if language is not None:
        statement = statement.where(Conversation.language_detected == language.value)
    if escalation_state == "open":
        statement = statement.where(open_escalation)
    elif escalation_state == "none":
        statement = statement.where(~open_escalation)
    if cursor is not None:
        cursor_time, cursor_id = _decode_cursor(
            cursor, kind="conversation", principal=principal
        )
        statement = statement.where(
            tuple_(Conversation.last_message_time, Conversation.conversation_id)
            < tuple_(cursor_time, cursor_id)
        )

    try:
        rows = (await db.execute(statement)).mappings().all()
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc

    has_more = len(rows) > limit
    page = rows[:limit]
    items: list[OperatorConversationQueueItem] = []
    for row in page:
        latest = None
        awaiting_response_since = None
        if row["latest_occurred_at"] is not None:
            latest = OperatorLatestMessage(
                preview=_preview(
                    row["latest_content"],
                    row["latest_content_type"],
                    row["language_detected"],
                ),
                content_type=row["latest_content_type"],
                direction=row["latest_direction"],
                occurred_at=row["latest_occurred_at"],
            )
            if row["latest_direction"] == "inbound":
                awaiting_response_since = row["latest_occurred_at"]
        items.append(
            OperatorConversationQueueItem(
                conversation_id=row["conversation_id"],
                customer=OperatorCustomerSummary(
                    display_name=row["customer_display_name"],
                    phone_masked=row["customer_phone_masked"],
                ),
                language=row["language_detected"],
                status=row["status"],
                message_count=row["message_count"],
                latest_message=latest,
                awaiting_response_since=awaiting_response_since,
                open_escalation=OperatorOpenEscalation(
                    exists=row["has_open_escalation"]
                ),
                ownership=_ownership_from_row(row),
            )
        )

    next_cursor = None
    if has_more and page:
        boundary = page[-1]
        next_cursor = _encode_cursor(
            kind="conversation",
            occurred_at=boundary["last_message_time"],
            item_id=boundary["conversation_id"],
            principal=principal,
        )
    response.headers["Cache-Control"] = "no-store"
    return OperatorConversationQueueResponse(items=items, next_cursor=next_cursor)


@router.post(
    "/{conversation_id}/escalations",
    response_model=OperatorEscalationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_operator_conversation_escalation(
    conversation_id: UUID,
    body: OperatorEscalationCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _escalation_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("escalation.create"))
    ],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OperatorEscalationResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await create_operator_escalation(
            db,
            conversation_id=conversation_id,
            reason=body.reason,
            escalation_type=body.type,
            priority=body.priority,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            idempotency_key=idempotency_key,
            idempotency_secret=settings.browser_idempotency_hmac_secret,
            request_id=request_id,
            source_network_fingerprint=(
                principal.session.record.ip_prefix_fingerprint
            ),
            user_agent_fingerprint=(
                principal.session.record.user_agent_fingerprint
            ),
        )
        payload = _escalation_response(result)
    except ConversationNotFound as exc:
        raise _operator_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        ) from exc
    except IdempotencyConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another request.",
        ) from exc
    except IdempotencyInProgress as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_IN_PROGRESS",
            message="The original request is still in progress.",
        ) from exc
    except EscalationAlreadyOpen as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="ESCALATION_ALREADY_OPEN",
            message="This conversation already has an active escalation.",
        ) from exc
    except OperatorEscalationUnavailable as exc:
        raise _operator_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="SERVICE_UNAVAILABLE",
            message="The escalation service is temporarily unavailable.",
        ) from exc

    response.status_code = (
        status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = (
        "true" if result.replayed else "false"
    )
    return payload


@router.post(
    "/{conversation_id}/ownership",
    response_model=OperatorOwnershipTransitionResponse,
)
async def change_operator_conversation_ownership(
    conversation_id: UUID,
    body: OperatorOwnershipTransitionRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _read_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    _ownership_principal: Annotated[
        BrowserPrincipal,
        Depends(require_capability("conversation.ownership.change")),
    ],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OperatorOwnershipTransitionResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await transition_ownership(
            db,
            conversation_id=conversation_id,
            target_owner_type=body.target_owner_type,
            expected_version=body.expected_version,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            idempotency_key=idempotency_key,
            idempotency_secret=settings.browser_idempotency_hmac_secret,
            request_id=request_id,
            ai_adapter=settings.ai_adapter,
            source_network_fingerprint=(
                principal.session.record.ip_prefix_fingerprint
            ),
            user_agent_fingerprint=(
                principal.session.record.user_agent_fingerprint
            ),
        )
    except OwnershipConversationNotFound as exc:
        raise _operator_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        ) from exc
    except OwnershipIdempotencyConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another request.",
        ) from exc
    except OwnershipIdempotencyInProgress as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_IN_PROGRESS",
            message="The unchanged ownership request is still in progress.",
        ) from exc
    except OwnershipConflict as exc:
        owner = (
            exc.current.human_owner_display_name
            if exc.current.owner_type == "human"
            else "MBB AI Assistant"
        ) or "another Human Operator"
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="OWNERSHIP_CONFLICT",
            message=f"This conversation is now controlled by {owner}.",
        ) from exc
    except ReturnToAIDisabled as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="AI_DISABLED",
            message=(
                "The MBB AI Assistant is currently disabled. "
                "This conversation remains under human control."
            ),
        ) from exc
    except ReturnToAIUnavailable as exc:
        raise _operator_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="AI_UNAVAILABLE",
            message=(
                "The MBB AI Assistant is currently unavailable. "
                "This conversation remains under human control."
            ),
        ) from exc
    except OwnershipTransitionUnavailable as exc:
        raise _operator_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="SERVICE_UNAVAILABLE",
            message="The ownership service is temporarily unavailable.",
        ) from exc

    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = (
        "true" if result.replayed else "false"
    )
    return OperatorOwnershipTransitionResponse(
        conversation_id=conversation_id,
        ownership=_ownership_response(result.ownership),
    )


@router.post(
    "/{conversation_id}/notes",
    response_model=OperatorInternalNoteItem,
    status_code=status.HTTP_201_CREATED,
)
async def create_operator_internal_note(
    conversation_id: UUID,
    body: OperatorInternalNoteCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _read_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    _note_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("internal_note.create"))
    ],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OperatorInternalNoteItem:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await create_internal_note(
            db,
            note_id=idempotency_key,
            conversation_id=conversation_id,
            content=body.text,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            idempotency_secret=settings.browser_idempotency_hmac_secret,
            request_id=request_id,
            settings=settings,
            source_network_fingerprint=(
                principal.session.record.ip_prefix_fingerprint
            ),
            user_agent_fingerprint=(
                principal.session.record.user_agent_fingerprint
            ),
        )
    except InternalNoteConversationNotFound as exc:
        raise _operator_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        ) from exc
    except InternalNoteIdempotencyConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another internal note.",
        ) from exc
    except InternalNoteUnavailable as exc:
        raise _operator_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="SERVICE_UNAVAILABLE",
            message="The internal note service is temporarily unavailable.",
        ) from exc

    response.status_code = (
        status.HTTP_200_OK if result.replayed else status.HTTP_201_CREATED
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = (
        "true" if result.replayed else "false"
    )
    return _internal_note_response(result)


@router.post(
    "/{conversation_id}/replies",
    response_model=OperatorMessageItem,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_operator_reply(
    conversation_id: UUID,
    body: OperatorReplyCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _read_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    _reply_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("message.reply"))
    ],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OperatorMessageItem:
    validate_state_changing_request(request, settings)
    try:
        result = await accept_operator_reply(
            db,
            conversation_id=conversation_id,
            text=body.text,
            expected_ownership_version=body.expected_ownership_version,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            message_id=idempotency_key,
            messaging_adapter=settings.messaging_adapter,
            whatsapp_mode=settings.whatsapp_mode,
        )
    except ReplyConversationNotFound as exc:
        raise _operator_error(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        ) from exc
    except ReplyIdempotencyConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another reply.",
        ) from exc
    except ReplyOwnershipConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="OWNERSHIP_CONFLICT",
            message="Only the current Human owner can reply to this conversation.",
        ) from exc
    except ReplyOwnershipVersionConflict as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="OWNERSHIP_VERSION_CONFLICT",
            message="Conversation ownership changed. Refresh before replying.",
        ) from exc
    except ReplyNotEligible as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="CONVERSATION_NOT_REPLY_ELIGIBLE",
            message="This conversation cannot currently receive replies.",
        ) from exc
    except ReplyChannelUnsupported as exc:
        raise _operator_error(
            status_code=status.HTTP_409_CONFLICT,
            code="CHANNEL_NOT_REPLY_ELIGIBLE",
            message="This conversation channel cannot receive operator replies.",
        ) from exc
    except ReplyAcceptanceUnavailable as exc:
        raise _operator_error(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="SERVICE_UNAVAILABLE",
            message="The reply service is temporarily unavailable.",
        ) from exc

    if result.message.delivery_state is None:
        try:
            _publish_operator_reply(result.message.message_id)
        except Exception as exc:
            log.error(
                "operator_reply.publication_failed",
                message_id=str(result.message.message_id),
                error_type=type(exc).__name__,
            )
            raise _operator_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="REPLY_PUBLICATION_FAILED",
                message="The reply was saved but could not be queued. Retry unchanged.",
            ) from exc
        try:
            accepted_message = await mark_operator_reply_accepted(
                db, result.message.message_id
            )
            result = OperatorReplyResult(accepted_message, replayed=result.replayed)
        except ReplyAcceptanceUnavailable as exc:
            raise _operator_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                code="SERVICE_UNAVAILABLE",
                message="The queued reply could not be confirmed. Retry unchanged.",
            ) from exc

    response.status_code = (
        status.HTTP_200_OK if result.replayed else status.HTTP_202_ACCEPTED
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = (
        "true" if result.replayed else "false"
    )
    return _reply_response(result)


@router.get(
    "/{conversation_id}/timeline",
    response_model=OperatorTimelineResponse,
)
async def get_operator_timeline(
    conversation_id: UUID,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    _message_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("message.read"))
    ],
    _note_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("internal_note.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    before: Annotated[str | None, Query(max_length=_MAX_CURSOR_LENGTH)] = None,
) -> OperatorTimelineResponse:
    access_statement = select(Conversation.conversation_id).where(
        Conversation.conversation_id == conversation_id,
        _operator_access_predicate(principal),
    )
    try:
        accessible_id = await db.scalar(access_statement)
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc
    if accessible_id is None:
        raise BrowserAuthError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        )

    message_items = select(
        literal("message").label("kind"),
        Message.message_id.label("item_id"),
        Message.timestamp.label("occurred_at"),
        Message.direction.label("direction"),
        Message.content_type.label("content_type"),
        Message.content.label("content"),
        Message.language.label("language"),
        Message.operator_author_account_id.label("author_account_id"),
        Message.author_display_name.label("author_display_name"),
        Message.delivery_state.label("delivery_state"),
        Message.delivery_state_timestamp.label("delivery_state_timestamp"),
    ).where(
        Message.conversation_id == conversation_id,
        (Message.operator_author_account_id.is_(None))
        | (Message.delivery_state.is_not(None)),
    )
    note_items = select(
        literal("internal_note").label("kind"),
        InternalNote.note_id.label("item_id"),
        InternalNote.created_at.label("occurred_at"),
        cast(literal(None), String(10)).label("direction"),
        cast(literal(None), String(20)).label("content_type"),
        InternalNote.content.label("content"),
        cast(literal(None), String(10)).label("language"),
        InternalNote.author_account_id.label("author_account_id"),
        InternalNote.author_display_name.label("author_display_name"),
        cast(literal(None), String(20)).label("delivery_state"),
        cast(literal(None), TIMESTAMP(timezone=True)).label(
            "delivery_state_timestamp"
        ),
    ).where(InternalNote.conversation_id == conversation_id)
    timeline = union_all(message_items, note_items).subquery("operator_timeline")
    statement = (
        select(timeline)
        .order_by(
            timeline.c.occurred_at.desc(),
            timeline.c.kind.desc(),
            timeline.c.item_id.desc(),
        )
        .limit(limit + 1)
    )
    if before is not None:
        before_time, before_kind, before_id = _decode_cursor(
            before,
            kind="timeline",
            principal=principal,
            conversation_id=conversation_id,
        )
        statement = statement.where(
            tuple_(timeline.c.occurred_at, timeline.c.kind, timeline.c.item_id)
            < tuple_(before_time, before_kind, before_id)
        )
    try:
        rows = (await db.execute(statement)).mappings().all()
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc

    has_more = len(rows) > limit
    newest_first = rows[:limit]
    items = [
        _timeline_message_item(row)
        if row["kind"] == "message"
        else _timeline_note_item(row)
        for row in reversed(newest_first)
    ]
    next_older_cursor = None
    if has_more and newest_first:
        boundary = newest_first[-1]
        next_older_cursor = _encode_cursor(
            kind="timeline",
            occurred_at=boundary["occurred_at"],
            item_id=boundary["item_id"],
            item_kind=boundary["kind"],
            conversation_id=conversation_id,
            principal=principal,
        )
    response.headers["Cache-Control"] = "no-store"
    return OperatorTimelineResponse(
        items=items,
        next_older_cursor=next_older_cursor,
    )


@router.get("/{conversation_id}", response_model=OperatorConversationDetail)
async def get_operator_conversation(
    conversation_id: UUID,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorConversationDetail:
    open_escalation = _open_escalation_exists()
    statement = (
        select(
            Conversation.conversation_id,
            Conversation.status,
            Conversation.language_detected,
            Conversation.message_count,
            Conversation.updated_at,
            Conversation.owner_type,
            Conversation.human_owner_account_id,
            Conversation.ai_execution_state,
            Conversation.ownership_version,
            Conversation.ownership_updated_at,
            Customer.name.label("customer_display_name"),
            _masked_phone_expression().label("customer_phone_masked"),
            Lead.score.label("lead_score"),
            Lead.stage.label("lead_stage"),
            Lead.intent.label("lead_intent"),
            Lead.product_interest[1:_PRODUCT_INTEREST_LIMIT].label(
                "lead_product_interests"
            ),
            open_escalation.label("has_open_escalation"),
            OperatorAccount.display_name.label("human_owner_display_name"),
        )
        .join(Customer, Customer.phone_number == Conversation.customer_id)
        .outerjoin(
            OperatorAccount,
            OperatorAccount.account_id == Conversation.human_owner_account_id,
        )
        .outerjoin(Lead, Lead.conversation_id == Conversation.conversation_id)
        .where(
            Conversation.conversation_id == conversation_id,
            _operator_access_predicate(principal),
        )
    )
    try:
        row = (await db.execute(statement)).mappings().one_or_none()
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc
    if row is None:
        raise BrowserAuthError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        )

    lead = None
    if row["lead_score"] is not None:
        lead = OperatorLeadSummary(
            score=row["lead_score"],
            stage=row["lead_stage"],
            intent=row["lead_intent"],
            product_interests=_display_interests(row["lead_product_interests"]),
        )
    response.headers["Cache-Control"] = "no-store"
    return OperatorConversationDetail(
        conversation_id=row["conversation_id"],
        status=row["status"],
        language=row["language_detected"],
        message_count=row["message_count"],
        updated_at=row["updated_at"],
        customer=OperatorCustomerSummary(
            display_name=row["customer_display_name"],
            phone_masked=row["customer_phone_masked"],
        ),
        lead=lead,
        open_escalation=OperatorOpenEscalation(
            exists=row["has_open_escalation"]
        ),
        ownership=_ownership_from_row(row),
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=OperatorMessageHistoryResponse,
)
async def get_operator_message_history(
    conversation_id: UUID,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    _message_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("message.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    before: Annotated[str | None, Query(max_length=_MAX_CURSOR_LENGTH)] = None,
) -> OperatorMessageHistoryResponse:
    access_statement = select(Conversation.conversation_id).where(
        Conversation.conversation_id == conversation_id,
        _operator_access_predicate(principal),
    )
    try:
        accessible_id = await db.scalar(access_statement)
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc
    if accessible_id is None:
        raise BrowserAuthError(
            status_code=status.HTTP_404_NOT_FOUND,
            code="CONVERSATION_NOT_FOUND",
            message="The conversation was not found.",
        )

    statement = (
        select(
            Message.message_id,
            Message.timestamp.label("occurred_at"),
            Message.direction,
            Message.content_type,
            Message.content,
            Message.language,
            Message.operator_author_account_id,
            Message.author_display_name,
            Message.delivery_state,
            Message.delivery_state_timestamp,
        )
        .where(Message.conversation_id == conversation_id)
        .where(
            (Message.operator_author_account_id.is_(None))
            | (Message.delivery_state.is_not(None))
        )
        .order_by(Message.timestamp.desc(), Message.message_id.desc())
        .limit(limit + 1)
    )
    if before is not None:
        before_time, before_id = _decode_cursor(
            before,
            kind="message",
            principal=principal,
            conversation_id=conversation_id,
        )
        statement = statement.where(
            tuple_(Message.timestamp, Message.message_id)
            < tuple_(before_time, before_id)
        )
    try:
        rows = (await db.execute(statement)).mappings().all()
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _service_unavailable(exc) from exc

    has_more = len(rows) > limit
    newest_first = rows[:limit]
    items: list[OperatorMessageItem] = []
    for row in reversed(newest_first):
        items.append(
            _operator_message_item(
                message_id=row["message_id"],
                occurred_at=row["occurred_at"],
                direction=row["direction"],
                content_type=row["content_type"],
                content=row["content"],
                language=row["language"],
                operator_author_account_id=row.get("operator_author_account_id"),
                author_display_name=row.get("author_display_name"),
                delivery_state=row.get("delivery_state"),
                delivery_state_timestamp=row.get("delivery_state_timestamp"),
            )
        )

    next_older_cursor = None
    if has_more and newest_first:
        boundary = newest_first[-1]
        next_older_cursor = _encode_cursor(
            kind="message",
            occurred_at=boundary["occurred_at"],
            item_id=boundary["message_id"],
            conversation_id=conversation_id,
            principal=principal,
        )
    response.headers["Cache-Control"] = "no-store"
    return OperatorMessageHistoryResponse(
        items=items,
        next_older_cursor=next_older_cursor,
    )
