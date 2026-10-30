"""Browser-session Customer profile and contact-preference endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import cast, exists, func, or_, select
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    require_recent_reauthentication,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.modules.customer_profile.service import (
    CustomerOptOutEvidenceInvalid,
    CustomerProfileForbidden,
    CustomerProfileNotFound,
    CustomerProfileStale,
    CustomerProfileUnavailable,
    ProfileValues,
    opt_out_customer,
    update_customer_profile,
)
from app.modules.m1_gateway.language_detector import is_opt_out
from app.request_ids import normalize_or_generate_request_id
from app.schemas.business_customers import (
    BusinessCustomerProfile,
    BusinessCustomerListItem,
    BusinessCustomerListResponse,
    CustomerChannelEvidence,
    CustomerOptOutEvidence,
    CustomerOptOutRequest,
    CustomerProfileUpdate,
)

router = APIRouter(prefix="/business/customers", tags=["business-customers"])
_CUSTOMER_REFERENCE_NAMESPACE = UUID("e7078780-7694-4b69-a16f-62e3df35ad36")


@router.get("", response_model=BusinessCustomerListResponse)
async def list_customers(
    response: Response,
    _customer: Annotated[
        BrowserPrincipal, Depends(require_capability("customer.read"))
    ],
    _conversation: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    q: Annotated[str, Query(max_length=100)] = "",
    offset: Annotated[int, Query(ge=0, le=10000)] = 0,
) -> BusinessCustomerListResponse:
    """Return one authorized CRM row per permanent Customer identity."""
    normalized_query = q.strip()
    latest_conversation = (
        select(
            Conversation.customer_id,
            Conversation.conversation_id,
            Conversation.last_message_time,
            func.row_number().over(
                partition_by=Conversation.customer_id,
                order_by=(
                    Conversation.last_message_time.desc(),
                    Conversation.conversation_id.desc(),
                ),
            ).label("position"),
        )
    ).subquery()
    verified_whatsapp = exists(
        select(Message.message_id)
        .join(Conversation, Conversation.conversation_id == Message.conversation_id)
        .where(
            Conversation.customer_id == Customer.phone_number,
            Message.whatsapp_message_id.is_not(None),
            func.btrim(Message.whatsapp_message_id) != "",
        )
    ).correlate(Customer)
    earliest_source = (
        select(Lead.source)
        .where(Lead.customer_id == Customer.phone_number)
        .order_by(Lead.created_at.asc(), Lead.lead_id.asc())
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        select(
            latest_conversation.c.conversation_id,
            Customer.name,
            Customer.phone_number,
            verified_whatsapp.label("has_verified_whatsapp"),
            earliest_source.label("acquisition_source"),
            Customer.last_interaction.label("last_interaction_at"),
        )
        .outerjoin(
            latest_conversation,
            (latest_conversation.c.customer_id == Customer.phone_number)
            & (latest_conversation.c.position == 1),
        )
        .order_by(
            Customer.last_interaction.desc(),
            Customer.phone_number.asc(),
        )
        .offset(offset)
        .limit(26)
    )
    if normalized_query:
        digits = "".join(character for character in normalized_query if character.isdigit())
        filters = [Customer.name.ilike(f"%{normalized_query}%")]
        if digits:
            filters.append(Customer.phone_number.like(f"%{digits[-4:]}"))
        statement = statement.where(or_(*filters))
    try:
        rows = (await db.execute(statement)).mappings().all()
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return BusinessCustomerListResponse(
        items=[
            BusinessCustomerListItem(
                customer_id=row["phone_number"],
                customer_reference=uuid5(
                    _CUSTOMER_REFERENCE_NAMESPACE,
                    row["phone_number"],
                ),
                conversation_id=row["conversation_id"],
                display_name=row["name"],
                phone_number=row["phone_number"],
                communication_channel=(
                    "whatsapp" if row["has_verified_whatsapp"] else None
                ),
                acquisition_source=row["acquisition_source"],
                acquisition_source_provenance=(
                    "earliest_recorded_lead"
                    if row["acquisition_source"] is not None
                    else None
                ),
                last_interaction_at=row["last_interaction_at"],
            )
            for row in rows[:25]
        ],
        has_more=len(rows) > 25,
    )


def _not_found() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status.HTTP_404_NOT_FOUND,
        code="CUSTOMER_PROFILE_NOT_FOUND",
        message="The Customer profile was not found.",
    )


def _unavailable() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="CUSTOMER_PROFILE_UNAVAILABLE",
        message="Customer profile data is temporarily unavailable.",
    )


async def _read_customer_profile(
    db: AsyncSession,
    *,
    customer_id: str,
    conversation_id: UUID | None,
) -> BusinessCustomerProfile:
    seed = (
        (
            await db.execute(
                select(
                    Customer.phone_number.label("customer_id"),
                    Customer.name,
                    Customer.city,
                    Customer.preferred_language,
                    Customer.opt_out_flag,
                    Customer.opt_out_at,
                    Customer.consent_given,
                    Customer.consent_timestamp,
                )
                .where(Customer.phone_number == customer_id)
            )
        )
        .mappings()
        .one_or_none()
    )
    if seed is None:
        raise _not_found()

    verified_whatsapp = await db.scalar(
        select(
            exists(
                select(Message.message_id)
                .join(
                    Conversation,
                    Conversation.conversation_id == Message.conversation_id,
                )
                .where(
                    Conversation.customer_id == seed["customer_id"],
                    Message.whatsapp_message_id.is_not(None),
                    func.btrim(Message.whatsapp_message_id) != "",
                )
            )
        )
    )
    source_rows = (
        await db.execute(
            select(Lead.source, func.min(Lead.created_at).label("earliest"))
            .where(Lead.customer_id == seed["customer_id"])
            .group_by(Lead.source)
            .order_by(func.min(Lead.created_at).asc(), Lead.source.asc())
            .limit(5)
        )
    ).all()
    candidate_rows = (
        await db.execute(
            select(Message.message_id, Message.timestamp, Message.content)
            .join(Conversation, Conversation.conversation_id == Message.conversation_id)
            .where(
                Conversation.customer_id == seed["customer_id"],
                Message.direction == "inbound",
                Message.content_type == "text",
                Message.whatsapp_message_id.is_not(None),
                func.btrim(Message.whatsapp_message_id) != "",
            )
            .order_by(Message.timestamp.desc(), Message.message_id.desc())
            .limit(50)
        )
    ).all()
    evidence = []
    for row in candidate_rows:
        if not is_opt_out(row.content):
            continue
        excerpt = " ".join(row.content.split())
        evidence.append(
            CustomerOptOutEvidence(
                message_id=row.message_id,
                received_at=row.timestamp,
                content_excerpt=excerpt[:120],
                content_truncated=len(excerpt) > 120,
                provenance="inbound_whatsapp_customer_message",
            )
        )
        if len(evidence) == 5:
            break

    return BusinessCustomerProfile(
        customer_id=seed["customer_id"],
        conversation_id=conversation_id,
        display_name=seed["name"],
        phone_number=seed["customer_id"],
        city=seed["city"],
        preferred_language=seed["preferred_language"],
        opted_out=seed["opt_out_flag"],
        opted_out_at=seed["opt_out_at"],
        consent_given=seed["consent_given"],
        consent_timestamp=seed["consent_timestamp"],
        communication_channels=(
            [
                CustomerChannelEvidence(
                    channel="whatsapp", provenance="verified_provider_message"
                )
            ]
            if verified_whatsapp
            else []
        ),
        acquisition_sources=[row.source for row in source_rows],
        opt_out_evidence=evidence,
    )


async def _read_profile(
    db: AsyncSession, conversation_id: UUID
) -> BusinessCustomerProfile:
    customer_id = await db.scalar(
        select(Conversation.customer_id).where(
            Conversation.conversation_id == conversation_id
        )
    )
    if customer_id is None:
        raise _not_found()
    return await _read_customer_profile(
        db,
        customer_id=customer_id,
        conversation_id=conversation_id,
    )


@router.get("/by-reference/{customer_reference}", response_model=BusinessCustomerProfile)
async def get_customer_profile_by_reference(
    customer_reference: UUID,
    response: Response,
    _customer: Annotated[
        BrowserPrincipal, Depends(require_capability("customer.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
    conversation_id: Annotated[UUID | None, Query()] = None,
) -> BusinessCustomerProfile:
    try:
        customer_id = await db.scalar(
            select(Customer.phone_number).where(
                func.uuid_generate_v5(
                    cast(
                        str(_CUSTOMER_REFERENCE_NAMESPACE),
                        PostgreSQLUUID(as_uuid=True),
                    ),
                    Customer.phone_number,
                )
                == customer_reference
            )
        )
        if customer_id is None:
            raise _not_found()
        if conversation_id is not None:
            conversation_matches = await db.scalar(
                select(
                    exists(
                        select(Conversation.conversation_id).where(
                            Conversation.conversation_id == conversation_id,
                            Conversation.customer_id == customer_id,
                        )
                    )
                )
            )
            if not conversation_matches:
                raise _not_found()
        profile = await _read_customer_profile(
            db,
            customer_id=customer_id,
            conversation_id=conversation_id,
        )
    except BrowserAuthError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return profile


@router.get("/{conversation_id}", response_model=BusinessCustomerProfile)
async def get_customer_profile(
    conversation_id: UUID,
    response: Response,
    _customer: Annotated[
        BrowserPrincipal, Depends(require_capability("customer.read"))
    ],
    _conversation: Annotated[
        BrowserPrincipal, Depends(require_capability("conversation.read"))
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BusinessCustomerProfile:
    try:
        profile = await _read_profile(db, conversation_id)
    except BrowserAuthError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return profile


@router.put("/{conversation_id}/profile", response_model=BusinessCustomerProfile)
async def put_customer_profile(
    conversation_id: UUID,
    body: CustomerProfileUpdate,
    request: Request,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("customer.manage"))
    ],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BusinessCustomerProfile:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        await update_customer_profile(
            db,
            conversation_id=conversation_id,
            expected=ProfileValues(
                body.expected.display_name,
                body.expected.city,
                body.expected.preferred_language.value,
            ),
            desired=ProfileValues(
                body.profile.display_name,
                body.profile.city,
                body.profile.preferred_language.value,
            ),
            reason_code=body.reason_code,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
        profile = await _read_profile(db, conversation_id)
    except CustomerProfileNotFound as exc:
        raise _not_found() from exc
    except CustomerProfileForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except CustomerProfileStale as exc:
        raise BrowserAuthError(
            status_code=409,
            code="CUSTOMER_PROFILE_STALE",
            message="The Customer profile changed since it was reviewed.",
        ) from exc
    except CustomerProfileUnavailable as exc:
        raise _unavailable() from exc
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return profile


@router.post("/{conversation_id}/opt-out", response_model=BusinessCustomerProfile)
async def post_customer_opt_out(
    conversation_id: UUID,
    body: CustomerOptOutRequest,
    request: Request,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("customer.manage"))
    ],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BusinessCustomerProfile:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        await opt_out_customer(
            db,
            conversation_id=conversation_id,
            evidence_message_id=body.evidence_message_id,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
        profile = await _read_profile(db, conversation_id)
    except CustomerProfileNotFound as exc:
        raise _not_found() from exc
    except CustomerProfileForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except CustomerOptOutEvidenceInvalid as exc:
        raise BrowserAuthError(
            status_code=409,
            code="CUSTOMER_OPT_OUT_EVIDENCE_INVALID",
            message="Select an authentic inbound WhatsApp opt-out message.",
        ) from exc
    except CustomerProfileUnavailable as exc:
        raise _unavailable() from exc
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return profile
