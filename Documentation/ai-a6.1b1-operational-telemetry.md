# AI-A6.1 operational telemetry

Local, optional observations of the AI critical path. `AI_OPS_ENABLED` defaults
to `false`. This stream never supplies business authority or replaces the
transactional AI audit. Acceptance, publication and worker observations complete
the existing AI critical-path stream. Lifecycle timing is derived from correlated
events only where unambiguous. No exporter, database store or Redis store is added.

## Wire contract

One JSON object per observation, rendered through structlog to existing stdout:

- `event` and `schema`: fixed `ai_ops.v1`.
- `observation`: `acceptance`, `publication`, `worker`, `turn`, `provider_call`, `provider_attempt`, `capability`,
  `persistence`, `grounding`, `fallback`, or `send`.
- `outcome`: bounded by `OUTCOMES` in `backend/app/ai/ops.py`. Spans emit `started`
  and a final observation; fallback emits only `selected`. Acceptance is a point
  observation: `accepted`, `duplicate`, `rate_limited`, or `unconfirmed`.
- `observed_at`: UTC ISO 8601 timestamp of observation, including `+00:00`.
- `duration_ms`: local monotonic duration on final span observations; otherwise
  null. Invalid or unavailable durations remain unknown.
- `reason`: a value from `REASONS` in `ops.py`, or null. No exception strings,
  diagnostic objects or arbitrary type names are emitted.
- Optional existing UUIDs: `task_id`, `turn_id`, `source_message_id`,
  `outbound_message_id`. Only UUID objects or parseable UUID strings survive.
- Optional bounded fields: `provider` (`deepseek`, `claude`, `disabled`, or
  `unknown`), `capability` (the four production registry names or `unknown`),
  `boundary` (`ordinary`, `terminal`, `handoff`, `draft_reply`, or `unknown`), `finish_reason`
  (the normalized provider enum or `unknown`), and `turn_outcome`
  (`response_generated`, `fallback_used`, `handoff_requested`,
  `order_draft_presented`, or `unknown`). `turn_outcome` describes the candidate
  audit outcome; only a committed persistence observation establishes that it
  was observed committing.
- Nonnegative integer fields, when observed: `provider_calls`,
  `provider_attempts`, `logical_capabilities`, `persistence_attempts`,
  `tool_rounds`, `provider_call_index`, `capability_index`, `attempt_index`,
  `returned_tool_calls`, `task_retries`. Integers are capped at signed 64-bit range; invalid
  values are discarded, never clamped to zero.
- Provider call/attempt observations always contain nullable `input_tokens`,
  `output_tokens`, `total_tokens`, `cache_hit_tokens`, `cache_miss_tokens`, and
  `reasoning_tokens`. Missing usage is null; observed zero remains zero.
- `route` is `broker`, `blackout`, `blackout_replay`, or `unknown`.
  `redelivered` is an observed boolean, omitted when unavailable.
  `worker_status` is the fixed allowlist of M1 return statuses in `ops.py`;
  `worker_send_result` projects the existing returned send status to
  `confirmed`, `skipped`, or `uncertain` (omitted for persistence failure).
  Neither implies that a send helper was invoked; only a send span measures it.
  `draft_state` is one of the six existing draft-reply states, observed only
  after commit. Unrecognized enum values become `unknown`.
- Persistence always includes nullable `transaction_outcome`: `committed` or
  `rolled_back` only after that operation returns successfully. A failed or
  interrupted commit without confirmed rollback leaves this null.
- Send always includes `send_result`: null at start; `confirmed`, `skipped`,
  or `uncertain` at finish. Cancellation/interruption remains visible in
  `outcome` and has `send_result=uncertain`.

All other input keys are discarded. No metrics or metric labels are introduced.
Arbitrary configured model names are omitted. IDs are correlation fields only.

## Boundaries and meanings

