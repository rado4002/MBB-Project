"""Offline delivery, identity, and inbound race evidence in disposable PostgreSQL."""

import asyncio
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
from app.models.relance_delivery import RelanceDelivery
from app.models.whatsapp_relance_opt_in import WhatsAppRelanceOptIn
from app.modules.m1_gateway.service import process_inbound
from app.modules.m6_relance.candidates import create_candidates
from app.modules.m6_relance.delivery import (
    DefiniteDeliveryFailure, deliver_candidate, dispatch_prepared_delivery,
    prepare_candidate_delivery,
)
from app.modules.m6_relance.readiness import record_verified_opt_in

DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="requires disposable PostgreSQL")


@pytest_asyncio.fixture(loop_scope="function")
async def factory():
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
        await engine.dispose()


class FakeTransport:
    offline_only = True

    def __init__(self, result="wamid.confirmed"):
        self.result = result
        self.calls = []

    async def send_template(self, phone, template_name, params, *, locale, idempotency_key):
        self.calls.append((phone, template_name, params, locale, idempotency_key))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _settings(**overrides):
    values = dict(
        RELANCE_DELAY_2=48,
        relance_template_french_name="followup_fr", relance_template_french_locale="fr",
        relance_template_lingala_name="followup_ln", relance_template_lingala_locale="ln",
        relance_template_swahili_name="followup_sw", relance_template_swahili_locale="sw",
    )
    values.update(overrides)
    return Settings(
        _env_file=None, **values,
    )


def test_second_attempt_delay_defaults_to_72_hours():
    assert Settings(_env_file=None).relance_delay_2_hours == 72


async def _seed(factory, *, language="french", consent=True):
    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    if now < datetime.now(timezone.utc):
        now += timedelta(days=1)
    old = now - timedelta(hours=26)
    phone = f"+243{uuid.uuid4().int % 10**9:09d}"
    conversation_id, source_id, lead_id, operator_id = (uuid.uuid4() for _ in range(4))
    async with factory() as session:
        session.add(Customer(phone_number=phone, city="Kinshasa", preferred_language=language))
        session.add(Conversation(
            conversation_id=conversation_id, customer_id=phone, language_detected=language,
            status="active", context={}, owner_type="ai", ai_execution_state="eligible",
            last_message_time=old,
        ))
        session.add(Message(
            message_id=source_id, conversation_id=conversation_id, timestamp=old,
            created_at=old, direction="inbound", content="I agree to WhatsApp follow-ups",
            content_type="text", language=language, whatsapp_message_id=f"opt-{source_id}",
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
        if consent:
            await record_verified_opt_in(
                session, customer_id=phone, source_message_id=source_id,
                operator_account_id=operator_id,
            )
            await session.commit()
        assert (await create_candidates(session, delay_hours=24, now=now))[1] == 1
        await session.commit()
        candidate_id = await session.scalar(
            select(RelanceCandidate.candidate_id).where(RelanceCandidate.lead_id == lead_id)
        )
    return now, phone, conversation_id, source_id, lead_id, operator_id, candidate_id


async def _outbound(factory):
    async with factory() as session:
        return (await session.scalars(select(Message).where(Message.direction == "outbound"))).all()


@pytest.mark.asyncio
async def test_confirmed_send_repeats_reuse_uuid_and_count_once(factory, monkeypatch):
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("network must not be used")))
    now, _, _, _, lead_id, _, candidate_id = await _seed(factory)
    fake = FakeTransport()
    first = await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                    settings=_settings(), now=now)
    second = await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                     settings=_settings(), now=now)
    rows = await _outbound(factory)
    assert first == second == ("sent", rows[0].message_id)
    assert len(rows) == len(fake.calls) == 1
    assert fake.calls[0][-1] == str(rows[0].message_id)
    assert rows[0].delivery_state == "sent" and rows[0].whatsapp_message_id == "wamid.confirmed"
    async with factory() as session:
        delivery = await session.get(RelanceDelivery, candidate_id)
        candidate = await session.get(RelanceCandidate, candidate_id)
        assert delivery.status == "sent" and delivery.outbound_message_id == rows[0].message_id
        assert candidate.confirmed_sent_at is not None
        assert await session.scalar(select(func.count()).select_from(RelanceCandidate).where(
            RelanceCandidate.lead_id == lead_id,
            RelanceCandidate.confirmed_sent_at.is_not(None))) == 1
        # A confirmed send is the only evidence that can start attempt 2's clock.


