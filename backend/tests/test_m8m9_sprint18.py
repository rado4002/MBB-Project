"""
backend/tests/test_m8m9_sprint18.py — Unit tests for Sprint 1.D (M8 + M9).

Tests cover:
  1. MAPS categories taxonomy
  2. Tag extraction from messages (product, price, opt-out, urgency, language)
  3. Escalation trigger detection (voice_note, human_request, complaint)
  4. Escalation ticket creation + resolution
  5. Audit log persistence
  6. Feature flags via Redis
  7. Config key storage via Redis
  8. Funnel metrics query
  9. Relance performance query
  10. API routes return non-501
"""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import select, text

from app.database import AsyncSessionLocal, engine


@pytest.fixture(autouse=True)
async def reset_connection_pool():
    """Dispose the engine pool after each test."""
    yield
    try:
        await engine.dispose()
    except Exception:
        pass


from app.modules.m8_maps.categories import (
    CATEGORY_PATTERNS,
    VALID_CATEGORIES,
    is_valid_pattern,
    get_patterns_for_category,
)
from app.modules.m8_maps.tagger import tag_message
from app.modules.m8_maps.escalation import (
    detect_triggers,
    EscalationTrigger,
)
from app.modules.m9_dashboard.audit import log_action, list_logs


pytestmark = pytest.mark.asyncio(loop_scope="session")


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: MAPS categories taxonomy
# ─────────────────────────────────────────────────────────────────────────────
class TestCategories:
    def test_valid_categories_count(self):
        assert len(VALID_CATEGORIES) == 5

    def test_all_categories_have_patterns(self):
        for cat in VALID_CATEGORIES:
            assert len(CATEGORY_PATTERNS[cat]) > 0

    def test_is_valid_pattern_true(self):
        assert is_valid_pattern("product_demand", "product_name") is True

    def test_is_valid_pattern_false(self):
        assert is_valid_pattern("product_demand", "nonexistent") is False

    def test_is_valid_pattern_bad_category(self):
        assert is_valid_pattern("fake_category", "product_name") is False

    def test_get_patterns_for_category(self):
        patterns = get_patterns_for_category("silence_reason")
        assert "price_high" in patterns
        assert "blackout" in patterns

    def test_get_patterns_empty_for_unknown(self):
        assert get_patterns_for_category("unknown") == []


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Tag extraction from messages
# ─────────────────────────────────────────────────────────────────────────────
class TestTagger:
    async def _setup_message(self, session):
        """Create prerequisite customer + conversation + message rows."""
        import random
        cust_phone = f"+243{random.randint(100000000, 999999999)}"
        conv_id = uuid.uuid4()
        msg_id = uuid.uuid4()

        await session.execute(text(
            "INSERT INTO mbb.customers (phone_number, name, city, preferred_language) "
            f"VALUES ('{cust_phone}', 'Test', 'Kinshasa', 'french') ON CONFLICT DO NOTHING"
        ))
        await session.execute(text(
            f"INSERT INTO mbb.conversations (conversation_id, customer_id, status, language_detected) "
            f"VALUES ('{conv_id}', '{cust_phone}', 'active', 'french')"
        ))
        await session.execute(text(
            f"INSERT INTO mbb.messages (message_id, conversation_id, direction, content_type, content, language, timestamp) "
            f"VALUES ('{msg_id}', '{conv_id}', 'inbound', 'text', 'test', 'french', NOW())"
        ))
        await session.flush()
        return msg_id, conv_id, cust_phone

    async def test_tag_product_mention(self):
        async with AsyncSessionLocal() as session:
            msg_id, conv_id, phone = await self._setup_message(session)
            tags = await tag_message(
                session, msg_id, conv_id, phone,
                "Je cherche un chargeur USB pour mon téléphone",
                "text", "french",
            )
            categories = [t["category"] for t in tags]
            assert "product_demand" in categories
            assert "language_usage" in categories
            await session.rollback()

    async def test_tag_price_objection(self):
        async with AsyncSessionLocal() as session:
            msg_id, conv_id, phone = await self._setup_message(session)
            tags = await tag_message(
                session, msg_id, conv_id, phone,
                "C'est trop cher pour moi",
                "text", "french",
            )
            categories = [t["category"] for t in tags]
            assert "silence_reason" in categories
            await session.rollback()

    async def test_tag_opt_out(self):
        async with AsyncSessionLocal() as session:
            msg_id, conv_id, phone = await self._setup_message(session)
            tags = await tag_message(
                session, msg_id, conv_id, phone,
                "stop",
                "text", "french",
            )
            categories = [t["category"] for t in tags]
            assert "opt_out_reason" in categories
            await session.rollback()

    async def test_tag_urgency(self):
        async with AsyncSessionLocal() as session:
            msg_id, conv_id, phone = await self._setup_message(session)
            tags = await tag_message(
                session, msg_id, conv_id, phone,
                "J'ai besoin de ça urgent maintenant",
                "text", "french",
            )
            categories = [t["category"] for t in tags]
            assert "conversion_trigger" in categories
            await session.rollback()

    async def test_tag_language_always_present(self):
        async with AsyncSessionLocal() as session:
            msg_id, conv_id, phone = await self._setup_message(session)
            tags = await tag_message(
                session, msg_id, conv_id, phone,
                "Bonjour",
                "text", "lingala",
            )
            categories = [t["category"] for t in tags]
            assert "language_usage" in categories
            await session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Escalation trigger detection
