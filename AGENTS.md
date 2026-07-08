# AGENTS.md

## Project Status

MBB-Project is in recovery and stabilization mode. Do not describe the project as production-ready, pilot-ready, or feature-ready unless the user asks for an assessment and the claim is proven by current validation.

This file contains durable repository rules. Step-specific scope, allowed files, and validation commands come from the current user prompt.

## Current Recovery Goal

Prioritize stabilizing the runtime foundation and proving one clean MVP flow:

Inbound WhatsApp/Baileys message -> FastAPI backend -> validation/storage -> processing -> outbound response generated or selected -> adapter send-back -> dashboard reads conversation safely.

Stability comes before feature expansion.

## Working Principles

- Truth before polish.
- Small verified steps before large changes.
- Preserve existing working behavior when possible.
- Prefer minimal, reversible fixes during recovery.
- Make the narrowest change that satisfies the current task.

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

## Architecture And Module Boundaries

- Respect existing module boundaries.
- Avoid direct imports between major modules when the surrounding architecture expects explicit APIs, service layers, events, or Celery tasks.
- Preserve adapter boundaries for messaging, AI, CRM, payment, and other external integrations when touching those areas.
- Do not introduce broad redesigns or new architectural patterns during recovery unless the user explicitly requests them.

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

## What Not To Do During Recovery

- Do not claim production, pilot, or feature readiness without proof.
- Do not add feature scope because it appears in old documentation or assistant files.
- Do not copy old production-ready, fixed-target, or broad build instructions into current work.
- Do not enforce GSD workflow commands or "never edit outside GSD" rules unless the user explicitly asks to use GSD.
- Do not apply CommonJS-only, `.cjs`-only, or Node-test-only rules from the GSD package to this repository as a whole.
- Do not remove Claude, GSD, Copilot, worktree, or old assistant files unless the user explicitly requests that cleanup.
