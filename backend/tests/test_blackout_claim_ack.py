import asyncio
import json
import unittest
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from app import redis_client
from app.tasks import celery_app as celery_config
from app.tasks import conversion, m1, relance


def _payload(message_id=None, whatsapp_message_id="safe-wa-id"):
    return {
        "message_id": str(message_id or uuid.uuid4()),
        "customer_phone": "+243810000099",
        "content": "synthetic",
        "content_type": "text",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "whatsapp_message_id": whatsapp_message_id,
    }


class _Pipeline:
    def __init__(self, client):
        self.client = client
        self.keys = []

    def llen(self, key):
        self.keys.append(key)
        return self

    async def execute(self):
        return [len(self.client.lists.get(key, [])) for key in self.keys]


class _FakeRedis:
    def __init__(self):
        self.lists = {
            redis_client._BLACKOUT_KEY: [],
            redis_client._BLACKOUT_PROCESSING_KEY: [],
            redis_client._BLACKOUT_QUARANTINE_KEY: [],
        }
        self.values = {}

    async def aclose(self):
        return None

    async def lmove(self, source, destination, wherefrom, whereto):
        source_list = self.lists.setdefault(source, [])
        if not source_list:
            return None
        item = source_list.pop(0 if wherefrom == "LEFT" else -1)
        destination_list = self.lists.setdefault(destination, [])
        if whereto == "LEFT":
            destination_list.insert(0, item)
        else:
            destination_list.append(item)
        return item

    async def lrem(self, key, count, value):
        self.assert_count_one(count)
        values = self.lists.setdefault(key, [])
        try:
            values.remove(value)
        except ValueError:
            return 0
        return 1

    @staticmethod
    def assert_count_one(count):
        if count != 1:
            raise AssertionError(f"expected exact one-item removal, got {count}")

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.values:
            return None
        if ex is None or ex <= 0:
            raise AssertionError("lock must have bounded expiry")
        self.values[key] = value
        return True

    async def eval(self, _script, number_of_keys, key, owner):
        if number_of_keys != 1:
            raise AssertionError("unexpected key count")
        if self.values.get(key) != owner:
            return 0
        del self.values[key]
        return 1

    async def llen(self, key):
        return len(self.lists.setdefault(key, []))

    def pipeline(self):
        return _Pipeline(self)


class RedisClaimAckTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_and_recovery_preserve_fifo_order(self):
        client = _FakeRedis()
        client.lists[redis_client._BLACKOUT_KEY] = ["a", "b", "c"]
        with patch.object(redis_client, "_blackout_client", return_value=client):
            self.assertEqual(await redis_client.blackout_claim_one(), "a")
            self.assertEqual(await redis_client.blackout_claim_one(), "b")
            client.lists[redis_client._BLACKOUT_KEY].append("d")
            self.assertEqual(await redis_client.blackout_recover_processing(), 2)
        self.assertEqual(client.lists[redis_client._BLACKOUT_KEY], ["a", "b", "c", "d"])
        self.assertEqual(client.lists[redis_client._BLACKOUT_PROCESSING_KEY], [])

    async def test_exact_acknowledgement_removes_only_processing_item(self):
        client = _FakeRedis()
        client.lists[redis_client._BLACKOUT_PROCESSING_KEY] = ["one", "two"]
        with patch.object(redis_client, "_blackout_client", return_value=client):
            self.assertTrue(await redis_client.blackout_acknowledge("one"))
        self.assertEqual(client.lists[redis_client._BLACKOUT_PROCESSING_KEY], ["two"])

    async def test_lock_is_bounded_and_release_is_owner_safe(self):
        client = _FakeRedis()
        with patch.object(redis_client, "_blackout_client", return_value=client):
            self.assertTrue(await redis_client.blackout_acquire_drain_lock("owner-1"))
            self.assertFalse(await redis_client.blackout_acquire_drain_lock("owner-2"))
            self.assertFalse(await redis_client.blackout_release_drain_lock("wrong-owner"))
            self.assertTrue(await redis_client.blackout_release_drain_lock("owner-1"))
            self.assertTrue(await redis_client.blackout_acquire_drain_lock("owner-2"))
            del client.values[redis_client._BLACKOUT_DRAIN_LOCK_KEY]  # simulate expiry
            self.assertTrue(await redis_client.blackout_acquire_drain_lock("owner-3"))

    async def test_quarantine_copies_before_exact_processing_removal(self):
        client = _FakeRedis()
        client.lists[redis_client._BLACKOUT_PROCESSING_KEY] = ["bad"]
        with patch.object(redis_client, "_blackout_client", return_value=client):
            self.assertTrue(await redis_client.blackout_quarantine("bad"))
        self.assertEqual(client.lists[redis_client._BLACKOUT_PROCESSING_KEY], [])
        self.assertEqual(client.lists[redis_client._BLACKOUT_QUARANTINE_KEY], ["bad"])


