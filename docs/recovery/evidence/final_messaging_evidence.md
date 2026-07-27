# Final Messaging Evidence

- Repository HEAD: `8284b0d6a9d369ca2e7ddbb756ba28abcf7a8fd2`
- Evidence UTC timestamp: `2026-07-27T05:58:38.765Z`
- Outcome: `MESSAGING EVIDENCE FAILURE`
- Git state at capture start: clean

## Authorization and isolated method

The user replied `READY` after the required authorization prompt. A fresh
isolated PostgreSQL and Redis runtime was then started with:

```text
postgres
redis
api
celery_worker
baileys_live
```

The worker consumed only `default`, used concurrency `1`, and had
`BAILEYS_SEND_MAX_ATTEMPTS=1`. WhatsApp sending was enabled only in the worker
and Baileys. The API kept sending disabled. AI, CRM, payment, relance,
scheduled tasks, and M1-to-MAPS fanout remained disabled. Beat was absent.

The retained session volume was mounted as an external volume. The outbound
idempotency ledger used a separate disposable volume. Logout was disabled and
was never called.

Before the controlled marker was disclosed:

- Baileys socket-open events: `1`
- QR-generation events: `0`
- Bridge health connected: `true`
- API health: HTTP `200`
- Worker queues: `default` only
- Active, reserved, and scheduled tasks: `0`, `0`, `0`
- Database business rows: `0`
- Default queue and unacknowledged counts: `0`
- Outbound-ledger rows: `0`
- Inbound candidates, HTTP 202s, M1 executions, and sends: `0`

The temporary setup required a pre-message correction to the temporary
Compose override so the API health check used the `baileys_live` service name
and Baileys could start without a health-dependency cycle. This happened
before the zero-state baseline and did not change repository files or create
message side effects.

## Sanitized marker and observed cardinalities

- Marker SHA-256:
  `ba1f6dd9f880dd8aecae291127b46283373768a20f0e5573e1359db0f7ee5151`
- Marker plaintext retained in evidence: no
- User delivery confirmation: `RECEIVED`
- User clarification: the marker was submitted twice

| Measure | Required | Observed |
|---|---:|---:|
| Exact marker candidates | 1 | 2 |
| HTTP 202 responses | 1 | 2 |
| Effective M1 executions | 1 | 2 |
| Inbound rows | 1 | 2 |
| Fallback outbound rows | 1 | 2 |
| Baileys send attempts | 1 | 2 |
| Successful send results | 1 | 2 |
| Matching sent-ledger rows | 1 | 2 |
| Inbound duplicate-ID groups | 0 | 0 |
| Inbound duplicate excess rows | 0 | 0 |
| Ledger replays | 0 | 0 |
| Inconclusive sends | 0 | 0 |
| Adapter failures | 0 | 0 |
| Forward failures | 0 | 0 |

The two sends used two unique outbound ledger key hashes. Both ledger records
were in `sent` state; `unknown` and `in_progress` counts were `0`. The adapter
was configured for one attempt per task, so the observed total represents two
separate inbound submissions, not an automatic retry.

## Side-effect totals

- Customers created in the disposable database: `1`
- Conversations created in the disposable database: `1`
- Leads: `0`
- Relances: `0`
- MAPS tags: `0`
- Orders: `0`
- Payments: `0`
- Escalation tickets: `0`
- CRM effects: `0`
- Payment effects: `0`
- Relance effects: `0`
- MAPS effects: `0`
- Scheduled effects: `0`
- Default queue depth after processing: `0`
- Unacknowledged task counts after processing: `0`
- Blackout pending, processing, and quarantine counts: `0`

## Disposition and cleanup

Delivery of the local fallback was observed by the user, and the path produced
no failed, replayed, or inconclusive send. The required exactly-once controlled
trial nevertheless failed because the approved marker was submitted twice,
producing deterministic `2` cardinalities instead of `1`.

No automatic retry was attempted. The worker and Baileys were stopped as soon
as the mismatch was confirmed. The five-service project, its network,
disposable PostgreSQL/Redis/ledger volumes, locally built test images,
temporary secrets, marker plaintext, and runtime files were then removed.

- Phase 3 project containers remaining: `0`
- Phase 3 project networks remaining: `0`
- Phase 3 project volumes remaining: `0`
- Phase 3 locally built images remaining: `0`
- Retained Baileys session present and unmounted: yes
- Logout called: no
- Backup volume mounted or modified: no

## Claim boundary

This evidence proves successful delivery was observed for two distinct
submissions with no retries, replays, or inconclusive sends. It does not satisfy
or claim the requested exactly-once `1/1` messaging evidence. A new controlled
trial requires fresh explicit authorization and exactly one user submission.
It does not establish production readiness.
