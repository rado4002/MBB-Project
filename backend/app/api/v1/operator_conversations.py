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

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import exists, func, literal, select, true, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import BrowserPrincipal, require_capability
from app.api.browser_auth_errors import BrowserAuthError
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.schemas.common import ConversationStatus, Language
from app.schemas.operator_conversations import (
    OperatorConversationDetail,
    OperatorConversationQueueItem,
    OperatorConversationQueueResponse,
    OperatorCustomerSummary,
    OperatorLatestMessage,
    OperatorLeadSummary,
    OperatorMessageHistoryResponse,
    OperatorMessageItem,
    OperatorMessageMedia,
    OperatorOpenEscalation,
)

router = APIRouter(
    prefix="/operator/conversations",
    tags=["operator-conversations"],
)

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
    kind: Literal["conversation", "message"],
    occurred_at: datetime,
    item_id: UUID,
    principal: BrowserPrincipal,
    conversation_id: UUID | None = None,
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
    kind: Literal["conversation", "message"],
    principal: BrowserPrincipal,
    conversation_id: UUID | None = None,
) -> tuple[datetime, UUID]:
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
            Customer.name.label("customer_display_name"),
            _masked_phone_expression().label("customer_phone_masked"),
            latest_message.c.latest_content,
            latest_message.c.latest_content_type,
            latest_message.c.latest_direction,
            latest_message.c.latest_occurred_at,
            open_escalation.label("has_open_escalation"),
        )
        .join(Customer, Customer.phone_number == Conversation.customer_id)
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
            Customer.name.label("customer_display_name"),
            _masked_phone_expression().label("customer_phone_masked"),
            Lead.score.label("lead_score"),
            Lead.stage.label("lead_stage"),
            Lead.intent.label("lead_intent"),
            Lead.product_interest[1:_PRODUCT_INTEREST_LIMIT].label(
                "lead_product_interests"
            ),
            open_escalation.label("has_open_escalation"),
        )
        .join(Customer, Customer.phone_number == Conversation.customer_id)
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
        )
        .where(Message.conversation_id == conversation_id)
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
        content_type = row["content_type"]
        is_text = content_type == "text"
        items.append(
            OperatorMessageItem(
                message_id=row["message_id"],
                occurred_at=row["occurred_at"],
                direction=row["direction"],
                sender_type=(
                    "customer" if row["direction"] == "inbound" else "unknown"
                ),
                content_type=content_type,
                text=row["content"] if is_text else None,
                media=(
                    None
                    if is_text
                    else OperatorMessageMedia(kind=content_type, available=False)
                ),
                language=row["language"],
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
