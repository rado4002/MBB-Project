# BCA-2 Slice 6 — Conversation authority consolidation

Validated locally on 2026-09-15 from clean `main` at
`ef6aabb163f0002526fdef691a6e2fa8e55c055b`, matching `origin/main`.
The protected annotated tag `local-stabilized-v1` remains at
`cb39748deecf8ebe28c6ce3cded734754becbeb1`.

## Authority model and inspection

PostgreSQL owner, ownership version and AI execution state are authoritative.
An AI owner may be eligible or paused, with no human assigned. A Human owner
identifies an operator account and requires paused AI. Every explicit ownership
transition increments the version. Lifecycle labels and escalation records
cannot assign an actor or resume AI.

The runtime trace covered these writers and readers:

| Paths | Responsibility and result |
| --- | --- |
| `backend/app/models/conversation.py` | Initial owner/version/execution defaults and exclusive-owner constraints; retained unchanged. |
| `backend/app/modules/m4_conversation/ownership.py` | Human Takeover and Return to AI, row lock, compare-and-set version, durable idempotency and operator audit; unchanged. Return still requires the owning operator or administrator and an eligible configured AI adapter. Disabled/unavailable adapters fail closed. |
| `backend/app/modules/m4_conversation/ai_handoff.py` | Atomic, versioned AI pause with attention ticket; never assigns a Human. Existing duplicate/replay behavior retained. |
| `backend/app/api/v1/operator_conversations.py`, `frontend/src/api/conversations.ts` | Supported authenticated ownership and operator escalation routes/read projections; frontend uses `/ownership` and `/escalations`, not the legacy toggle. |
| `backend/app/ai/turn.py`, `backend/app/ai/capabilities.py`, `backend/app/tasks/m1.py` | AI capability, commit and send authority rechecks; retained. Redis session history carries an ownership version but does not grant authority. |
| `backend/app/modules/m4_conversation/operator_replies.py`, `backend/app/tasks/operator_replies.py` | Human actor/version/execution checks at acceptance and send; retained. Existing lifecycle restrictions can deny a reply but cannot grant ownership. |
| `backend/app/modules/m7_conversion/order_drafts.py` | Authoritative AI ownership/version checks on draft preparation and confirmation; unchanged. |
| `backend/app/api/v1/conversations.py` | Legacy handoff removed; lifecycle status retained as typed metadata; legacy attention creation retained without authority changes. |
| `backend/app/modules/m8_maps/operator_escalation.py` | Operator attention creation, uniqueness, durable idempotency and audit; already independent of ownership, unchanged. |
| `backend/app/modules/m8_maps/escalation.py`, `backend/app/tasks/escalation.py` | Legacy ticket creation retained, with lifecycle writes removed. Resolution/assignment helpers remain for ticket records, with row locking and terminal-state guards. No registered runtime resolution/assignment caller was found after Streamlit retirement. |
| `backend/app/modules/m1_gateway/service.py` | Reuses the latest conversation without status filtering; creates only when the customer has no conversation. |
| `backend/app/modules/m6_relance/eligibility.py` | Existing dormant lifecycle exclusion inspected and retained. No relance redesign or readiness claim. |

## Legacy cleanup and retained semantics

Removed PUT `/api/v1/conversations/{conversation_id}/handoff`, its service
`handoff_conversation`, and `HandoffToggle`/`HandoffToggleResponse`. They treated
`escalated` as Human control and `active` as bot control without a versioned
transition. Searches found no supported frontend consumer of that route.
External consumers are unclear from the codebase; they must use the supported
authenticated operator ownership transition.

PUT `/status` remains for lifecycle metadata and validates `ConversationStatus`.
Its OpenAPI description explicitly separates it from takeover, pause and resume.
Unknown values return 422. Historical `escalated` values remain readable and
writable as labels; they do not assert current ticket state or Human ownership.
No historical rows are rewritten. List/filter behavior and existing lifecycle
restrictions remain; changing lifecycle never creates an execution owner.

Legacy ticket creation no longer writes `escalated`. Resolution only records
resolution metadata; it no longer writes `active`. Assignment names a ticket
reviewer, not a conversation owner. Resolution and assignment lock and refresh
the ticket row, rejecting resolved/closed tickets so a stale reader cannot
reopen or overwrite a completed record. Existing ticket constraints and the
one-active-ticket uniqueness guarantee remain intact.

Human Takeover may advance an AI handoff ticket to in-progress; explicit Return
to AI may close that ticket. Those remain consequences of an ownership
transition. The reverse implication does not exist: resolving a ticket does
not return control. A paused AI remains paused, including after resolution,
until the existing explicit takeover/return workflow authorizes execution.

M1 previously selected only active/qualifying/nurturing records. An escalated,
dormant or converted latest record could therefore be bypassed for an older
AI record or a freshly created default-AI conversation. M1 now uses latest
message time, creation time and UUID as deterministic tie-breakers across all
lifecycle labels. Its existing customer upsert serializes concurrent inbound
selection and first-conversation creation. It does not rewrite lifecycle or
ownership, merge historical conversations, or define a new journey-reset flow.

Voice-note behavior is preserved: eligible AI records get an attention ticket
and localized acknowledgment; human-owned/AI-paused records still persist and
attach inbound voice notes but suppress autonomous processing. Duplicate input
does not create a second ticket or acknowledgment. Ticket creation itself does
not pause AI or assign a Human.

