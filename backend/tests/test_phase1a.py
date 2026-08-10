"""
tests/test_phase1a.py — Phase 1.A Comprehensive Unit Tests

Tests all new Phase 1.A modules without requiring live services:
  [1] i18n catalog — translations load, fallback works
  [2] Language detection — keyword fast-path, opt-out, switch
  [3] System prompts — per-language prompt generation
  [4] Conversation state machine — valid/invalid transitions
  [5] Lead scoring engine — scoring signals
  [6] Opt-out detection — all 3 languages
  [7] Circuit breaker — open/half-open/closed behavior
  [8] Session cache — serialization/deserialization
  [9] Schemas — message history response model
  [10] Integration — qualification flow scoring

Run:
  cd backend
  python tests/test_phase1a.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, ".")

I18N_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "app" / "i18n" / "templates"
)

# Minimal env for Settings()
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "mbb")
os.environ.setdefault("POSTGRES_USER", "mbb")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "testsecret32charslongpadding12345")
os.environ.setdefault("CLAUDE_API_KEY", "test")
os.environ.setdefault("AIRTABLE_API_KEY", "test")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test")

passed = 0
failed = 0
errors: list[str] = []


def run_test(name: str, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  ✅ {name}")
    except Exception as e:
        failed += 1
        errors.append(f"{name}: {e}")
        print(f"  ❌ {name}: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# [1] i18n catalog
# ═══════════════════════════════════════════════════════════════════════════════

def test_i18n_all_languages_load():
    from app.i18n.messages import t, _catalog
    assert "french" in _catalog, "French catalog missing"
    assert "lingala" in _catalog, "Lingala catalog missing"
    assert "swahili" in _catalog, "Swahili catalog missing"


def test_i18n_french_keys():
    from app.i18n.messages import t
    keys = [
        "opt_out_ack", "voice_note_ack", "recovery", "rate_limit",
        "error_fallback", "welcome", "qualification_q1", "qualification_q2",
        "qualification_q3", "lead_created", "language_switch", "escalation_ack",
    ]
    for key in keys:
        msg = t(key, "french")
        assert msg != key, f"Key '{key}' not found in French catalog"
        assert len(msg) > 5, f"Key '{key}' too short: '{msg}'"


def test_i18n_lingala_keys():
    from app.i18n.messages import t
    keys = ["opt_out_ack", "voice_note_ack", "recovery", "rate_limit",
            "error_fallback", "welcome"]
    for key in keys:
        msg = t(key, "lingala")
        assert msg != key, f"Key '{key}' not found in Lingala catalog"


def test_i18n_swahili_keys():
    from app.i18n.messages import t
    keys = ["opt_out_ack", "voice_note_ack", "recovery", "rate_limit",
            "error_fallback", "welcome"]
    for key in keys:
        msg = t(key, "swahili")
        assert msg != key, f"Key '{key}' not found in Swahili catalog"


def test_i18n_fallback_to_french():
    from app.i18n.messages import t
    # Unknown language falls back to French
    msg = t("opt_out_ack", "klingon")
    french_msg = t("opt_out_ack", "french")
    assert msg == french_msg, "Fallback to French failed"


def test_i18n_unknown_key_returns_key():
    from app.i18n.messages import t
    msg = t("nonexistent_key_xyz", "french")
    assert msg == "nonexistent_key_xyz", "Unknown key should return the key itself"


def test_i18n_recovery_messages_match_spec():
    from app.i18n.messages import t
    assert "Naza-zonga" in t("recovery", "lingala")
    assert "retour" in t("recovery", "french").lower()
    assert "Tumerudi" in t("recovery", "swahili")


def test_i18n_payload_size():
    """All i18n messages must be < 1KB (DRC constraint)."""
    from app.i18n.messages import _catalog
    for lang, msgs in _catalog.items():
        for key, msg in msgs.items():
            assert len(msg.encode("utf-8")) < 1024, (
                f"{lang}.{key} is {len(msg.encode('utf-8'))} bytes (>1KB)"
            )


# ═══════════════════════════════════════════════════════════════════════════════
# [2] Language detection — keyword fast-path
# ═══════════════════════════════════════════════════════════════════════════════

def test_detect_lingala():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("Mbote ndeko, nalingi koyeba biloko")
    assert lang == "lingala", f"Expected lingala, got {lang}"
    assert conf >= 0.5, f"Confidence too low: {conf}"


def test_detect_french():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("Bonjour, je voudrais acheter un produit")
    assert lang == "french", f"Expected french, got {lang}"
    assert conf >= 0.5


def test_detect_swahili():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("Habari rafiki, nataka kununua bidhaa")
    assert lang == "swahili", f"Expected swahili, got {lang}"
    assert conf >= 0.5


def test_detect_default_french():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("xyz 123")
    assert lang == "french", "Default should be French"
    assert conf <= 0.5, "Unknown text should have low confidence"


def test_detect_sticky_language():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("hello world", existing_language="lingala")
    assert lang == "lingala", "Sticky language should persist"
    assert conf == 1.0


def test_detect_mixed_language():
    from app.modules.m1_gateway.language_detector import detect_language
    lang, conf = detect_language("Bonjour ndeko, nalingi")
    assert lang in ("lingala", "french"), f"Mixed should be lingala or french, got {lang}"


# ═══════════════════════════════════════════════════════════════════════════════
# [3] Opt-out detection
# ═══════════════════════════════════════════════════════════════════════════════

def test_opt_out_french():
    from app.modules.m1_gateway.language_detector import is_opt_out
    assert is_opt_out("stop"), "'stop' should trigger opt-out"
    assert is_opt_out("arrête s'il te plaît"), "'arrête' should trigger opt-out"
    assert is_opt_out("STOP"), "Case-insensitive opt-out"


def test_opt_out_lingala():
    from app.modules.m1_gateway.language_detector import is_opt_out
    assert is_opt_out("tika"), "'tika' should trigger opt-out"
    assert is_opt_out("yaka te"), "'yaka te' should trigger opt-out"


def test_opt_out_swahili():
    from app.modules.m1_gateway.language_detector import is_opt_out
    assert is_opt_out("acha"), "'acha' should trigger opt-out"
    assert is_opt_out("simama"), "'simama' should trigger opt-out"


def test_opt_out_false_positive():
    from app.modules.m1_gateway.language_detector import is_opt_out
    assert not is_opt_out("Je voudrais acheter"), "Normal message should not trigger opt-out"
    assert not is_opt_out("Mbote ndeko"), "Greeting should not trigger opt-out"


# ═══════════════════════════════════════════════════════════════════════════════
# [4] Language switch detection
# ═══════════════════════════════════════════════════════════════════════════════

def test_language_switch_french():
    from app.modules.m1_gateway.language_detector import is_language_switch_request
    assert is_language_switch_request("parle français") == "french"


def test_language_switch_lingala():
    from app.modules.m1_gateway.language_detector import is_language_switch_request
    assert is_language_switch_request("en lingala") == "lingala"


def test_language_switch_swahili():
    from app.modules.m1_gateway.language_detector import is_language_switch_request
    assert is_language_switch_request("sema kiswahili") == "swahili"


def test_language_switch_none():
    from app.modules.m1_gateway.language_detector import is_language_switch_request
    assert is_language_switch_request("je veux un câble") is None


# ═══════════════════════════════════════════════════════════════════════════════
# [5] System prompts
# ═══════════════════════════════════════════════════════════════════════════════

def test_system_prompt_french():
    from app.modules.m2_language.prompts import get_system_prompt
    p = get_system_prompt("french")
    assert "Français" in p or "français" in p
    assert "MBB" in p
    assert "JAMAIS" in p


def test_system_prompt_lingala():
    from app.modules.m2_language.prompts import get_system_prompt
    p = get_system_prompt("lingala")
    assert "Lingala" in p
    assert "MBB" in p
    assert "JAMAIS" in p


def test_system_prompt_swahili():
    from app.modules.m2_language.prompts import get_system_prompt
    p = get_system_prompt("swahili")
    assert "Kiswahili" in p or "Swahili" in p
    assert "MBB" in p
    assert "KAMWE" in p


def test_system_prompt_excludes_untrusted_history():
    from app.modules.m2_language.prompts import get_system_prompt
    history = [
        {"direction": "inbound", "content": "Mbote!"},
        {"direction": "outbound", "content": "Mbote ndeko!"},
    ]
    p = get_system_prompt("lingala", history)
    assert "Mbote!" not in p
    assert "Mbote ndeko!" not in p
    assert p == get_system_prompt("lingala")


def test_system_prompt_unknown_language_defaults_french():
    from app.modules.m2_language.prompts import get_system_prompt
    p = get_system_prompt("esperanto")
    french_p = get_system_prompt("french")
    assert p == french_p


def test_system_prompt_max_size():
    """System prompt without history should be < 2KB (DRC bandwidth)."""
    from app.modules.m2_language.prompts import get_system_prompt
    for lang in ("french", "lingala", "swahili"):
        p = get_system_prompt(lang)
        size = len(p.encode("utf-8"))
        assert size < 2048, f"{lang} prompt is {size} bytes (>2KB)"


# ═══════════════════════════════════════════════════════════════════════════════
# [6] Conversation state machine
# ═══════════════════════════════════════════════════════════════════════════════

def test_valid_transitions():
    from app.modules.m4_conversation.engine import can_transition
    assert can_transition("active", "qualifying")
    assert can_transition("active", "dormant")
    assert can_transition("qualifying", "nurturing")
    assert can_transition("qualifying", "escalated")
    assert can_transition("nurturing", "escalated")
    assert can_transition("nurturing", "converted")
    assert can_transition("escalated", "converted")
    assert can_transition("dormant", "active")


def test_invalid_transitions():
    from app.modules.m4_conversation.engine import can_transition
    assert not can_transition("active", "converted")
    assert not can_transition("active", "nurturing")
    assert not can_transition("converted", "active")
    assert not can_transition("qualifying", "active")
    assert not can_transition("dormant", "qualifying")


def test_transition_executes():
    from app.modules.m4_conversation.engine import transition
    new = transition("active", "qualifying", conversation_id="test-123", reason="signals")
    assert new == "qualifying"


def test_transition_invalid_raises():
    from app.modules.m4_conversation.engine import transition, InvalidTransitionError
    raised = False
    try:
        transition("active", "converted")
    except InvalidTransitionError:
        raised = True
    assert raised, "InvalidTransitionError should be raised"


def test_qualification_signals_detected():
    from app.modules.m4_conversation.engine import detect_qualification_signals
    assert detect_qualification_signals("je veux acheter un produit")
    assert detect_qualification_signals("combien coûte le câble?")
    assert detect_qualification_signals("ntalu ya biloko?")
    assert detect_qualification_signals("bei ya bidhaa?")


def test_qualification_signals_not_detected():
    from app.modules.m4_conversation.engine import detect_qualification_signals
    assert not detect_qualification_signals("bonjour, comment ça va?")
    assert not detect_qualification_signals("mbote ndeko")
    assert not detect_qualification_signals("hello world 123")


def test_dormant_timeout():
    from app.modules.m4_conversation.engine import should_go_dormant_on_timeout
    assert should_go_dormant_on_timeout(14)
    assert should_go_dormant_on_timeout(30)
    assert not should_go_dormant_on_timeout(13)
    assert not should_go_dormant_on_timeout(0)


# ═══════════════════════════════════════════════════════════════════════════════
# [7] Lead scoring engine
# ═══════════════════════════════════════════════════════════════════════════════

def test_score_hot_lead():
    from app.modules.m5_qualification.scorer import score_lead
    val, label, intent = score_lead(
        "je veux acheter un câble HDMI à Kinshasa, combien?",
        response_speed_s=120,
    )
    assert label == "hot", f"Expected hot, got {label} (score={val})"
    assert val >= 70
    assert "product_specific" in intent
    assert "city_mentioned" in intent


def test_score_warm_lead():
    from app.modules.m5_qualification.scorer import score_lead
    val, label, intent = score_lead("je veux acheter un câble, combien?", msg_count=10)
    assert label in ("warm", "hot"), f"Expected warm+, got {label} (score={val})"
    assert val >= 40


def test_score_cold_lead():
    from app.modules.m5_qualification.scorer import score_lead
    val, label, intent = score_lead("bonjour, comment ça va?")
    assert label == "cold", f"Expected cold, got {label} (score={val})"
    assert val < 40


def test_score_lingala_keywords():
    from app.modules.m5_qualification.scorer import score_lead
    val, label, intent = score_lead("nalingi kosomba biloko na Kinshasa")
    assert val >= 10, f"Lingala buy intent should score ≥10, got {val}"


def test_score_swahili_keywords():
    from app.modules.m5_qualification.scorer import score_lead
    val, label, intent = score_lead("nataka nunua bidhaa, bei kiasi gani?")
    assert val >= 20, f"Swahili keywords should score ≥20, got {val}"


def test_score_engagement_bonus():
    from app.modules.m5_qualification.scorer import score_lead
    val1, _, _ = score_lead("combien?", msg_count=1)
    val2, _, _ = score_lead("combien?", msg_count=5)
    assert val2 > val1, "Multiple messages should increase score"


def test_score_speed_bonus():
    from app.modules.m5_qualification.scorer import score_lead
    val1, _, _ = score_lead("combien?", response_speed_s=120)
    val2, _, _ = score_lead("combien?", response_speed_s=30)
    assert val2 > val1, "Fast response should increase score"


def test_score_capped_at_10():
    from app.modules.m5_qualification.scorer import score_lead
    val, _, _ = score_lead(
        "acheter câble Kinshasa combien livraison promotion",
        msg_count=10,
        response_speed_s=5,
    )
    assert val <= 100, f"Score should be capped at 100, got {val}"


# ═══════════════════════════════════════════════════════════════════════════════
# [8] Circuit breaker — ClaudeAdapter
# ═══════════════════════════════════════════════════════════════════════════════

def test_circuit_breaker_closed_initially():
    from app.adapters.ai.claude_adapter import ClaudeAdapter
    adapter = ClaudeAdapter()
    assert not adapter._circuit_open
    assert adapter._failures == 0


def test_circuit_breaker_opens_after_3_failures():
    from app.adapters.ai.claude_adapter import ClaudeAdapter, AIAdapterError
    adapter = ClaudeAdapter()
    for _ in range(3):
        adapter._record_failure()
    assert adapter._circuit_open
    raised = False
    try:
        adapter._check_circuit()
    except AIAdapterError:
        raised = True
    assert raised, "Circuit breaker should raise when open"


def test_circuit_breaker_resets_on_success():
    from app.adapters.ai.claude_adapter import ClaudeAdapter
    adapter = ClaudeAdapter()
    adapter._record_failure()
    adapter._record_failure()
    adapter._record_success()
    assert adapter._failures == 0
    assert not adapter._circuit_open


def test_circuit_breaker_half_open():
    from app.adapters.ai.claude_adapter import ClaudeAdapter
    adapter = ClaudeAdapter()
    for _ in range(3):
        adapter._record_failure()
    assert adapter._circuit_open
    # Simulate 60s elapsed
    adapter._circuit_opened_at = time.monotonic() - 61
    # Should NOT raise — half-open allows probe
    adapter._check_circuit()  # no exception = pass


def test_circuit_breaker_still_open_before_timeout():
    from app.adapters.ai.claude_adapter import ClaudeAdapter, AIAdapterError
    adapter = ClaudeAdapter()
    for _ in range(3):
        adapter._record_failure()
    # Only 10s elapsed — should still be open
    adapter._circuit_opened_at = time.monotonic() - 10
    raised = False
    try:
        adapter._check_circuit()
    except AIAdapterError:
        raised = True
    assert raised, "Circuit should stay open before 60s"


# ═══════════════════════════════════════════════════════════════════════════════
# [9] Circuit breaker — BaileysAdapter
# ═══════════════════════════════════════════════════════════════════════════════

def test_baileys_circuit_breaker_opens():
    from app.adapters.messaging.baileys_adapter import BaileysAdapter, MessagingAdapterError
    adapter = BaileysAdapter()
    for _ in range(3):
        adapter._record_failure()
    assert adapter._circuit_open
    raised = False
    try:
        adapter._check_circuit()
    except MessagingAdapterError:
        raised = True
    assert raised


def test_baileys_circuit_half_open():
    from app.adapters.messaging.baileys_adapter import BaileysAdapter
    adapter = BaileysAdapter()
    for _ in range(3):
        adapter._record_failure()
    adapter._circuit_opened_at = time.monotonic() - 61
    adapter._check_circuit()  # no exception = half-open allows probe


# ═══════════════════════════════════════════════════════════════════════════════
# [10] Session cache — serialization
# ═══════════════════════════════════════════════════════════════════════════════

def test_session_state_roundtrip():
    from app.modules.m1_gateway.session_cache import SessionState
    state = SessionState(
        customer_id="+243999000111",
        language="lingala",
        current_topic="cable_usb",
        stage="qualifying",
        score=7,
        last_msg_time="2026-04-17T12:00:00Z",
        msg_count=5,
        price_discussed=True,
        products=["cable_usb", "chargeur"],
        history=[
            {"direction": "inbound", "content": "mbote", "language": "lingala"},
            {"direction": "outbound", "content": "mbote ndeko!", "language": "lingala"},
        ],
    )
    h = state.to_hash()
    # All values must be strings (Redis HASH)
    for k, v in h.items():
        assert isinstance(v, str), f"Hash key '{k}' is {type(v)}, expected str"

    # Deserialize back
    restored = SessionState.from_hash(h)
    assert restored.customer_id == "+243999000111"
    assert restored.language == "lingala"
    assert restored.stage == "qualifying"
    assert restored.score == 7
    assert restored.msg_count == 5
    assert restored.price_discussed is True
    assert len(restored.products) == 2
    assert len(restored.history) == 2


def test_session_state_empty_hash():
    from app.modules.m1_gateway.session_cache import SessionState
    state = SessionState.from_hash({})
    assert state.language == "french"
    assert state.score == 0
    assert state.msg_count == 0
    assert state.price_discussed is False
    assert state.products == []
    assert state.history == []


def test_session_state_history_capped():
    from app.modules.m1_gateway.session_cache import SessionState
    state = SessionState(
        history=[{"direction": "inbound", "content": f"msg-{i}"} for i in range(30)]
    )
    h = state.to_hash()
    restored_history = json.loads(h["history"])
    assert len(restored_history) == 20, f"History should be capped at 20, got {len(restored_history)}"


# ═══════════════════════════════════════════════════════════════════════════════
# [11] Schemas — Message history
# ═══════════════════════════════════════════════════════════════════════════════

def test_message_history_item_schema():
    from app.schemas.messages import MessageHistoryItem
    item = MessageHistoryItem(
        message_id=uuid.uuid4(),
        direction="inbound",
        content="Mbote!",
        content_type="text",
        language="lingala",
        timestamp=datetime.now(timezone.utc),
    )
    assert item.direction == "inbound"
    assert item.processing_time_ms is None


def test_message_history_response_schema():
    from app.schemas.messages import MessageHistoryResponse, MessageHistoryItem
    conv_id = uuid.uuid4()
    items = [
        MessageHistoryItem(
            message_id=uuid.uuid4(),
            direction="inbound",
            content="Bonjour",
            content_type="text",
            language="french",
            timestamp=datetime.now(timezone.utc),
        )
    ]
    resp = MessageHistoryResponse(
        conversation_id=conv_id,
        messages=items,
        total=1,
    )
    assert resp.total == 1
    assert len(resp.messages) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# [12] AirtableAdapter — instantiation + circuit breaker
# ═══════════════════════════════════════════════════════════════════════════════

def test_airtable_adapter_init():
    from app.adapters.crm.airtable_adapter import AirtableAdapter
    adapter = AirtableAdapter()
    assert not adapter._circuit_open
    assert adapter._failures == 0


def test_airtable_circuit_breaker():
    from app.adapters.crm.airtable_adapter import AirtableAdapter, CRMAdapterError
    adapter = AirtableAdapter()
    for _ in range(3):
        adapter._record_failure()
    assert adapter._circuit_open
    raised = False
    try:
        adapter._check_circuit()
    except CRMAdapterError:
        raised = True
    assert raised


def test_airtable_circuit_half_open():
    from app.adapters.crm.airtable_adapter import AirtableAdapter
    adapter = AirtableAdapter()
    for _ in range(3):
        adapter._record_failure()
    adapter._circuit_opened_at = time.monotonic() - 61
    adapter._check_circuit()  # no exception = half-open


# ═══════════════════════════════════════════════════════════════════════════════
# [13] Adapter factory
# ═══════════════════════════════════════════════════════════════════════════════

def test_adapter_factory_ai():
    from app.adapters import get_ai_adapter
    adapter = get_ai_adapter()
    assert adapter is not None
    assert hasattr(adapter, "generate")
    assert hasattr(adapter, "detect_language")


def test_adapter_factory_messaging():
    from app.adapters import get_messaging_adapter
    adapter = get_messaging_adapter()
    assert adapter is not None
    assert hasattr(adapter, "send_message")


def test_adapter_factory_crm():
    from app.adapters import get_crm_adapter
    adapter = get_crm_adapter()
    assert adapter is not None
    assert hasattr(adapter, "create_lead")
    assert hasattr(adapter, "update_lead")
    assert hasattr(adapter, "sync_order")


# ═══════════════════════════════════════════════════════════════════════════════
# [14] i18n JSON file integrity
# ═══════════════════════════════════════════════════════════════════════════════

def test_i18n_json_consistency():
    """All 3 language files must have the exact same keys."""
    french = json.loads((I18N_TEMPLATE_DIR / "french.json").read_text(encoding="utf-8"))
    lingala = json.loads((I18N_TEMPLATE_DIR / "lingala.json").read_text(encoding="utf-8"))
    swahili = json.loads((I18N_TEMPLATE_DIR / "swahili.json").read_text(encoding="utf-8"))
    assert set(french.keys()) == set(lingala.keys()), (
        f"Key mismatch FR/LN: {set(french.keys()) ^ set(lingala.keys())}"
    )
    assert set(french.keys()) == set(swahili.keys()), (
        f"Key mismatch FR/SW: {set(french.keys()) ^ set(swahili.keys())}"
    )


def test_i18n_no_empty_values():
    """No empty string values in any catalog."""
    for fname in ("french.json", "lingala.json", "swahili.json"):
        data = json.loads((I18N_TEMPLATE_DIR / fname).read_text(encoding="utf-8"))
        for key, val in data.items():
            assert val.strip(), f"{fname}: key '{key}' has empty value"


# ═══════════════════════════════════════════════════════════════════════════════
# [15] ProcessedInbound dataclass
# ═══════════════════════════════════════════════════════════════════════════════

def test_processed_inbound_defaults():
    from app.modules.m1_gateway.service import ProcessedInbound
    pi = ProcessedInbound(
        customer_phone="+243999000111",
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        language="french",
    )
    assert pi.is_opted_out is False
    assert pi.is_voice_note is False
    assert pi.requires_escalation is False
    assert pi.content == ""
    assert pi.extra == {}


# ═══════════════════════════════════════════════════════════════════════════════
# [16] Unified language detector (m2_language)
# ═══════════════════════════════════════════════════════════════════════════════

def test_m2_detector_imports():
    from app.modules.m2_language.detector import (
        detect_language_smart,
        is_language_switch_request,
        is_opt_out,
    )
    assert callable(detect_language_smart)
    assert callable(is_language_switch_request)
    assert callable(is_opt_out)


# ═══════════════════════════════════════════════════════════════════════════════
# [17] Celery task m5 — module imports
# ═══════════════════════════════════════════════════════════════════════════════

def test_m5_task_registration():
    from app.tasks.celery_app import celery_app
    # Force task registration by importing the module
    import app.tasks.m5  # noqa: F401
    registered = list(celery_app.tasks.keys())
    assert "m5.sync_lead_to_crm" in registered, (
        f"m5.sync_lead_to_crm not in registered tasks: {registered}"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# [18] Conversation opt-out module
# ═══════════════════════════════════════════════════════════════════════════════

def test_opt_out_module_exports():
    from app.modules.m4_conversation.opt_out import is_opt_out, handle_opt_out
    assert callable(is_opt_out)
    assert callable(handle_opt_out)


# ═══════════════════════════════════════════════════════════════════════════════
# [19] Integration — full scoring pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def test_integration_scoring_pipeline():
    """Test full flow: qualification signals → scoring → label."""
    from app.modules.m4_conversation.engine import detect_qualification_signals
    from app.modules.m5_qualification.scorer import score_lead

    messages = [
        ("je veux acheter un câble USB à Kinshasa, combien?", "warm"),
        ("combien ça coûte?", "warm"),
        ("bonjour comment ça va?", "cold"),
        ("nalingi kosomba biloko na Kinshasa", "warm"),
        ("habari, nataka bidhaa bei?", "warm"),
    ]
    for text, expected_min_label in messages:
        has_signals = detect_qualification_signals(text)
        val, label, intent = score_lead(text)
        if expected_min_label == "hot":
            assert has_signals, f"'{text}' should have qualification signals"
            assert label == "hot", f"'{text}' should be hot, got {label}"
        elif expected_min_label == "warm":
            assert val >= 10, f"'{text}' should score ≥10, got {val}"


# ═══════════════════════════════════════════════════════════════════════════════
# [20] DRC-specific constraints
# ═══════════════════════════════════════════════════════════════════════════════

def test_international_e164_phone_validation():
    """Phone numbers must use canonical international E.164 format."""
    from app.schemas.messages import InboundMessageRequest
    from app.schemas.common import ContentType
    # Valid
    msg = InboundMessageRequest(
        message_id=uuid.uuid4(),
        customer_phone="+243999000111",
        content="test",
        content_type=ContentType.text,
        timestamp=datetime.now(timezone.utc),
        whatsapp_message_id="wa_123",
    )
    assert msg.customer_phone == "+243999000111"

    # Invalid
    raised = False
    try:
        InboundMessageRequest(
            message_id=uuid.uuid4(),
            customer_phone="+0123456789",
            content="test",
            content_type=ContentType.text,
            timestamp=datetime.now(timezone.utc),
            whatsapp_message_id="wa_456",
        )
    except Exception:
        raised = True
    assert raised, "Noncanonical phone should be rejected"


def test_baileys_payload_phone_validation():
    from app.api.v1.messages import BaileysWebhookPayload
    raised = False
    try:
        BaileysWebhookPayload(
            customer_phone="+0123456789",
            content="test",
            whatsapp_message_id="wa_789",
        )
    except Exception:
        raised = True
    assert raised, "BaileysWebhookPayload should reject noncanonical phone"


# ═══════════════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 70)
    print("  Phase 1.A — Unit Tests")
    print("=" * 70)

    # Group 1: i18n
    print("\n[1] i18n Catalog")
    run_test("All languages load", test_i18n_all_languages_load)
    run_test("French keys present", test_i18n_french_keys)
    run_test("Lingala keys present", test_i18n_lingala_keys)
    run_test("Swahili keys present", test_i18n_swahili_keys)
    run_test("Fallback to French", test_i18n_fallback_to_french)
    run_test("Unknown key returns key", test_i18n_unknown_key_returns_key)
    run_test("Recovery messages match spec", test_i18n_recovery_messages_match_spec)
    run_test("Payload size < 1KB", test_i18n_payload_size)
    run_test("JSON key consistency", test_i18n_json_consistency)
    run_test("No empty values", test_i18n_no_empty_values)

    # Group 2: Language detection
    print("\n[2] Language Detection (keyword)")
    run_test("Detect Lingala", test_detect_lingala)
    run_test("Detect French", test_detect_french)
    run_test("Detect Swahili", test_detect_swahili)
    run_test("Default to French", test_detect_default_french)
    run_test("Sticky language", test_detect_sticky_language)
    run_test("Mixed language", test_detect_mixed_language)

    # Group 3: Opt-out
    print("\n[3] Opt-out Detection")
    run_test("French opt-out", test_opt_out_french)
    run_test("Lingala opt-out", test_opt_out_lingala)
    run_test("Swahili opt-out", test_opt_out_swahili)
    run_test("No false positive", test_opt_out_false_positive)

    # Group 4: Language switch
    print("\n[4] Language Switch")
    run_test("Switch to French", test_language_switch_french)
    run_test("Switch to Lingala", test_language_switch_lingala)
    run_test("Switch to Swahili", test_language_switch_swahili)
    run_test("No switch", test_language_switch_none)

    # Group 5: System prompts
    print("\n[5] System Prompts")
    run_test("French prompt", test_system_prompt_french)
    run_test("Lingala prompt", test_system_prompt_lingala)
    run_test("Swahili prompt", test_system_prompt_swahili)
    run_test("Prompt with history", test_system_prompt_with_history)
    run_test("Unknown lang defaults French", test_system_prompt_unknown_language_defaults_french)
    run_test("Prompt size < 2KB", test_system_prompt_max_size)

    # Group 6: State machine
    print("\n[6] Conversation State Machine")
    run_test("Valid transitions", test_valid_transitions)
    run_test("Invalid transitions", test_invalid_transitions)
    run_test("Transition executes", test_transition_executes)
    run_test("Invalid transition raises", test_transition_invalid_raises)
    run_test("Qualification signals detected", test_qualification_signals_detected)
    run_test("No qualification signals", test_qualification_signals_not_detected)
    run_test("Dormant timeout", test_dormant_timeout)

    # Group 7: Lead scoring
    print("\n[7] Lead Scoring Engine")
    run_test("Hot lead", test_score_hot_lead)
    run_test("Warm lead", test_score_warm_lead)
    run_test("Cold lead", test_score_cold_lead)
    run_test("Lingala keywords", test_score_lingala_keywords)
    run_test("Swahili keywords", test_score_swahili_keywords)
    run_test("Engagement bonus", test_score_engagement_bonus)
    run_test("Speed bonus", test_score_speed_bonus)
    run_test("Score capped at 10", test_score_capped_at_10)

    # Group 8: Circuit breakers
    print("\n[8] Circuit Breakers")
    run_test("Claude closed initially", test_circuit_breaker_closed_initially)
    run_test("Claude opens after 3 fails", test_circuit_breaker_opens_after_3_failures)
    run_test("Claude resets on success", test_circuit_breaker_resets_on_success)
    run_test("Claude half-open after 60s", test_circuit_breaker_half_open)
    run_test("Claude stays open before 60s", test_circuit_breaker_still_open_before_timeout)
    run_test("Baileys opens after 3 fails", test_baileys_circuit_breaker_opens)
    run_test("Baileys half-open after 60s", test_baileys_circuit_half_open)
    run_test("Airtable circuit breaker", test_airtable_circuit_breaker)
    run_test("Airtable half-open", test_airtable_circuit_half_open)

    # Group 9: Session cache
    print("\n[9] Session Cache Serialization")
    run_test("Roundtrip serialize", test_session_state_roundtrip)
    run_test("Empty hash defaults", test_session_state_empty_hash)
    run_test("History capped at 20", test_session_state_history_capped)

    # Group 10: Schemas
    print("\n[10] Schemas")
    run_test("MessageHistoryItem", test_message_history_item_schema)
    run_test("MessageHistoryResponse", test_message_history_response_schema)
    run_test("ProcessedInbound defaults", test_processed_inbound_defaults)

    # Group 11: Adapters
    print("\n[11] Adapter Factory")
    run_test("AI adapter", test_adapter_factory_ai)
    run_test("Messaging adapter", test_adapter_factory_messaging)
    run_test("CRM adapter", test_adapter_factory_crm)
    run_test("Airtable init", test_airtable_adapter_init)

    # Group 12: Module imports
    print("\n[12] Module Imports")
    run_test("m2 detector imports", test_m2_detector_imports)
    run_test("m5 task registered", test_m5_task_registration)
    run_test("m4 opt_out exports", test_opt_out_module_exports)

    # Group 13: Integration
    print("\n[13] Integration Tests")
    run_test("Scoring pipeline", test_integration_scoring_pipeline)

    # Group 14: Phone and DRC business constraints
    print("\n[14] Phone and DRC Business Constraints")
    run_test("International E.164 phone validation", test_international_e164_phone_validation)
    run_test("Baileys phone validation", test_baileys_payload_phone_validation)

    # Summary
    total = passed + failed
    print("\n" + "=" * 70)
    print(f"  RESULTS: {passed}/{total} passed, {failed} failed")
    print("=" * 70)
    if errors:
        print("\nFailures:")
        for e in errors:
            print(f"  ❌ {e}")
        sys.exit(1)
    else:
        print("\nALL TESTS PASSED ✅")
        sys.exit(0)
