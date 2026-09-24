"""Isolated PostgreSQL evidence for no-send Relance delivery readiness."""

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.relance_candidate import RelanceCandidate
from app.models.whatsapp_relance_opt_in import WhatsAppRelanceOptIn
from app.modules.m6_relance.candidates import create_candidates
from app.modules.m6_relance.readiness import (
    check_delivery_readiness,
    record_verified_opt_in,
)

DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requires disposable PostgreSQL")


@pytest_asyncio.fixture(loop_scope="function")
async def factory():
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
        await engine.dispose()


async def _seed(factory, language="french"):
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    phone = f"+243{uuid.uuid4().int % 10**9:09d}"
    conversation_id, message_id, lead_id, operator_id = (uuid.uuid4() for _ in range(4))
    async with factory() as session:
        session.add(Customer(phone_number=phone, city="Kinshasa", preferred_language=language))
        session.add(Conversation(
            conversation_id=conversation_id, customer_id=phone, language_detected=language,
            status="active", context={}, owner_type="ai", ai_execution_state="eligible",
            last_message_time=old,
        ))
        session.add(Message(
            message_id=message_id, conversation_id=conversation_id, timestamp=old,
            created_at=old, direction="inbound", content="I agree to WhatsApp follow-ups",
            content_type="text", language=language, whatsapp_message_id=f"opt-in-{message_id}",
        ))
        session.add(Lead(
            lead_id=lead_id, customer_id=phone, conversation_id=conversation_id,
            score="warm", score_value=5, stage="consideration", intent="buy",
            product_interest=[], source="whatsapp",
        ))
        session.add(OperatorAccount(
            account_id=operator_id, username_normalized=f"op.{operator_id.hex[:12]}",
            display_name="Verifier", password_hash="not-used", role="operator",
            status="active", auth_version=1, must_change_password=False,
        ))
        await session.commit()
        _, created = await create_candidates(session, delay_hours=24)
        assert created == 1
        await session.commit()
        candidate_id = await session.scalar(
            select(RelanceCandidate.candidate_id).where(RelanceCandidate.lead_id == lead_id)
        )
    return phone, conversation_id, message_id, operator_id, candidate_id


def _settings(**values):
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_missing_consent_and_generic_customer_flag_do_not_authorize(factory):
    phone, _, _, _, candidate_id = await _seed(factory)
    async with factory() as session:
        customer = await session.get(Customer, phone)
        customer.consent_given = True
        customer.consent_timestamp = datetime.now(timezone.utc)
        await session.commit()
        result = await check_delivery_readiness(
            session, candidate_id=candidate_id,
            settings=_settings(relance_template_french_name="approved_fr",
                               relance_template_french_locale="fr"),
        )
        assert (result.status, result.reason) == ("blocked", "missing_consent")


@pytest.mark.asyncio
async def test_missing_template_and_three_explicit_language_selections(factory, monkeypatch):
    import app.adapters.messaging.whatsapp_official_adapter as official

    def unexpected_network(*args, **kwargs):
        raise AssertionError("provider/network call attempted")

    monkeypatch.setattr(httpx, "AsyncClient", unexpected_network)
    monkeypatch.setattr(official.WhatsAppOfficialAdapter, "send_template", unexpected_network)
    settings = _settings(
        relance_template_french_name="followup_fr", relance_template_french_locale="fr",
        relance_template_lingala_name="followup_ln", relance_template_lingala_locale="ln",
        relance_template_swahili_name="followup_sw", relance_template_swahili_locale="sw",
    )
    for language, name, locale in (
        ("french", "followup_fr", "fr"),
        ("lingala", "followup_ln", "ln"),
        ("swahili", "followup_sw", "sw"),
    ):
        phone, _, message_id, operator_id, candidate_id = await _seed(factory, language)
        async with factory() as session:
            await record_verified_opt_in(
                session, customer_id=phone, source_message_id=message_id,
                operator_account_id=operator_id,
            )
            await session.commit()
            missing = await check_delivery_readiness(
                session, candidate_id=candidate_id, settings=_settings()
            )
            assert (missing.status, missing.reason) == ("blocked", "missing_template")
            ready = await check_delivery_readiness(
                session, candidate_id=candidate_id, settings=settings
            )
            assert (ready.status, ready.language, ready.template_name, ready.template_locale) == (
                "ready_for_delivery", language, name, locale
            )
            assert await session.scalar(select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )) == 0


