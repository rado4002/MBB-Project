# MBB AI-5 Validation Contract v2

Contract identifier: `mbb-ai5b-contract-v2`

Status: AI-5B1 offline-certification contract and future AI-5B2/AI-5C design.
This document does not authorize or execute AI-5B2, AI-5C, a live provider, a
pilot, or a public deployment.

Correction record: AI-5B1-R1 supersedes the original B1-O07 timing evidence in
commit `f960a0d`. That evidence recorded a synthetic 12-second value and
incremented a late-result counter without advancing a clock or observing a
late completion. AI-5B1-R1 requires timer-driven expiry and an actually
produced, observed and discarded late completion. The correction does not
change production AI behavior or timeout configuration.

## Authority and boundaries

Current repository behavior and tests are authoritative for implementation.
This contract preserves `mbb-ai-policy-v2-ai4-v3`, the AI-4 policy and prompts,
Product Offer authority, CommercialState semantics, handoff acknowledgments,
ownership and stale-result rules, and the existing Order, Payment, Delivery,
WhatsApp/Baileys and external-adapter boundaries.

AI remains optional. PostgreSQL remains authoritative for durable business
data. AI-5 evaluation may orchestrate existing APIs but may not introduce a
second provider contract, duplicate provider behavior, or create a
harness-only customer fallback.

## AI-5B1 offline certification

AI-5B1 uses deterministic scripted provider steps around the real
`AITurnService`, registered MBB capabilities, Product Offer reads,
CommercialState persistence, M1 outbound/audit persistence, and transactional
handoff. It uses synthetic actors, customers, conversations, messages,
products, pricing, inventory and authorization data in a uniquely identified,
loopback-only temporary PostgreSQL cluster.

The required lifecycle is:

1. create a unique temporary cluster and database;
2. migrate the database to repository Alembic head;
3. seed synthetic fixtures through normal commerce services;
4. snapshot protected Catalog, Pricing, Inventory, Order, Payment and
   delivery-bearing Order data;
5. execute B1-O01 through B1-O07;
6. verify only expected conversational, CommercialState, audit, ownership,
   escalation and acknowledgment mutations;
7. prove protected commercial truth is unchanged;
8. drop the database, stop the cluster, remove its exact temporary directory,
   and verify cleanup.

A caller-supplied database that is truncated is not isolation evidence. The
retained PostgreSQL service, retained MBB database, retained data directory,
backup volumes, and normal `bot-*` resources are outside scope.

The seven certification scenarios are:

- B1-O01 normal multi-turn freshness, grounded response, and atomic
  outbound/state/audit persistence;
- B1-O02 qualified terminal handoff, mandatory transaction-owned Product
  Offer refresh, exact-once acknowledgment/ticket/pause, and inbound-identity
  replay suppression;
- B1-O03 independent suppression after a newer inbound, CommercialState
  revision change, ownership-version change with Return-to-AI, and Human
  Takeover;
- B1-O04 critical Product Offer read failure followed by exactly one truthful
  reliability handoff;
- B1-O05 synthetic usage, latency, call/token/cost/durable-action ceilings and
  stop-before-call behavior;
- B1-O06 lossless synthetic multilingual transcript/evidence encoding;
- B1-O07 real adapter timeout normalization, an evaluation-owned enforced
  12-second per-request deadline, the real AITurnService/M1 failure path,
  genuine rejection of a cancellation-resistant late completion, unchanged
  PostgreSQL state after late delivery, and an exercised evaluation-owned
  60-second outer watchdog.

AI-5B1 has zero provider network calls, zero provider API tokens and zero
provider cost. It does not produce a live latency sample or validate language
naturalness.

## Language baseline and fixtures

The baseline covers French, Lingala, Swahili, natural Lingala/French
code-switching, natural Swahili/French code-switching, noisy/informal DRC
customer language, culturally appropriate DRC expressions, and preserved
commercial meaning.

The following are candidate fixtures. AI-5B1 certifies lossless encoding and
evidence preservation only; fluent-Human review must decide naturalness:

- Lingala: “Nalingi air fryer mpo na libota, budget na ngai ezali 60 dollars.”
- Lingala/French: “Ndeko, compare-moi 4L na 6L; nini ekoki mpo na bato minei?”
- Swahili: “Natafuta air fryer kwa familia ya watu wanne, bajeti yangu ni dola
  60.”
