# MBB ya Kin — Resilience Test Suite

## Installation Complete ✓

The resilience testing infrastructure has been fully implemented:

| Component | Status | Location |
|-----------|--------|----------|
| **Toxiproxy Config** | ✅ | `docker-compose.resilience.yml` |
| **Chaos Test Suite** | ✅ | `backend/tests/test_chaos.py` |
| **Blackout Queue Tests** | ✅ | `backend/tests/test_resilience.py` |
| **PowerShell Runner** | ✅ | `scripts/run_resilience_tests.ps1` |

---

## Current Status: Docker Registry Unreachable

**Issue:** Docker Hub (`registry-1.docker.io`) is currently unavailable from your network.

**Impact:** Cannot pull required images:
- `nginx:1.25-alpine`
- `grafana/loki:2.9.0`
- `ghcr.io/shopify/toxiproxy:2.9.0`
- `postgres:16-alpine`
- `redis:7-alpine`
- `grafana/grafana:*`
- `prom/prometheus:*`

---

## When Registry Access is Restored

### Option 1: Full automated run

```powershell
# Builds images, starts stack, runs all tests:
.\scripts\run_resilience_tests.ps1

# With specific scenarios only:
.\scripts\run_resilience_tests.ps1 -Scenarios S4,S5,S6

# Verbose output for debugging:
.\scripts\run_resilience_tests.ps1 -VerboseOutput
```

### Option 2: Manual step-by-step

```powershell
# 1. Start the stack with Toxiproxy:
docker compose -f docker-compose.yml `
               -f docker-compose.dev.yml `
               -f docker-compose.resilience.yml `
               up -d --build

# 2. Wait for services (check with):
docker compose ps
docker compose logs api --tail=20

# 3. Run blackout queue tests (requires only Redis):
cd backend
python tests/test_resilience.py

# 4. Run chaos tests (requires full stack + Toxiproxy):
python tests/test_chaos.py

# Or specific scenarios:
python tests/test_chaos.py --scenarios S4,S5 --verbose
```

---

## Test Scenarios

### Suite 1: Blackout Queue (`test_resilience.py`)

Unit tests for AOF-persisted message queue (7 checks):

| # | Test | Validates |
|---|------|-----------|
| 1 | enqueue → persist | Message survives Redis restart |
| 2 | queue_length | Depth counter accuracy |
| 3 | FIFO order | Messages processed in order |
| 4 | partial dequeue | Batch size > available items |
| 5 | empty queue | No crash on empty dequeue |
| 6 | corrupt payload | Skip bad JSON, return valid messages |
| 7 | Prometheus gauge | Metric syncs with queue depth |

**Requirements:** Redis on `localhost:6379` (DB 3)

### Suite 2: Chaos Tests (`test_chaos.py`)

Docker container manipulation (6 scenarios, ~25 checks):

| ID | Scenario | What It Proves | Duration |
|----|----------|---------------|----------|
| **S1** | Redis SIGSTOP → restart | API never returns 500 during Redis blackout; fail-open rate-limit and dedup | 30s |
| **S2** | Postgres SIGSTOP → restart | Tasks queue in Redis, retry on recovery | 90s |
| **S3** | Worker SIGKILL | Docker restarts worker; duplicate messages deduped | 60s |
| **S4** | Toxiproxy: 500ms + 50KB/s | Response < 5s, payload < 10KB on slow 3G | 15s |
| **S5** | Payload size sweep | All endpoints ≤ 10,240 bytes | 10s |
| **S6** | Blackout queue drain | Queued messages processed on recovery | 40s |

**Requirements:**
- Full Docker stack running
- Toxiproxy on port 8474
- `requests` + `redis` packages: `pip install requests redis`

---

## Troubleshooting

### Redis connection failed

```powershell
# Check Redis container:
docker compose ps redis
docker compose logs redis --tail=50

# Restart Redis only:
docker compose restart redis
```

### API not responding

```powershell
# Check API logs:
docker compose logs api --tail=100

# Check all service health:
docker compose ps
```

### Toxiproxy not available (S4 skipped)

```bash
# Verify Toxiproxy is in the compose file:
docker compose -f docker-compose.yml `
               -f docker-compose.dev.yml `
               -f docker-compose.resilience.yml `
               ps toxiproxy

# Check port 8474:
curl http://localhost:8474/version
```

### Test hangs during container restart

- **Normal:** S1-S3 stop/kill containers and wait for recovery (up to 60s each)
- **Issue:** If a container doesn't restart, kill the test (`Ctrl+C`) and check:
  ```powershell
  docker compose ps
  docker compose restart <service>
  ```

---

## Architecture Notes

### DRC Resilience Design

Every component in `test_chaos.py` validates a specific DRC constraint:

| Constraint | Test | Pass Criteria |
|-----------|------|--------------|
| **6-hour blackout** | S1, S2, S6 | Redis AOF persists queue; Celery retries after recovery |
| **Slow 3G (400 kbps)** | S4 | Async 202 in < 5s; Toxiproxy simulates 500ms + 50KB/s |
| **Low bandwidth** | S4, S5 | All responses ≤ 10KB (no pagination overflow) |
| **Unstable power** | S1, S2, S3 | Containers restart cleanly; no duplicate processing |
| **Network partitions** | S4 | API remains responsive during latency spikes |

### Key Files Modified

| File | Purpose |
|------|---------|
| `app/redis_utils.py` | **NEW** — Reusable rate-limit, dedup, caching helpers |
| `app/modules/m1_gateway/session_cache.py` | **NEW** — Typed session HASH operations |
| `app/cache.py` | Added `get_cache_client()`, `TTL.DEDUP`, `TTL.CUSTOMER` |
| `app/tasks/m1.py` | **FIXED** — Removed `.decode()` bug, uses `session_cache` |
| `app/api/v1/messages.py` | **FIXED** — Atomic dedup (was race-prone), uses `redis_utils` |

---

## Next Steps

1. **Wait for Docker Hub connectivity** or use a corporate proxy/mirror
2. **Run full test suite:**
   ```powershell
   .\scripts\run_resilience_tests.ps1
   ```
3. **Expected result:** All 32+ checks pass (7 unit + 25 chaos)
4. **If failures:** Check logs in `docker compose logs <service>`

---

## Contact / Issues

- **Logs location:** `docker compose logs <service> --tail=100`
- **Stop stack:** `docker compose down` (preserves volumes) or `docker compose down -v` (wipes data)
- **Reset Redis:** `docker compose restart redis`
- **Force rebuild:** `docker compose up -d --build --force-recreate`

**The system is ready to test as soon as Docker registry access is restored.**
