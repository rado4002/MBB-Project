"""Live Redis checks for the blackout queue's claim/ack recovery contract.

Run from backend with ``python tests/test_resilience.py``. Redis DB 3 must be
disposable and empty; without Redis, local development reports SKIPPED.
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, ".")

# Minimal env so Settings() can instantiate without Docker secrets.
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


async def main() -> int:
    import redis.asyncio as aioredis

    from app.config import get_settings
    from app import redis_client as queue

    settings = get_settings()
    client = aioredis.from_url(
        f"redis://{settings.redis_host}:{settings.redis_port}/3",
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    keys = (
        queue._BLACKOUT_KEY,
        queue._BLACKOUT_PROCESSING_KEY,
        queue._BLACKOUT_QUARANTINE_KEY,
        queue._BLACKOUT_DRAIN_LOCK_KEY,
    )
    errors: list[str] = []
    owns_keys = False

    async def clean_queue() -> None:
        await client.delete(*keys)

    async def raw_depths() -> tuple[int, int, int]:
        depths = await asyncio.gather(*(client.llen(key) for key in keys[:3]))
        return tuple(int(depth) for depth in depths)

    async def enqueue_persists() -> None:
        assert await queue.blackout_enqueue({"wa_id": "msg001", "content": "Mbote"})
        assert await raw_depths() == (1, 0, 0)

    async def queue_length() -> None:
        for i in range(4):
            assert await queue.blackout_enqueue({"wa_id": f"msg{i}", "seq": i})
        assert await queue.blackout_queue_length() == 4
        assert await queue.blackout_depths() == {
            "pending_depth": 4,
            "processing_depth": 0,
            "quarantine_depth": 0,
        }
        assert await raw_depths() == (4, 0, 0)

    async def fifo_claim_and_ack() -> None:
        for i in range(6):
            assert await queue.blackout_enqueue({"wa_id": f"fifo{i}", "seq": i})
        for i in range(6):
            raw = await queue.blackout_claim_one()
            assert raw is not None
            assert json.loads(raw)["seq"] == i, f"FIFO violation at position {i}"
            assert await queue.blackout_acknowledge(raw)
        assert await raw_depths() == (0, 0, 0)

    async def recover_abandoned_claims() -> None:
        for i in range(4):
            assert await queue.blackout_enqueue({"wa_id": f"recovery{i}", "seq": i})
        for i in range(2):
            raw = await queue.blackout_claim_one()
            assert raw is not None and json.loads(raw)["seq"] == i
        assert await raw_depths() == (2, 2, 0)
        assert await queue.blackout_enqueue({"wa_id": "recovery4", "seq": 4})
        assert await queue.blackout_recover_processing() == 2
        assert await raw_depths() == (5, 0, 0)
        for i in range(5):
            raw = await queue.blackout_claim_one()
            assert raw is not None and json.loads(raw)["seq"] == i
            assert await queue.blackout_acknowledge(raw)
        assert await raw_depths() == (0, 0, 0)

    async def empty_claim() -> None:
        assert await queue.blackout_claim_one() is None
        assert not await queue.blackout_acknowledge("not-claimed")
        assert await queue.blackout_recover_processing() == 0
        assert await raw_depths() == (0, 0, 0)

    async def quarantine_corrupt_payload() -> None:
        corrupt = "NOT_JSON{{{{"
        await client.rpush(
            queue._BLACKOUT_KEY,
            json.dumps({"wa_id": "good1"}),
            corrupt,
            json.dumps({"wa_id": "good2"}),
        )
        first = await queue.blackout_claim_one()
        assert first is not None and json.loads(first)["wa_id"] == "good1"
        assert await queue.blackout_acknowledge(first)
        malformed = await queue.blackout_claim_one()
        assert malformed == corrupt
        assert await queue.blackout_quarantine(malformed)
        last = await queue.blackout_claim_one()
        assert last is not None and json.loads(last)["wa_id"] == "good2"
        assert await queue.blackout_acknowledge(last)
        assert await raw_depths() == (0, 0, 1)
        assert await client.lrange(queue._BLACKOUT_QUARANTINE_KEY, 0, -1) == [corrupt]

    async def gauge_sync() -> None:
        for i in range(3):
            assert await queue.blackout_enqueue({"wa_id": f"gauge{i}"})
        depth = await queue.blackout_queue_length()
        assert depth == 3
        assert queue.BLACKOUT_QUEUE_DEPTH._value.get() == depth

    checks = (
        ("enqueue persists", enqueue_persists),
        ("queue length and depths", queue_length),
        ("FIFO claim and ack", fifo_claim_and_ack),
        ("abandoned claim recovery", recover_abandoned_claims),
        ("empty claim", empty_claim),
        ("corrupt payload quarantine", quarantine_corrupt_payload),
        ("Prometheus gauge sync", gauge_sync),
    )

    print("MBB ya Kin — Blackout Queue Resilience Tests", flush=True)
    try:
        try:
            await client.ping()
        except Exception as exc:
            print(f"SKIPPED — Redis unavailable: {exc}")
            return 0

        if any(await asyncio.gather(*(client.exists(key) for key in keys))):
            print("FAILED — blackout DB 3 keys are not empty; refusing to alter existing data")
            return 1
        owns_keys = True

        for index, (label, check) in enumerate(checks, start=1):
            print(f"[{index}] {label}...", flush=True)
            try:
                await check()
                print("    OK", flush=True)
            except Exception as exc:
                errors.append(f"{label}: {exc!r}")
                print(f"    FAIL: {exc!r}", flush=True)
            finally:
                await clean_queue()

        if errors:
            print(f"FAILED — {len(errors)} error(s):")
            for error in errors:
                print(f"  {error}")
            return 1
        print("ALL 7 RESILIENCE TESTS PASSED")
        return 0
    finally:
        try:
            if owns_keys:
                await clean_queue()
        finally:
            await client.aclose()
            if queue._blackout_pool is not None:
                await queue._blackout_pool.disconnect()
                queue._blackout_pool = None


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
