"""
Phase 1.E — Relance Flow Integration Tests
==========================================
Tests relance scheduling, eligibility, quiet-hours enforcement, and template fallbacks.

Run: pytest backend/tests/integration/test_relance_flow.py -v --tb=short
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.relance import Relance
from app.modules.m6_relance.eligibility import find_eligible_leads
from app.modules.m6_relance.hooks import get_fallback_hook
from app.modules.m6_relance.scheduler import calculate_next_relance_time
from app.schemas.common import Language

pytestmark = pytest.mark.asyncio(loop_scope="session")

KINSHASA_TZ_OFFSET = timedelta(hours=1)
QUIET_START = 22
QUIET_END = 7


@pytest.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


def _phone() -> str:
    return f"+243{uuid.uuid4().int % 10**9:09d}"


async def _seed_lead(
    session: AsyncSession,
    silent_hours: int = 25,
    opt_out: bool = False,
    relance_count: int = 0,
    score: str = "warm",
) -> tuple[Customer, Conversation, Lead]:
    phone = _phone()
    customer = Customer(
        phone_number=phone,
        name="Test",
        city="Kinshasa",
        preferred_language=Language.french.value,
        opt_out_flag=opt_out,
    )
    session.add(customer)
    await session.flush()

    conv = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=phone,
        language_detected=Language.french.value,
        context={},
        last_message_time=datetime.now(timezone.utc) - timedelta(hours=silent_hours),
        message_count=3,
    )
    session.add(conv)
    await session.flush()

    lead = Lead(
        lead_id=uuid.uuid4(),
        conversation_id=conv.conversation_id,
        customer_id=phone,
        intent="buy",
        product_interest=["câble HDMI"],
        source="whatsapp",
        score=score,
        score_value=55 if score == "warm" else 80,
        stage="nurturing",
        relance_count=relance_count,
    )
    session.add(lead)
    await session.commit()
    return customer, conv, lead


# ── Scheduling cadence ────────────────────────────────────────────────────────

async def test_relance_attempt1_scheduled_24h_after_last_message():
    """Attempt 1 is scheduled +24h after last customer message"""
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(last_customer_message_time=base, attempt_number=1)
    expected = base + timedelta(hours=24)
    assert scheduled == expected


async def test_relance_attempt2_scheduled_60h_quiet_hours_adjusted():
    """Attempt 2 (+60h) adjusted if it lands in Kinshasa quiet hours (22:00-07:00)"""
    # 2026-05-01 10:00 UTC + 60h = 2026-05-03 22:00 UTC = 23:00 Kinshasa → quiet
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(last_customer_message_time=base, attempt_number=2)
    kinshasa_hour = (scheduled + KINSHASA_TZ_OFFSET).hour
    assert not (QUIET_START <= kinshasa_hour or kinshasa_hour < QUIET_END), (
        f"Attempt 2 scheduled at Kinshasa hour {kinshasa_hour} (inside quiet window)"
    )


async def test_relance_attempt3_scheduled_8_5_days_quiet_hours_adjusted():
    """Attempt 3 (+8.5d) adjusted if in quiet hours"""
    base = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(last_customer_message_time=base, attempt_number=3)
    kinshasa_hour = (scheduled + KINSHASA_TZ_OFFSET).hour
    assert not (QUIET_START <= kinshasa_hour or kinshasa_hour < QUIET_END), (
        f"Attempt 3 scheduled at Kinshasa hour {kinshasa_hour} (inside quiet window)"
    )


# ── Eligibility ───────────────────────────────────────────────────────────────

async def test_lead_eligible_after_24h_silence(db: AsyncSession):
    """Lead with 25h silence and 0 relances is eligible"""
    _, _, lead = await _seed_lead(db, silent_hours=25, relance_count=0)
    eligible = await find_eligible_leads(db)
    assert lead.lead_id in [l.lead_id for l in eligible]


async def test_lead_not_eligible_if_recent(db: AsyncSession):
    """Lead with only 6h silence is NOT eligible"""
    phone = _phone()
    customer = Customer(
        phone_number=phone, name="Recent", city="Kinshasa",
        preferred_language=Language.french.value, opt_out_flag=False,
    )
    db.add(customer)
    await db.flush()

    conv = Conversation(
        conversation_id=uuid.uuid4(), customer_id=phone,
        language_detected=Language.french.value, context={},
        last_message_time=datetime.now(timezone.utc) - timedelta(hours=6),
        message_count=2,
    )
    db.add(conv)
    await db.flush()

    lead = Lead(
        lead_id=uuid.uuid4(), conversation_id=conv.conversation_id, customer_id=phone,
        intent="buy", product_interest=["câble"], source="whatsapp",
        score="warm", score_value=50, stage="nurturing", relance_count=0,
    )
    db.add(lead)
    await db.commit()

    eligible = await find_eligible_leads(db)
    assert lead.lead_id not in [l.lead_id for l in eligible]


async def test_opted_out_lead_not_eligible(db: AsyncSession):
    """Opted-out customer's leads are excluded from relance eligibility"""
    _, _, lead = await _seed_lead(db, silent_hours=30, opt_out=True, relance_count=0)
    eligible = await find_eligible_leads(db)
    assert lead.lead_id not in [l.lead_id for l in eligible]