@pytest.mark.asyncio
async def test_concurrent_execution_one_logical_transport_attempt(factory):
    now, _, _, _, _, _, candidate_id = await _seed(factory)
    fake = FakeTransport()
    results = await asyncio.gather(*(deliver_candidate(
        factory, candidate_id=candidate_id, transport=fake, settings=_settings(), now=now,
    ) for _ in range(3)))
    rows = await _outbound(factory)
    assert len(rows) == len(fake.calls) == 1
    assert all(result[1] == rows[0].message_id for result in results)
    assert results.count(("sent", rows[0].message_id)) >= 1


@pytest.mark.asyncio
async def test_inbound_after_preparation_cancels_before_dispatch(factory):
    now, phone, _, _, _, _, candidate_id = await _seed(factory)
    state, message_id = await prepare_candidate_delivery(
        factory, candidate_id=candidate_id, settings=_settings(), now=now)
    assert state == "prepared"
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="Back again",
            content_type="text", timestamp=now + timedelta(minutes=1),
            whatsapp_message_id=f"return-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    fake = FakeTransport()
    result = await dispatch_prepared_delivery(
        factory, candidate_id=candidate_id, outbound_message_id=message_id,
        transport=fake, settings=_settings(), now=now + timedelta(minutes=1),
    )
    assert result == "cancelled" and fake.calls == []
    assert await _outbound(factory) == []
    async with factory() as session:
        assert (await session.get(RelanceDelivery, candidate_id)).status == "cancelled"
        assert (await session.get(RelanceCandidate, candidate_id)).cancelled_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["human", "escalation", "opt_out", "consent", "template"])
async def test_final_recheck_blocks_changed_authority(factory, change):
    now, phone, conversation_id, _, _, operator_id, candidate_id = await _seed(factory)
    state, message_id = await prepare_candidate_delivery(
        factory, candidate_id=candidate_id, settings=_settings(), now=now)
    assert state == "prepared"
    async with factory() as session:
        if change == "human":
            conversation = await session.get(Conversation, conversation_id)
            conversation.owner_type = "human"
            conversation.human_owner_account_id = operator_id
            conversation.ai_execution_state = "paused"
        elif change == "escalation":
            session.add(EscalationTicket(
                conversation_id=conversation_id, customer_id=phone,
                reason="complex_complaint", priority="medium", status="open",
                transcript_snapshot=[],
            ))
        elif change == "opt_out":
            customer = await session.get(Customer, phone)
            customer.opt_out_flag = True
            customer.opt_out_at = now
        elif change == "consent":
            grant = await session.scalar(select(WhatsAppRelanceOptIn))
            await session.delete(grant)
        await session.commit()
    settings = _settings() if change != "template" else Settings(_env_file=None)
    fake = FakeTransport()
    assert await dispatch_prepared_delivery(
        factory, candidate_id=candidate_id, outbound_message_id=message_id,
        transport=fake, settings=settings, now=now,
    ) == "cancelled"
    assert fake.calls == [] and await _outbound(factory) == []


@pytest.mark.asyncio
async def test_missing_consent_and_template_fail_closed(factory):
    now, _, _, _, _, _, candidate_id = await _seed(factory, consent=False)
    fake = FakeTransport()
    assert (await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                    settings=_settings(), now=now))[0] == "cancelled"
    assert fake.calls == [] and await _outbound(factory) == []
    async with factory() as session:
        assert (await session.get(RelanceDelivery, candidate_id)).reason == "missing_consent"
    now, _, _, _, _, _, candidate_id = await _seed(factory)
    assert (await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                    settings=Settings(_env_file=None), now=now))[0] == "cancelled"
    assert fake.calls == [] and await _outbound(factory) == []
    async with factory() as session:
        assert (await session.get(RelanceDelivery, candidate_id)).reason == "missing_template"


