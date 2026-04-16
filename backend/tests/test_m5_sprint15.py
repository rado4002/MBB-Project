"""
tests/test_m5_sprint15.py — Unit tests for M5 Sprint 1.5 (Lead Qualification & Nurturing)

Tests:
  1. Weighted scoring (Phase 1.B spec: 0-100 scale with signal weights)
  2. Stage progression (AWARENESS → CONSIDERATION → DECISION)
  3. Qualification flow (2-3 smart questions)
  4. Information extraction (city, product, urgency, budget)
  5. Nurturing response generation (hot/warm/cold)
"""
import sys
sys.path.insert(0, '.')

from app.modules.m5_qualification.scorer import score_lead
from app.modules.m5_qualification.stages import (
    suggest_stage_from_score,
    can_transition,
)
from app.modules.m5_qualification.service import (
    _extract_city,
    _extract_products,
    _extract_urgency,
    _extract_budget,
)


# ── Weighted Scoring Tests (Phase 1.B) ──────────────────────────────────────


def test_scorer_hot_lead():
    """HOT lead: product+price+city+fast response → score ≥ 70"""
    text = "je veux acheter câble HDMI 2m à Kinshasa combien ça coûte"
    score, label, intent = score_lead(text, msg_count=5, response_speed_s=30)
    assert score >= 70, f"Expected hot (≥70), got {score}"
    assert label == "hot"
    assert "product_specific" in intent
    assert "city_mentioned" in intent
    assert "price_inquiry" in intent


def test_scorer_warm_lead():
    """WARM lead: product+city, medium engagement → score 40-69"""
    text = "je cherche un chargeur pour téléphone à Kinshasa"
    score, label, intent = score_lead(text, msg_count=3, response_speed_s=300)
    assert 40 <= score < 70, f"Expected warm (40-69), got {score}"
    assert label == "warm"


def test_scorer_cold_lead():
    """COLD lead: vague intent, no specifics → score < 40"""
    text = "salut, quoi de neuf"
    score, label, intent = score_lead(text, msg_count=1, response_speed_s=None)
    assert score < 40, f"Expected cold (<40), got {score}"
    assert label == "cold"


def test_scorer_response_speed_weight():
    """Response speed contributes 25% (max 25 points)"""
    text = "câble"  # Basic product mention
    # Fast response (< 60s) → +25 points
    fast_score, _, _ = score_lead(text, msg_count=1, response_speed_s=30)
    # Slow response (> 1h) → +0 points
    slow_score, _, _ = score_lead(text, msg_count=1, response_speed_s=4000)
    assert fast_score - slow_score == 25, "Fast response should add 25 points"


def test_scorer_engagement_depth():
    """Engagement depth contributes 10% (max 10 points)"""
    text = "câble"
    low_score, _, _ = score_lead(text, msg_count=1)
    high_score, _, _ = score_lead(text, msg_count=10)
    assert high_score - low_score == 10, "10+ messages should add 10 points"


def test_scorer_capped_at_100():
    """Score cannot exceed 100"""
    text = "je veux acheter câble HDMI USB type-c chargeur à Kinshasa combien prix"
    score, _, _ = score_lead(text, msg_count=20, response_speed_s=10)
    assert score <= 100, f"Score should be capped at 100, got {score}"


# ── Stage Progression Tests ─────────────────────────────────────────────────


def test_stage_from_score_hot():
    """Score 70+ → DECISION stage"""
    assert suggest_stage_from_score(75) == "decision"
    assert suggest_stage_from_score(100) == "decision"


def test_stage_from_score_warm():
    """Score 40-69 → CONSIDERATION stage"""
    assert suggest_stage_from_score(50) == "consideration"
    assert suggest_stage_from_score(40) == "consideration"
    assert suggest_stage_from_score(69) == "consideration"


def test_stage_from_score_cold():
    """Score < 40 → AWARENESS stage"""
    assert suggest_stage_from_score(30) == "awareness"
    assert suggest_stage_from_score(0) == "awareness"


