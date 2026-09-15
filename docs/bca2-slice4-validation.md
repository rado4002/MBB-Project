# BCA-2 Slice 4 — retirement validation and recovery

Local validation and final certification on 2026-09-15. **Complete:** the
repository owner removed the residual generated cache tree and this workspace
verified `Test-Path -LiteralPath E:\Bot\dashboard` returns `False`. No
production, public deployment, pilot, or general readiness claim.

## Recovered state and preserved work

HEAD, local `origin/main`, and remote `refs/heads/main` matched
`0d7feead4e2b7adff3eca0c230f23f9e6b7d3dad`. There were 63 changed tracked
files, all unstaged, no staged changes, and no untracked nonignored files.
The entire retirement diff was preserved without reset, restore, or reseeding.
The protected `local-stabilized-v1` target remains
`cb39748deecf8ebe28c6ce3cded734754becbeb1`.

Docker Desktop was stopped. After starting it, the existing four
`mbb-slice4-*` containers, API/Nginx images, two networks, and anonymous
PostgreSQL/Redis volumes were recovered. Existing synthetic data was retained
through validation. `.tmp/slice4/` contained `browser.py`, `compose.yml`,
`conversation.json`, `credentials.json`, `seed.py`, and `workflow.png`.
There was no completed browser-results file or saved frontend/backend test log.
Consequently earlier test totals were not treated as independently recovered
proof; commands were rerun and their new results recorded.

The preserved changes remove the dashboard source, requirements, Dockerfile,
test, Compose service/dependencies/secrets, Nginx routing/upstream/rate zone,
Streamlit CORS origins and unused JWT issuer constant. CI builds React/Nginx
instead. Documentation distinguishes the current React operator workflow from
historical analytics UI claims. The alternative TLS template now includes
React rules with API prefixes protected from extension matching.

Resumption additionally corrects two stale README descriptions and removes
the obsolete Streamlit-specific `.gitignore` rules. No frontend application,
test, dependency, database schema, or business service was changed.

## Issue dispositions

### Missing API path 503

Recovered Nginx logs show `/api/v1/missing.js` rejected at
`2026-09-15T01:32:22Z`: `limiting requests, excess: 20.756 by zone "api"`.
The corresponding 503 has an empty upstream response time. This was the
preserved rate limit after the automated workflow burst, not SPA fallback or
an API routing defect. Validation allows the bucket to drain and spaces probes;
no production rate limit or routing safeguard was weakened.

`/api/v1/missing`, `/api/v1/missing.js`, and `/api/assets/missing.css` each
returned JSON 404 on resumption. `/api`, `/assets`, `/assets/missing.js`,
`/assets/noextension`, `/missing.css`, `/favicon.ico`, `/.env`, `/metrics`,
and `/metrics/missing.js` returned non-SPA 404.

### Project setup route mismatch

`backend/tests/test_project_setup.py:138` omits `operator_replies.*` from
its exact expected route set. `backend/app/tasks/celery_app.py` contains that
route to `default`, introduced in commit `5c78182` (manual operator replies).
Both files are identical to the Slice 4 baseline. Runtime comparison found
exactly this extra route, no missing routes, and no common queue mismatch.
This is a pre-existing setup-check defect; left unchanged as requested.

### Dashboard filesystem cleanup

All 29 tracked dashboard files are deleted in the recovered diff. At recovery,
only generated `.pytest_cache` and `__pycache__` trees remained, including
compiled application bytecode. `Get-ChildItem` could not enter `.pytest_cache`;
`Get-Acl` was unauthorized and `icacls` reported an invalid handle. The root
was verified as the ordinary directory `E:\Bot\dashboard`, without a link
target.
Automatic approval review rejected both a combined cleanup command and the
explicit native PowerShell `Remove-Item -LiteralPath E:\Bot\dashboard
-Recurse -Force` command, giving only `blocked by policy`.
Deletion was not bypassed through another tool. The repository owner then
removed the residual tree manually; final certification in this workspace
verified its absence with `Test-Path -LiteralPath E:\Bot\dashboard` returning
`False`.

## Runtime and contract evidence

The saved Nginx log proves successful login/Inbox, a reply POST 202, note POST
201, and escalation POST 201 before interruption. Resumed Chromium validation
uses the same account and conversation, without repeating those business writes.
It passed login, root bundle loading, direct conversation navigation and refresh,
Secure/HttpOnly/host-only/SameSite=Lax cookies, empty local/session storage,
invalid-CSRF API 403, recovered human ownership and visible persisted reply/note,
all routing checks above, and no JavaScript page errors (21 assertions).

Database inspection confirmed exactly one reply with delivery state `accepted`,
one internal note, and one escalation on the human-owned conversation.
This proves local acceptance/persistence, not external message delivery.

