# Final Database Evidence

- Repository HEAD: `8284b0d6a9d369ca2e7ddbb756ba28abcf7a8fd2`
- Evidence UTC timestamp: `2026-07-27T05:58:38.765Z`
- Capture mode: read-only retained-database inspection
- Git state at capture start: clean

## Method

Only the repository PostgreSQL service was started, using the retained
`bot_postgres_data` volume. No Redis, API, worker, Beat, Baileys, dashboard,
Nginx, monitoring, or backup service was started.

The validation was executed through `psql` in an explicit read-only
transaction:

```text
BEGIN TRANSACTION READ ONLY;
SELECT version_num FROM alembic_version;
SELECT count(*) FROM each listed mbb table;
SELECT duplicate inbound WhatsApp ID groups and excess rows;
SELECT the canonical phone constraint definition;
SELECT aggregate valid/invalid customer phone counts;
ROLLBACK;
```

The PostgreSQL service was stopped immediately after the read-only inspection.
No insert, update, delete, DDL, migration, or persistent test-data operation
was executed.

## Results

- Alembic revision: `e2f3a4b5c6d7`
- Inbound duplicate WhatsApp ID groups: `0`
- Inbound duplicate excess rows: `0`
- Canonical phone constraint:
  `CHECK (((phone_number)::text ~ '^\+[1-9][0-9]{6,14}$'::text))`
- Existing customers checked: `34`
- Customers satisfying the constraint: `34`
- Customers violating the constraint: `0`
- Partial unique index
  `uq_messages_inbound_whatsapp_message_id`: present for nonblank inbound IDs

| Table | Aggregate count |
|---|---:|
| customers | 34 |
| conversations | 34 |
| messages | 108 |
| leads | 3 |
| relances | 0 |
| maps_tags | 50 |
| orders | 0 |
| payments | 0 |
| escalation_tickets | 2 |
| admin_audit_log | 0 |

## Cleanup and retained state

- PostgreSQL stopped after inspection: yes
- `bot_postgres_data` retained: yes
- `mbb_postgres_backup_pre_e164_20260727123728` mounted during capture: no
- Backup volume content inspected or modified: no
- Database row contents recorded: no
- Secrets or customer-controlled values recorded: no

## Claim boundary

This evidence proves the listed revision, aggregate counts, uniqueness
aggregates, and phone-constraint aggregates at the capture time. It does not
claim application readiness, database performance, or correctness of
uninspected row content. Git was clean before capture; the only later worktree
changes are the four requested evidence files.