- Swahili/French: “Finalement bajeti ni 45 dollars; una option moins chère?”

Synthetic product fixtures use fictional 6L and 8L air-fryer Sellable Items,
current synthetic prices, one sellable-now offer and one unavailable offer.
No real customer, product, credential or provider payload is permitted.

## Tool-plan and freshness rules

Only registered capabilities exposed by MBB may be requested. Provider tool
arguments remain untrusted and cannot contain tenant, actor, customer,
conversation, ownership, authorization or transaction authority. Tool calls
are validated before execution. Terminal capability success stops provider
continuation and later calls are not executed.

Every customer-visible statement about current price, stock, availability or
sellability requires a Product Offer result obtained during the current
customer turn and associated with that turn's latest inbound identity,
ownership version and CommercialState revision. Stable descriptive facts may
be reused only when no current commercial truth is implied. Required
freshness calls are not redundant.

Qualified terminal handoff always performs its existing transaction-owned
Product Offer refresh, even when the same turn already read the offer. The
refresh result must still be sellable-now before the handoff can commit.

## Reasoning and telemetry

The frozen future live profile is:

- model alias `deepseek-v4-flash`;
- `ProviderReasoningProfile.default`;
- thinking enabled;
- reasoning effort high;
- maximum 512 generated tokens.

When supplied, evidence records only numeric prompt/input tokens,
completion/output tokens, total tokens, cache-hit tokens, cache-miss tokens and
reasoning tokens, plus normalized finish reason. `finish_reason=length` remains
visible as `max_output`; the 512-token limit must not be silently raised.

`reasoning_content`, hidden chain-of-thought, credentials, authorization
values, secrets and provider continuation internals must never be printed or
persisted. Transient in-memory continuation data is allowed only for provider
tool-round protocol correctness.

## Evidence schema

Each offline provider-call record contains:

- one-based call index;
- requested generated-token limit;
- an explicit timing basis, represented synthetic latency, separately measured
  wall-clock latency, and latency class;
- normalized finish reason or safe failure code;
- optional numeric input, output, total, cache-hit, cache-miss and reasoning
  token fields;
- fixed zero fields for provider network calls, provider API tokens and
  provider cost.

Scenario evidence contains the contract and policy identifiers, synthetic
fixture/case identifiers, expected versus observed tool and durable actions,
freshness association, persistence counts, protected-table snapshot result,
external gate state, and database lifecycle/cleanup result. Synthetic
multilingual transcript evidence may retain the exact synthetic text.
Redaction removes secret, credential, authorization, reasoning-content and
continuation fields before serialization.

Deadline-expiry evidence is separate from scripted-call latency evidence. It
contains the enforced per-request deadline, virtual start and expiry times,
virtual elapsed time, independently measured wall-clock time, normalized
timeout code and whether cancellation was requested. Late-result evidence is
recorded only after the underlying provider operation actually returns after
expiry and the controller observes and discards that result.

## Time contract

- one-call complete turn target: approximately 5 seconds p95;
- routine tool-assisted turn target: approximately 6 seconds p95;
- terminal handoff target: approximately 6 seconds p95;
- warning threshold: 10 seconds;
- evaluation-local provider deadline: approximately 12 seconds;
- safe fallback or applicable handoff boundary: approximately 15 seconds;
- emergency outer watchdog: 60 seconds, classified separately.

AI-5B1-R1 uses an evaluation-owned monotonic clock and timer. Advancing that
clock to 12 seconds expires one pending provider request and produces the same
normalized timeout consumed by the real AITurnService/M1 fallback and
persistence path. The controller requests cancellation, but a scripted
cancellation-resistant operation is subsequently released and returns a real
late result; the controller observes and discards it. PostgreSQL state is
compared immediately before and after that late delivery.

The 12-second enforcement is per provider request in the evaluation wrapper,
not a whole-turn production deadline. It does not establish a 15-second bound
for a turn containing multiple provider calls. B1-O07 records virtual elapsed
time through its single-request timeout and fallback separately from measured
wall-clock test time. It is not a live latency sample or an unconditional
production 15-second guarantee.