## Files changed and schema impact

- `backend/app/api/v1/conversations.py`
- `backend/app/modules/m1_gateway/service.py`
- `backend/app/modules/m8_maps/__init__.py`
- `backend/app/modules/m8_maps/escalation.py`
- `backend/app/schemas/admin.py`
- `backend/app/schemas/common.py`
- `backend/app/tasks/escalation.py` (docstrings describing actual ticket semantics)
- `backend/tests/test_conversation_authority_postgres.py`
- `backend/tests/test_operator_escalation_postgres.py`
- `backend/tests/test_project_setup.py` (removed retired schema import)
- `backend/tests/test_schema_api_validation.py` (removed retired schema import)
- `docs/bca2-slice6-validation.md`

No database schema or migration change. The API removes the legacy handoff
schemas/route and narrows lifecycle input to the existing enum. No payment,
order implementation, relance, AI-system simplification or frontend change.

## Validation evidence

Docker Desktop was initially stopped. On starting it, all retained `bot-*`,
Slice 3 and `mbb-slice5-postgres` containers remained stopped; none auto-started.
A fresh `postgres:16-alpine` container, `mbb-slice6-postgres`, bound only to
`127.0.0.1:19546`, held synthetic database `slice6_acceptance`. Existing databases
and retained volumes were not used. The unmodified migrations passed through
`a3b4c5d6e7f8`, also confirmed from `mbb.alembic_version`.

Validation used the backend venv Python with `POSTGRES_HOST=127.0.0.1`,
`POSTGRES_PORT=19546`, synthetic user `slice6`, and database `slice6_acceptance`.
`E2_TEST_DATABASE_URL`, `AI1E_TEST_DATABASE_URL`, `AI4E_TEST_DATABASE_URL`, and
`AI6B_TEST_DATABASE_URL` all pointed to that disposable database. AI adapters
and AI turn provider were disabled; WhatsApp/CRM/payment send gates, relance,
scheduling and M1 MAPS fanout were false. Redis pointed to unused port 19547;
Celery used memory backends. No application service, worker or beat was started.
Tests that exercise enabled adapter branches replace adapters with local fakes
or fail-on-call guards; no real provider, CRM, payment or WhatsApp effects ran.

Commands below ran from `backend` with that isolated environment:

```powershell
./venv/Scripts/python.exe -m alembic upgrade head
./venv/Scripts/python.exe -X utf8 -m pytest tests/test_conversation_authority_postgres.py tests/test_conversation_ownership_postgres.py tests/test_operator_ownership_api.py tests/test_operator_escalation_postgres.py tests/test_operator_escalation_api.py tests/test_ai_handoff_postgres.py tests/test_ai4e_postgres.py tests/test_m1_outbound_idempotency.py tests/test_operator_reply_service.py tests/test_operator_reply_api.py tests/test_order_drafts.py tests/test_order_drafts_postgres.py tests/test_ai_evaluation.py tests/test_ai4_journey.py -q --tb=short --disable-warnings
./venv/Scripts/python.exe -X utf8 -m pytest tests/test_conversation_authority_postgres.py -k 'stale_ticket_reader or m1_voice_note' -q --tb=short --disable-warnings
./venv/Scripts/python.exe -X utf8 tests/test_schema_api_validation.py
./venv/Scripts/python.exe -X utf8 -c "import runpy; runpy.run_path('tests/conftest.py'); runpy.run_path('tests/test_project_setup.py')"
git diff --check
```

- Affected suite: **212 passed, 2 warnings**, 99.09 seconds.
- Five subsequently added stale-ticket/real-M1 voice tests: **5 passed,
  26 deselected, 2 warnings**, 5.89 seconds. Total distinct passing tests: **217**.
- Schema/API diagnostic: **all 12 checks passed**.
- Project setup diagnostic: only the already documented Slice 5 failure,
  `Celery task routes do not match current task names`; other checks passed.
  No Celery-route repair is claimed or included.
- `git diff --check`: passed.

The first focused run had 25 passes and one test-only error: an assertion read
an expired async ORM object after rollback. Moving the assertion before
rollback fixed it; the affected suite above includes the corrected test.

The 31 Slice 6 PostgreSQL cases prove all six lifecycle labels under each of
AI-eligible, AI-paused and Human ownership; stale versions fail closed; two
concurrent inbound messages reuse the intended latest record, even with an
older active AI record; first simultaneous inbound messages create one record;
ticket creation/assignment/resolution preserve authority and history;
concurrent operator escalation/takeover and resolution/return remain safe;
stale ticket readers cannot overwrite resolution; and real M1 voice-note
routing/replay preserves the authority gate. Existing tests additionally prove
takeover/return eligibility, audits, idempotency, stale commit/send rejection,
AI handoff behavior, and the connected AI-6 Product Offer → draft → exact
confirmation → replay → pending HTTP Order read journey.

The disposable Slice 6 container and its anonymous test volume were removed
after validation. Retained containers remained stopped and their data was
preserved. Docker Desktop was stopped again to restore its initial state.
The temporary validation environment script was removed before commit.

## Completion boundary

This is local/codebase validation only, not production, pilot or public
deployment evidence. The dedicated AI System Audit may begin as a separately
authorized next task; it was not started here. This Slice 6 work is committed
normally on `main` without pushing or moving the protected stabilization tag.