@pytest.mark.asyncio
async def test_unsupported_language_and_opt_out_override(factory):
    phone, conversation_id, message_id, operator_id, candidate_id = await _seed(factory)
    settings = _settings(relance_template_french_name="followup_fr",
                         relance_template_french_locale="fr")
    async with factory() as session:
        await record_verified_opt_in(session, customer_id=phone,
                                     source_message_id=message_id,
                                     operator_account_id=operator_id)
        await session.commit()
        conversation = await session.get(Conversation, conversation_id)
        conversation.language_detected = "unknown"
        with session.no_autoflush:
            unsupported = await check_delivery_readiness(
                session, candidate_id=candidate_id, settings=settings
            )
        assert (unsupported.status, unsupported.reason) == ("blocked", "unsupported_language")
        await session.rollback()
        customer = await session.get(Customer, phone)
        customer.opt_out_flag = True
        customer.opt_out_at = datetime.now(timezone.utc)
        await session.commit()
        opted_out = await check_delivery_readiness(
            session, candidate_id=candidate_id, settings=settings
        )
        assert (opted_out.status, opted_out.reason) == ("blocked", "opted_out")
        customer.opt_out_flag = False
        await session.commit()
        stale = await check_delivery_readiness(
            session, candidate_id=candidate_id, settings=settings
        )
        assert (stale.status, stale.reason) == ("blocked", "missing_consent")


@pytest.mark.asyncio
async def test_opt_in_requires_matching_inbound_and_active_operator(factory):
    phone, _, message_id, operator_id, _ = await _seed(factory)
    async with factory() as session:
        with pytest.raises(ValueError, match="inbound text evidence"):
            await record_verified_opt_in(
                session, customer_id=phone, source_message_id=uuid.uuid4(),
                operator_account_id=operator_id,
            )
        operator = await session.get(OperatorAccount, operator_id)
        operator.status = "disabled"
        with pytest.raises(ValueError, match="active Human Operator"):
            await record_verified_opt_in(
                session, customer_id=phone, source_message_id=message_id,
                operator_account_id=operator_id,
            )
        operator.status = "active"
        first = await record_verified_opt_in(
            session, customer_id=phone, source_message_id=message_id,
            operator_account_id=operator_id,
        )
        second = await record_verified_opt_in(
            session, customer_id=phone, source_message_id=message_id,
            operator_account_id=operator_id,
        )
        assert first.opt_in_id == second.opt_in_id
        assert await session.scalar(select(func.count()).select_from(WhatsAppRelanceOptIn)) == 1


@pytest.mark.asyncio
async def test_ownership_and_escalation_still_block_readiness(factory):
    phone, conversation_id, message_id, operator_id, candidate_id = await _seed(factory)
    settings = _settings(relance_template_french_name="followup_fr",
                         relance_template_french_locale="fr")
    async with factory() as session:
        await record_verified_opt_in(session, customer_id=phone,
                                     source_message_id=message_id,
                                     operator_account_id=operator_id)
        conversation = await session.get(Conversation, conversation_id)
        conversation.owner_type = "human"
        conversation.human_owner_account_id = operator_id
        conversation.ai_execution_state = "paused"
        await session.commit()
        owned = await check_delivery_readiness(session, candidate_id=candidate_id,
                                               settings=settings)
        assert (owned.status, owned.reason) == ("blocked", "ineligible_candidate")
        conversation.owner_type = "ai"
        conversation.human_owner_account_id = None
        conversation.ai_execution_state = "eligible"
        session.add(EscalationTicket(
            conversation_id=conversation_id, customer_id=phone,
            reason="complex_complaint", priority="medium", status="open",
            transcript_snapshot=[],
        ))
        await session.commit()
        escalated = await check_delivery_readiness(session, candidate_id=candidate_id,
                                                   settings=settings)
        assert (escalated.status, escalated.reason) == ("blocked", "ineligible_candidate")
