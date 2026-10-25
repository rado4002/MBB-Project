# MBB ya Kin

MBB is a business-operating platform for the Democratic Republic of Congo,
combining catalog, pricing, stock, order acceptance, and customer conversations.
WhatsApp is a customer channel; the React workspace supports human operators
and administrators. Development proceeds through coherent vertical capabilities
that respect domain ownership and tolerate limited connectivity and power loss.

## Current capabilities

The following is an implementation inventory, not a claim that the full platform
has passed end-to-end validation.

| Area | Implemented | Boundary / remaining evidence |
| --- | --- | --- |
| Products and variants | Product-family browsing; administrator creation of inactive Products and first variants; lifecycle controls and commercial review; product images with variant overrides. Variants are Sellable Items with their own identity. | Image management uses HTTPS asset URLs, not an upload service. The isolated Products → Stock → Inbox journey is covered by a real-browser regression test. |
| Pricing | Current USD prices and history; manual/automatic USD-CDF exchange-rate authority; derived CDF quotes; Administrator workspace for manual rate review, update, history, and explicit return to manual mode. | Pricing owns monetary authority. Automatic refresh is default-off and unavailable in the workspace pending provider approval and credential validation. The ExchangeRate-API adapter uses the provider's documented Bearer-token request format. |
| Stock | Quantity-derived availability, unknown-count handling, administrator receive/adjust operations, movement history, stale-state checks, operation-key replay protection, atomic full-quantity allocation for paid single-line Orders, and one-time physical deduction at guarded dispatch; role-shaped Business Stock UI and search. | Operators see availability; administrator contracts expose exact quantities and maintenance. Allocation is an explicit application contract; only guarded dispatch consumes it. |
| Orders | Authoritative order drafts, exact customer confirmation/cancellation, changed-term reconfirmation, idempotent pending Order creation; browser-authenticated Orders workspace showing the accepted snapshot, verified Inbox association, Payment/allocation/delivery readiness, Administrator-only cancellation, preparation, moto-taxi dispatch, and customer-receipt confirmation; guarded, audited `confirmed → preparing → delivering → delivered` for fully paid and allocated single-line Orders. Operators and Administrators can record failed or disputed delivery issues against dispatched Orders; Administrators can close a documented false alarm or clear an issue when delivery continues. Open issues block delivery confirmation without changing Order, Payment, dispatch, delivery evidence, or Stock facts. | Pending acceptance does not establish payment, allocation, fulfillment, or delivery. Generic HTTP, service, and Celery lifecycle writes fail closed for guarded transitions; dispatch does not confirm customer receipt. Delivery semantics remain human-reviewed, and the recorded delivery time is MBB's message-receipt time rather than exact physical handoff time. Delivery Issue Reporting and Triage V1 does not return Stock, refund, reverse, exchange, notify, or add Order statuses. Orders with any Payment state require a future Payment-owned cancellation workflow. |
| Payments | Provider-independent creation of one durable, unverified pending Payment intent from an accepted single-line Order, with explicit method selection, immutable commercial and customer snapshots, and transaction-safe replay protection. | No provider initiation, provider-event acceptance, settlement verification, money movement, Order confirmation, or automatic Stock allocation is enabled. |
| Customers | Contextual masked profiles reached from an authorized Inbox conversation; Operator/Administrator viewing of name, city, preferred language, contactability facts, verified communication-channel evidence, and acquisition sources; Administrator-only audited edits with stale-write protection; one-way opt-out from verified inbound WhatsApp evidence, including pending internal follow-up cancellation. | Phone identity remains masked and immutable. Communication channel is distinct from acquisition source. Consent grant and opt-out reversal are disabled pending an approved evidence and revocation policy; no cross-channel identity merge or external action is performed. |
| Inbox | Conversation history, ownership/takeover and return-to-AI controls, replies, internal notes, escalation creation, read-only offer lookup, Relance state, and lazy read-only Customer Activity with bounded recent conversations and related Orders. | Reply acceptance is distinct from external delivery. Ownership and execution state remain authoritative across AI and human actions. Customer Activity masks phone identity, keeps communication channel distinct from acquisition source, and does not merge identities without an authoritative Customer relationship. |
| AI | Provider-neutral turns; DeepSeek and Claude adapters; product search/detail, order-draft and human-handoff capabilities; commercial grounding, durable recovery, stale-work guards, and evaluation tooling. | Optional and constrained by business contracts. `AI_ADAPTER` and `AI_TURN_PROVIDER` default to `disabled`; adapter implementation does not prove a live connection or current provider quality. |
| Relance | Durable V2 candidates, consent/template readiness, offline delivery contract, two-attempt cadence, stop/uncertainty handling, and Inbox projections. | V2 delivery rejects live transports and has no production delivery task or sender wiring. Legacy Relance code remains separate; its AI execution was retired. Live V2 follow-up is pending. |

