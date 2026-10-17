# AGENTS.md

MBB is a business-operating platform developed through coherent vertical
capabilities. Current capabilities and development status belong in `README.md`;
the current user prompt defines task scope and acceptance criteria.

## Scope and delivery

- Define the business outcome before implementation.
- Deliver one complete, measurable business capability at a time.
- Make the narrowest reversible change that satisfies the authorized task.
- Preserve existing interfaces and behavior unless the task authorizes changes.
- Keep inspection, review, and planning read-only.
- Respect allowed-file lists; explain necessary dependencies before changing scope.
- Do not add features, broad refactoring, or a full audit without a task need.
- Optimize only against measured or strongly evidenced constraints.

## Domain integrity

- Each domain owns its authoritative data, invariants, lifecycle, and operations.
- Cross-domain operations use explicit application/service contracts; do not bypass
  domain ownership or duplicate business truth.
- Products owns catalog and sellable identity.
- Stock owns quantities and inventory operations.
- Pricing owns prices and exchange-rate authority.
- Orders owns order acceptance and lifecycle.
- Compose domain facts through explicit read contracts such as Product Offer.
- Inbox and AI consume authorized contracts; neither becomes a separate source of
  commercial truth or bypasses conversation ownership.
- Keep AI optional and subordinate to authoritative business rules.
- Preserve adapter boundaries for messaging, AI, CRM, payments, and other providers.
- Keep Baileys a messaging channel adapter rather than the business core.
- Preserve the modular monolith unless evidence justifies a different architecture.
- Keep PostgreSQL authoritative for durable business data.
- Keep Redis temporary for caching, queues, and coordination.
- Use Celery selectively where asynchronous execution benefits the capability.

## Safety and resilience

- Preserve authentication, authorization, browser-session, Origin, CSRF, and
  reauthentication controls.
- Preserve validation, escaping, uniqueness, idempotency, audit, and send-ledger
  guarantees; retries must not create duplicate business effects.
- Keep external capabilities default-off; enabling external side effects requires
  explicit authorization and applicable safety gates.
- Protect secrets in configuration, code, logs, and reports.
- Prefer structured logging in backend changes.
- Preserve supported localization in customer-facing messages; avoid new
  English-only hardcoded responses unless the existing flow or task requires them.
- Design for WhatsApp-first users, low bandwidth, intermittent 3G/4G, and power loss.
- Make write operations idempotent where relevant and practical.
- Bound external calls with timeouts and appropriate retry or circuit-breaker rules.
- Avoid heavy payloads, polling loops, and WebSockets without task justification.

## Evidence and completion

- Verify claims against current repository evidence; distinguish facts, assumptions,
  risks, and recommendations. State "unclear from the codebase" when appropriate.
- Separate implementation, isolated tests, connected integration, and runtime proof.
- Require end-to-end integration evidence across the relevant domain contracts before
  declaring a business capability complete; working modules alone are insufficient.
- Exercise the claimed journey and relevant failure, authorization, and replay paths
  at the boundary being claimed.
- Run task-relevant validation and report commands, results, skips, and limitations.
- Do not infer production, pilot, public-deployment, or general feature readiness from
  local tests or historical evidence.
- Report exact files changed or inspected, verified findings, validation, remaining
  gaps, and a recommended next step. State clearly when no files changed.

## Protected baseline and Git

- Preserve the annotated `local-stabilized-v1` tag at
  `cb39748deecf8ebe28c6ce3cded734754becbeb1`; never move, delete, or recreate it.
- Allow `main` to advance through controlled work while the baseline remains fixed.
- Reopen validated stabilization paths only for a reproducible defect or a new claim
  boundary requiring matching validation.
- Use only the repository owner's approved human Git author and committer identity.
- Never add AI authors, committers, co-authors, sign-offs, contributor trailers, or
  generated-by attribution.
- Historical roadmaps and assistant files do not authorize scope or impose workflows.
- Apply GSD-specific workflow or language restrictions only when explicitly requested;
  do not apply package-local restrictions to the whole repository.
- Do not remove Claude, GSD, Copilot, worktree, or other assistant files without an
  explicit cleanup request.