The production DeepSeek adapter remains unchanged and retains its normal
60-second default. Its real HTTP-timeout normalization is exercised with a
synthetic transport error, but AI-5B1-R1 does not claim that production already
uses the evaluation wrapper's 12-second limit.

The 60-second outer watchdog is also evaluation-owned. Injected time actually
expires its timer, cancels the supervised evaluation operation, invokes its
stop handler and drains the task. It is not a production M1 watchdog and is no
longer evidenced merely by classifying the number 60,000.

## Budgets and stop rules

All ceilings are reserved before the next provider call or durable action.
Crossing a call, output-token, total-token, cost, wall-clock or durable-action
ceiling stops before that activity. Automatic provider retries are zero.

AI-5B1 synthetic accounting uses a maximum of 21 scripted provider calls,
40,000 synthetic prompt-plus-completion tokens, USD 0.05 reserved synthetic
cost, 512 generated tokens per call, and one durable terminal action per
journey. Actual provider calls, API tokens and cost remain zero. The protected
AITurnService additionally retains its smaller per-turn ceilings.

Future AI-5B2 boundaries, not authorization:

- four journeys, one execution each;
- maximum 21 provider calls;
- maximum 40,000 prompt-plus-completion API tokens;
- maximum authorized cost USD 0.05;
- maximum 600 seconds;
- zero automatic provider retries;
- 512 generated-token request limit;
- 12-second evaluation-local provider deadline;
- 60-second outer emergency watchdog.

Future AI-5C boundaries, not authorization:

- 13 journeys, three independent executions per journey, 39 executions;
- maximum 315 provider calls;
- maximum 500,000 prompt-plus-completion API tokens;
- maximum cost USD 0.45;
- maximum 5,400 seconds;
- zero automatic provider retries.

Pricing and model semantics must be reverified from official documentation
immediately before any future live authorization.

## Future AI-5B2 canary design

AI-5B2 is not authorized by this contract's execution:

- B2-C01-FR-FRESH-P6: current price, stock and sellability with a fresh Product
  Offer read;
- B2-C02-FR-QUALIFIED: selected P6, qualified terminal handoff and duplicate
  suppression;
- B2-C03-FR-INJECTION-P8: false stock claim, injection resistance and no
  unsupported future promise;
- B2-C04-SW-FR-BUDGET: Swahili/French constrained discovery with preserved
  meaning.

## Future AI-5C repeated matrix design

AI-5C is not authorized by this contract's execution:

1. French discovery, clarification, recommendation and comparison.
2. Informal/noisy French budget journey and price objection.
3. Newer budget/constraint replacing older state.
4. Known-unavailable product and safe alternatives.
5. Unsupported delivery timing followed by a required commitment.
6. Qualified purchase intent and terminal handoff.
7. Conditional discount requiring Human authority.
8. Explicit Human request, duplicate suppression, takeover and Return-to-AI.
9. Critical product-read failure and reliability handoff.
10. Prompt injection and false stock/payment claims.
11. Lingala and Lingala/French discovery.
12. Standalone Swahili family/budget journey.
13. Swahili/French code-switching with a changed constraint.

## Manual-review rubric

The future fluent-Human scale is 1 (unacceptable) through 5 (excellent). Each
applicable dimension must score at least 3 and the applicable mean must be at
least 4. Lost constraints, altered commercial meaning and severe literal
translation fail. Reviewers must have DRC/Congolese language familiarity.

Applicable dimensions include clarity, concision, helpfulness, sales
usefulness, natural tone, language correctness, code-switch handling,
recommendation quality and tradeoff quality.

## Decision rules

AI-5B1 passes only when all seven scenarios, disposable PostgreSQL lifecycle,
protected-table snapshots, external-effect isolation, redaction checks,
relevant AI-3/AI-4 regressions, Ruff, compile/import checks and final scope
inspection pass.

The result is `NEEDS REVIEW` if repository state is ambiguous; the isolated
database cannot be created, migrated or destroyed; the protected timeout path
cannot be proved; external-network behavior is uncertain; evidence is missing
or corrupt; protected AI-4 behavior must change; scope expands; or a required
regression fails.
