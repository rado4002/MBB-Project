"""Safe Customer profile and one-way contact opt-out operations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.modules.m1_gateway.language_detector import is_opt_out
from app.modules.m6_relance.service import cancel_all_relances
from app.operator_identity.audit import append_operator_audit_event


class CustomerProfileError(Exception):
    pass


class CustomerProfileNotFound(CustomerProfileError):
    pass


class CustomerProfileForbidden(CustomerProfileError):
    pass


class CustomerProfileStale(CustomerProfileError):
    pass


class CustomerOptOutEvidenceInvalid(CustomerProfileError):
    pass


class CustomerProfileUnavailable(CustomerProfileError):
    pass


@dataclass(frozen=True)
class ProfileValues:
    display_name: str | None
    city: str
    preferred_language: str


@dataclass(frozen=True)
class ProfileMutationResult:
    customer: Customer
    changed: bool


@dataclass(frozen=True)
class OptOutMutationResult:
    customer: Customer
    changed: bool
    relances_cancelled: int


def _values(customer: Customer) -> ProfileValues:
    return ProfileValues(customer.name, customer.city, customer.preferred_language)


async def _locked_customer(
    session: AsyncSession, conversation_id: uuid.UUID
) -> Customer:
    customer = await session.scalar(
        select(Customer)
        .join(Conversation, Conversation.customer_id == Customer.phone_number)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update(of=Customer)
    )
    if customer is None:
        raise CustomerProfileNotFound
    return customer


async def update_customer_profile(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    expected: ProfileValues,
    desired: ProfileValues,
    reason_code: str,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None,
    user_agent_fingerprint: str | None,
) -> ProfileMutationResult:
    if actor_role != "administrator":
        raise CustomerProfileForbidden
    try:
        customer = await _locked_customer(session, conversation_id)
        current = _values(customer)
        if current == desired:
            await session.commit()
            return ProfileMutationResult(customer=customer, changed=False)
        if current != expected:
            await session.rollback()
            raise CustomerProfileStale

        changed_fields = [
            field
            for field in ("display_name", "city", "preferred_language")
            if getattr(current, field) != getattr(desired, field)
        ]
        customer.name = desired.display_name
        customer.city = desired.city
        customer.preferred_language = desired.preferred_language
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action="customer.profile.update",
            target_type="customer_via_conversation",
            target_id=str(conversation_id),
            reason_code=reason_code,
            outcome="succeeded",
            metadata={"changed_fields": changed_fields},
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
        )
        await session.commit()
        return ProfileMutationResult(customer=customer, changed=True)
    except CustomerProfileError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise CustomerProfileUnavailable from exc


async def opt_out_customer(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    evidence_message_id: uuid.UUID,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None,
    user_agent_fingerprint: str | None,
) -> OptOutMutationResult:
    if actor_role != "administrator":
        raise CustomerProfileForbidden
    try:
        customer = await _locked_customer(session, conversation_id)
        evidence = (
            await session.execute(
                select(Message.content)
                .join(
                    Conversation,
                    Conversation.conversation_id == Message.conversation_id,
                )
                .where(
                    Message.message_id == evidence_message_id,
                    Conversation.customer_id == customer.phone_number,
                    Message.direction == "inbound",
                    Message.content_type == "text",
                    Message.whatsapp_message_id.is_not(None),
                    Message.whatsapp_message_id != "",
                )
            )
        ).scalar_one_or_none()
        if evidence is None or not is_opt_out(evidence):
            await session.rollback()
            raise CustomerOptOutEvidenceInvalid
        if customer.opt_out_flag:
            await session.commit()
            return OptOutMutationResult(
                customer=customer, changed=False, relances_cancelled=0
            )

        now = datetime.now(timezone.utc)
        customer.opt_out_flag = True
        customer.opt_out_at = now
        lead_ids = (
            await session.scalars(
                select(Lead.lead_id).where(Lead.customer_id == customer.phone_number)
            )
        ).all()
        cancelled = 0
        for lead_id in lead_ids:
            cancelled += await cancel_all_relances(session, lead_id=lead_id)
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action="customer.contact.opt_out",
            target_type="customer_via_conversation",
            target_id=str(conversation_id),
            reason_code="customer_requested",
            outcome="succeeded",
            metadata={
                "evidence_message_id": str(evidence_message_id),
                "evidence_provenance": "inbound_whatsapp_customer_message",
                "relances_cancelled": cancelled,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=now,
        )
        await session.commit()
        return OptOutMutationResult(
            customer=customer, changed=True, relances_cancelled=cancelled
        )
    except CustomerProfileError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise CustomerProfileUnavailable from exc
