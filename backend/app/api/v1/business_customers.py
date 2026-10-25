"""Browser-session Customer profile and contact-preference endpoints."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import exists, func, select
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
    CustomerChannelEvidence,
    CustomerOptOutEvidence,
    CustomerOptOutRequest,
    CustomerProfileUpdate,
)

router = APIRouter(prefix="/business/customers", tags=["business-customers"])


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


async def _read_profile(
    db: AsyncSession, conversation_id: UUID
) -> BusinessCustomerProfile:
    seed = (
        (
            await db.execute(
                select(
                    Conversation.customer_id,
                    Customer.name,
                    func.concat("***", func.right(Customer.phone_number, 4)).label(
                        "phone_masked"
                    ),
                    Customer.city,
                    Customer.preferred_language,
                    Customer.opt_out_flag,
                    Customer.opt_out_at,
                    Customer.consent_given,
                    Customer.consent_timestamp,
                )
                .join(Customer, Customer.phone_number == Conversation.customer_id)
                .where(Conversation.conversation_id == conversation_id)
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
            select(Lead.source, func.max(Lead.created_at).label("latest"))
            .where(Lead.customer_id == seed["customer_id"])
            .group_by(Lead.source)
            .order_by(func.max(Lead.created_at).desc(), Lead.source.asc())
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
        conversation_id=conversation_id,
        display_name=seed["name"],
        phone_masked=seed["phone_masked"],
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