Primary implementation: [Catalog](backend/app/modules/catalog/service.py),
[Pricing](backend/app/modules/pricing/service.py),
[Inventory](backend/app/modules/inventory/service.py),
[Order Stock allocation](backend/app/modules/m7_conversion/order_allocation.py),
[Payment intents](backend/app/modules/m7_conversion/payment_intents.py),
[Customer profiles](backend/app/modules/customer_profile/service.py),
[order drafts](backend/app/modules/m7_conversion/order_drafts.py),
[operator contracts](backend/app/api/v1/operator_conversations.py),
[AI capabilities](backend/app/ai/capabilities.py),
[Relance](backend/app/modules/m6_relance/delivery.py), and
[React routes](frontend/src/app/router.tsx).

## Architecture and ownership

MBB is a modular monolith: FastAPI and PostgreSQL provide the business backend;
Redis supports temporary state and queues; Celery handles selected asynchronous
work. Nginx serves the React build and same-origin API. Streamlit is retired.

Each domain owns its data, invariants, lifecycle, and operations. Cross-domain
work uses explicit application/service contracts. The read-only
[Product Offer](backend/app/modules/product_offer/service.py) contract composes
catalog identity, Pricing authority, and Stock availability for Inbox, AI, and
order preparation. Consumers must not invent or independently maintain those
facts. Baileys and other external integrations remain adapters.

## Evidence and development status

Recovery and stabilization are closed for the validated local/codebase scope.
The protected annotated tag `local-stabilized-v1` remains at
`cb39748deecf8ebe28c6ce3cded734754becbeb1`; `main` has advanced through product work.
This is not production, pilot, public-deployment, or general feature readiness.

Evidence is scoped to the revision and conditions in each report:

- [Local runtime evidence](docs/recovery/evidence/final_local_runtime_evidence.md)
  and [messaging evidence](docs/recovery/evidence/final_messaging_evidence.md)
  record historical isolated startup/recovery and controlled Baileys
  inbound-to-fallback delivery. They do not validate today's entire application
  or permanently approve the unofficial WhatsApp transport for production.
- [React HTTPS validation](docs/bca2-slice3-validation.md) records a local browser
  journey through authentication, ownership, accepted reply, note, and escalation.
- [Order acceptance validation](docs/bca2-slice5-validation.md) records connected
  M1 confirmation/replay through one pending Order and its read API, with zero
  Payments or external effects and unchanged inventory. It uses a scripted AI
  adapter, not live-provider inference.
- [Conversation authority validation](docs/bca2-slice6-validation.md) records local
  integration checks for ownership, handoff, and stale-authority protection.
- Current Products, Stock, offer-lookup, AI, and Relance test sources cover
  individual contracts and selected connected paths. Test presence alone is not
  a passing result; earlier reports do not certify later commits.