| Boundary | Timing and final outcomes |
| --- | --- |
| Acceptance | Emitted after the existing acceptance marker call on the broker/blackout paths. Duplicate and rate-limit exits are explicit. An exhausted path is `unconfirmed`: a broker error does not prove rejection. Customer timestamps are never acceptance timestamps. |
| Publication | Surrounds only existing `send_task` calls at ingress and blackout replay. `confirmed` means the call returned; an exception is `unconfirmed`, with cancellation/interruption distinct. No task arguments, headers, retry or acknowledgement behavior change. |
| Worker | Surrounds the complete async M1 processing body, from entry through its return or exception, including persistence and send. `worker_status` describes the business result; `succeeded` means the function returned, not that business work succeeded. A Celery Retry remains a retry, never a completed lifecycle. |
| Draft reply persistence | Existing reply handling plus conditional commit. `not_applicable` means no draft reply was recognized and no commit is claimed. Stale/failure returns follow their existing rollback. Successful draft state is emitted only after commit. |
| `generate_finalized` | Entire service invocation, including setup, guards and terminal persistence. `response_generated`, `handoff_requested`, `order_draft_presented`, `failed`, `rejected`, `stale`, `cancelled`, or `interrupted`. Terminal success follows its commit; ordinary generation precedes persistence. |
| Provider call | One invocation of the configured adapter, after request validation. Includes any adapter retry delay. `succeeded`, `failed`, `cancelled`, or `interrupted`. |
| DeepSeek attempt | One transport invocation through response normalization; request construction excluded. Normalized usage is available only after successful parsing. |
| Claude attempt | One SDK `messages.create` invocation, excluding retry sleeps. SDK input/output usage is observed without changing the legacy bridge result. The existing four-attempt loop and delays remain unchanged. Circuit rejection emits no attempt. |
| Logical capability | One authorized loop dispatch, including denial/failure. One span encloses all terminal transaction retries. Finalizer proposals and remaining unexecuted calls are excluded. `succeeded`, `denied`, `failed`, `stale`, `cancelled`, or `interrupted`. |
| Terminal persistence | One existing transaction attempt, including capability work, audit and commit/rollback. `retry` only when another attempt will follow; exhausted retries report `rolled_back`. |
| Ordinary outbound persistence | `_persist_outbound`, including eligibility checks, state/message/audit writes and commit/rollback. `committed`, `rolled_back`, `stale`, `failed`, `cancelled`, or `interrupted`. |
| Grounding | Existing validation calls only: `passed` or `rejected`, with unexpected failure/interruption reported separately. It is the existing commercial-price guard, not a general factuality score. |
| Fallback | `selected` at M1's existing execution-error branch; candidate `fallback_used` subsequently travels on ordinary persistence. Selection does not imply commit or send. |
| Send | Entire ordinary/handoff helper, including safety gates and handoff verification. A nonempty provider acknowledgment yields `confirmed`; this can be a ledger replay and is not recipient delivery/read confirmation. Empty IDs/errors are `uncertain`; existing safety gates are `skipped`. |

Turn counters count observed boundary entries, independent of orchestration
budget counters. They are not used to enforce limits. Provider-call ordinals
and capability ordinals are scoped to a turn; attempt ordinals to an adapter
call. Terminal persistence inherits its logical capability ordinal. Ordinary
persistence links the turn UUID to the outbound UUID; send joins on that
outbound UUID. No new durable identifier is created.

Provider-call usage can duplicate attempt usage: never sum both streams. Do
not infer totals from partial usage or missing attempts. Nested durations are
not additive. Missing observations mean incomplete telemetry, not proof that
an action was absent. A process killed without unwinding may leave only a
start observation (or no observation).

## Correlation and lifecycle calculations

Worker observations carry the existing Celery task UUID, retry count and
redelivery flag when available. That context flows through the turn, providers,
capabilities, persistence and send; no new identifier is generated. The inbound
message UUID joins acceptance to publication; the publication result carries the
existing task UUID. Conversation/customer IDs are omitted from this minimal
stream. Never group metric labels by any correlation ID, timestamp or ordinal.

For a uniquely matched original publication and worker start, compute
`worker.started.observed_at - publication.started.observed_at`. This includes
the publication operation; it is not exact broker residence time. Publication
completion can precede or follow worker start. Match the publication start/end
by inbound UUID and route only if unambiguous, then use its returned task UUID.
The separate monotonic publication duration describes the local publish call.

For a uniquely matched accepted inbound and terminal worker completion, compute
`worker.finished.observed_at - acceptance.observed_at`. This includes blackout
waiting where acceptance preceded replay. Acceptance follows publication in the
existing API, so it may even follow a fast worker; do not reorder business work.
Send-completion latency can similarly use the correlated send's finish time.
Retries, duplicate deliveries, missing events, ambiguous matches, clock skew or
negative deltas leave those measurements unknown, never zero. The original
publication is not the publication of a subsequent Celery retry. Preserve all
execution observations rather than selecting one arbitrarily.