async def test_lead_with_3_relances_not_eligible(db: AsyncSession):
    """Lead that already had 3 relances is not eligible (hard limit)"""
    _, _, lead = await _seed_lead(db, silent_hours=25, relance_count=3)
    eligible = await find_eligible_leads(db)
    assert lead.lead_id not in [l.lead_id for l in eligible]


# ── Fallback templates ────────────────────────────────────────────────────────

@pytest.mark.parametrize("attempt", [1, 2, 3])
@pytest.mark.parametrize("lang", [Language.french, Language.lingala, Language.swahili])
async def test_all_fallback_templates_exist(attempt: int, lang: Language):
    """All 9 fallback templates (3 langs × 3 attempts) return non-empty strings"""
    text = get_fallback_hook(attempt, lang)
    assert isinstance(text, str)
    assert len(text) >= 20, f"Template too short: attempt={attempt} lang={lang.value}: '{text}'"


async def test_fallback_templates_are_distinct():
    """All 9 fallback templates are distinct (no copy-paste duplicates)"""
    templates = []
    for attempt in [1, 2, 3]:
        for lang in [Language.french, Language.lingala, Language.swahili]:
            templates.append(get_fallback_hook(attempt, lang))
    assert len(set(templates)) == 9, "Some fallback templates are duplicates"


async def test_french_fallback_contains_french_words():
    """French fallbacks contain French (not Lingala/Swahili)"""
    for attempt in [1, 2, 3]:
        text = get_fallback_hook(attempt, Language.french)
        has_french = any(kw in text.lower() for kw in ["le", "la", "vous", "disponible", "offre", "produit", "hey", "merci"])
        assert has_french, f"French fallback attempt {attempt} doesn't look French: '{text}'"


async def test_lingala_fallback_contains_lingala_words():
    """Lingala fallbacks contain Lingala markers"""
    for attempt in [1, 2, 3]:
        text = get_fallback_hook(attempt, Language.lingala)
        has_lingala = any(kw in text.lower() for kw in ["ndeko", "mbote", "nalingi", "malamu", "batu", "olingi", "eloko"])
        assert has_lingala, f"Lingala fallback attempt {attempt} doesn't look Lingala: '{text}'"


async def test_swahili_fallback_contains_swahili_words():
    """Swahili fallbacks contain Swahili markers"""
    for attempt in [1, 2, 3]:
        text = get_fallback_hook(attempt, Language.swahili)
        has_swahili = any(kw in text.lower() for kw in ["habari", "rafiki", "bidhaa", "sawa", "watu", "ofa", "mwisho"])
        assert has_swahili, f"Swahili fallback attempt {attempt} doesn't look Swahili: '{text}'"


# ── Max relance hard limit ────────────────────────────────────────────────────

async def test_relance_max_3_attempts_enforced(db: AsyncSession):
    """Relance service enforces max 3 attempts per lead"""
    _, _, lead = await _seed_lead(db, silent_hours=25, relance_count=0)
    base_time = datetime.now(timezone.utc)

    hook_types = ["reciprocity", "social_proof", "scarcity"]
    for attempt in range(1, 4):
        r = Relance(
            relance_id=uuid.uuid4(),
            lead_id=lead.lead_id,
            attempt_number=attempt,
            scheduled_at=base_time + timedelta(hours=24 * attempt),
            value_hook=f"Hook #{attempt}",
            hook_type=hook_types[attempt - 1],
        )
        db.add(r)
    await db.commit()

    result = await db.execute(
        select(Relance).where(Relance.lead_id == lead.lead_id)
    )
    relances = result.scalars().all()
    assert len(relances) == 3

    result = await db.execute(select(Lead).where(Lead.lead_id == lead.lead_id))
    saved_lead = result.scalar_one()
    saved_lead.relance_count = 3
    await db.commit()

    eligible = await find_eligible_leads(db)
    assert lead.lead_id not in [l.lead_id for l in eligible]
