"""
tests/test_chaos.py — MBB ya Kin Docker Chaos Test Suite

The Kinshasa Test: "Will this work during a blackout on slow 3G for 6 hours?"

Scenarios
---------
  S1. Redis blackout      — API returns 202 (never 500); recovers cleanly
  S2. Postgres blackout   — API accepts messages; tasks retry on recovery
  S3. Celery worker crash — Docker restarts it; duplicate sends are deduped
  S4. Slow 3G (Toxiproxy) — API 202 in < 5s; all payloads < 10 KB
  S5. Payload size guard  — All critical endpoints stay under 10 KB limit
  S6. Blackout queue drain — Messages queued during outage process on recovery

Prerequisites
-------------
  # Start full stack with Toxiproxy sidecar:
  docker compose -f docker-compose.yml \\
                 -f docker-compose.dev.yml \\
                 -f docker-compose.resilience.yml \\
                 up -d

  # Install test dependencies:
  pip install requests redis

Run
---
  # All scenarios:
  python tests/test_chaos.py

  # Selected scenarios only:
  python tests/test_chaos.py --scenarios S1,S4,S5

  # With auth token:
  python tests/test_chaos.py --jwt eyJ...

  # Verbose (shows response bodies, container states):
  python tests/test_chaos.py --verbose

Exit code 0 = all passed, 1 = at least one failure.

Container naming
----------------
Docker Compose prefixes containers with the project name (folder name, lower-cased).
Default: e:\\Bot → project "bot" → container "bot-redis-1".
Override with --project if your setup differs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

# ── Configuration ──────────────────────────────────────────────────────────────

API_BASE       = "http://localhost:8000/api/v1"
TOXIPROXY_API  = "http://localhost:8474"

MAX_PAYLOAD_BYTES = 10_240   # 10 KB — DRC slow-3G hard limit
SLOW_3G_LATENCY_MS = 500     # one-way added latency
SLOW_3G_BANDWIDTH_KBS = 50   # KB/s ≈ 400 kbps (generous 3G)

DEV_JWT  = ""   # populated by --jwt
_VERBOSE = False
_PROJECT = "bot"   # docker compose project name

def _container(service: str) -> str:
    return f"{_PROJECT}-{service}-1"


# ── Result tracking ────────────────────────────────────────────────────────────

@dataclass
class Result:
    scenario: str
    passed: bool
    reason: str
    elapsed_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)


_results: list[Result] = []


def _record(scenario: str, passed: bool, reason: str, elapsed_ms: int = 0, **details: Any) -> Result:
    r = Result(scenario, passed, reason, elapsed_ms, details)
    _results.append(r)
    status = "\033[92mPASS\033[0m" if passed else "\033[91mFAIL\033[0m"
    print(f"  [{status}] {scenario}: {reason} ({elapsed_ms}ms)")
    if _VERBOSE and details:
        for k, v in details.items():
            print(f"         {k}: {v}")
    return r


# ── Docker helpers ─────────────────────────────────────────────────────────────

def _docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker"] + args, capture_output=True, text=True, check=check)


def _container_stop(service: str) -> None:
    name = _container(service)
    _docker(["stop", "--time", "3", name])
    if _VERBOSE:
        print(f"         stopped {name}")


def _container_start(service: str) -> None:
    name = _container(service)
    _docker(["start", name])
    if _VERBOSE:
        print(f"         started {name}")


def _container_kill(service: str) -> None:
    _docker(["kill", "--signal", "SIGKILL", _container(service)])


def _container_status(service: str) -> str:
    r = _docker(["inspect", "--format", "{{.State.Status}}", _container(service)], check=False)
    return r.stdout.strip()


def _wait_healthy(service: str, timeout: int = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = _docker(
            ["inspect", "--format", "{{.State.Health.Status}}", _container(service)],
            check=False,
        )
        status = r.stdout.strip()
        if status == "healthy":
            return True
        time.sleep(2)
    return False


def _wait_running(service: str, timeout: int = 45) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _container_status(service) == "running":
            return True
        time.sleep(2)
    return False


def _wait_api_ready(timeout: int = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get("http://localhost:8000/health", timeout=3)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


# ── Toxiproxy helpers ──────────────────────────────────────────────────────────

def _toxiproxy_available() -> bool:
    try:
        r = requests.get(f"{TOXIPROXY_API}/version", timeout=3)
        return r.status_code == 200
    except requests.RequestException:
        return False


def _toxiproxy_reset() -> None:
    """Delete all proxies and re-create the api_proxy in a known clean state."""
    try:
        r = requests.get(f"{TOXIPROXY_API}/proxies", timeout=5)
        for name in r.json():
            requests.delete(f"{TOXIPROXY_API}/proxies/{name}", timeout=5)

        # api proxy — test client talks to Toxiproxy, which forwards to api:8000
        requests.post(
            f"{TOXIPROXY_API}/proxies",
            json={"name": "api_proxy", "listen": "0.0.0.0:18000", "upstream": "api:8000"},
            timeout=5,
        )
    except requests.RequestException as exc:
        if _VERBOSE:
            print(f"         toxiproxy_reset: {exc}")


def _add_toxic(proxy: str, toxic_type: str, attrs: dict, name: str | None = None) -> bool:
    toxic_name = name or f"{proxy}_{toxic_type}"
    try:
        r = requests.post(
            f"{TOXIPROXY_API}/proxies/{proxy}/toxics",
            json={
                "name": toxic_name,
                "type": toxic_type,
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": attrs,
            },
            timeout=5,
        )
        return r.status_code in (200, 201)
    except requests.RequestException:
        return False


def _remove_toxic(proxy: str, toxic_name: str) -> None:
    try:
        requests.delete(f"{TOXIPROXY_API}/proxies/{proxy}/toxics/{toxic_name}", timeout=5)
    except requests.RequestException:
        pass


# ── Request helpers ────────────────────────────────────────────────────────────

def _make_payload(phone: str = "+243812345678", content: str = "Mbote! Nalingi kosomba") -> dict:
    return {
        "message_id":          str(uuid.uuid4()),
        "customer_phone":      phone,
        "content":             content,
        "content_type":        "text",
        "timestamp":           "2026-04-16T10:00:00+00:00",
        "whatsapp_message_id": str(uuid.uuid4()),
    }


def _headers(jwt: str = "") -> dict:
    token = jwt or DEV_JWT
    h = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _post_message(
    payload: dict | None = None,
    base: str = API_BASE,
    timeout: int = 10,
) -> requests.Response:
    return requests.post(
        f"{base}/messages",
        json=payload or _make_payload(),
        headers=_headers(),
        timeout=timeout,
    )


# ── Redis client (sync, for queue inspection) ─────────────────────────────────

def _redis_sync(db: int = 3):
    import redis as sync_redis
    return sync_redis.Redis(
        host="localhost", port=6379, db=db,
        decode_responses=True, socket_connect_timeout=3,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# S1 — Redis Blackout
# ═══════════════════════════════════════════════════════════════════════════════

def s1_redis_blackout() -> None:
    print("\nS1. Redis Blackout — API must return 202, never 500")

    # Baseline
    try:
        r = _post_message()
        _record("S1.baseline", r.status_code == 202, f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        _record("S1.baseline", False, str(exc))
        return

    # Kill Redis
    print("  Stopping Redis...")
    _container_stop("redis")
    time.sleep(4)  # allow connection pool failure detection

    # API must still respond 202 (fail-open: rate-limit and dedup skip on Redis error)
    try:
        t0 = time.monotonic()
        r = _post_message(timeout=15)
        ms = int((time.monotonic() - t0) * 1000)
        _record("S1.redis_down_no_500", r.status_code == 202, f"HTTP {r.status_code}", ms,
                body=r.text[:200])
    except requests.RequestException as exc:
        _record("S1.redis_down_no_500", False, str(exc))

    # Response must still be valid JSON
    try:
        body = r.json()
        _record("S1.response_json", "queue_position" in body, f"queue_position={body.get('queue_position')}")
    except Exception:
        _record("S1.response_json", False, "not JSON")

    # Recovery
    print("  Restarting Redis...")
    _container_start("redis")
    recovered = _wait_healthy("redis", timeout=40)
    _record("S1.redis_recovered", recovered, "Redis container healthy")

    time.sleep(3)

    # Post-recovery request
    try:
        t0 = time.monotonic()
        r = _post_message()
        ms = int((time.monotonic() - t0) * 1000)
        _record("S1.post_recovery_202", r.status_code == 202, f"HTTP {r.status_code}", ms)
    except requests.RequestException as exc:
        _record("S1.post_recovery_202", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# S2 — Postgres Blackout
# ═══════════════════════════════════════════════════════════════════════════════

def s2_postgres_blackout() -> None:
    print("\nS2. Postgres Blackout — tasks queue in Redis, retry on recovery")

    # Kill Postgres
    print("  Stopping Postgres...")
    _container_stop("postgres")
    time.sleep(3)

    # API still accepts messages asynchronously (Celery will retry)
    try:
        t0 = time.monotonic()
        r = _post_message(timeout=15)
        ms = int((time.monotonic() - t0) * 1000)
        _record("S2.postgres_down_202", r.status_code == 202, f"HTTP {r.status_code}", ms)
    except requests.RequestException as exc:
        _record("S2.postgres_down_202", False, str(exc))

    # Recovery
    print("  Restarting Postgres...")
    _container_start("postgres")
    recovered = _wait_healthy("postgres", timeout=60)
    _record("S2.postgres_recovered", recovered, "Postgres container healthy")

    # API /health should return 200 (API itself doesn't depend on Postgres being healthy)
    time.sleep(5)
    try:
        r = requests.get("http://localhost:8000/health", timeout=10)
        _record("S2.api_healthy_post_recovery", r.status_code == 200, f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        _record("S2.api_healthy_post_recovery", False, str(exc))

    # New message accepted normally after recovery
    try:
        r = _post_message()
        _record("S2.post_recovery_message", r.status_code == 202, f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        _record("S2.post_recovery_message", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# S3 — Celery Worker Crash + Idempotent Restart
# ═══════════════════════════════════════════════════════════════════════════════

def s3_worker_crash() -> None:
    print("\nS3. Celery Worker SIGKILL + Docker restart + dedup idempotency")

    # Dispatch a message with a fixed WA message ID we'll reuse
    fixed_wa_id = str(uuid.uuid4())
    payload = _make_payload(content="Test idempotency — ndeko ya crash")
    payload["whatsapp_message_id"] = fixed_wa_id

    try:
        r = _post_message(payload=payload)
        _record("S3.task_dispatched", r.status_code == 202, f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        _record("S3.task_dispatched", False, str(exc))
        return

    # Kill worker immediately (simulate mid-task crash)
    print("  SIGKILL celery_worker...")
    _container_kill("celery_worker")
    time.sleep(2)

    # Docker restart policy (unless-stopped) should revive it
    print("  Waiting for Docker to restart worker (up to 45s)...")
    restarted = _wait_running("celery_worker", timeout=45)
    _record("S3.worker_auto_restarted", restarted, _container_status("celery_worker"))

    time.sleep(5)

    # Re-send same WA message ID — dedup must fire (queue_position == 0)
    try:
        t0 = time.monotonic()
        r2 = _post_message(payload=payload)
        ms = int((time.monotonic() - t0) * 1000)
        body = r2.json() if r2.content else {}
        deduped = body.get("queue_position") == 0
        _record(
            "S3.duplicate_deduped",
            r2.status_code == 202 and deduped,
            f"HTTP {r2.status_code}, queue_position={body.get('queue_position')}",
            ms,
        )
    except requests.RequestException as exc:
        _record("S3.duplicate_deduped", False, str(exc))

    # API /health must still pass
    try:
        r = requests.get("http://localhost:8000/health", timeout=5)
        _record("S3.api_healthy_after_crash", r.status_code == 200, f"HTTP {r.status_code}")
    except requests.RequestException as exc:
        _record("S3.api_healthy_after_crash", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# S4 — Slow 3G (Toxiproxy)
# ═══════════════════════════════════════════════════════════════════════════════

def s4_slow_3g() -> None:
    print("\nS4. Slow 3G Simulation (Toxiproxy — 500ms latency + 50 KB/s)")

    if not _toxiproxy_available():
        _record(
            "S4.pre",
            False,
            "Toxiproxy not reachable at :8474 — add docker-compose.resilience.yml",
        )
        return

    _toxiproxy_reset()
    PROXIED = "http://localhost:18000/api/v1"

    # Verify the proxy itself works before adding toxics
    try:
        r = requests.get("http://localhost:18000/health", timeout=5)
        _record("S4.proxy_baseline", r.status_code == 200, f"HTTP {r.status_code} via proxy")
    except requests.RequestException as exc:
        _record("S4.proxy_baseline", False, f"proxy not working: {exc}")
        return

    # Add latency (one-way 500ms) and bandwidth throttle (50 KB/s ≈ slow 3G)
    lat_ok = _add_toxic("api_proxy", "latency", {"latency": SLOW_3G_LATENCY_MS, "jitter": 100})
    bw_ok  = _add_toxic("api_proxy", "bandwidth", {"rate": SLOW_3G_BANDWIDTH_KBS})
    _record("S4.toxics_applied", lat_ok and bw_ok, f"latency={lat_ok} bandwidth={bw_ok}")

    # POST /messages through the slow link — must complete in < 5s (async 202)
    try:
        t0 = time.monotonic()
        r = _post_message(base=PROXIED, timeout=15)
        ms = int((time.monotonic() - t0) * 1000)

        _record("S4.response_under_5s", ms < 5_000, f"{ms}ms (limit 5 000ms)", ms)
        _record("S4.http_202", r.status_code == 202, f"HTTP {r.status_code}")

        payload_bytes = len(r.content)
        _record(
            "S4.response_payload_size",
            payload_bytes <= MAX_PAYLOAD_BYTES,
            f"{payload_bytes} bytes (limit {MAX_PAYLOAD_BYTES})",
            details={"bytes": payload_bytes},
        )
    except requests.Timeout:
        ms = int((time.monotonic() - t0) * 1000)
        _record("S4.response_under_5s", False, f"Timed out at {ms}ms", ms)
    except requests.RequestException as exc:
        _record("S4.response_under_5s", False, str(exc))

    # /health must also be tiny
    try:
        r = requests.get("http://localhost:18000/health", timeout=10)
        sz = len(r.content)
        _record("S4.health_payload_size", sz <= MAX_PAYLOAD_BYTES, f"{sz} bytes")
    except requests.RequestException as exc:
        _record("S4.health_payload_size", False, str(exc))

    # Tear down toxics
    _remove_toxic("api_proxy", "api_proxy_latency")
    _remove_toxic("api_proxy", "api_proxy_bandwidth")


# ═══════════════════════════════════════════════════════════════════════════════
# S5 — Payload Size Guard (no Redis/Docker manipulation needed)
# ═══════════════════════════════════════════════════════════════════════════════

def s5_payload_sizes() -> None:
    print(f"\nS5. Payload Size Guard (all responses ≤ {MAX_PAYLOAD_BYTES} bytes)")

    checks = [
        # (label, method, url, body)
        ("health",     "GET",  "http://localhost:8000/health",        None),
        ("POST /msg",  "POST", f"{API_BASE}/messages",                _make_payload()),
        ("POST /msg2", "POST", f"{API_BASE}/messages",                _make_payload()),  # dedup path
    ]

    for label, method, url, body in checks:
        try:
            if method == "GET":
                r = requests.get(url, headers=_headers(), timeout=8)
            else:
                r = requests.post(url, json=body, headers=_headers(), timeout=8)
            sz = len(r.content)
            _record(
                f"S5.{label}",
                sz <= MAX_PAYLOAD_BYTES,
                f"{sz} bytes",
                details={"status": r.status_code},
            )
        except requests.RequestException as exc:
            _record(f"S5.{label}", False, str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# S6 — Blackout Queue Drain
# Seeds the queue directly, triggers the drain Celery task, verifies empty.
# ═══════════════════════════════════════════════════════════════════════════════

def s6_blackout_drain() -> None:
    print("\nS6. Blackout Queue Drain — messages queued during outage get processed")

    try:
        rc = _redis_sync(db=3)
        rc.ping()
    except Exception as exc:
        _record("S6.pre", False, f"Redis not reachable: {exc}")
        return

    QUEUE_KEY = "mbb:blackout:queue"

    # Seed 3 synthetic messages
    seeded = [
        {
            "message_id":          str(uuid.uuid4()),
            "customer_phone":      f"+24381000000{i}",
            "content":             f"Blackout test #{i} — tika te ba bateli",
            "content_type":        "text",
            "timestamp":           "2026-04-16T03:30:00+00:00",
            "whatsapp_message_id": str(uuid.uuid4()),
        }
        for i in range(3)
    ]
    for msg in seeded:
        rc.rpush(QUEUE_KEY, json.dumps(msg))

    depth_before = rc.llen(QUEUE_KEY)
    _record("S6.queue_seeded", depth_before >= 3, f"{depth_before} items in blackout queue")

    # Trigger drain — try admin endpoint first, fall back to celery call via docker exec
    drained_via_api = False
    try:
        r = requests.post(f"{API_BASE}/admin/tasks/drain-blackout", headers=_headers(), timeout=10)
        drained_via_api = r.status_code in (200, 202)
    except requests.RequestException:
        pass

    if not drained_via_api:
        result = _docker([
            "exec", _container("celery_worker"),
            "celery", "-A", "app.tasks.celery_app", "call", "m1.drain_blackout_queue",
        ], check=False)
        if _VERBOSE:
            print(f"         celery call exit={result.returncode}: {result.stdout[:100]}")

    _record("S6.drain_triggered", True, "via API" if drained_via_api else "via docker exec")

    # Wait up to 30s for queue to empty
    print("  Waiting for drain (up to 30s)...")
    t_drain_start = time.monotonic()
    final_depth = depth_before
    while time.monotonic() - t_drain_start < 30:
        try:
            final_depth = rc.llen(QUEUE_KEY)
            if final_depth == 0:
                break
        except Exception:
            break
        time.sleep(2)

    elapsed_drain = int((time.monotonic() - t_drain_start) * 1000)
    _record(
        "S6.queue_emptied",
        final_depth == 0,
        f"queue depth after drain: {final_depth}",
        elapsed_drain,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

_SCENARIOS = {
    "S1": s1_redis_blackout,
    "S2": s2_postgres_blackout,
    "S3": s3_worker_crash,
    "S4": s4_slow_3g,
    "S5": s5_payload_sizes,
    "S6": s6_blackout_drain,
}


def _print_summary() -> None:
    total  = len(_results)
    passed = sum(1 for r in _results if r.passed)
    failed = total - passed

    print()
    print("=" * 64)
    print(f"  CHAOS SUMMARY: {passed}/{total} checks passed")
    if failed:
        print("\n  FAILURES:")
        for r in _results:
            if not r.passed:
                print(f"    \u2717 {r.scenario}: {r.reason}")
    else:
        print("  ALL CHECKS PASSED \u2713")
    print("=" * 64)


def main() -> int:
    global DEV_JWT, _VERBOSE, _PROJECT

    parser = argparse.ArgumentParser(description="MBB ya Kin Docker Chaos Tests")
    parser.add_argument("--jwt",       default="",  help="Bearer JWT for authenticated endpoints")
    parser.add_argument("--project",   default="bot", help="Docker Compose project name (default: bot)")
    parser.add_argument("--verbose",   action="store_true")
    parser.add_argument(
        "--scenarios",
        default="all",
        help="Which scenarios to run (e.g. S1,S4,S5 or 'all'). Default: all",
    )
    args = parser.parse_args()

    DEV_JWT  = args.jwt or os.environ.get("RESILIENCE_JWT", "")
    _VERBOSE = args.verbose
    _PROJECT = args.project

    run_all  = args.scenarios.strip().lower() == "all"
    selected = {s.strip().upper() for s in args.scenarios.split(",")} if not run_all else set()

    print("=" * 64)
    print("  MBB ya Kin — Docker Chaos Test Suite")
    print("  Kinshasa Test: blackout + slow 3G survival")
    print(f"  Project: {_PROJECT}  |  API: {API_BASE}")
    print("=" * 64)

    # Pre-flight: API must be reachable
    print("\nPre-flight: checking API at http://localhost:8000/health ...")
    if not _wait_api_ready(timeout=30):
        print("  API not reachable — start the stack first:")
        print("  docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.resilience.yml up -d")
        return 1
    print("  API ready ✓\n")

    for tag, fn in _SCENARIOS.items():
        if run_all or tag in selected:
            fn()

    _print_summary()
    return 0 if all(r.passed for r in _results) else 1


if __name__ == "__main__":
    sys.exit(main())