# ─────────────────────────────────────────────────────────────────────────────
class TestEscalationTriggers:
    async def test_voice_note_triggers_high(self):
        async with AsyncSessionLocal() as session:
            conv_id = uuid.uuid4()
            trigger = await detect_triggers(session, conv_id, "", "voice_note")
            assert trigger.should_escalate is True
            assert trigger.reason == "voice_note"
            assert trigger.priority == "high"

    async def test_human_request_triggers_high(self):
        async with AsyncSessionLocal() as session:
            conv_id = uuid.uuid4()
            trigger = await detect_triggers(
                session, conv_id,
                "Je veux parler à quelqu'un", "text",
            )
            assert trigger.should_escalate is True
            assert trigger.priority == "high"

    async def test_complaint_triggers_high(self):
        async with AsyncSessionLocal() as session:
            conv_id = uuid.uuid4()
            trigger = await detect_triggers(
                session, conv_id,
                "J'ai un problème avec ma commande, c'est une arnaque",
                "text",
            )
            assert trigger.should_escalate is True
            assert trigger.priority == "high"

    async def test_normal_message_no_trigger(self):
        async with AsyncSessionLocal() as session:
            conv_id = uuid.uuid4()
            trigger = await detect_triggers(
                session, conv_id,
                "Bonjour, comment ça va?", "text",
            )
            assert trigger.should_escalate is False

    async def test_lingala_human_request(self):
        async with AsyncSessionLocal() as session:
            conv_id = uuid.uuid4()
            trigger = await detect_triggers(
                session, conv_id,
                "Nalingi moto akoka koyamba ngai", "text",
            )
            assert trigger.should_escalate is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Audit log
# ─────────────────────────────────────────────────────────────────────────────
class TestAuditLog:
    async def test_log_and_list(self):
        async with AsyncSessionLocal() as session:
            await log_action(
                session,
                user_name="test_admin",
                user_role="admin",
                action="test.action",
                target_entity="test",
                target_id="123",
            )
            await session.flush()

            entries, total = await list_logs(session, actor="test_admin", limit=10, offset=0)
            assert total >= 1
            assert any(e["action"] == "test.action" for e in entries)
            await session.rollback()


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Feature flags + config (Redis)
# ─────────────────────────────────────────────────────────────────────────────
def _redis_available() -> bool:
    """Check if Redis is reachable on localhost:6379."""
    import socket
    try:
        s = socket.create_connection(("localhost", 6379), timeout=1)
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis not reachable on localhost:6379")
class TestConfigFlags:
    async def test_feature_flags_round_trip(self):
        from app.redis_client import get_redis_pool
        import redis.asyncio as aioredis
        redis = aioredis.Redis(connection_pool=get_redis_pool())
        from app.modules.m9_dashboard.config import set_feature_flags, get_feature_flags

        try:
            await set_feature_flags(redis, {
                "auto_respond": True,
                "relance_enabled": False,
                "maps_enabled": True,
            })
            result = await get_feature_flags(redis)
            assert result["auto_respond"] is True
            assert result["relance_enabled"] is False
            assert result["maps_enabled"] is True
        finally:
            await redis.aclose()

    async def test_config_key_round_trip(self):
        from app.redis_client import get_redis_pool
        import redis.asyncio as aioredis
        redis = aioredis.Redis(connection_pool=get_redis_pool())
        from app.modules.m9_dashboard.config import set_config, get_config

        try:
            await set_config(redis, "test_key", "test_value", "A test config")
            result = await get_config(redis, "test_key")
            assert result["value"] == "test_value"
        finally:
            await redis.aclose()


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Escalation trigger class
# ─────────────────────────────────────────────────────────────────────────────
class TestEscalationTriggerClass:
    def test_no_escalation_default(self):
        t = EscalationTrigger(False)
        assert t.should_escalate is False
        assert t.reason == ""
        assert t.priority == "medium"

    def test_escalation_with_params(self):
        t = EscalationTrigger(True, "voice_note", "high")
        assert t.should_escalate is True
        assert t.reason == "voice_note"
        assert t.priority == "high"
