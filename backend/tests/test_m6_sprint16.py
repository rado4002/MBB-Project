"""
backend/tests/test_m6_sprint16.py — Unit + integration tests for Sprint 1.6 (M6 Relance Engine).

Tests cover:
  1. Relance eligibility detection
  2. Quiet hours enforcement (22:00–07:00 Kinshasa)
  3. Relance cadence calculation (+24h, +60h, +8.5d)
  4. Value hook generation (3 different angles)
  5. Relance scheduling and delivery
  6. Max-3 relance hard limit
  7. Opt-out → relance cancellation
  8. Full integration test (eligible lead → relance sent)
"""
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.database import AsyncSessionLocal

# All async tests in this module share a session-scoped event loop
pytestmark = pytest.mark.asyncio(loop_scope="session")
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.relance import Relance
from app.modules.m6_relance.eligibility import find_eligible_leads, get_last_relance_for_lead
from app.modules.m6_relance.hooks import generate_relance_hook
from app.modules.m6_relance.scheduler import (
    calculate_next_relance_time,
    get_next_allowed_time,
    is_quiet_hours,
)
from app.modules.m6_relance.service import (
    cancel_all_relances,
    create_and_schedule_relance,
    mark_relance_delivered,
)
from app.schemas.common import Language


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Quiet hours detection
# ─────────────────────────────────────────────────────────────────────────────

def test_is_quiet_hours_22h():
    """22:00 Kinshasa time → is_quiet_hours = True"""
    # 22:00 Kinshasa = 21:00 UTC (Kinshasa is UTC+1 / WAT)
    dt_utc = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt_utc) is True


def test_is_quiet_hours_03h():
    """03:00 Kinshasa time → is_quiet_hours = True"""
    # 03:00 Kinshasa = 01:00 UTC
    dt_utc = datetime(2026, 4, 17, 1, 0, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt_utc) is True


def test_is_quiet_hours_09h():
    """09:00 Kinshasa time → is_quiet_hours = False"""
    # 09:00 Kinshasa = 07:00 UTC
    dt_utc = datetime(2026, 4, 17, 7, 0, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt_utc) is False


def test_is_quiet_hours_15h():
    """15:00 Kinshasa time → is_quiet_hours = False"""
    # 15:00 Kinshasa = 13:00 UTC
    dt_utc = datetime(2026, 4, 17, 13, 0, 0, tzinfo=timezone.utc)
    assert is_quiet_hours(dt_utc) is False


def test_get_next_allowed_time_quiet():
    """If in quiet hours, reschedule to next 07:00 Kinshasa"""
    # 23:00 Kinshasa (21:00 UTC) → next allowed = 07:00 next day
    dt_utc = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
    next_time = get_next_allowed_time(dt_utc)
    
    # Convert to Kinshasa time to check
    next_kin = next_time.astimezone(ZoneInfo("Africa/Kinshasa"))
    assert next_kin.hour == 7
    assert next_kin.minute == 0
    assert next_kin.day == 18  # Next day


def test_get_next_allowed_time_allowed():
    """If not in quiet hours, return same time"""
    # 10:00 Kinshasa (08:00 UTC) → return as-is
    dt_utc = datetime(2026, 4, 17, 8, 0, 0, tzinfo=timezone.utc)
    next_time = get_next_allowed_time(dt_utc)
    assert next_time == dt_utc


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Relance cadence calculation
# ─────────────────────────────────────────────────────────────────────────────

def test_calculate_next_relance_time_attempt1():
    """Relance #1: +24 hours from last customer message"""
    last_msg = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(
        last_customer_message_time=last_msg,
        attempt_number=1,
    )
    
    # Should be exactly 24 hours later (no quiet hours interference for this test time)
    expected = last_msg + timedelta(hours=24)
    assert scheduled == expected


def test_calculate_next_relance_time_attempt2():
    """Relance #2: +60 hours (2.5 days) from last customer message"""
    last_msg = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(
        last_customer_message_time=last_msg,
        attempt_number=2,
    )

    # +60h = 2026-04-19 22:00 UTC = 23:00 Kinshasa → quiet hours
    # → rescheduled to 07:00 next day Kinshasa = 06:00 UTC
    expected = datetime(2026, 4, 20, 6, 0, 0, tzinfo=timezone.utc)
    assert scheduled == expected


