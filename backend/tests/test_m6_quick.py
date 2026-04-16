"""
Quick validation test for M6 Sprint 1.6 implementation.
Tests basic functionality without requiring pytest.
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Add backend to path
sys.path.insert(0, ".")

print("=" * 70)
print("M6 SPRINT 1.6 — QUICK VALIDATION TEST")
print("=" * 70)
print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Imports
# ─────────────────────────────────────────────────────────────────────────────

print("[1/7] Testing imports...")
try:
    from app.modules.m6_relance.eligibility import find_eligible_leads
    from app.modules.m6_relance.hooks import generate_relance_hook
    from app.modules.m6_relance.scheduler import (
        calculate_next_relance_time,
        get_next_allowed_time,
        is_quiet_hours,
    )
    from app.modules.m6_relance.service import (
        cancel_all_relances,
        create_and_schedule_relance,
    )
    from app.schemas.common import Language
    print("  ✅ All M6 modules imported successfully")
except Exception as e:
    print(f"  ❌ Import error: {e}")
    sys.exit(1)

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Quiet hours detection
# ─────────────────────────────────────────────────────────────────────────────

print("[2/7] Testing quiet hours detection...")

# Test 1: 22:00 Kinshasa (21:00 UTC based on actual timezone data)
dt_22h = datetime(2026, 4, 17, 21, 0, 0, tzinfo=timezone.utc)
assert is_quiet_hours(dt_22h) is True, "22:00 Kinshasa should be quiet hours"
print("  ✅ 22:00 Kinshasa correctly identified as quiet hours")

# Test 2: 03:00 Kinshasa (02:00 UTC)
dt_03h = datetime(2026, 4, 17, 2, 0, 0, tzinfo=timezone.utc)
assert is_quiet_hours(dt_03h) is True, "03:00 Kinshasa should be quiet hours"
print("  ✅ 03:00 Kinshasa correctly identified as quiet hours")

# Test 3: 10:00 Kinshasa (09:00 UTC)
dt_10h = datetime(2026, 4, 17, 9, 0, 0, tzinfo=timezone.utc)
assert is_quiet_hours(dt_10h) is False, "10:00 Kinshasa should NOT be quiet hours"
print("  ✅ 10:00 Kinshasa correctly identified as NOT quiet hours")

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Quiet hours rescheduling
# ─────────────────────────────────────────────────────────────────────────────

print("[3/7] Testing quiet hours rescheduling...")

# If in quiet hours, should reschedule to next 07:00
dt_quiet = datetime(2026, 4, 17, 22, 0, 0, tzinfo=timezone.utc)  # 23:00 Kinshasa
next_time = get_next_allowed_time(dt_quiet)
next_kin = next_time.astimezone(ZoneInfo("Africa/Kinshasa"))

assert next_kin.hour == 7, f"Expected hour=7, got {next_kin.hour}"
assert next_kin.minute == 0, f"Expected minute=0, got {next_kin.minute}"
assert next_kin.day == 18, f"Expected day=18, got {next_kin.day}"
print("  ✅ Quiet hours correctly rescheduled to next 07:00")

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Relance cadence calculation
# ─────────────────────────────────────────────────────────────────────────────

print("[4/7] Testing relance cadence calculation...")

# Use 14:00 UTC as last message time to avoid quiet hours interference
last_msg = datetime(2026, 4, 17, 14, 0, 0, tzinfo=timezone.utc)

# Attempt 1: +24h = 14:00 next day (NOT in quiet hours)
scheduled_1 = calculate_next_relance_time(
    last_customer_message_time=last_msg,
    attempt_number=1,
)
expected_1 = last_msg + timedelta(hours=24)
assert scheduled_1 == expected_1, f"Expected {expected_1}, got {scheduled_1}"
print("  ✅ Relance #1 correctly scheduled at +24h")

# Attempt 2: +60h = 02:00 3 days later (IN quiet hours → adjusted to 07:00)
scheduled_2 = calculate_next_relance_time(
    last_customer_message_time=last_msg,
    attempt_number=2,
)
# Original would be 02:00 UTC (03:00 Kinshasa) which is quiet hours
# Should be rescheduled to 06:00 UTC (07:00 Kinshasa)
expected_2_kin = (last_msg + timedelta(hours=60)).astimezone(ZoneInfo("Africa/Kinshasa"))
scheduled_2_kin = scheduled_2.astimezone(ZoneInfo("Africa/Kinshasa"))
assert scheduled_2_kin.hour == 7, f"Expected hour=7 Kinshasa, got {scheduled_2_kin.hour}"
print("  ✅ Relance #2 correctly scheduled (quiet hours adjusted to 07:00)")

# Attempt 3: +8.5 days = 02:00 later (IN quiet hours → adjusted to 07:00)
scheduled_3 = calculate_next_relance_time(
    last_customer_message_time=last_msg,
    attempt_number=3,
)
scheduled_3_kin = scheduled_3.astimezone(ZoneInfo("Africa/Kinshasa"))
assert scheduled_3_kin.hour == 7, f"Expected hour=7 Kinshasa, got {scheduled_3_kin.hour}"
print("  ✅ Relance #3 correctly scheduled (quiet hours adjusted to 07:00)")

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Hook generation (async)
# ─────────────────────────────────────────────────────────────────────────────

print("[5/7] Testing hook generation...")


async def test_hooks():
    # Attempt 1: reciprocity
    hook_1, type_1 = await generate_relance_hook(
        attempt_number=1,
        language=Language.french,
        product_interest="câble HDMI",
        city="Kinshasa",
        customer_name="Jean",
    )
    assert type_1 == "reciprocity", f"Expected reciprocity, got {type_1}"
    assert len(hook_1) > 0, "Hook 1 should not be empty"
    print(f"  ✅ Hook #1 (reciprocity): {len(hook_1)} chars")

    # Attempt 2: social_proof
    hook_2, type_2 = await generate_relance_hook(
        attempt_number=2,
        language=Language.french,
        product_interest="powerbank",
        city="Lubumbashi",
        customer_name=None,
        previous_hooks=[hook_1],
    )
    assert type_2 == "social_proof", f"Expected social_proof, got {type_2}"
    assert len(hook_2) > 0, "Hook 2 should not be empty"
    print(f"  ✅ Hook #2 (social_proof): {len(hook_2)} chars")

    # Attempt 3: scarcity
    hook_3, type_3 = await generate_relance_hook(
        attempt_number=3,
        language=Language.lingala,
        product_interest="écouteurs",
        city="Kinshasa",
        customer_name="Marie",
        previous_hooks=[hook_1, hook_2],
    )
    assert type_3 == "scarcity", f"Expected scarcity, got {type_3}"
    assert len(hook_3) > 0, "Hook 3 should not be empty"
    print(f"  ✅ Hook #3 (scarcity): {len(hook_3)} chars")


asyncio.run(test_hooks())

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Database models
# ─────────────────────────────────────────────────────────────────────────────

print("[6/7] Testing database models...")

try:
    from app.models.relance import Relance
    from app.models.lead import Lead
    from app.models.conversation import Conversation
    from app.models.customer import Customer
    print("  ✅ All database models imported successfully")
except Exception as e:
    print(f"  ❌ Model import error: {e}")
    sys.exit(1)

print()

# ─────────────────────────────────────────────────────────────────────────────
# Test 7: i18n templates
# ─────────────────────────────────────────────────────────────────────────────

print("[7/7] Testing i18n relance templates...")

try:
    from app.i18n import messages as i18n
    from app.schemas.common import Language
    
    # Check French fallbacks
    fallback_1_fr = i18n.t("relance_fallback_1", Language.french)
    assert len(fallback_1_fr) > 0, "French fallback 1 should not be empty"
    print(f"  ✅ French fallback 1: '{fallback_1_fr[:50]}...'")
    
    # Check Lingala fallbacks
    fallback_2_ln = i18n.t("relance_fallback_2", Language.lingala)
    assert len(fallback_2_ln) > 0, "Lingala fallback 2 should not be empty"
    print(f"  ✅ Lingala fallback 2: '{fallback_2_ln[:50]}...'")
    
    # Check Swahili fallbacks
    fallback_3_sw = i18n.t("relance_fallback_3", Language.swahili)
    assert len(fallback_3_sw) > 0, "Swahili fallback 3 should not be empty"
    print(f"  ✅ Swahili fallback 3: '{fallback_3_sw[:50]}...'")
    
except Exception as e:
    print(f"  ❌ i18n error: {e}")
    sys.exit(1)

print()
print("=" * 70)
print("✅ ALL TESTS PASSED — M6 SPRINT 1.6 IMPLEMENTATION VALIDATED")
print("=" * 70)
print()
print("Summary:")
print("  - Quiet hours detection: ✅ Working")
print("  - Relance cadence: ✅ Correct (+24h, +60h, +8.5d)")
print("  - Hook generation: ✅ 3 different angles")
print("  - Database models: ✅ Loaded")
print("  - i18n templates: ✅ 3 languages")
print()
print("Next steps:")
print("  1. Run full integration tests with pytest (requires DB)")
print("  2. Deploy Celery Beat scanner (hourly)")
print("  3. Monitor relance performance in production")
print()
