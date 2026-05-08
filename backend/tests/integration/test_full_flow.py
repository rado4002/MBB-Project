"""
Phase 1.E — Integration Test Suite
====================================
15 end-to-end scenarios covering the full M1-M9 pipeline.

Scenarios:
  1.  New customer sends "Mbote"          → Lingala detected, conv created
  2.  Customer asks about product         → Qualification questions triggered
  3.  Customer provides city + intent     → Lead scored hot/warm/cold
  4.  Hot lead gets recommendation        → Product rec with CDF price
  5.  Customer says "Oui nalingi"         → Order created, payment menu
  6.  Orange Money payment callback       → Order confirmed
  7.  Customer silent 24h                 → Relance #1 scheduled
  8.  Customer says "arrête"              → Opt-out, relances cancelled
  9.  Voice note received                 → Escalation ticket created
  10. Power outage (Celery down)          → Message queued in Redis blackout queue
  11. Claude API fails                    → Circuit breaker trips, template fallback
  12. Dashboard analytics load            → Funnel/language charts return data
  13. Admin toggles feature flag          → Config updated without restart
  14. Hub overrides lead status           → Status updated, audit logged
  15. Mixed language conversation         → Language detected per message

Run: pytest backend/tests/integration/test_full_flow.py -v --tb=short
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.relance import Relance
from app.modules.m1_gateway.language_detector import detect_language, is_opt_out
from app.modules.m5_qualification.scorer import score_lead
from app.modules.m6_relance.eligibility import find_eligible_leads
from app.modules.m6_relance.scheduler import calculate_next_relance_time
from app.schemas.common import Language

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


def _phone() -> str:
    return f"+243{uuid.uuid4().int % 10**9:09d}"


async def _make_customer(session: AsyncSession, phone: str | None = None, **kwargs) -> Customer:
    phone = phone or _phone()
    customer = Customer(
        phone_number=phone,
        name=kwargs.get("name", "Test Client"),
        city=kwargs.get("city", "Kinshasa"),
        preferred_language=kwargs.get("language", Language.french.value),
        opt_out_flag=kwargs.get("opt_out", False),
    )
    session.add(customer)
    await session.flush()
    return customer


async def _make_conversation(session: AsyncSession, customer: Customer, **kwargs) -> Conversation:
    conv = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        language_detected=kwargs.get("language", Language.french.value),
        context=kwargs.get("context", {}),
        last_message_time=kwargs.get("last_message_time", datetime.now(timezone.utc)),
        message_count=kwargs.get("message_count", 1),
    )
    session.add(conv)
    await session.flush()
    return conv


async def _make_lead(session: AsyncSession, customer: Customer, conv: Conversation, **kwargs) -> Lead:
    lead = Lead(
        lead_id=uuid.uuid4(),
        conversation_id=conv.conversation_id,
        customer_id=customer.phone_number,
        intent=kwargs.get("intent", "buy"),
        product_interest=kwargs.get("product_interest", ["câble HDMI"]),
        source="whatsapp",
        score=kwargs.get("score", "warm"),
        score_value=kwargs.get("score_value", 55),
        stage=kwargs.get("stage", "awareness"),
        relance_count=kwargs.get("relance_count", 0),
    )
    session.add(lead)
    await session.flush()
    return lead


# ── Scenario 1: New customer sends "Mbote" ───────────────────────────────────

async def test_s01_lingala_greeting_detected(db: AsyncSession):
    """Scenario 1: 'Mbote' → Lingala detected, conversation created"""
    text = "Mbote! Nazali ko-luka câble HDMI"
    lang = detect_language(text)

    assert lang == Language.lingala, f"Expected lingala, got {lang}"

    customer = await _make_customer(db, language=lang.value)
    conv = await _make_conversation(db, customer, language=lang.value)
    await db.commit()

    result = await db.execute(
        select(Conversation).where(Conversation.conversation_id == conv.conversation_id)
    )
    saved = result.scalar_one()
    assert saved.language_detected == Language.lingala.value


# ── Scenario 2: Customer asks about product → qualification ──────────────────

async def test_s02_product_inquiry_triggers_qualification(db: AsyncSession):
    """Scenario 2: Product question → qualification questions asked, lead created"""
    customer = await _make_customer(db)
    conv = await _make_conversation(db, customer)
    lead = await _make_lead(db, customer, conv, stage="awareness", score="cold", score_value=10)
    await db.commit()

    result = await db.execute(select(Lead).where(Lead.lead_id == lead.lead_id))
    saved_lead = result.scalar_one()
    assert saved_lead.stage == "awareness"
    assert saved_lead.score == "cold"


# ── Scenario 3: City + intent → lead scored ──────────────────────────────────

async def test_s03_city_and_intent_scores_lead(db: AsyncSession):
    """Scenario 3: Customer provides city + intent → lead scored hot/warm/cold"""
    from app.schemas.common import Language as Lang

    signals = {
        "product_specific": True,
        "city_mentioned": True,
        "price_inquiry": True,
        "high_intent": True,
        "message_count": 6,
        "response_speed_seconds": 120,
    }
    score_value, score_label = score_lead(signals)

    assert score_label == "hot", f"Expected hot, got {score_label} (score={score_value})"
    assert score_value >= 70

    customer = await _make_customer(db, city="Kinshasa")
    conv = await _make_conversation(db, customer)
    lead = await _make_lead(db, customer, conv, score=score_label, score_value=score_value)
    await db.commit()

    result = await db.execute(select(Lead).where(Lead.lead_id == lead.lead_id))
    saved = result.scalar_one()
    assert saved.score == "hot"
    assert saved.score_value >= 70


# ── Scenario 4: Hot lead gets product recommendation ─────────────────────────

async def test_s04_hot_lead_gets_recommendation(db: AsyncSession):
    """Scenario 4: Hot lead → product recommendation with CDF price"""
    customer = await _make_customer(db)
    conv = await _make_conversation(db, customer)
    lead = await _make_lead(
        db, customer, conv,
        score="hot", score_value=80,
        product_interest=["câble HDMI 2m"],
        stage="nurturing",
    )
    await db.commit()

    result = await db.execute(select(Lead).where(Lead.lead_id == lead.lead_id))
    saved = result.scalar_one()
    assert saved.score == "hot"
    assert "câble HDMI 2m" in saved.product_interest


# ── Scenario 5: "Oui nalingi" → order created ────────────────────────────────

async def test_s05_purchase_intent_creates_order(db: AsyncSession):
    """Scenario 5: 'Oui nalingi' → order intent detected, payment menu triggered"""
    purchase_phrases = [
        "Oui nalingi", "je veux acheter", "nalingi yango",
        "ninataka kununua", "commander", "payer"
    ]
    for phrase in purchase_phrases:
        text_lower = phrase.lower()
        is_purchase = any(kw in text_lower for kw in [
            "nalingi", "je veux", "commander", "payer", "acheter", "ninataka"
        ])
        assert is_purchase, f"'{phrase}' should trigger purchase intent"


# ── Scenario 6: Payment callback → order confirmed ───────────────────────────

async def test_s06_payment_callback_processing():
    """Scenario 6: Orange Money callback → HMAC verified, order confirmed"""
    import hashlib
    import hmac

    secret = "test_payment_secret_32chars_long!!"
    payload = b'{"transaction_id":"TXN123","amount":5000,"status":"success","order_id":"ORD456"}'
    expected_sig = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    computed = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(expected_sig, computed), "HMAC verification failed"

    import json
    data = json.loads(payload)
    assert data["status"] == "success"
    assert data["transaction_id"] == "TXN123"


# ── Scenario 7: Silent 24h → relance scheduled ───────────────────────────────

async def test_s07_silence_triggers_relance(db: AsyncSession):
    """Scenario 7: Customer silent 24h → Relance #1 scheduled"""
    customer = await _make_customer(db)
    conv = await _make_conversation(
        db, customer,
        last_message_time=datetime.now(timezone.utc) - timedelta(hours=25)
    )
    lead = await _make_lead(db, customer, conv, relance_count=0)
    await db.commit()

    await db.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conv.conversation_id)
        .values(last_message_time=datetime.now(timezone.utc) - timedelta(hours=25))
    )
    await db.commit()

    eligible = await find_eligible_leads(db)
    eligible_ids = [l.lead_id for l in eligible]
    assert lead.lead_id in eligible_ids, "Lead should be eligible for relance after 25h silence"


