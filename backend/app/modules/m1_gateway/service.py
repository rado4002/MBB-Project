"""
M1 Gateway — Service layer.

Handles the synchronous DB-touching parts of inbound message processing:
  1. Customer upsert (create or touch last_interaction)
  2. Conversation upsert (create or continue active conversation)
  3. Opt-out guard (reject silently if customer opted out)
  4. Language detection (keyword fast-path, sticky within conversation)
  5. Inbound message persistence
  6. Voice-note → escalation routing flag

All DB operations use SQLAlchemy async sessions.
Returns a structured `ProcessedInbound` dataclass consumed by the Celery task.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer import Customer
from app.models.conversation import Conversation
from app.models.message import Message
from app.modules.m1_gateway.language_detector import (
    detect_language,
    is_language_switch_request,
    is_opt_out,
)

log = structlog.get_logger(__name__)


@dataclass
class ProcessedInbound:
    """Result returned by process_inbound() to the router / Celery task."""
    customer_phone: str
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    language: str
    is_opted_out: bool = False
    is_voice_note: bool = False
    requires_escalation: bool = False
    whatsapp_message_id: str = ""
    content: str = ""
    extra: dict = field(default_factory=dict)


async def process_inbound(
    *,
    session: AsyncSession,
    customer_phone: str,
    content: str,
    content_type: str,
    timestamp: datetime,
    whatsapp_message_id: str,
    message_id: uuid.UUID,
) -> ProcessedInbound:
    """
    Full synchronous M1 processing: upsert customer + conversation,
    detect language, persist inbound message, return structured result.
    """
    now = datetime.now(timezone.utc)

    # ── 1. Customer upsert ────────────────────────────────────────────────────
    stmt = pg_insert(Customer).values(
        phone_number=customer_phone,
        city="Kinshasa",            # Default — updated when customer tells us
        preferred_language="french",
        opt_out_flag=False,
        consent_given=False,
        club_member=False,
        club_points=0,
        created_at=now,
        last_interaction=now,
    ).on_conflict_do_update(
        index_elements=["phone_number"],
        set_={"last_interaction": now},
    )
    await session.execute(stmt)

    # Reload to get current opt-out status and language
    result = await session.execute(
        select(Customer).where(Customer.phone_number == customer_phone)
    )
    customer: Customer = result.scalar_one()

    # ── 2. Opt-out guard ──────────────────────────────────────────────────────
    # Check both DB flag and content of this message
    message_has_opt_out = is_opt_out(content)
    if customer.opt_out_flag:
        log.info("m1.opted_out_blocked", phone=customer_phone)
        return ProcessedInbound(
            customer_phone=customer_phone,
            conversation_id=uuid.uuid4(),  # dummy — won't be used
            message_id=message_id,
            language=customer.preferred_language,
            is_opted_out=True,
            content=content,
            whatsapp_message_id=whatsapp_message_id,
        )

    if message_has_opt_out:
        # Immediately set opt-out flag in DB
        await session.execute(
            update(Customer)
            .where(Customer.phone_number == customer_phone)
            .values(opt_out_flag=True, opt_out_at=now)
        )
        
        # Cancel all pending relances for this customer's leads (Sprint 1.6)
        from app.modules.m6_relance.service import cancel_all_relances
        from app.models.lead import Lead
        
        # Find all leads for this customer
        leads_result = await session.execute(
            select(Lead.lead_id).where(Lead.customer_id == customer_phone)
        )
        lead_ids = [row[0] for row in leads_result.all()]
        
        # Cancel relances for each lead
        total_cancelled = 0
        for lead_id in lead_ids:
            cancelled_count = await cancel_all_relances(session, lead_id=lead_id)
            total_cancelled += cancelled_count
        
        log.info("m1.opt_out_registered", phone=customer_phone, relances_cancelled=total_cancelled)
        return ProcessedInbound(
            customer_phone=customer_phone,
            conversation_id=uuid.uuid4(),  # dummy
            message_id=message_id,
            language=customer.preferred_language,
            is_opted_out=True,
            content=content,
            whatsapp_message_id=whatsapp_message_id,
        )

    # ── 3. Load or create active conversation ─────────────────────────────────
    conv_result = await session.execute(
        select(Conversation)
        .where(
            Conversation.customer_id == customer_phone,
            Conversation.status.in_(["active", "qualifying", "nurturing"]),
        )
        .order_by(Conversation.last_message_time.desc())
        .limit(1)
    )
    conversation: Conversation | None = conv_result.scalar_one_or_none()

    existing_language: str | None = None
    if conversation:
        existing_language = conversation.language_detected

    # ── 4. Language detection ─────────────────────────────────────────────────
    switch_req = is_language_switch_request(content)
    if switch_req:
        language = switch_req
        existing_language = None  # Override sticky
    else:
        language, _ = detect_language(content, existing_language)

    if conversation is None:
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected=language,
            context={},
            message_count=1,
        )
        session.add(conversation)
        await session.flush()  # get the PK
        log.info(
            "m1.conversation_created",
            conv_id=str(conversation.conversation_id),
            phone=customer_phone,
        )
    else:
        # Update language if switched
        if switch_req and conversation.language_detected != language:
            await session.execute(
                update(Conversation)
                .where(Conversation.conversation_id == conversation.conversation_id)
                .values(language_detected=language, last_message_time=now, updated_at=now)
            )
        else:
            await session.execute(
                update(Conversation)
                .where(Conversation.conversation_id == conversation.conversation_id)
                .values(last_message_time=now, updated_at=now,
                        message_count=Conversation.message_count + 1)
            )

    # ── 5. Persist inbound message ────────────────────────────────────────────
    msg = Message(
        message_id=message_id,
        conversation_id=conversation.conversation_id,
        timestamp=timestamp,
        direction="inbound",
        content=content,
        content_type=content_type,
        language=language,
        whatsapp_message_id=whatsapp_message_id,
    )
    session.add(msg)
    await session.flush()

    # ── 6. Voice-note flag ────────────────────────────────────────────────────
    is_voice = content_type == "voice_note"

    log.info(
        "m1.inbound_persisted",
        phone=customer_phone,
        msg_id=str(message_id),
        conv_id=str(conversation.conversation_id),
        lang=language,
        voice=is_voice,
    )

    return ProcessedInbound(
        customer_phone=customer_phone,
        conversation_id=conversation.conversation_id,
        message_id=message_id,
        language=language,
        is_voice_note=is_voice,
        requires_escalation=is_voice,
        content=content,
        whatsapp_message_id=whatsapp_message_id,
    )


async def persist_outbound(
    *,
    session: AsyncSession,
    conversation_id: uuid.UUID,
    content: str,
    language: str,
    processing_time_ms: int,
    persuasion_hook: str | None = None,
) -> uuid.UUID:
    """Save an outbound (bot) message to mbb.messages. Returns the new message_id."""
    msg_id = uuid.uuid4()
    msg = Message(
        message_id=msg_id,
        conversation_id=conversation_id,
        timestamp=datetime.now(timezone.utc),
        direction="outbound",
        content=content,
        content_type="text",
        language=language,
        processing_time_ms=processing_time_ms,
        persuasion_hook=persuasion_hook,
    )
    session.add(msg)
    await session.flush()
    await session.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(message_count=Conversation.message_count + 1)
    )
    return msg_id