@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected", [
    (DefiniteDeliveryFailure(), "failed"), (RuntimeError("timeout"), "uncertain"),
    ("", "uncertain"),
])
async def test_failure_and_ambiguity_never_consume_or_retry(factory, result, expected):
    now, _, _, _, _, _, candidate_id = await _seed(factory)
    fake = FakeTransport(result)
    first = await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                    settings=_settings(), now=now)
    second = await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                     settings=_settings(), now=now)
    assert first == second and first[0] == expected
    assert await dispatch_prepared_delivery(
        factory, candidate_id=candidate_id, outbound_message_id=first[1],
        transport=fake, settings=_settings(), now=now,
    ) == expected
    assert len(fake.calls) == len(await _outbound(factory)) == 1
    async with factory() as session:
        assert (await session.get(RelanceCandidate, candidate_id)).confirmed_sent_at is None
        assert (await session.get(RelanceDelivery, candidate_id)).status == expected


@pytest.mark.asyncio
async def test_reserved_outbound_is_uncertain_without_automatic_retry(factory):
    now, _, _, _, _, _, candidate_id = await _seed(factory)
    status, message_id = await prepare_candidate_delivery(
        factory, candidate_id=candidate_id, settings=_settings(), now=now)
    fake = FakeTransport()
    assert status == "prepared"
    assert await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                   settings=_settings(), now=now) == ("prepared", message_id)
    assert fake.calls == [] and len(await _outbound(factory)) == 1


@pytest.mark.asyncio
async def test_crash_after_dispatch_claim_stays_uncertain_and_never_retries(factory):
    now, _, _, _, _, _, candidate_id = await _seed(factory)

    class CrashTransport:
        offline_only = True

        async def send_template(self, *args, **kwargs):
            raise SystemExit("simulated process loss after dispatch claim")

    with pytest.raises(SystemExit, match="simulated process loss"):
        await deliver_candidate(
            factory, candidate_id=candidate_id, transport=CrashTransport(),
            settings=_settings(), now=now,
        )
    async with factory() as session:
        delivery = await session.get(RelanceDelivery, candidate_id)
        assert delivery.status == "uncertain"
        assert delivery.dispatch_started_at is not None
        message_id = delivery.outbound_message_id
    fake = FakeTransport()
    assert await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                   settings=_settings(), now=now) == ("uncertain", message_id)
    assert fake.calls == [] and len(await _outbound(factory)) == 1


