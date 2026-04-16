"""
Blackout Queue Resilience Integration Tests
MBB ya Kin — validates zero-message-loss guarantee during Kinshasa power outages.

Requirements:
  - Redis running on localhost:6379 (DB 3 is used exclusively by the blackout queue).
  - If Redis is not reachable the suite exits with SKIPPED (not FAILED) so local
    dev without Docker is not broken.

Run:
  cd backend
  python tests/test_resilience.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, ".")

# Minimal env so Settings() can instantiate without Docker secrets
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

print("=" * 60)
print("MBB ya Kin — Blackout Queue Resilience Tests")
print("=" * 60)

errors: list[str] = []


# ── Redis availability gate ───────────────────────────────────────────────────

async def _redis_reachable() -> bool:
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        s = get_settings()
        url = f"redis://{s.redis_host}:{s.redis_port}/3"
        client = aioredis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        await client.ping()
        await client.aclose()
        return True
    except Exception:
        return False


if not asyncio.run(_redis_reachable()):
    print("\n  ⚠  Redis not reachable on localhost:6379.")
    print("     Start Docker stack first:  docker compose ... up -d")
    print("     Skipping live tests — this is expected in local dev without Docker.\n")
    print("=" * 60)
    print("SKIPPED — Redis unavailable")
    sys.exit(0)

print("  Redis reachable ✓\n")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _reset_pool() -> None:
    """Reset the module-level connection pool so each asyncio.run() gets fresh connections.
    Required on Windows where asyncio.run() closes the event loop between calls,
    leaving cached pool connections attached to a dead loop."""
    import app.redis_client as _mod
    _mod._blackout_pool = None


async def _clean_queue() -> None:
    """Wipe the blackout queue key so each test starts from a clean slate."""
    from app.redis_client import _blackout_client, _BLACKOUT_KEY
    client = _blackout_client()
    await client.delete(_BLACKOUT_KEY)
    await client.aclose()


async def _raw_length() -> int:
    """Direct Redis LLEN — bypasses the module under test."""
    from app.redis_client import _blackout_client, _BLACKOUT_KEY
    client = _blackout_client()
    n = await client.llen(_BLACKOUT_KEY)
    await client.aclose()
    return int(n)


# ── Test 1: enqueue persists to Redis ────────────────────────────────────────

print("[1] blackout_enqueue persists message...")
async def _t1() -> None:
    from app.redis_client import blackout_enqueue
    await _clean_queue()
    ok = await blackout_enqueue({"wa_id": "msg001", "phone": "+243970000001", "content": "Mbote"})
    assert ok is True, f"blackout_enqueue returned {ok!r}, expected True"
    n = await _raw_length()
    assert n == 1, f"Expected queue depth 1 after single enqueue, got {n}"

try:
    _reset_pool()
    asyncio.run(_t1())
    print("    enqueue → Redis persist OK")
except Exception as exc:
    errors.append(f"enqueue: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 2: queue length reflects real count ──────────────────────────────────

print("\n[2] blackout_queue_length matches Redis LLEN...")
async def _t2() -> None:
    from app.redis_client import blackout_enqueue, blackout_queue_length
    await _clean_queue()
    for i in range(4):
        await blackout_enqueue({"wa_id": f"msg{i:03d}", "seq": i})
    reported = await blackout_queue_length()
    raw = await _raw_length()
    assert reported == 4, f"blackout_queue_length returned {reported}, expected 4"
    assert raw == 4,      f"Raw Redis LLEN returned {raw}, expected 4"

try:
    _reset_pool()
    asyncio.run(_t2())
    print("    queue_length == LLEN OK")
except Exception as exc:
    errors.append(f"queue_length: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 3: FIFO order preserved ──────────────────────────────────────────────

print("\n[3] FIFO order preserved across enqueue → dequeue...")
async def _t3() -> None:
    from app.redis_client import blackout_enqueue, blackout_dequeue_batch
    await _clean_queue()
    msgs = [{"wa_id": f"fifo{i}", "seq": i} for i in range(6)]
    for m in msgs:
        await blackout_enqueue(m)
    batch = await blackout_dequeue_batch(batch_size=6)
    assert len(batch) == 6, f"Expected 6 messages back, got {len(batch)}"
    for idx, msg in enumerate(batch):
        assert msg["seq"] == idx, (
            f"FIFO violation at position {idx}: got seq={msg['seq']}, expected {idx}"
        )
    remaining = await _raw_length()
    assert remaining == 0, f"Queue should be empty after full dequeue, got depth={remaining}"

try:
    _reset_pool()
    asyncio.run(_t3())
    print("    FIFO order OK — queue empty after drain")
except Exception as exc:
    errors.append(f"FIFO: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 4: partial batch (request > available) ───────────────────────────────

print("\n[4] Partial dequeue (batch_size > available messages)...")
async def _t4() -> None:
    from app.redis_client import blackout_enqueue, blackout_dequeue_batch
    await _clean_queue()
    for i in range(3):
        await blackout_enqueue({"wa_id": f"part{i}", "i": i})
    batch = await blackout_dequeue_batch(batch_size=50)   # ask for more than exist
    assert len(batch) == 3, f"Expected 3 messages, got {len(batch)}"
    remaining = await _raw_length()
    assert remaining == 0, f"Queue should be empty, got {remaining}"

try:
    _reset_pool()
    asyncio.run(_t4())
    print("    partial dequeue OK")
except Exception as exc:
    errors.append(f"partial_dequeue: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 5: dequeue on empty queue returns [] ─────────────────────────────────

print("\n[5] Dequeue on empty queue returns empty list (no crash)...")
async def _t5() -> None:
    from app.redis_client import blackout_dequeue_batch
    await _clean_queue()
    batch = await blackout_dequeue_batch(batch_size=10)
    assert batch == [], f"Expected [], got {batch!r}"

try:
    _reset_pool()
    asyncio.run(_t5())
    print("    empty dequeue → [] OK")
except Exception as exc:
    errors.append(f"empty_dequeue: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 6: invalid JSON in queue is skipped, valid messages still returned ───

print("\n[6] Corrupt payload in queue is skipped, valid messages still returned...")
async def _t6() -> None:
    from app.redis_client import _blackout_client, _BLACKOUT_KEY, blackout_dequeue_batch
    await _clean_queue()
    client = _blackout_client()
    # Manually push: valid, corrupt, valid
    await client.rpush(_BLACKOUT_KEY, json.dumps({"wa_id": "good1"}))
    await client.rpush(_BLACKOUT_KEY, b"NOT_JSON{{{{")
    await client.rpush(_BLACKOUT_KEY, json.dumps({"wa_id": "good2"}))
    await client.aclose()

    batch = await blackout_dequeue_batch(batch_size=10)
    # Corrupt entry breaks FIFO scan (None sentinel reached after corrupt)
    # Module logs a warning and stops — so we get 2 valid items (good1 + corrupt skipped,
    # but pipeline pops all 3 at once so: good1 parsed OK, NOT_JSON → warning, good2 parsed OK)
    wa_ids = [m.get("wa_id") for m in batch]
    assert "good1" in wa_ids, f"good1 missing from batch: {wa_ids}"
    assert "good2" in wa_ids, f"good2 missing from batch: {wa_ids}"

try:
    _reset_pool()
    asyncio.run(_t6())
    print("    corrupt entry skipped, valid messages returned OK")
except Exception as exc:
    errors.append(f"corrupt_payload: {exc}")
    print(f"    FAIL: {exc}")


# ── Test 7: Prometheus gauge syncs with queue depth ───────────────────────────

print("\n[7] Prometheus BLACKOUT_QUEUE_DEPTH gauge syncs on queue_length call...")
async def _t7() -> None:
    from app.redis_client import BLACKOUT_QUEUE_DEPTH, blackout_enqueue, blackout_queue_length
    await _clean_queue()
    for i in range(3):
        await blackout_enqueue({"wa_id": f"gauge{i}"})
    depth = await blackout_queue_length()          # also calls BLACKOUT_QUEUE_DEPTH.set()
    gauge_val = BLACKOUT_QUEUE_DEPTH._value.get()  # prometheus_client internal API
    assert gauge_val == depth == 3, (
        f"Gauge={gauge_val}, blackout_queue_length()={depth}, expected 3"
    )

try:
    _reset_pool()
    asyncio.run(_t7())
    print("    Prometheus gauge synced OK")
except Exception as exc:
    errors.append(f"gauge_sync: {exc}")
    print(f"    FAIL: {exc}")


# ── Summary ───────────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
if errors:
    print(f"FAILED — {len(errors)} error(s):")
    for e in errors:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("ALL 7 RESILIENCE TESTS PASSED ✓")
    sys.exit(0)