`/nginx-health` returns 200. `/health` returns backend 503 because the isolated
fixture intentionally has no Baileys bridge. This also prevents the recovered
Compose `service_healthy` dependency from starting Nginx automatically; the
existing Nginx container was started directly after identifying that reason.
No bridge was enabled and no health gate in supported configuration changed.

The fixture loads its own environment, not the repository `.env`. Only Nginx,
API, PostgreSQL, and Redis run; API/data use an internal network, and ingress
binds loopback ports 19443/19083. Runtime inspection confirms `AI_ADAPTER` and
`AI_TURN_PROVIDER` disabled, and WhatsApp sends, CRM sends, payment sends,
relance, scheduling, and M1 MAPS fanout false. No worker or Beat runs.

All backend changes are limited to `backend/app/main.py`,
`backend/app/security.py`, and the comment in
`backend/app/modules/m9_dashboard/__init__.py`. Analytics/audit/config services,
MAPS/relance/lead contracts, models and migrations retain baseline content.
No persistent project database or backup was modified by this validation.

## Validation commands

- `npm run typecheck`, `npm run lint`, `npm run build` in `frontend/`: passed
  on Node 22.14.0.
- `docker build -f nginx/Dockerfile -t mbb-slice4-nginx .`: passed using the
  declared default `node:22-alpine` build stage; existing layers were cached.
- Focused frontend run of `ResponsiveInbox.test.tsx` and
  `OperatorReplyComposer.test.tsx`: 28/28 passed. Full default-concurrency runs
  had variable asynchronous timing failures (146/149 and 144/149 passed).
  A first intended one-worker invocation used PowerShell's `npm` wrapper,
  which swallowed the worker flag; its 148/149 result is not a serial run.
- `npm.cmd run test:run -- --maxWorkers=1`: **149/149 passed in 14 files**.
  Together with the focused pass and unchanged frontend baseline, this supports
  pre-existing resource-sensitive test timing, not a Slice 4 regression. The
  default-concurrency limitation remains explicit; no test was weakened.
- `python -m pytest tests/test_browser_auth_api.py
  tests/test_browser_session_store.py tests/test_operator_conversation_reads.py
  tests/test_operator_ownership_api.py tests/test_operator_reply_api.py
  tests/test_internal_notes_api.py tests/test_operator_escalation_api.py -q`
  in `backend/`: **72 passed**, two python-jose datetime deprecation warnings.
- `python .tmp/slice4/resume_browser.py`: all 21 assertions passed.
- Compose `config --format json`, with explicit assertions for absent retired
  service/dependencies/runtime strings: base, base+dev, base+prod, recovery,
  recovery+routing, recovery+routing+worker+baileys-offline all passed.
  Recovery uses `.env.recovery-dryrun.example`. Rendering does not start services.
- `nginx -t` in temporary containers using the real HTTP, HTTPS, alternative
  HTTPS template (with `NGINX_SERVER_NAME=localhost` substitution), and recovery
  configuration: all passed. The existing HTTPS `listen ... http2` deprecation
  warning remains.
- `git diff --check`: passed.
- Final certification rerendered base, development, production, recovery,
  recovery+routing, and recovery with routing, worker, and baileys-offline
  profiles. All passed the absent-service/dependency/runtime-string assertions.
  Nginx configuration was unchanged after its prior real-config `nginx -t`
  passes, so it was not rerun after filesystem-only cleanup.

## Residual references

Remaining textual Streamlit references are historical: the thirteen design and
planning documents under `Documentation/` now carry an explicit supersession
notice; README labels the old stabilization observation as historical;
`RESILIENCE_CHECKLIST_PHASE1D.md`, `tests/security/audit_checklist.md`, and
`docs/bca2-slice3-validation.md` retain point-in-time evidence. The docstring in
`backend/alembic/versions/c7d8e9f0a1b2_create_materialized_views.py` records the
original purpose of shared views; the migration and views are preserved.
This report also names the retired technology as evidence.
`m9_dashboard` remains a shared backend namespace, not an application dependency;
Grafana dashboards are unrelated and retained.

## Temporary runtime cleanup

`docker compose -f .tmp/slice4/compose.yml down --volumes` removed all four
fixture containers, both networks, and the two mapped anonymous volumes.
Inspection by their recorded exact volume IDs confirms both are absent.
`docker image rm mbb-slice4-api:latest mbb-slice4-nginx:latest` removed the
fixture image tags/images; the older recovered Nginx image ID was already
absent at the final check. No `mbb-slice4-*` containers, networks, or images
remain. Shared PostgreSQL/Redis base images and unrelated project volumes remain.

The stopped legacy `bot-dashboard-1` container and the `bot-dashboard:latest`
and `mbb-recovery-dryrun-dashboard:latest` images were removed. No dashboard
containers or named images remain. No global Docker prune was performed.
Build cache was not globally purged.
Docker Desktop was stopped again after cleanup, restoring its initial stopped
state; the stop command completed successfully.