- The Products → Stock → Inbox regression runs the real React UI through HTTPS,
  FastAPI, and disposable PostgreSQL. It covers creation, price, lifecycle,
  stock transitions, offer lookup, role restrictions, stale-write rejection,
  Inbox Customer Activity navigation to an authoritative related Order, and the
  contextual Customer profile read/edit and evidence-backed opt-out path.

Payment, fulfillment, live Relance, and limited-user/public operation require
their own authorized scope and matching integration/runtime evidence.

## Local setup

Use Python 3.12 for the backend, Node 22 compatible with the frontend package
engines, and Docker with Compose v2 for isolated infrastructure. The checked-in
images use Python 3.12 and Node 22.

1. Create a local `.env` from [`.env.example`](.env.example) if one does not exist;
   retain the disabled AI/send/scheduling gates. Do not overwrite an existing setup.
2. Provision the secret files referenced by the chosen Compose services.
   [`scripts/init_secrets.sh`](scripts/init_secrets.sh) is an optional Bash helper
   that skips existing files, but creates placeholders and does not provision
   every current secret. Replace placeholders and supply missing files before
   startup. Never commit `.env`, credentials, or `secrets/` contents.
3. Keep the database name `mbb` when using the current
   [`scripts/init_db.sql`](scripts/init_db.sql). Use disposable data for validation;
   never reuse the retained stabilization backup as a test database.
4. Install backend development dependencies from `backend/`:

   ```powershell
   python -m venv venv
   .\venv\Scripts\python.exe -m pip install -r requirements-test.txt
   ```

5. Install and check the React frontend from `frontend/`:

   ```powershell
   npm ci
   npm run typecheck
   npm run lint
   npm run test:run -- --maxWorkers=1
   npm run build
   ```

Runtime startup is task-specific: select only required services from
[`docker-compose.yml`](docker-compose.yml) and the appropriate override. The
[development override](docker-compose.dev.yml) adds Baileys and exposes debug
ports; an unrestricted `up` also includes workers and Beat. Keep external effects
and scheduling disabled unless explicitly authorized. The
[production override](docker-compose.prod.yml) is configuration, not deployment
approval or public-runtime proof.

The supported authenticated browser path is **HTTPS Nginx → React + same-origin
FastAPI**. Nginx builds the frontend via `nginx/Dockerfile`; Node is not a runtime
service. Browser access requires explicit `BROWSER_AUTH_ENABLED`, an exact allowed
origin, independent session/CSRF/idempotency secrets, and provisioned accounts
through [`backend/scripts/operator_accounts.py`](backend/scripts/operator_accounts.py).
HTTP or `npm run dev` alone does not satisfy the Secure-cookie authentication
contract. See [frontend setup](frontend/README.md) for that contract; its historical
scope sections are not a complete current feature inventory.

## Validation and safe development

Follow [AGENTS.md](AGENTS.md) for durable engineering and Git rules. The current
approved task defines scope; historical roadmaps and design targets do not add
features or establish readiness.

Run focused tests for the changed contracts. Backend PostgreSQL suites require
their suite-specific disposable database variables; for example,
`AI2B_TEST_DATABASE_URL` enables the commerce browser API suites. Read fixtures
before running them and report skipped tests. Frontend tests use mocked HTTP
contracts and do not establish backend integration or runtime delivery.

Run the isolated Products → Stock → Inbox browser regression from the repository
root after installing backend test and frontend dependencies and ensuring Docker
is running. The runner uses cached Playwright Chromium, creates only disposable
containers and temporary files, and disables all external effects:

```powershell
python scripts/run_products_stock_inbox_e2e.py
```

The example environment disables AI, WhatsApp sends, CRM writes, payments,
Relance, scheduled tasks, and MAPS fanout. Check effective configuration for each
authorized run; defaults and implementation are not evidence of current runtime
state. Preserve authorization, Origin/CSRF, reauthentication, uniqueness,
idempotency, escaping, audit, and send-ledger boundaries.

License: internal MBB project; not for external distribution.