@pytest.mark.asyncio
async def test_live_adapter_cannot_enter_offline_delivery(factory):
    from app.adapters.messaging.whatsapp_official_adapter import WhatsAppOfficialAdapter

    now, _, _, _, _, _, candidate_id = await _seed(factory)
    with pytest.raises(ValueError, match="offline transport only"):
        await deliver_candidate(
            factory, candidate_id=candidate_id, transport=WhatsAppOfficialAdapter(),
            settings=_settings(), now=now,
        )
    assert await _outbound(factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("language,name,locale", [
    ("french", "followup_fr", "fr"),
    ("lingala", "followup_ln", "ln"),
    ("swahili", "followup_sw", "sw"),
])
async def test_explicit_locale_and_message_language(factory, language, name, locale):
    now, _, _, _, _, _, candidate_id = await _seed(factory, language=language)
    fake = FakeTransport()
    assert (await deliver_candidate(factory, candidate_id=candidate_id, transport=fake,
                                    settings=_settings(), now=now))[0] == "sent"
    assert fake.calls[0][1:4] == (name, [], locale)
    assert (await _outbound(factory))[0].language == language


async def _scan_at(factory, at, settings=None):
    settings = settings or _settings()
    async with factory() as session:
        result = await create_candidates(
            session, delay_hours=settings.relance_delay_1_hours,
            settings=settings, now=at,
        )
        await session.commit()
        return result


async def _attempts(factory, lead_id):
    async with factory() as session:
        return (await session.scalars(
            select(RelanceCandidate).where(RelanceCandidate.lead_id == lead_id)
            .order_by(RelanceCandidate.attempt_number)
        )).all()


@pytest.mark.asyncio
async def test_second_attempt_waits_from_confirmed_send_and_stops_after_two(factory):
    now, _, _, _, lead_id, _, first_id = await _seed(factory)
    fake = FakeTransport()
    assert (await deliver_candidate(factory, candidate_id=first_id, transport=fake,
                                    settings=_settings(), now=now))[0] == "sent"
    assert (await _scan_at(factory, now + timedelta(hours=47)))[1] == 0
    assert (await _scan_at(factory, now + timedelta(hours=48)))[1] == 1
    rows = await _attempts(factory, lead_id)
    assert [row.attempt_number for row in rows] == [1, 2]
    assert rows[1].source_message_id == rows[0].source_message_id
    assert (await _scan_at(factory, now + timedelta(hours=72)))[1] == 0
    second = await deliver_candidate(
        factory, candidate_id=rows[1].candidate_id, transport=fake,
        settings=_settings(), now=now + timedelta(hours=48),
    )
    assert second[0] == "sent" and len(fake.calls) == 2
    assert len(await _outbound(factory)) == 2
    assert (await _scan_at(factory, now + timedelta(days=30)))[1] == 0
    assert len(await _attempts(factory, lead_id)) == 2


@pytest.mark.asyncio
async def test_second_delay_is_configurable_and_rechecked_before_dispatch(factory):
    now, _, _, _, lead_id, _, first_id = await _seed(factory)
    fake = FakeTransport()
    await deliver_candidate(factory, candidate_id=first_id, transport=fake,
                            settings=_settings(), now=now)
    longer = _settings(RELANCE_DELAY_2=72)
    assert (await _scan_at(factory, now + timedelta(hours=48), longer))[1] == 0
    assert (await _scan_at(factory, now + timedelta(hours=72), longer))[1] == 1
    second = (await _attempts(factory, lead_id))[1]
    # A policy change after selection is applied again at delivery.
    latest_policy = _settings(RELANCE_DELAY_2=96)
    assert (await deliver_candidate(
        factory, candidate_id=second.candidate_id, transport=fake,
        settings=latest_policy, now=now + timedelta(hours=72),
    ))[0] == "blocked"
    assert len(fake.calls) == 1
    assert (await deliver_candidate(
        factory, candidate_id=second.candidate_id, transport=fake,
        settings=latest_policy, now=now + timedelta(hours=96),
    ))[0] == "sent"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_customer_reply_after_first_send_stops_sequence(factory):
    now, phone, _, _, lead_id, _, first_id = await _seed(factory)
    fake = FakeTransport()
    await deliver_candidate(factory, candidate_id=first_id, transport=fake,
                            settings=_settings(), now=now)
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="I am back",
            content_type="text", timestamp=now + timedelta(hours=1),
            whatsapp_message_id=f"reply-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    assert (await _scan_at(factory, now + timedelta(days=5)))[1] == 0
    assert len(await _attempts(factory, lead_id)) == 1


@pytest.mark.asyncio
async def test_reply_after_second_selection_cancels_before_dispatch(factory):
    now, phone, _, _, lead_id, _, first_id = await _seed(factory)
    await deliver_candidate(factory, candidate_id=first_id, transport=FakeTransport(),
                            settings=_settings(), now=now)
    due = now + timedelta(hours=48)
    assert (await _scan_at(factory, due))[1] == 1
    second = (await _attempts(factory, lead_id))[1]
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="I am back",
            content_type="text", timestamp=due + timedelta(minutes=1),
            whatsapp_message_id=f"second-reply-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    fake = FakeTransport()
    assert (await deliver_candidate(
        factory, candidate_id=second.candidate_id, transport=fake,
        settings=_settings(), now=due + timedelta(minutes=1),
    ))[0] == "cancelled"
    assert fake.calls == [] and second.confirmed_sent_at is None
    assert (await _scan_at(factory, due + timedelta(days=5)))[1] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["human", "ai_paused", "escalation", "opt_out", "dormant"])