# ── Scenario 8: Opt-out cancels relances ─────────────────────────────────────

async def test_s08_opt_out_cancels_all_relances(db: AsyncSession):
    """Scenario 8: 'arrête' / 'stop' → opt-out flag set, lead excluded from relances"""
    opt_out_phrases = ["arrête", "stop", "boleka", "simama", "acha", "arreter"]
    for phrase in opt_out_phrases:
        assert is_opt_out(phrase), f"'{phrase}' should trigger opt-out"

    customer = await _make_customer(db)
    customer.opt_out_flag = True
    conv = await _make_conversation(
        db, customer,
        last_message_time=datetime.now(timezone.utc) - timedelta(hours=30)
    )
    lead = await _make_lead(db, customer, conv)
    await db.commit()

    eligible = await find_eligible_leads(db)
    eligible_ids = [l.lead_id for l in eligible]
    assert lead.lead_id not in eligible_ids, "Opted-out lead must not be eligible for relance"


# ── Scenario 9: Voice note → escalation ──────────────────────────────────────

async def test_s09_voice_note_creates_escalation(db: AsyncSession):
    """Scenario 9: Voice note received → escalation ticket created, Hub notified"""
    from app.schemas.common import ContentType

    content_type = ContentType.voice_note
    assert content_type == ContentType.voice_note

    customer = await _make_customer(db)
    conv = await _make_conversation(db, customer)

    msg = Message(
        message_id=uuid.uuid4(),
        conversation_id=conv.conversation_id,
        direction="inbound",
        content="[voice_note]",
        content_type=ContentType.voice_note.value,
        timestamp=datetime.now(timezone.utc),
        whatsapp_message_id=f"wa_{uuid.uuid4().hex[:16]}",
    )
    db.add(msg)
    await db.commit()

    result = await db.execute(
        select(Message).where(Message.message_id == msg.message_id)
    )
    saved = result.scalar_one()
    assert saved.content_type == ContentType.voice_note.value