def test_stage_transitions_valid():
    """Valid stage transitions"""
    assert can_transition("awareness", "consideration") is True
    assert can_transition("consideration", "decision") is True
    assert can_transition("decision", "consideration") is True  # Can regress


def test_stage_transitions_invalid():
    """Invalid stage transitions"""
    assert can_transition("decision", "awareness") is False  # Can't skip back


# ── Information Extraction Tests ────────────────────────────────────────────


def test_extract_city_kinshasa():
    """Detect Kinshasa (full name and 'Kin')"""
    assert _extract_city("je suis à kinshasa") == "Kinshasa"
    assert _extract_city("ozali na kin") == "Kinshasa"
    assert _extract_city("je suis à Lubumbashi") == "Lubumbashi"


def test_extract_products_multiple():
    """Extract multiple product keywords"""
    products = _extract_products("je veux câble hdmi et chargeur usb")
    assert "câble" in products or "cable" in products
    assert "hdmi" in products
    assert "chargeur" in products or "charger" in products
    assert "usb" in products


def test_extract_urgency_urgent():
    """Detect urgent keywords (today, now, fast)"""
    assert _extract_urgency("je veux ça aujourd'hui") == "urgent"
    assert _extract_urgency("nalingi yango lelo") == "urgent"
    assert _extract_urgency("ninahitaji leo") == "urgent"


def test_extract_urgency_flexible():
    """Detect flexible keywords (tomorrow, week, month)"""
    assert _extract_urgency("demain c'est bon") == "flexible"
    assert _extract_urgency("semaine prochaine") == "flexible"


def test_extract_budget_range():
    """Extract and categorize budget from text"""
    assert _extract_budget("mon budget est 3000 CDF") == "0-5000"
    assert _extract_budget("j'ai 10000 francs") == "5000-20000"
    assert _extract_budget("jusqu'à 30000 CDF") == "20000-50000"
    assert _extract_budget("60000 fc") == "50000+"


def test_extract_no_budget():
    """Return None if no budget mentioned"""
    assert _extract_budget("je veux un câble") is None


# ── Integration Tests ────────────────────────────────────────────────────────


def test_full_qualification_flow():
    """Simulate full qualification: Q1 → Q2 → Q3 → lead creation"""
    # Q1 Answer: Product interest
    products = _extract_products("je cherche un câble hdmi")
    assert products
    assert any("hdmi" in p or "câble" in p or "cable" in p for p in products)

    # Q2 Answer: City
    city = _extract_city("je suis à kinshasa")
    assert city == "Kinshasa"

    # Q3 Answer: Urgency
    urgency = _extract_urgency("j'en ai besoin aujourd'hui")
    assert urgency == "urgent"

    # Final scoring
    full_text = "je veux acheter câble hdmi à kinshasa maintenant combien"
    score, label, intent = score_lead(full_text, msg_count=5, response_speed_s=45)
    assert score >= 70, "Full qualification should yield hot lead"
    assert label == "hot"


if __name__ == "__main__":
    # Run all tests
    tests = [
        test_scorer_hot_lead,
        test_scorer_warm_lead,
        test_scorer_cold_lead,
        test_scorer_response_speed_weight,
        test_scorer_engagement_depth,
        test_scorer_capped_at_100,
        test_stage_from_score_hot,
        test_stage_from_score_warm,
        test_stage_from_score_cold,
        test_stage_transitions_valid,
        test_stage_transitions_invalid,
        test_extract_city_kinshasa,
        test_extract_products_multiple,
        test_extract_urgency_urgent,
        test_extract_urgency_flexible,
        test_extract_budget_range,
        test_extract_no_budget,
        test_full_qualification_flow,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            print(f"✓ {test.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"✗ {test.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ {test.__name__}: ERROR - {e}")
            failed += 1

    print(f"\n{passed}/{len(tests)} tests passed")
    if failed > 0:
        sys.exit(1)