async def test_authority_change_after_first_send_suppresses_second(factory, change):
    now, phone, conversation_id, _, lead_id, operator_id, first_id = await _seed(factory)
    await deliver_candidate(factory, candidate_id=first_id, transport=FakeTransport(),
                            settings=_settings(), now=now)
    async with factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if change == "human":
            conversation.owner_type = "human"
            conversation.human_owner_account_id = operator_id
            conversation.ai_execution_state = "paused"
        elif change == "ai_paused":
            conversation.ai_execution_state = "paused"
        elif change == "escalation":
            session.add(EscalationTicket(
                conversation_id=conversation_id, customer_id=phone,
                reason="complex_complaint", priority="medium", status="open",
                transcript_snapshot=[],
            ))
        elif change == "opt_out":
            customer = await session.get(Customer, phone)
            customer.opt_out_flag = True
            customer.opt_out_at = now + timedelta(hours=1)
        else:
            conversation.status = "dormant"
        await session.commit()
    assert (await _scan_at(factory, now + timedelta(days=4)))[1] == 0
    assert len(await _attempts(factory, lead_id)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["consent", "template"])
async def test_second_readiness_blocks_selection_and_delivery(factory, change):
    now, _, _, _, lead_id, _, first_id = await _seed(factory)
    await deliver_candidate(factory, candidate_id=first_id, transport=FakeTransport(),
                            settings=_settings(), now=now)
    due = now + timedelta(hours=48)
    assert (await _scan_at(factory, due))[1] == 1
    second = (await _attempts(factory, lead_id))[1]
    if change == "consent":
        async with factory() as session:
            grant = await session.scalar(select(WhatsAppRelanceOptIn))
            await session.delete(grant)
            await session.commit()
        settings = _settings()
    else:
        settings = _settings(relance_template_french_name="")
    fake = FakeTransport()
    assert (await deliver_candidate(factory, candidate_id=second.candidate_id,
                                    transport=fake, settings=settings, now=due))[0] == "cancelled"
    assert fake.calls == []
    assert len(await _outbound(factory)) == 1
    assert (await _scan_at(factory, due + timedelta(hours=1), settings))[1] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["consent", "template"])
async def test_second_candidate_requires_current_readiness(factory, change):
    now, _, _, _, lead_id, _, first_id = await _seed(factory)
    await deliver_candidate(factory, candidate_id=first_id, transport=FakeTransport(),
                            settings=_settings(), now=now)
    settings = _settings()
    if change == "consent":
        async with factory() as session:
            grant = await session.scalar(select(WhatsAppRelanceOptIn))
            await session.delete(grant)
            await session.commit()
    else:
        settings = _settings(relance_template_french_name="")
    assert (await _scan_at(factory, now + timedelta(hours=48), settings))[1] == 0
    assert len(await _attempts(factory, lead_id)) == 1