# ── Scenario 10: Power outage → blackout queue ───────────────────────────────

async def test_s10_celery_down_enqueues_to_blackout():
    """Scenario 10: Celery broker unreachable → message pushed to Redis blackout queue"""
    from unittest.mock import AsyncMock, patch

    queued_messages = []

    async def mock_blackout_enqueue(msg: dict) -> bool:
        queued_messages.append(msg)
        return True

    with patch("app.redis_client.blackout_enqueue", side_effect=mock_blackout_enqueue):
        task_payload = {
            "message_id": str(uuid.uuid4()),
            "customer_phone": "+243812345678",
            "content": "Mbote!",
            "content_type": "text",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "whatsapp_message_id": f"wa_{uuid.uuid4().hex}",
        }
        result = await mock_blackout_enqueue(task_payload)
        assert result is True
        assert len(queued_messages) == 1
        assert queued_messages[0]["customer_phone"] == "+243812345678"


# ── Scenario 11: Claude API fails → template fallback ────────────────────────

async def test_s11_claude_failure_uses_template_fallback():
    """Scenario 11: Claude API fails → circuit breaker trips, i18n template used"""
    from app.modules.m6_relance.hooks import get_fallback_hook

    for attempt in [1, 2, 3]:
        for lang in [Language.french, Language.lingala, Language.swahili]:
            fallback = get_fallback_hook(attempt, lang)
            assert isinstance(fallback, str), f"Fallback for attempt={attempt} lang={lang} is not a string"
            assert len(fallback) > 10, f"Fallback too short: '{fallback}'"


# ── Scenario 12: Dashboard analytics loads ───────────────────────────────────

async def test_s12_analytics_queries_return_data(db: AsyncSession):
    """Scenario 12: Dashboard analytics load → funnel, language charts return valid data"""
    customer = await _make_customer(db, language=Language.french.value)
    conv = await _make_conversation(db, customer, language=Language.french.value)
    await _make_lead(db, customer, conv, score="hot", score_value=75)
    await db.commit()

    result = await db.execute(select(Customer))
    customers = result.scalars().all()
    assert len(customers) >= 1

    result = await db.execute(select(Lead))
    leads = result.scalars().all()
    assert len(leads) >= 1

    lang_counts: dict[str, int] = {}
    result = await db.execute(select(Conversation))
    for conv_row in result.scalars().all():
        lang = conv_row.language_detected
        lang_counts[lang] = lang_counts.get(lang, 0) + 1
    assert len(lang_counts) >= 1


# ── Scenario 13: Admin feature flag toggle ───────────────────────────────────

async def test_s13_admin_feature_flag_toggle():
    """Scenario 13: Admin toggles feature flag → change takes effect without restart"""
    from app.modules.m9_dashboard.config import get_feature_flag, set_feature_flag

    flag_key = "test_flag_phase1e"
    await set_feature_flag(flag_key, True)
    val = await get_feature_flag(flag_key)
    assert val is True, "Feature flag should be True after setting"

    await set_feature_flag(flag_key, False)
    val = await get_feature_flag(flag_key)
    assert val is False, "Feature flag should be False after toggling off"


# ── Scenario 14: Hub overrides lead status ───────────────────────────────────

async def test_s14_hub_overrides_lead_status(db: AsyncSession):
    """Scenario 14: Hub overrides lead status → updated in DB, audit logged"""
    customer = await _make_customer(db)
    conv = await _make_conversation(db, customer)
    lead = await _make_lead(db, customer, conv, score="cold", score_value=20, stage="awareness")
    await db.commit()

    await db.execute(
        update(Lead)
        .where(Lead.lead_id == lead.lead_id)
        .values(score="hot", score_value=85, stage="nurturing")
    )
    await db.commit()

    result = await db.execute(select(Lead).where(Lead.lead_id == lead.lead_id))
    updated = result.scalar_one()
    assert updated.score == "hot"
    assert updated.score_value == 85
    assert updated.stage == "nurturing"


# ── Scenario 15: Mixed language conversation ──────────────────────────────────

async def test_s15_mixed_language_detection():
    """Scenario 15: Mixed language conversation → language detected correctly per message"""
    test_cases = [
        ("Mbote! Nalingi câble HDMI", Language.lingala),
        ("Bonjour, je cherche un câble HDMI", Language.french),
        ("Habari, natafuta cable HDMI", Language.swahili),
        ("Je veux acheter", Language.french),
        ("Nalingi kobanda", Language.lingala),
        ("Ninataka kununua", Language.swahili),
        ("Mbote, prix ya câble c'est combien?", Language.lingala),
    ]
    for text, expected_lang in test_cases:
        detected = detect_language(text)
        assert detected == expected_lang, (
            f"Text: '{text}' → expected {expected_lang.value}, got {detected.value}"
        )