class CanonicalDrainTests(unittest.IsolatedAsyncioTestCase):
    def _patch_helpers(self, claims, **overrides):
        values = iter(claims)

        async def claim():
            return next(values)

        defaults = {
            "new_blackout_drain_owner": Mock(return_value="owner"),
            "blackout_acquire_drain_lock": AsyncMock(return_value=True),
            "blackout_release_drain_lock": AsyncMock(return_value=True),
            "blackout_recover_processing": AsyncMock(return_value=0),
            "blackout_claim_one": AsyncMock(side_effect=claim),
            "blackout_acknowledge": AsyncMock(return_value=True),
            "blackout_quarantine": AsyncMock(return_value=True),
            "blackout_depths": AsyncMock(return_value={
                "pending_depth": 0,
                "processing_depth": 0,
                "quarantine_depth": 0,
            }),
        }
        defaults.update(overrides)
        stack = ExitStack()
        for name, value in defaults.items():
            stack.enter_context(patch.object(redis_client, name, value))
        return stack, defaults

    async def test_success_publishes_before_acknowledgement(self):
        raw = json.dumps(_payload())
        events = []
        publish = Mock(side_effect=lambda *_a, **_k: events.append("publish"))

        async def ack(_raw):
            events.append("ack")
            return True

        stack, helpers = self._patch_helpers(
            [raw, None], blackout_acknowledge=AsyncMock(side_effect=ack)
        )
        with stack, patch.object(m1.celery_app, "send_task", publish):
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(events, ["publish", "ack"])
        self.assertEqual(result["published"], 1)
        self.assertEqual(result["acknowledged"], 1)
        helpers["blackout_quarantine"].assert_not_awaited()

    async def test_publication_failure_leaves_processing_unacknowledged(self):
        raw = json.dumps(_payload())
        client = _FakeRedis()
        client.lists[redis_client._BLACKOUT_KEY] = [raw]
        with (
            patch.object(redis_client, "_blackout_client", return_value=client),
            patch.object(m1.celery_app, "send_task", side_effect=ConnectionError("broker")),
        ):
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["status"], "publication_failed")
        self.assertEqual(result["failed"], 1)
        self.assertEqual(client.lists[redis_client._BLACKOUT_KEY], [])
        self.assertEqual(client.lists[redis_client._BLACKOUT_PROCESSING_KEY], [raw])

    async def test_recovered_claim_is_republished_and_acknowledged(self):
        raw = json.dumps(_payload())
        stack, helpers = self._patch_helpers(
            [raw, None], blackout_recover_processing=AsyncMock(return_value=1)
        )
        with stack, patch.object(m1.celery_app, "send_task") as publish:
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["recovered"], 1)
        publish.assert_called_once()
        helpers["blackout_acknowledge"].assert_awaited_once_with(raw)

    async def test_previously_published_unacked_claim_is_safely_replayed(self):
        raw = json.dumps(_payload())
        stack, helpers = self._patch_helpers(
            [raw, None], blackout_recover_processing=AsyncMock(return_value=1)
        )
        with stack, patch.object(m1.celery_app, "send_task") as publish:
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["published"], 1)
        publish.assert_called_once()  # replay is intentionally permitted
        helpers["blackout_acknowledge"].assert_awaited_once()

    async def test_lock_contention_skips_every_queue_operation(self):
        stack, helpers = self._patch_helpers(
            [], blackout_acquire_drain_lock=AsyncMock(return_value=False)
        )
        with stack, patch.object(m1.celery_app, "send_task") as publish:
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["status"], "already_running")
        self.assertFalse(result["lock_acquired"])
        helpers["blackout_recover_processing"].assert_not_awaited()
        helpers["blackout_claim_one"].assert_not_awaited()
        publish.assert_not_called()

    async def test_malformed_item_is_quarantined_then_later_valid_item_publishes(self):
        bad = "raw-private-payload"
        good = json.dumps(_payload())
        safe_log = Mock()
        stack, helpers = self._patch_helpers([bad, good, None])
        with (
            stack,
            patch.object(m1, "log", safe_log),
            patch.object(m1.celery_app, "send_task") as publish,
        ):
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(result["published"], 1)
        helpers["blackout_quarantine"].assert_awaited_once_with(bad)
        publish.assert_called_once()
        self.assertNotIn(bad, repr(safe_log.method_calls))

    async def test_quarantine_failure_leaves_claim_unacknowledged(self):
        bad = "not-json"
        stack, helpers = self._patch_helpers(
            [bad], blackout_quarantine=AsyncMock(return_value=False)
        )
        with stack, patch.object(m1.celery_app, "send_task") as publish:
            result = await m1._drain(SimpleNamespace())
        self.assertEqual(result["status"], "redis_error")
        helpers["blackout_acknowledge"].assert_not_awaited()
        publish.assert_not_called()

    async def test_drainer_has_no_recovery_notification_or_messaging_side_effect(self):
        raw = json.dumps(_payload())
        stack, _helpers = self._patch_helpers([raw, None])
        with (
            stack,
            patch("app.adapters.get_messaging_adapter") as messaging,
            patch.object(m1.celery_app, "send_task"),
        ):
            await m1._drain(SimpleNamespace())
        messaging.assert_not_called()