These are log analysis definitions, not runtime correlation state or business
inputs. Export/retention activation and an operational collector are outside
this slice. No exact broker residence/retry-publication latency, costs, delivery
or read receipts, content quality, commercial conversion/revenue, human response
latency, SQL/cache spans or ancillary MAPS timing is claimed.

## Privacy and failure isolation

The dedicated renderer has only `JSONRenderer`; it does not inherit structlog
contextvars, general-log processors, exception rendering or bound customer
context. The allowlist excludes prompts, customer text, reasoning, tool
arguments/results, commercial contents, credentials, URLs, provider message
IDs and model names. Existing general logs are outside this stream's privacy
claim and must not be bulk-exported as `ai_ops.v1` evidence.

Repository logging uses synchronous `PrintLogger`; a stdout write may block.
A lazy per-process standard-library `QueueListener` therefore consumes a
`Queue(maxsize=256)`. The caller uses `put_nowait`; full queues drop records.
The listener reuses structlog/PrintLogger with a private stream wrapper so a
blocked telemetry write does not hold the general logger's PrintLogger lock.
The sink is not registered with stdlib logging shutdown (which can wait for a
handler lock). It catches sink failures without recursive logging. Forked
children reset queue/listener/context state. No
custom scheduler, exporter, retry loop, background event loop, shutdown flush
or durable queue exists. Buffered records can be lost on exit.

Projection, settings, clocks, enqueue and emitter failures are contained inside
the observation wrapper. Its context manager never suppresses business
exceptions, including cancellation. No telemetry I/O is awaited, and no
telemetry result controls a commit, rollback, retry, ownership decision or send.

Focused offline tests cover content sentinels, rendering, blocked/full/broken
sinks, context isolation, provider cancellation/retries, transaction retries,
commit/rollback, send confirmation and enabled/disabled behavior equivalence.
PostgreSQL integration evidence still requires the existing disposable-database
test gates; skipped tests do not establish database behavior.

## Original implementation validation (2026-09-20)

From `backend`, focused validation passed **81 tests**:

```text
python -m pytest -q -p no:cacheprovider --tb=short tests/test_ai_operational_telemetry.py
```

The final affected regression selection passed **2,478 tests**, with four
explicit deselections:

```text
python -m pytest -q -p no:cacheprovider --tb=short tests/test_ai_operational_telemetry.py tests/test_ai_turn_service.py tests/test_deepseek_adapter.py tests/test_m1_outbound_idempotency.py tests/test_order_drafts.py tests/test_ai_provider_contract.py tests/test_ai_capabilities.py tests/test_ai_product_capabilities.py tests/test_ai_audit.py tests/test_ai_commercial_grounding.py tests/test_ai4d_continuity.py tests/test_ai4e_contracts.py tests/test_m1_maps_fanout_gate.py tests/test_baileys_outbound_idempotency.py --deselect=tests/test_ai_audit.py::test_migration_is_linear_additive_seed_free_and_reversible --deselect=tests/test_ai4d_continuity.py::test_goal_budget_and_need_continuity_prevents_repeated_clarification --deselect=tests/test_ai4d_continuity.py::test_explicit_budget_replacement_causes_constrained_research_and_latest_wins --deselect=tests/test_ai4d_continuity.py::test_vague_price_objection_changes_concern_without_inventing_hard_budget
```

All four were first observed failing in the broader run and reproduced by
loading `app.ai.turn` from baseline
`bf8d31f02daffd9e7cfc310e3286dc8ea9c2491f` into a separate test process using
`git show`, without replacing working files. The migration assertion expects
an older head; the three continuity fixtures fail commercial grounding. Those
tests, migrations and grounding behavior were not changed for telemetry.

The following command collected **63 skipped tests** because the existing
`AI1D_TEST_DATABASE_URL`, `AI1E_TEST_DATABASE_URL`, `AI4D_TEST_DATABASE_URL`,
`AI4E_TEST_DATABASE_URL`, and `AI6B_TEST_DATABASE_URL` gates were unset:

```text
python -m pytest -q -p no:cacheprovider -rs tests/test_ai_runtime_audit_postgres.py tests/test_ai_audit_postgres.py tests/test_ai_handoff_postgres.py tests/test_ai4d_continuity_postgres.py tests/test_ai4e_postgres.py tests/test_order_drafts_postgres.py
```