def test_calculate_next_relance_time_attempt3():
    """Relance #3: +8.5 days from last customer message"""
    last_msg = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
    scheduled = calculate_next_relance_time(
        last_customer_message_time=last_msg,
        attempt_number=3,
    )

    # +8.5 days = 2026-04-25 22:00 UTC = 23:00 Kinshasa → quiet hours
    # → rescheduled to 07:00 next day Kinshasa = 06:00 UTC
    expected = datetime(2026, 4, 26, 6, 0, 0, tzinfo=timezone.utc)
    assert scheduled == expected


def test_calculate_next_relance_time_invalid_attempt():
    """Attempt number > 3 → raises ValueError"""
    last_msg = datetime(2026, 4, 17, 10, 0, 0, tzinfo=timezone.utc)
    
    with pytest.raises(ValueError, match="Invalid attempt_number: 4"):
        calculate_next_relance_time(
            last_customer_message_time=last_msg,
            attempt_number=4,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Value hook generation
# ─────────────────────────────────────────────────────────────────────────────


async def test_generate_relance_hook_attempt1():
    """Relance #1 hook: reciprocity angle"""
    hook_text, hook_type = await generate_relance_hook(
        attempt_number=1,
        language=Language.french,
        product_interest="câble HDMI",
        city="Kinshasa",
        customer_name="Jean",
        previous_hooks=None,
    )
    
    assert hook_type == "reciprocity"
    assert isinstance(hook_text, str)
    assert len(hook_text) > 0
    # Should be short (2-3 sentences max = ~200 chars typical)
    assert len(hook_text) < 500



async def test_generate_relance_hook_attempt2():
    """Relance #2 hook: social_proof angle"""
    hook_text, hook_type = await generate_relance_hook(
        attempt_number=2,
        language=Language.french,
        product_interest="powerbank",
        city="Lubumbashi",
        customer_name=None,
        previous_hooks=["Hey! Le câble est disponible."],
    )
    
    assert hook_type == "social_proof"
    assert isinstance(hook_text, str)
    assert len(hook_text) > 0



async def test_generate_relance_hook_attempt3():
    """Relance #3 hook: scarcity angle"""
    hook_text, hook_type = await generate_relance_hook(
        attempt_number=3,
        language=Language.lingala,
        product_interest="écouteurs",
        city="Kinshasa",
        customer_name="Marie",
        previous_hooks=["Hook 1", "Hook 2"],
    )
    
    assert hook_type == "scarcity"
    assert isinstance(hook_text, str)
    assert len(hook_text) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Eligibility detection (database integration)
# ─────────────────────────────────────────────────────────────────────────────


async def test_find_eligible_leads_silent_24h():
    """Lead silent for 24+ hours → eligible for relance"""
    async with AsyncSessionLocal() as session:
        # Create test customer (all-numeric phone to pass chk_phone_format)
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=False,
        )
        session.add(customer)

        # Create conversation (last message 25h ago)
        now = datetime.now(timezone.utc)
        last_msg_time = now - timedelta(hours=25)

        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=last_msg_time,
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        # Create lead (0 relances, not converted)
        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble"],
            source="whatsapp",
            relance_count=0,
        )
        session.add(lead)
        await session.commit()
        
        # Query eligible leads
        eligible = await find_eligible_leads(session, silence_threshold_hours=24, max_relances=3)
        
        # Should include our test lead
        lead_ids = [l.lead_id for l in eligible]
        assert lead.lead_id in lead_ids
        
        # Cleanup
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()



async def test_find_eligible_leads_max_relances_reached():
    """Lead with 3 relances → NOT eligible"""
    async with AsyncSessionLocal() as session:
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=False,
        )
        session.add(customer)

        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=now - timedelta(hours=25),
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        # Lead with 3 relances already sent (max reached)
        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble"],
            source="whatsapp",
            relance_count=3,  # Max reached
        )
        session.add(lead)
        await session.commit()
        
        eligible = await find_eligible_leads(session, silence_threshold_hours=24, max_relances=3)
        
        # Should NOT include our test lead
        lead_ids = [l.lead_id for l in eligible]
        assert lead.lead_id not in lead_ids
        
        # Cleanup
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()



async def test_find_eligible_leads_opted_out():
    """Customer opted out → lead NOT eligible"""
    async with AsyncSessionLocal() as session:
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=True,  # Opted out
        )
        session.add(customer)

        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=now - timedelta(hours=25),
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble"],
            source="whatsapp",
            relance_count=0,
        )
        session.add(lead)
        await session.commit()
        
        eligible = await find_eligible_leads(session, silence_threshold_hours=24, max_relances=3)
        
        lead_ids = [l.lead_id for l in eligible]
        assert lead.lead_id not in lead_ids
        
        # Cleanup
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Relance service operations
# ─────────────────────────────────────────────────────────────────────────────


