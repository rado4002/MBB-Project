# Final Local Runtime Evidence

- Repository HEAD: `8284b0d6a9d369ca2e7ddbb756ba28abcf7a8fd2`
- Evidence UTC timestamp: `2026-07-27T05:58:38.765Z`
- Runtime type: fresh isolated production-like local runtime
- Git state at capture start: clean

## Effective scope and method

The isolated Compose project was `mbbevidencecapture`. The effective service
scope contained exactly:

```text
postgres
redis
api
celery_worker
dashboard
nginx
```

Baileys, Beat, monitoring, and backup were excluded. Fresh disposable
PostgreSQL and Redis volumes and temporary secret material were used. The
current repository sources were built for the runtime.

The first disposable startup stopped before application traffic because its
temporary database name did not match the checked-in initialization script's
required `mbb` name. The failed disposable resources were removed, the
temporary environment was corrected to `POSTGRES_DB=mbb`, and a fresh isolated
runtime was started. No application or repository file was changed.

Validation methods included Compose effective-service inspection, service
health inspection, HTTP probes, Celery inspect, Redis queue-depth commands,
aggregate SQL, a hash-only disposable persistence sentinel, controlled service
restart, and post-restart repetition of the same checks.

## Safety configuration

The API and worker both reported:

```text
APP_ENV=production
AI_ADAPTER=disabled
WHATSAPP_MODE=official
WHATSAPP_SEND_ENABLED=false
CRM_SEND_ENABLED=false
PAYMENT_SEND_ENABLED=false
RELANCE_ENABLED=false
SCHEDULED_TASKS_ENABLED=false
M1_MAPS_FANOUT_ENABLED=false
```

## Before and after one controlled restart

- Services running: `6`
- Services healthy: `6`
- PostgreSQL revision: `e2f3a4b5c6d7`
- Worker count: `1`
- Redacted worker reference hash: `8064532f3a4e`
- Worker queues:
  `default`, `relance`, `maps`, `escalation`, `conversion`
- Active tasks: `0`
- Reserved tasks: `0`
- Scheduled tasks: `0`
- Queue depths for all five worker queues: `0`
- Redis unacknowledged hash count: `0`
- Redis unacknowledged index count: `0`
- Blackout pending, processing, and quarantine depths: `0`
- Disposable persistence sentinel count after restart: `1`
- Sentinel SHA-256:
  `a834eb49cc145b824c23a1f94593bce99248f25cf54793894921db289fc053d1`
- Core business-table counts before restart: all `0`
- Core business-table counts after restart: all `0`

## Routing probes

| Probe | Before restart | After restart |
|---|---:|---:|
| `/health` over HTTP | 200 | 200 |
| `/api/v1/health` over HTTP | 200 | 200 |
| dashboard unauthenticated | 401 | 401 |
| dashboard authenticated with temporary credentials | 200 | 200 |
| public `/metrics` | 404 | 404 |
| root `/` | 404 | 404 |
| local HTTPS | unavailable | unavailable |

The local HTTPS probe returned connection code `000` with curl exit `35`.
The checked-in recovery Nginx scope is explicitly HTTP-only; no TLS-success
claim is made.

## Side-effect totals

- Business Celery tasks received: `0`
- M1 executions: `0`
- Messaging send attempts or successes: `0`
- CRM effects: `0`
- Payment effects: `0`
- Relance effects: `0`
- MAPS effects: `0`
- Scheduled effects: `0`

## Cleanup and Git state

The project was removed with the equivalent of:

```text
docker compose --project-name mbbevidencecapture down --volumes --rmi local --remove-orphans
```

Post-cleanup counts for project containers, networks, volumes, and locally
built images were all `0`. Temporary secrets and runtime files were removed.
The retained backup volume and retained Baileys session were not mounted.
Git remained clean until creation of the four requested evidence files.

## Claim boundary

This evidence covers the isolated six-service runtime, one controlled restart,
the listed health/routing results, disposable database persistence, queue
quiescence, and disabled external-action gates. It does not prove TLS routing,
external provider behavior, sustained operation, or production readiness.