From the repository root, Ruff checks and whitespace validation passed:

```text
python -m ruff check backend/app/ai/ops.py backend/app/ai/turn.py backend/app/tasks/m1.py backend/app/adapters/ai/claude_adapter.py backend/app/adapters/ai/deepseek_adapter.py backend/app/config.py backend/tests/test_ai_operational_telemetry.py
python -m ruff format --check backend/app/ai/ops.py backend/tests/test_ai_operational_telemetry.py
git diff --check
```

No live provider calls, external exports, runtime activation or production
validation were performed. At that commit, the ingress/worker boundary remained
unimplemented; the reconciliation below closes that gap.


## AI-A6.1B reconciliation (2026-09-21)

The approved design baseline is `bf8d31f02daffd9e7cfc310e3286dc8ea9c2491f`.
The existing implementation is `5fd3b55ded56e0627185fbbe21a2290d248a26ef`.
Inspection found its provider/capability/AI-persistence/send mechanism already
implemented; the original 81 telemetry tests passed unchanged in this session.
The concrete gaps were acceptance/publication/worker correlation, draft-reply
and non-AI early-return persistence coverage, and an unnecessary conversation
UUID in the minimal identifier allowlist. The corrective change extends that
same observation/queue mechanism; it does not replace it. It does not change
provider adapters, AI orchestration, configuration defaults, audit or schemas.

Focused validation from `backend`:

```text
python -m pytest -q -p no:cacheprovider --tb=short tests/test_ai_operational_telemetry.py
# Final telemetry suite: 111 passed.

python -m pytest -q -p no:cacheprovider --tb=short tests/test_ai_operational_telemetry.py tests/test_ai_turn_service.py tests/test_deepseek_adapter.py tests/test_ai_provider_contract.py tests/test_m1_outbound_idempotency.py
# 277 passed, including the then-110 telemetry tests. One additional
# worker-to-provider correlation test was subsequently added and passed in
# the final 111-test telemetry suite above; no application code changed.

python -m pytest -q -p no:cacheprovider --tb=short tests/test_inbound_publication_failure.py
# 17 passed, separate process because this suite replaces the Celery module.

python -m pytest -q -p no:cacheprovider --tb=short tests/test_blackout_claim_ack.py
# 14 passed, separate process.
```

A new disposable PostgreSQL 17 cluster was initialized under the host temporary
directory, bound only to loopback on a dynamically selected free port. Its only
database was `measurement_test`, migrated with `python -m alembic upgrade head`.
The four explicit test URL gates below pointed exclusively to that database;
no existing application database or Redis instance was used.

```text
AI1E_TEST_DATABASE_URL=postgresql+asyncpg://measurement_test@127.0.0.1:<temporary-port>/measurement_test
AI4D_TEST_DATABASE_URL=<same disposable URL>
AI4E_TEST_DATABASE_URL=<same disposable URL>
AI6B_TEST_DATABASE_URL=<same disposable URL>
AI_OPS_ENABLED=true
python -m pytest -q -p no:cacheprovider --tb=short tests/test_ai_runtime_audit_postgres.py tests/test_ai4d_continuity_postgres.py tests/test_ai4e_postgres.py tests/test_order_drafts_postgres.py
# 55 passed, zero skipped.
```

The same 55 PostgreSQL tests were also run in a fresh Python process with
`app.ai.ops.emit` replaced by a function that always raises a synthetic
`RuntimeError`, using `pytest.main` with the same arguments and disposable URL
gates. This exercises real transactional behavior while every observation
fails: **55 passed, zero skipped**.

From repository root, Ruff checks on all telemetry implementation files,
including `backend/app/api/v1/messages.py`, passed. Ruff formatting checks for
`backend/app/ai/ops.py` and `backend/tests/test_ai_operational_telemetry.py`, and
`git diff --check`, passed.

The tests use scripted provider and messaging transports. Allowlist/privacy,
nullable versus zero usage, bounded/full/blocked/broken sink behavior,
settings/projection/clock/emitter failures, cancellation/retry propagation,
stale/grounding/fallback outcomes, terminal commit/rollback, and send semantics
are covered. New observations preserve existing publication kwargs and
publish-before-ack order. No telemetry SQL/Redis persistence, metric labels,
exporter, live provider calls, send activation or push was introduced.

This is local/codebase measurement validation, not production deployment,
collector/retention validation, or production/pilot readiness evidence.
