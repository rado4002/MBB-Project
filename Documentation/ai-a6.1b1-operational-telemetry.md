# AI-A6.1B1 operational telemetry

Local, optional observations of the AI critical path. `AI_OPS_ENABLED` defaults
to `false`. This stream never supplies business authority or replaces the
transactional AI audit. Ingress, publication, worker and lifecycle timing remain
deferred to AI-A6.1B2. No exporter, database store or Redis store is added.

## Wire contract

One JSON object per observation, rendered through structlog to existing stdout:

- `event` and `schema`: fixed `ai_ops.v1`.
- `observation`: `turn`, `provider_call`, `provider_attempt`, `capability`,
  `persistence`, `grounding`, `fallback`, or `send`.
- `outcome`: bounded by `OUTCOMES` in `backend/app/ai/ops.py`. Spans emit `started`
  and a final observation; fallback emits only `selected`.
- `observed_at`: UTC ISO 8601 timestamp of observation, including `+00:00`.
- `duration_ms`: local monotonic duration on final span observations; otherwise
  null. Invalid or unavailable durations remain unknown.
- `reason`: a value from `REASONS` in `ops.py`, or null. No exception strings,
  diagnostic objects or arbitrary type names are emitted.
- Optional existing UUIDs: `turn_id`, `conversation_id`, `source_message_id`,
  `outbound_message_id`. Only UUID objects or parseable UUID strings survive.
- Optional bounded fields: `provider` (`deepseek`, `claude`, `disabled`, or
  `unknown`), `capability` (the four production registry names or `unknown`),
  `boundary` (`ordinary`, `terminal`, `handoff`, or `unknown`), `finish_reason`
  (the normalized provider enum or `unknown`), and `turn_outcome`
  (`response_generated`, `fallback_used`, `handoff_requested`,
  `order_draft_presented`, or `unknown`). `turn_outcome` describes the candidate
  audit outcome; only a committed persistence observation establishes that it
  was observed committing.
- Nonnegative integer fields, when observed: `provider_calls`,
  `provider_attempts`, `logical_capabilities`, `persistence_attempts`,
  `tool_rounds`, `provider_call_index`, `capability_index`, `attempt_index`,
  `returned_tool_calls`. Integers are capped at signed 64-bit range; invalid
  values are discarded, never clamped to zero.
- Provider call/attempt observations always contain nullable `input_tokens`,
  `output_tokens`, `total_tokens`, `cache_hit_tokens`, `cache_miss_tokens`, and
  `reasoning_tokens`. Missing usage is null; observed zero remains zero.
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
| `generate_finalized` | Entire service invocation, including setup, guards and terminal persistence. `response_generated`, `handoff_requested`, `order_draft_presented`, `failed`, `rejected`, `stale`, `cancelled`, or `interrupted`. Terminal success follows its commit; ordinary generation precedes persistence. |
| Provider call | One invocation of the configured adapter, after request validation. Includes any adapter retry delay. `succeeded`, `failed`, `cancelled`, or `interrupted`. |
| DeepSeek attempt | One transport invocation through response normalization; request construction excluded. Normalized usage is available only after successful parsing. |
| Claude attempt | One SDK `messages.create` invocation, excluding retry sleeps. SDK input/output usage is observed without changing the legacy bridge result. The existing four-attempt loop and delays remain unchanged. Circuit rejection emits no attempt. |
| Logical capability | One authorized loop dispatch, including denial/failure. One span encloses all terminal transaction retries. Finalizer proposals and remaining unexecuted calls are excluded. `succeeded`, `denied`, `failed`, `stale`, `cancelled`, or `interrupted`. |
| Terminal persistence | One existing transaction attempt, including capability work, audit and commit/rollback. `retry` only when another attempt will follow; exhausted retries report `rolled_back`. |
| Ordinary AI persistence | `_persist_outbound` with an AI audit record, including eligibility checks, state/message/audit writes and commit/rollback. `committed`, `rolled_back`, `stale`, `failed`, `cancelled`, or `interrupted`. |
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
start observation (or no observation). Complete lifecycle latency is deferred.

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

## Implementation validation (2026-09-20)

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
validation were performed. AI-A6.1B2 remains unimplemented.
