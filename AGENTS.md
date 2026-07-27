# AGENTS.md

## Project Status

MBB-Project is fully stabilized for the validated local/codebase scope.
Recovery and local/codebase stabilization are complete. This is not a
production-ready, pilot-ready, publicly deployed, or general feature-readiness
claim.

The protected stabilization baseline is the annotated tag
`local-stabilized-v1` at
`cb39748deecf8ebe28c6ce3cded734754becbeb1`. Main may advance through
controlled work while that tag remains fixed. Do not reopen a validated
recovery path without a new reproducible defect or a new claim boundary that
requires matching validation.

This file contains durable repository rules. Step-specific scope, allowed files, and validation commands come from the current user prompt.

## Current Product-Development Goal

Proceed through controlled product development:

Preserve the stabilized foundation
-> define one MVP business journey
-> define contracts
-> implement one complete vertical capability
-> validate internally
-> prepare limited-user validation.

The current next phase is Step 23B: first MVP business journey definition.
Documentation alignment does not perform Step 23B.

## Working Principles

- Truth before polish.
- Small verified steps before large changes.
- Preserve existing working behavior when possible.
- Prefer minimal, reversible changes.
- Make the narrowest change that satisfies the current task.
- Deliver one measurable business capability at a time.
- Establish the business outcome before implementation.
- Prefer complete vertical slices over disconnected partial modules.
- Preserve the validated stabilization guarantees.
- Keep AI optional and subordinate to authoritative business rules.
- Keep external capabilities default-off unless explicitly authorized.
- Optimize only in response to measured or strongly evidenced constraints.
- Allow main to advance while keeping the stabilization tag fixed.

## Controlled Development Workflow

Use one safe Codex run when the task is simple. Separate product definition,
investigation, implementation, and external validation when risk or uncertainty
justifies it.

Evidence must match the claim being made. Product definition is not
implementation evidence, local validation is not public-deployment evidence,
and enabling any external side effect requires explicit authorization.

## Scope Control Rules

- Do not add features unless the user prompt explicitly authorizes them.
- Do not perform broad refactoring unless the task explicitly requires it.
- Do not change files outside the current task scope unless necessary to complete the task; explain the dependency before or while making the change.
- If the user prompt lists allowed files, do not edit outside that list unless the task cannot be completed without it. If editing outside the list is necessary, explain why in the final report.
- During inspection, audit, review, or planning tasks, do not modify files.
- During implementation tasks, touch only the files needed for the requested change.
- Do not force a full repository audit for small targeted tasks.

## Evidence And Truthfulness Rules

- Do not assume the project works unless it has been validated in this session or the user provides current evidence.
- Do not hallucinate missing files, flows, modules, APIs, tests, or runtime behavior.
- If something is unclear from the codebase, say: "unclear from the codebase."
- Use exact file paths when reporting problems.
- Separate facts, assumptions, risks, and recommendations.
- Mention uncertainty directly instead of filling gaps with guesses.

## Code-Change Rules

- Preserve existing interfaces and behavior unless the task explicitly asks to change them.
- Prefer targeted fixes over rewrites.
- Avoid changing formatting, naming, or structure in unrelated code.
- Keep configuration and secrets handling conservative; do not expose secrets in code, logs, or reports.
- When touching backend code, prefer structured logging over print statements.
- When touching customer-facing message logic, avoid English-only hardcoded user-facing strings unless the existing flow already requires them or the user asks for them.

## Git Attribution Rules

AI assistants may help inspect, design, edit, test, validate, stage or prepare
commits, but they must never be recorded as Git authors, Git committers,
co-authors, sign-offs, contributor trailers or generated-by identities.

All commits must use the repository owner’s approved human Git identity.
Do not add Co-authored-by, Generated-by, Assisted-by or similar AI attribution.

## Architecture And Module Boundaries

- Respect existing module boundaries.
- Avoid direct imports between major modules when the surrounding architecture expects explicit APIs, service layers, events, or Celery tasks.
- Preserve adapter boundaries for messaging, AI, CRM, payment, and other external integrations when touching those areas.
- Do not introduce a broad redesign without an approved product reason or measured operational evidence.
- Preserve the modular monolith unless evidence justifies extraction.
- Keep PostgreSQL authoritative for durable business data.
- Keep Redis temporary: cache, broker, queue, or coordination data must not silently become the authoritative business record.
- Use Celery selectively for work that benefits from asynchronous execution; do not move work to Celery by default.
- Keep Baileys as a messaging channel adapter, not the business core.

## DRC And Product Constraints

Treat these as product realities when they are relevant to touched code:

- Unstable power and intermittent recovery.
- Unstable 3G/4G and low bandwidth.
- WhatsApp-first users.
- Write operations should be idempotent when practical and relevant.
- External API calls should use bounded retries, timeouts, or circuit-breaker behavior when practical and relevant.
- Avoid heavy payloads, polling loops, and WebSockets unless the task explicitly justifies them.

## Validation Rules

- Validate changes before claiming success.
- Run only validation commands relevant to the task.
- Report validation commands and results honestly.
- If validation cannot be run, say why and list the remaining risk.
- Do not claim a flow works end-to-end unless it was actually exercised end-to-end.

## Required Final Report Format

For implementation tasks, final responses should include:

- Exact files changed.
- Summary of what changed.
- Validation command(s) run and result.
- Remaining risks or unknowns.
- Recommended next step.
- If no files were changed, say so clearly and list what was inspected or validated.

For inspection or review tasks, final responses should include:

- Exact files inspected.
- Findings with file-path evidence.
- Clear separation of facts, assumptions, risks, and recommendations.
- No claims of fixes unless changes were explicitly requested and made.

## Post-Stabilization Boundaries

- Do not claim production, pilot, public-deployment, or general feature readiness without matching proof.
- Do not reopen validated paths without a new reproducible defect or a new claim boundary.
- Do not add feature scope because it appears in old documentation or assistant files.
- Do not add speculative infrastructure or premature microservices.
- Do not allow external side effects without explicit authorization and applicable safety gates.
- Do not weaken safety gates, uniqueness, idempotency, escaping, authentication, or send-ledger guarantees.
- Do not move, delete, recreate, or replace `local-stabilized-v1`.
- Do not copy old production-ready, fixed-target, or broad build instructions into current work.
- Do not enforce GSD workflow commands or "never edit outside GSD" rules unless the user explicitly asks to use GSD.
- Do not apply CommonJS-only, `.cjs`-only, or Node-test-only rules from the GSD package to this repository as a whole.
- Do not remove Claude, GSD, Copilot, worktree, or old assistant files unless the user explicitly requests that cleanup.
- Do not add an AI identity as an author, committer, co-author, sign-off, contributor trailer, or generated-by identity.
