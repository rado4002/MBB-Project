"""No-send WhatsApp template readiness for a durable Relance candidate."""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.relance_candidate import RelanceCandidate
from app.models.whatsapp_relance_opt_in import WhatsAppRelanceOptIn

_TEMPLATE_NAME = re.compile(r"[a-z0-9_]+\Z")
_TEMPLATE_LOCALE = re.compile(r"[a-z]{2,3}(?:_[A-Z]{2})?\Z")


@dataclass(frozen=True)
class DeliveryReadiness:
    status: str
    reason: str | None = None
    language: str | None = None
    template_name: str | None = None
    template_locale: str | None = None


async def record_verified_opt_in(
    session: AsyncSession, *, customer_id: str, source_message_id: uuid.UUID,
    operator_account_id: uuid.UUID,
) -> WhatsAppRelanceOptIn:
    """Persist a Human verification of explicit opt-in in a customer inbound.

    This is an internal write contract, not a public capture endpoint. The
    caller must verify the wording of the referenced inbound before invoking.
    """
    customer = await session.scalar(
        select(Customer).where(Customer.phone_number == customer_id).with_for_update()
    )
    if customer is None or customer.opt_out_flag:
        raise ValueError("customer is absent or opted out")
    operator = await session.get(OperatorAccount, operator_account_id)
    if operator is None or operator.status != "active" or operator.role not in (
        "operator", "administrator"
    ):
        raise ValueError("active Human Operator verification is required")
    source = await session.scalar(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.conversation_id)
        .where(
            Message.message_id == source_message_id,
            Message.direction == "inbound",
            Message.content_type == "text",
            Conversation.customer_id == customer_id,
        )
    )
    if source is None:
        raise ValueError("customer inbound text evidence is required")
    granted_at = max(source.timestamp, source.created_at)
    if customer.opt_out_at is not None and granted_at <= customer.opt_out_at:
        raise ValueError("opt-in evidence predates opt-out")
    existing = await session.scalar(
        select(WhatsAppRelanceOptIn).where(
            WhatsAppRelanceOptIn.source_message_id == source_message_id
        )
    )
    if existing is not None:
        if (existing.customer_id != customer_id or
                existing.verified_by_operator_account_id != operator_account_id):
            raise ValueError("opt-in evidence was already verified differently")
        return existing
    record = WhatsAppRelanceOptIn(
        customer_id=customer_id,
        source_message_id=source_message_id,
        verified_by_operator_account_id=operator_account_id,
        granted_at=granted_at,
    )
    session.add(record)
    await session.flush()
    return record


def _configured_template(settings: Settings, language: str) -> tuple[str, str] | None:
    if language == "french":
        name, locale = settings.relance_template_french_name, settings.relance_template_french_locale
    elif language == "lingala":
        name, locale = settings.relance_template_lingala_name, settings.relance_template_lingala_locale
    elif language == "swahili":
        name, locale = settings.relance_template_swahili_name, settings.relance_template_swahili_locale
    else:
        return None
    name, locale = name.strip(), locale.strip()
    if not _TEMPLATE_NAME.fullmatch(name) or not _TEMPLATE_LOCALE.fullmatch(locale):
        return None
    return name, locale


async def check_delivery_readiness(
    session: AsyncSession, *, candidate_id: uuid.UUID, settings: Settings | None = None,
) -> DeliveryReadiness:
    """Evaluate contract readiness only; never dispatch or create an outbound Message.

    Configured template names/locales are assertions of external approval, not
    proof from Meta. A future sender must re-check all business and send gates.
    """
    candidate = await session.get(RelanceCandidate, candidate_id)
    if candidate is None:
        return DeliveryReadiness("blocked", "missing_candidate")
    if candidate.cancelled_at is not None or candidate.confirmed_sent_at is not None:
        return DeliveryReadiness("blocked", "inactive_candidate")
    lead = await session.get(Lead, candidate.lead_id)
    if lead is None:
        return DeliveryReadiness("blocked", "missing_candidate")
    customer = await session.get(Customer, lead.customer_id)
    conversation = await session.get(Conversation, lead.conversation_id)
    if customer is None or conversation is None or conversation.customer_id != customer.phone_number:
        return DeliveryReadiness("blocked", "missing_candidate")
    return await check_customer_delivery_readiness(
        session, customer=customer, conversation=conversation, settings=settings or get_settings()
    )


async def check_customer_delivery_readiness(
    session: AsyncSession, *, customer: Customer, conversation: Conversation, settings: Settings,
) -> DeliveryReadiness:
    """Shared consent/template authority for selection and final dispatch."""
    if customer.opt_out_flag:
        return DeliveryReadiness("blocked", "opted_out")
    if (conversation.owner_type != "ai" or conversation.ai_execution_state != "eligible"
            or conversation.status not in ("active", "qualifying", "nurturing")):
        return DeliveryReadiness("blocked", "ineligible_candidate")
    active_escalation = await session.scalar(
        select(EscalationTicket.ticket_id).where(
            EscalationTicket.conversation_id == conversation.conversation_id,
            EscalationTicket.status.in_(("open", "in_progress")),
        ).limit(1)
    )
    if active_escalation is not None:
        return DeliveryReadiness("blocked", "ineligible_candidate")
    grant = await session.scalar(
        select(WhatsAppRelanceOptIn)
        .where(WhatsAppRelanceOptIn.customer_id == customer.phone_number)
        .order_by(WhatsAppRelanceOptIn.granted_at.desc(), WhatsAppRelanceOptIn.opt_in_id.desc())
        .limit(1)
    )
    if grant is None or (customer.opt_out_at is not None and
                         grant.granted_at <= customer.opt_out_at):
        return DeliveryReadiness("blocked", "missing_consent")
    language = conversation.language_detected
    if language not in ("french", "lingala", "swahili"):
        return DeliveryReadiness("blocked", "unsupported_language", language=language)
    template = _configured_template(settings, language)
    if template is None:
        return DeliveryReadiness("blocked", "missing_template", language=language)
    return DeliveryReadiness(
        "ready_for_delivery", language=language,
        template_name=template[0], template_locale=template[1],
    )
