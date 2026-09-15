# BCA-2 Slice 3 — React HTTPS serving validation

Validated locally on 2026-09-15, including recovery of an interrupted run.
Baseline: `fd6d5147e38466cf23bf4e26b1b65364935f8419`.
This report records local serving and workflow evidence, not production,
public deployment, pilot readiness, or authorization to retire Streamlit.

## Serving architecture

`browser → HTTPS Nginx → React at / + FastAPI at /api/*`

The existing Nginx service uses `nginx/Dockerfile`. Its build stage runs
`npm ci` against `frontend/package-lock.json`, then `npm run build`. Only the
compiled `dist` is copied into the Nginx runtime; Node/npm are absent there.
`nginx/react.conf` supplies the shared static-file and SPA navigation rules.
API and health prefixes outrank file-extension matching. Missing assets,
file-like paths, `/api`, and hidden files cannot reach the SPA fallback.
The API rate limit remains in effect, scoped to API traffic.

Production Compose pins the browser origin to `https://api.mbb.cd`, matching
the application TLS virtual host. The frontend retains relative API URLs.
Browser auth enablement and its secrets remain explicit environment settings;
no backend auth, cookie, CSRF, or external-send contract was changed.
Streamlit's service, HTTP proxy route, and separate TLS virtual host remain.

## Recovery and evidence retained

HEAD, the local `origin/main` reference, and remote `refs/heads/main` all matched
the baseline on recovery. There were six modified tracked files, three new
Nginx files, no staged files, and no intervening commits. All were preserved.
The four `mbb-slice3-*` containers and their two networks were recovered intact.
No project-labelled volumes were listed. Synthetic PostgreSQL/Redis state was
preserved in the existing containers/storage.

Before interruption, typecheck, lint, the frontend build, and all 149 tests in
14 files had passed. Higher-concurrency test attempts had intermittent timeout
and focus failures; the successful command used one worker. No frontend source,
lockfile, package manifest, or test configuration changed on resumption.
Nginx logs and database state confirmed that the selected conversation already
had human ownership and an accepted reply. Resumption did not reseed or repeat
that reply; it completed notes, escalation, refresh, and routing checks.

## Runtime proof

The standalone, ignored `.tmp/slice3/compose.yml` does not load the repository
`.env`. Only Nginx, FastAPI, PostgreSQL, and Redis run. Application/data services
use an internal-only network; Nginx also has ingress with loopback ports 18443
(HTTPS) and 18083 (HTTP). The repository TLS site configuration is mounted
unchanged, with a temporary localhost certificate in its expected certificate
path. Browser auth uses the exact local origin `https://localhost:18443`.
Chromium accepts this test certificate; public certificate trust is not proven.

| Check | Observed result |
| --- | --- |
| HTTPS root | React bundle loads and navigates to login |
| Browser authentication | Login reaches the real Inbox; Secure, HttpOnly, host-only, SameSite=Lax cookie; no local/session storage |
| Direct navigation and refresh | Conversation page loads and reconstructs the authenticated session |
| CSRF | Invalid-token note mutation returns API 403; zero invalid-note writes |
| Ownership | Authenticated human takes control; AI is paused; state survives reload |
| Reply | API returns 202 and `delivery_state=accepted`; persisted and visible after reload |
| Internal note | Created through the visible Internal Note label and real backend; persisted after reload |
| Escalation | Created through the React form; open escalation visible after reload |
| API misses | `/api/v1/missing`, `/api/v1/missing.js`, `/api/assets/missing.css`: JSON 404 |
| Static/security misses | `/api`, `/assets`, `/assets/missing.js`, `/assets/noextension`, `/missing.css`, `/favicon.ico`, `/.env`, `/metrics`, `/metrics/missing.js`: non-SPA 404 |
| Health | `/nginx-health`: 200; `/health`: backend 503 for deliberately absent Baileys bridge, never SPA HTML |
| Browser errors | No JavaScript page errors |

The recovered conversation had exactly one accepted reply, one internal note,
and one escalation after completion. Local evidence remains in
`.tmp/slice3/browser-results.json`, `.tmp/slice3/workflow.png`, the temporary
browser harnesses, and the stopped containers' logs/data. These are local
validation artifacts, not committed credentials or fixtures.

## Commands and results

- `npm ci --no-audit --no-fund` in `frontend`: passed.
- `npm run typecheck` and `npm run lint`: passed.
- `npm.cmd run test:run -- --maxWorkers=1`: 149/149 passed.
- `npm.cmd run build`: passed on local Node 22.14.0.
- `docker build -t mbb-slice3-api backend`: passed.
- `docker build -f nginx/Dockerfile --build-arg NODE_IMAGE=node:20-alpine -t mbb-slice3-nginx .`: passed using cached Node 20.20.2.
- `python .tmp/slice3/resume_browser.py`: all remaining browser assertions passed.
- `docker compose -f .tmp/slice3/compose.yml exec -T nginx nginx -t`: passed; existing `listen ... http2` deprecation warnings remain.
- `docker compose -f .tmp/slice3/compose.yml config --quiet`: passed.
- `docker compose -f docker-compose.yml config --quiet`: passed.
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config --quiet`: passed.
- `docker compose -f docker-compose.yml -f docker-compose.dev.yml config --quiet`: passed.

The default Node 22 Docker base could not be fetched because Docker Hub
authentication connections timed out. The cached Node 20 build reported a
development test-library engine warning. Its final JS/CSS SHA-256 hashes matched
the successful native Node 22 build byte-for-byte. A clean default Node 22 Docker
build remains to be checked when registry access is available; no dependency
versions or lockfile were changed to work around that environment limitation.

## Safety, cleanup, and next boundary

Runtime settings confirmed AI adapters disabled and WhatsApp sends, CRM sends,
payments, relance, scheduled tasks, and M1 fanout all false. No worker, Beat,
Baileys bridge, or provider service ran in this fixture. Existing manual reply
acceptance requires Baileys mode, so the fixture selects that mode while keeping
delivery disabled. Accepted/queued replies are not evidence of external delivery.
The existing production override's official mode does not support that manual
reply path; this slice does not change that pre-existing channel restriction.

`docker compose -f .tmp/slice3/compose.yml stop` stopped all four services with
exit code 0. Containers, networks, images, synthetic state, and ignored local
artifacts were retained for inspection; no valid work was discarded. The
protected `local-stabilized-v1` tag still resolves to
`cb39748deecf8ebe28c6ce3cded734754becbeb1`.

Complete Streamlit retirement is **not authorized**. Its removal needs a separate
approved scope and matching coverage of remaining uses. The immediate follow-up
is the default Node 22 image build once Docker registry access is restored.