Ignored `.tmp/slice4/` harnesses, synthetic fixture configuration, logs, and
result JSON files remain as local evidence. The original files were preserved;
resumption added separate browser results and a separate screenshot. They are
not committed. The existing Slice 3 fixtures and certificate remain untouched.

## Completion gate

The filesystem cache tree is absent and final certification passed. The Slice 4
commit may proceed; no push is included in this work.

## Exact changed-file manifest

All pre-existing Slice 4 changes below were preserved. Resumption changes are
README clarifications, `.gitignore`, and this report. `D` means deleted tracked
content; `M` means modified.

- `M` `.env.example`
- `M` `.env.recovery-dryrun.example`
- `M` `.github/agents/mbb-assistant.agent.md`
- `M` `.github/workflows/ci.yml`
- `M` `.gitignore`
- `M` `Documentation/Architecture/High Level Design/1. Architecture Design Doc.md`
- `M` `Documentation/Architecture/High Level Design/2. Modeling & Technology Stack.md`
- `M` `Documentation/Architecture/High Level Design/3. Data Architecture & Interface Design.md`
- `M` `Documentation/Architecture/Low Level Design/1. Module Component Design.md`
- `M` `Documentation/Architecture/Low Level Design/2. Database Design.md`
- `M` `Documentation/Architecture/Low Level Design/3. API Detailed Specification.md`
- `M` `Documentation/Architecture/Low Level Design/4. Security Design.md`
- `M` `Documentation/Implementation Roadmap & Workflow.md`
- `M` `Documentation/Phase 1/Phase 1.A - Conversational Foundation.md`
- `M` `Documentation/Phase 1/Phase 1.D - Intelligence & Oversight.md`
- `M` `Documentation/Phase 1/Phase 1.E - Validation & Launch.md`
- `M` `Documentation/Project Definition & Foundation.md`
- `M` `Documentation/functional-and-non-functional-requirements.md`
- `M` `Makefile`
- `M` `README.md`
- `M` `backend/app/main.py`
- `M` `backend/app/modules/m9_dashboard/__init__.py`
- `M` `backend/app/security.py`
- `D` `dashboard/Dockerfile`
- `D` `dashboard/app/main.py`
- `D` `dashboard/app/pages/__init__.py`
- `D` `dashboard/app/pages/admin/__init__.py`
- `D` `dashboard/app/pages/admin/audit_log.py`
- `D` `dashboard/app/pages/admin/bot_config.py`
- `D` `dashboard/app/pages/admin/content_manager.py`
- `D` `dashboard/app/pages/admin/escalation_manager.py`
- `D` `dashboard/app/pages/admin/system_control.py`
- `D` `dashboard/app/pages/analytics/__init__.py`
- `D` `dashboard/app/pages/analytics/funnel.py`
- `D` `dashboard/app/pages/analytics/languages.py`
- `D` `dashboard/app/pages/analytics/maps_insights.py`
- `D` `dashboard/app/pages/analytics/relance.py`
- `D` `dashboard/app/pages/analytics/response_time.py`
- `D` `dashboard/app/pages/hub/__init__.py`
- `D` `dashboard/app/pages/hub/conversation_mirror.py`
- `D` `dashboard/app/pages/hub/escalation_response.py`
- `D` `dashboard/app/pages/hub/lead_operations.py`
- `D` `dashboard/app/pages/lab/__init__.py`
- `D` `dashboard/app/pages/lab/maps_tag_manager.py`
- `D` `dashboard/app/pages/lab/tone_audit.py`
- `D` `dashboard/app/utils/__init__.py`
- `D` `dashboard/app/utils/auth.py`
- `D` `dashboard/app/utils/db.py`
- `D` `dashboard/app/utils/export.py`
- `D` `dashboard/requirements.txt`
- `D` `dashboard/tests/test_conversation_mirror_safety.py`
- `M` `docker-compose.dev.yml`
- `M` `docker-compose.prod.yml`
- `M` `docker-compose.recovery-dryrun.yml`
- `M` `docker-compose.yml`
- `M` `docs/pilot_runbook.md`
- `M` `frontend/README.md`
- `M` `nginx/conf.d/mbb.conf`
- `M` `nginx/conf.d/mbb.recovery-dryrun.conf`
- `M` `nginx/conf.d/mbb.ssl.conf`
- `M` `nginx/conf.d/mbb.ssl.conf.prod`
- `M` `nginx/nginx.conf`
- `M` `nginx/nginx.recovery-dryrun.conf`
- `M` `tests/load/locustfile.py`
- `A` `docs/bca2-slice4-validation.md` (new, unstaged report)