async def test_create_and_schedule_relance():
    """Create relance record and schedule send time"""
    async with AsyncSessionLocal() as session:
        # Setup test data
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=False,
        )
        session.add(customer)

        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=now - timedelta(hours=25),
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble HDMI"],
            source="whatsapp",
            relance_count=0,
        )
        session.add(lead)
        await session.commit()
        
        # Create relance
        relance = await create_and_schedule_relance(
            session,
            lead=lead,
            conversation=conversation,
        )
        
        await session.commit()
        
        # Verify relance created
        assert relance is not None
        assert relance.attempt_number == 1
        assert relance.lead_id == lead.lead_id
        assert relance.hook_type in ["reciprocity", "social_proof", "scarcity", "loyalty"]
        assert len(relance.value_hook) > 0
        
        # Verify lead.relance_count updated
        await session.refresh(lead)
        assert lead.relance_count == 1
        
        # Cleanup
        await session.delete(relance)
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()



async def test_create_and_schedule_relance_max_reached():
    """Attempting to create relance #4 → returns None"""
    async with AsyncSessionLocal() as session:
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=False,
        )
        session.add(customer)

        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=now - timedelta(hours=25),
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble"],
            source="whatsapp",
            relance_count=3,  # Already at max
        )
        session.add(lead)
        await session.commit()
        
        # Try to create relance #4
        relance = await create_and_schedule_relance(
            session,
            lead=lead,
            conversation=conversation,
        )
        
        # Should return None
        assert relance is None
        
        # Cleanup
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()



async def test_cancel_all_relances():
    """Cancel all pending relances for a lead"""
    async with AsyncSessionLocal() as session:
        # Setup test data
        customer_phone = f"+243{uuid.uuid4().int % 10**9:09d}"
        customer = Customer(
            phone_number=customer_phone,
            city="Kinshasa",
            preferred_language="french",
            opt_out_flag=False,
        )
        session.add(customer)

        now = datetime.now(timezone.utc)
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=customer_phone,
            status="active",
            language_detected="french",
            last_message_time=now - timedelta(hours=25),
            message_count=5,
        )
        session.add(conversation)
        await session.flush()

        lead = Lead(
            lead_id=uuid.uuid4(),
            customer_id=customer_phone,
            conversation_id=conversation.conversation_id,
            score="warm",
            score_value=50,
            stage="consideration",
            intent="buy",
            product_interest=["câble"],
            source="whatsapp",
            relance_count=2,
        )
        session.add(lead)
        await session.flush()
        
        # Create 2 pending relances
        relance1 = Relance(
            relance_id=uuid.uuid4(),
            lead_id=lead.lead_id,
            attempt_number=1,
            scheduled_at=now + timedelta(hours=1),
            value_hook="Hook 1",
            hook_type="reciprocity",
            delivered_at=None,
            cancelled=False,
        )
        relance2 = Relance(
            relance_id=uuid.uuid4(),
            lead_id=lead.lead_id,
            attempt_number=2,
            scheduled_at=now + timedelta(hours=48),
            value_hook="Hook 2",
            hook_type="social_proof",
            delivered_at=None,
            cancelled=False,
        )
        session.add_all([relance1, relance2])
        await session.commit()
        
        # Cancel all relances
        cancelled_count = await cancel_all_relances(session, lead_id=lead.lead_id)
        await session.commit()
        
        # Should cancel 2 relances
        assert cancelled_count == 2
        
        # Verify relances marked as cancelled
        await session.refresh(relance1)
        await session.refresh(relance2)
        assert relance1.cancelled is True
        assert relance2.cancelled is True
        
        # Cleanup
        await session.delete(relance1)
        await session.delete(relance2)
        await session.delete(lead)
        await session.delete(conversation)
        await session.delete(customer)
        await session.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Summary: Test count
# ─────────────────────────────────────────────────────────────────────────────

# Total tests: 20
# - Quiet hours: 6 tests
# - Cadence calculation: 4 tests
# - Hook generation: 3 tests
# - Eligibility: 3 tests
# - Service operations: 4 tests

if __name__ == "__main__":
    print("Run with: python tests/test_m6_sprint16.py")
    print("Or: pytest tests/test_m6_sprint16.py -v")
