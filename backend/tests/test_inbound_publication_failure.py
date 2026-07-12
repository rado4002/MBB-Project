import sys
import unittest
import uuid
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


prometheus_module = ModuleType("prometheus_client")
prometheus_module.Gauge = Mock
sys.modules.setdefault("prometheus_client", prometheus_module)

fake_celery_app = SimpleNamespace(send_task=Mock())
celery_app_module = ModuleType("app.tasks.celery_app")
celery_app_module.celery_app = fake_celery_app
celery_app_module.run_async = Mock()
sys.modules["app.tasks.celery_app"] = celery_app_module

from fastapi import HTTPException

from app import redis_utils
from app.api.v1 import messages
from app.schemas.common import ContentType
from app.schemas.messages import InboundMessageRequest


WA_ID = "authoritative-WA-id"


def _payload() -> InboundMessageRequest:
    return InboundMessageRequest(
        message_id=uuid.UUID("00000000-0000-0000-0000-000000000601"),
        customer_phone="+243810000041",
        content="Bonjour.",
        content_type=ContentType.text,
        timestamp=datetime(2026, 7, 12, tzinfo=timezone.utc),
        whatsapp_message_id=WA_ID,
    )


class InboundPublicationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        *,
        duplicate=False,
        limited=False,
        publish_error=None,
        acceptance_like=False,
        blackout_result=True,
        blackout_error=None,
        marker_result=True,
    ):
        events = []

        async def lookup(_wa_id):
            events.append("lookup")
            return duplicate

        async def rate_limit(_phone):
            events.append("rate_limit")
            return limited

        def publish(*_args, **_kwargs):
            events.append("publish")
            if acceptance_like:
                events.append("broker_acceptance_like")
            if publish_error:
                raise publish_error
            return SimpleNamespace(id="task-id")

        async def blackout(task_payload):
            events.append("blackout")
            if blackout_error:
                raise blackout_error
            return blackout_result

        async def mark(_wa_id):
            events.append("mark")
            return marker_result

        fake_celery_app.send_task = Mock(side_effect=publish)
        with (
            patch.object(messages, "has_accepted_inbound", lookup),
            patch.object(messages, "rate_limit_check", rate_limit),
            patch.object(messages, "blackout_enqueue", blackout),
            patch.object(messages, "mark_inbound_accepted", mark),
        ):
            try:
                result = await messages._handle_inbound(payload=_payload(), source="test")
                error = None
            except HTTPException as exc:
                result = None
                error = exc
        return result, error, events, fake_celery_app.send_task

    async def test_normal_success_marks_only_after_publication(self):
        result, error, events, publish = await self._run()
        self.assertIsNone(error)
        self.assertEqual(result.status, "queued")
        self.assertEqual(events, ["lookup", "rate_limit", "publish", "mark"])
        publish.assert_called_once()

    async def test_existing_accepted_duplicate_short_circuits(self):
        result, error, events, publish = await self._run(duplicate=True)
        self.assertIsNone(error)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(events, ["lookup"])
        publish.assert_not_called()

    async def test_redis_lookup_failure_fails_open_and_publishes(self):
        with (
            patch.object(redis_utils, "get_cache_client", side_effect=ConnectionError("offline")),
            patch.object(messages, "has_accepted_inbound", redis_utils.has_accepted_inbound),
            patch.object(messages, "rate_limit_check", AsyncMock(return_value=False)),
            patch.object(messages, "mark_inbound_accepted", AsyncMock(return_value=True)),
        ):
            fake_celery_app.send_task = Mock(return_value=SimpleNamespace(id="task-id"))
            result = await messages._handle_inbound(payload=_payload(), source="test")
        self.assertEqual(result.status, "queued")
        fake_celery_app.send_task.assert_called_once()

    async def test_rate_limited_request_creates_no_marker_or_path(self):
        result, error, events, publish = await self._run(limited=True)
        self.assertIsNone(result)
        self.assertEqual(error.status_code, 429)
        self.assertEqual(events, ["lookup", "rate_limit"])
        publish.assert_not_called()

    async def test_celery_failure_blackout_success_preserves_full_payload(self):
        captured = []

        async def blackout(task_payload):
            captured.append(task_payload)
            return True

        fake_celery_app.send_task = Mock(side_effect=ConnectionError("broker"))
        with (
            patch.object(messages, "has_accepted_inbound", AsyncMock(return_value=False)),
            patch.object(messages, "rate_limit_check", AsyncMock(return_value=False)),
            patch.object(messages, "blackout_enqueue", blackout),
            patch.object(messages, "mark_inbound_accepted", AsyncMock(return_value=True)) as mark,
        ):
            result = await messages._handle_inbound(payload=_payload(), source="test")
        self.assertEqual(result.status, "queued")
        self.assertEqual(
            set(captured[0]),
            {"message_id", "customer_phone", "content", "content_type", "timestamp", "whatsapp_message_id"},
        )
        self.assertEqual(captured[0]["whatsapp_message_id"], WA_ID)
        mark.assert_awaited_once_with(WA_ID)

    async def test_celery_failure_blackout_false_returns_retryable_503(self):
        result, error, events, _publish = await self._run(
            publish_error=ConnectionError("broker"), blackout_result=False
        )
        self.assertIsNone(result)
        self.assertEqual(error.status_code, 503)
        self.assertIn("not_accepted_retry", error.detail)
        self.assertNotIn("mark", events)

    async def test_celery_failure_blackout_raise_returns_retryable_503(self):
        result, error, events, _publish = await self._run(
            publish_error=ConnectionError("broker"),
            blackout_error=ConnectionError("blackout"),
        )
        self.assertIsNone(result)
        self.assertEqual(error.status_code, 503)
        self.assertNotIn("mark", events)

    async def test_ambiguous_publication_blackout_success_is_accepted(self):
        class AmbiguousError(TimeoutError):
            pass

        error = AmbiguousError("unknown acknowledgement")
        result, http_error, events, _publish = await self._run(
            publish_error=error, acceptance_like=True
        )
        self.assertIsNone(http_error)
        self.assertEqual(result.status, "queued")
        self.assertIn("broker_acceptance_like", events)
        self.assertEqual(events[-2:], ["blackout", "mark"])

    async def test_ambiguous_publication_blackout_failure_is_retryable(self):
        result, error, events, _publish = await self._run(
            publish_error=TimeoutError("unknown acknowledgement"), blackout_result=False
        )
        self.assertIsNone(result)
        self.assertEqual(error.status_code, 503)
        self.assertNotIn("mark", events)

    async def test_marker_write_failure_after_celery_success_keeps_acceptance(self):
        result, error, events, publish = await self._run(marker_result=False)
        self.assertIsNone(error)
        self.assertEqual(result.status, "queued")
        self.assertEqual(events, ["lookup", "rate_limit", "publish", "mark"])
        publish.assert_called_once()

    async def test_marker_write_failure_after_blackout_success_keeps_acceptance(self):
        result, error, events, _publish = await self._run(
            publish_error=ConnectionError("broker"), marker_result=False
        )
        self.assertIsNone(error)
        self.assertEqual(result.status, "queued")
        self.assertEqual(events[-2:], ["blackout", "mark"])

    async def test_marker_helpers_use_authoritative_key_ttl_and_safe_logs(self):
        client = SimpleNamespace(
            exists=AsyncMock(side_effect=ConnectionError("read")),
            set=AsyncMock(side_effect=ConnectionError("write")),
            aclose=AsyncMock(),
        )
        safe_log = Mock()
        with (
            patch.object(redis_utils, "get_cache_client", return_value=client),
            patch.object(redis_utils, "log", safe_log),
        ):
            self.assertFalse(await redis_utils.has_accepted_inbound(WA_ID))
            self.assertFalse(await redis_utils.mark_inbound_accepted(WA_ID))
        rendered_calls = repr(safe_log.warning.call_args_list)
        self.assertNotIn(WA_ID, rendered_calls)
        self.assertIn("accepted_marker_read_failed", rendered_calls)
        self.assertIn("accepted_marker_write_failed", rendered_calls)

    async def test_marker_helpers_use_existing_key_and_24_hour_ttl(self):
        client = SimpleNamespace(
            exists=AsyncMock(return_value=1),
            set=AsyncMock(return_value=True),
            aclose=AsyncMock(),
        )
        with patch.object(redis_utils, "get_cache_client", return_value=client):
            self.assertTrue(await redis_utils.has_accepted_inbound(WA_ID))
            self.assertTrue(await redis_utils.mark_inbound_accepted(WA_ID))
        expected_key = f"mbb:dedup:{WA_ID}"
        client.exists.assert_awaited_once_with(expected_key)
        client.set.assert_awaited_once_with(expected_key, "1", ex=86_400)

    async def test_unconfirmed_marker_write_is_logged_and_returns_false(self):
        client = SimpleNamespace(set=AsyncMock(return_value=False), aclose=AsyncMock())
        safe_log = Mock()
        with (
            patch.object(redis_utils, "get_cache_client", return_value=client),
            patch.object(redis_utils, "log", safe_log),
        ):
            self.assertFalse(await redis_utils.mark_inbound_accepted(WA_ID))
        rendered_call = repr(safe_log.warning.call_args)
        self.assertIn("accepted_marker_write_failed", rendered_call)
        self.assertNotIn(WA_ID, rendered_call)