class ScheduleAndObsoleteConsumerTests(unittest.TestCase):
    def test_schedule_gate_has_only_four_canonical_entries(self):
        expected = {
            "relance-scan-eligible",
            "m1-drain-blackout",
            "maps-aggregate-daily",
            "escalation-check-stale",
        }
        self.assertEqual(celery_config.beat_schedule_for(False), {})
        self.assertEqual(set(celery_config.beat_schedule_for(True)), expected)
        self.assertNotIn("conversion-drain-blackout", celery_config._BEAT_SCHEDULE)

    def test_maps_schedule_is_midnight_in_kinshasa_timezone(self):
        maps_schedule = celery_config.beat_schedule_for(True)["maps-aggregate-daily"]["schedule"]
        self.assertEqual(celery_config.celery_app.conf.timezone, "Africa/Kinshasa")
        self.assertEqual(maps_schedule.hour, {0})
        self.assertEqual(maps_schedule.minute, {0})
        midnight = datetime(2026, 7, 27, tzinfo=ZoneInfo("Africa/Kinshasa"))
        self.assertEqual(midnight.astimezone(timezone.utc).hour, 23)

    def test_relance_scanner_preserves_eta_delivery_flow(self):
        lead = SimpleNamespace(lead_id=uuid.uuid4(), conversation_id=uuid.uuid4())
        conversation = SimpleNamespace()
        scheduled = SimpleNamespace(
            relance_id=uuid.uuid4(),
            scheduled_at=datetime(2026, 7, 27, 8, tzinfo=timezone.utc),
        )
        session = AsyncMock()
        session.execute.return_value = SimpleNamespace(
            scalar_one_or_none=Mock(return_value=conversation)
        )
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session

        with (
            patch.object(relance, "AsyncSessionLocal", return_value=session_context),
            patch.object(
                relance,
                "find_eligible_leads",
                new=AsyncMock(return_value=[lead]),
            ),
            patch.object(
                relance,
                "create_and_schedule_relance",
                new=AsyncMock(return_value=scheduled),
            ),
            patch.object(relance.send_relance, "apply_async") as send,
        ):
            result = asyncio.run(relance._scan_and_schedule_relances())

        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["scheduled_count"], 1)
        send.assert_called_once_with(
            args=[str(scheduled.relance_id)],
            eta=scheduled.scheduled_at,
        )
        session.commit.assert_awaited_once()

    def test_obsolete_conversion_consumer_has_no_queue_or_publication_effect(self):
        with (
            patch.object(redis_client, "blackout_claim_one") as claim,
            patch.object(redis_client, "blackout_dequeue_batch") as dequeue,
            patch.object(conversion.celery_app, "send_task") as publish,
        ):
            result = conversion.drain_blackout_queue.run()
        self.assertEqual(result, {"status": "obsolete", "skipped": True, "drained": 0})
        claim.assert_not_called()
        dequeue.assert_not_called()
        publish.assert_not_called()

    def test_obsolete_relance_consumer_is_inert(self):
        with (
            patch.object(relance, "run_async") as run_async,
            patch.object(relance, "AsyncSessionLocal") as open_session,
            patch.object(relance.send_relance, "apply_async") as send,
        ):
            result = relance.process_due_relances.run()
        self.assertEqual(result, {"status": "obsolete", "skipped": True, "dispatched": 0})
        run_async.assert_not_called()
        open_session.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