@pytest.mark.asyncio
async def test_dormant_after_second_selection_blocks_dispatch(factory):
    now, _, conversation_id, _, lead_id, _, first_id = await _seed(factory)
    await deliver_candidate(factory, candidate_id=first_id, transport=FakeTransport(),
                            settings=_settings(), now=now)
    due = now + timedelta(hours=48)
    assert (await _scan_at(factory, due))[1] == 1
    second = (await _attempts(factory, lead_id))[1]
    async with factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        conversation.status = "dormant"
        await session.commit()
    fake = FakeTransport()
    assert (await deliver_candidate(factory, candidate_id=second.candidate_id,
                                    transport=fake, settings=_settings(), now=due))[0] == "cancelled"
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["failed", "cancelled", "uncertain", "prepared"])
async def test_unconfirmed_first_never_advances_to_second(factory, outcome):
    now, _, conversation_id, _, lead_id, operator_id, first_id = await _seed(factory)
    if outcome == "prepared":
        assert (await prepare_candidate_delivery(
            factory, candidate_id=first_id, settings=_settings(), now=now,
        ))[0] == "prepared"
    elif outcome == "cancelled":
        _, message_id = await prepare_candidate_delivery(
            factory, candidate_id=first_id, settings=_settings(), now=now,
        )
        async with factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            conversation.owner_type = "human"
            conversation.human_owner_account_id = operator_id
            conversation.ai_execution_state = "paused"
            await session.commit()
        assert await dispatch_prepared_delivery(
            factory, candidate_id=first_id, outbound_message_id=message_id,
            transport=FakeTransport(), settings=_settings(), now=now,
        ) == "cancelled"
    else:
        result = (DefiniteDeliveryFailure() if outcome == "failed"
                  else RuntimeError("ambiguous"))
        assert (await deliver_candidate(
            factory, candidate_id=first_id, transport=FakeTransport(result),
            settings=_settings(), now=now,
        ))[0] == outcome
    assert (await _scan_at(factory, now + timedelta(days=7)))[1] == 0
    assert [row.attempt_number for row in await _attempts(factory, lead_id)] == [1]
    async with factory() as session:
        assert (await session.get(RelanceCandidate, first_id)).confirmed_sent_at is None
        assert (await session.get(RelanceDelivery, first_id)).status == outcome


@pytest.mark.asyncio
async def test_uncertain_first_remains_blocked_even_after_new_inbound(factory):
    now, phone, _, _, lead_id, _, first_id = await _seed(factory)
    assert (await deliver_candidate(
        factory, candidate_id=first_id, transport=FakeTransport(RuntimeError("timeout")),
        settings=_settings(), now=now,
    ))[0] == "uncertain"
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="I am back",
            content_type="text", timestamp=now + timedelta(hours=1),
            whatsapp_message_id=f"uncertain-reply-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    assert (await _scan_at(factory, now + timedelta(days=5)))[1] == 0
    assert len(await _attempts(factory, lead_id)) == 1


@pytest.mark.asyncio
async def test_concurrent_second_scans_and_delivery_share_one_attempt(factory):
    now, _, _, _, lead_id, _, first_id = await _seed(factory)
    fake = FakeTransport()
    await deliver_candidate(factory, candidate_id=first_id, transport=fake,
                            settings=_settings(), now=now)
    due = now + timedelta(hours=48)
    results = await asyncio.gather(*(_scan_at(factory, due) for _ in range(3)))
    assert sum(result[1] for result in results) == 1
    second = (await _attempts(factory, lead_id))[1]
    deliveries = await asyncio.gather(*(deliver_candidate(
        factory, candidate_id=second.candidate_id, transport=fake,
        settings=_settings(), now=due,
    ) for _ in range(3)))
    assert len(await _attempts(factory, lead_id)) == 2
    assert len(await _outbound(factory)) == len(fake.calls) == 2
    assert len({result[1] for result in deliveries}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("language,locale", [
    ("french", "fr"), ("lingala", "ln"), ("swahili", "sw"),
])
async def test_second_attempt_uses_existing_localized_delivery(factory, language, locale):
    now, _, _, _, lead_id, _, first_id = await _seed(factory, language=language)
    fake = FakeTransport()
    await deliver_candidate(factory, candidate_id=first_id, transport=fake,
                            settings=_settings(), now=now)
    due = now + timedelta(hours=48)
    assert (await _scan_at(factory, due))[1] == 1
    second = (await _attempts(factory, lead_id))[1]
    assert (await deliver_candidate(factory, candidate_id=second.candidate_id,
                                    transport=fake, settings=_settings(), now=due))[0] == "sent"
    assert [call[3] for call in fake.calls] == [locale, locale]
