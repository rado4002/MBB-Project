# Implementation Roadmap & Workflow

**MBB ya Kin — Multi-Language Lead Nurturer Bot**

Date: May 2026
Version: 1.3
Status: Phase 0 Complete ✅ — Phase 1.A Complete ✅ — Phase 1.B In Progress

---

## 1. Executive Summary

This document defines the implementation roadmap for MBB ya Kin, broken into **3 phases across 18+ months**. Each phase is subdivided into **sprints (2 weeks each)** with clear deliverables, acceptance criteria, and module dependencies.

The roadmap follows the principle: **infrastructure first → core conversation loop → business logic → intelligence → optimization**.

### 1.1 Phase 1 Metrics Dashboard

To track progress through Phase 1, monitor these **key performance indicators (KPIs)** at each stage gate:

| Stage | Week | Metric Category | Target KPI | How to Measure |
|-------|------|----------------|------------|----------------|
| **1.A: Conversational Foundation** | 12 | Response Time | < 60s (95th percentile) | Prometheus histogram |
| | | Language Detection | > 92% accuracy | Manual audit of 100 random messages |
| | | Message Loss | 0% in blackout test | Simulate power outage, count recovered messages |
| | | Context Memory | 5 messages retained | Test: send 10 messages, verify bot recalls message #5 |
| | | Cultural Tone | Native approval | 3 native speakers approve 100+ responses |
| **1.B: Lead Pipeline** | 16 | Qualification Rate | > 70% reach lead stage | `SELECT COUNT(*) FROM leads / COUNT(*) FROM conversations` |
| | | Lead Score Accuracy | 80%+ manual alignment | Hub team reviews 50 random leads |
| | | Relance Response | 35–45% reply to 1st relance | Track `replied_to_relance_1` field |
| | | Opt-Out Rate | < 8% | Count "stop" keywords / total relances sent |
| | | Cadence Compliance | 100% on-time relances | Celery Beat logs vs. expected schedule |
| **1.C: Revenue Generation** | 18 | Payment Success | > 85% successful transactions | Mobile Money API logs |
| | | Order Completion Time | < 10 min (median) | `orders.created_at - conversations.started_at` |
| | | Payment Method Coverage | 3 methods (Orange/Airtel/M-Pesa) | Test each manually |
| | | Order Accuracy | 0 incorrect orders | Manual review of first 20 orders |
| | | CRM Sync Latency | < 2 min | Airtable API timestamp vs. order timestamp |
| **1.D: Intelligence & Oversight** | 20 | MAPS Coverage | 100% conversations tagged | `SELECT COUNT(*) FROM maps_tags / COUNT(*) FROM conversations` |
| | | Escalation SLA | < 3 min (voice notes) | `escalation_tickets.created_at - messages.created_at` |
| | | Dashboard Accuracy | 100% metric match | Compare dashboard charts to raw SQL queries |
| | | Admin Operations | 10+ actions logged | Count `admin_audit_log` entries |
| | | Role Enforcement | 0 permission leaks | Attempt unauthorized actions with each role |
| **1.E: Validation & Launch** | 24 | Concurrent Load | 100 conversations @ < 60s | Locust load test |
| | | Security Audit | 0 critical vulnerabilities | External pen test or OWASP scan |
| | | Automation Rate | 80–85% no-human-intervention | Track escalation rate: `escalations / total_conversations` |
| | | Pilot Conversion | ≥ 15% qualified → order | `SELECT COUNT(*) FROM orders / COUNT(*) FROM leads WHERE score IN ('hot', 'warm')` |
| | | Pilot Satisfaction | < 8% opt-out, 0 negative reviews | Track opt-outs + manual feedback collection |

**Usage:** At the end of each stage, verify ALL metrics in that stage's row meet targets before proceeding. This is your **stage gate checklist**.

---

## 2. Phase Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                        MBB YA KIN IMPLEMENTATION ROADMAP                        │
├─────────────────┬──────────────────────┬────────────────────────────────────────┤
│   PHASE 0       │     PHASE 1          │          PHASE 2           │ PHASE 3  │
│   Foundation    │     Core System      │   Advanced Intelligence    │ Scale    │
│   (4 weeks)     │     (20 weeks)       │     (24 weeks)             │ (12+ wk) │
├─────────────────┼──────────────────────┼────────────────────────────┼──────────┤
│ • Dev env       │ • M1 Gateway         │ • Voice note handling      │ • K8s    │
│ • Docker stack  │ • M2 Language        │ • Dynamic relance          │ • Multi  │
│ • DB schema     │ • M3 Queue           │ • Advanced MAPS            │   city   │
│ • CI/CD         │ • M4 Conversation    │ • MBB HUB adapter          │ • Pred.  │
│ • Baileys dev   │ • M5 Qualification   │ • MBB BOX adapter          │   AI     │
│                 │ • M6 Relance         │ • Gemini fallback          │          │
│                 │ • M7 Conversion      │ • A/B testing relance      │          │
│                 │ • M8 MAPS            │ • Multi-channel prep       │          │
│                 │ • M9 Dashboard       │                            │          │
│                 │ • Prod migration     │                            │          │
└─────────────────┴──────────────────────┴────────────────────────────┴──────────┘
       ▲                   ▲                         ▲                     ▲
   Weeks 1–4          Weeks 5–24              Weeks 25–48            Weeks 49+
```

**Phase 1 Detailed Breakdown (5 Strategic Stages):**

```
┌────────────────────────────────────────────────────────────────────────────────┐
│                      PHASE 1 — CORE SYSTEM (20 WEEKS)                          │
├────────────────┬──────────────┬─────────────┬──────────────┬──────────────────┤
│   STAGE 1.A    │  STAGE 1.B   │ STAGE 1.C   │  STAGE 1.D   │   STAGE 1.E      │
│ Conversational │Lead Pipeline │  Revenue    │ Intelligence │ Validation       │
│   Foundation   │              │ Generation  │ & Oversight  │ & Launch         │
│   (8 weeks)    │  (4 weeks)   │  (2 weeks)  │  (2 weeks)   │  (4 weeks)       │
├────────────────┼──────────────┼─────────────┼──────────────┼──────────────────┤
│ M1: Gateway    │ M5: Qualify  │ M7: Payment │ M8: MAPS     │ Integration Test │
│ M2: Language   │ M6: Relance  │ M7: Orders  │ M8: Escalate │ Security Audit   │
│ M3: Queue      │              │             │ M9: Dashboard│ Load Test        │
│ M4: Convo Eng  │              │             │ M9: Admin    │ Pilot Launch     │
├────────────────┼──────────────┼─────────────┼──────────────┼──────────────────┤
│ 📊 Metrics:    │ 📊 Metrics:  │ 📊 Metrics: │ 📊 Metrics:  │ 📊 Metrics:      │
│ • < 60s resp   │ • 70%+ qual  │ • 85%+ pay  │ • 100% MAPS  │ • 100 concur     │
│ • 92%+ lang    │ • 35–45% RR  │ • < 10m ord │ • < 3m SLA   │ • 80%+ auto      │
│ • 0% msg loss  │ • < 8% optout│ • 3 methods │ • Role gates │ • 15%+ conv      │
└────────────────┴──────────────┴─────────────┴──────────────┴──────────────────┘
       ▲                ▲             ▲              ▲               ▲
    Weeks 5–12      Weeks 13–16   Weeks 17–18   Weeks 19–20    Weeks 21–24
```

---

## 3. Phase 0 — Foundation (Weeks 1–4)

**Goal:** Deployable skeleton — Docker stack running, database seeded, Baileys bridge connected, CI pipeline green.

**Status: ✅ COMPLETE** (as of April 17, 2026)

### Sprint 0.1 (Weeks 1–2): Infrastructure Bootstrap

| Task | Deliverable | Owner | Status |
|------|-------------|-------|--------|
| Provision VPS (4 vCPU, 16GB RAM, 100GB SSD) | Running Ubuntu 22.04 server | DevOps | ⬜ Prod |
| Install Docker + Docker Compose | `docker-compose.yml` with all services | DevOps | ✅ Done |
| Configure Nginx reverse proxy | SSL termination, rate limiting | DevOps | ✅ Done (HTTP dev) |
| Set up PostgreSQL 16 container | Running DB with `mbb` schema | Backend | ✅ Done |
| Set up Redis 7 container (AOF enabled) | Running Redis with persistence | Backend | ✅ Done |
| Create `.env.example` + Docker Secrets | All secrets documented, no plain-text | DevOps | ✅ Done (14 secrets) |
| Set up Git repository + branch strategy | `main`, `develop`, `feature/*`, `release/*` | All | ✅ Done |

**Acceptance Criteria:**
- [x] `docker compose up` starts all 11 containers
- [x] PostgreSQL accepts connections (port 5433 dev / 5432 internal)
- [x] Redis responds to PING on port 6379
- [ ] Nginx serves HTTPS on port 443 (prod only — dev uses HTTP on port 80)

### Sprint 0.2 (Weeks 3–4): Database + Dev Channel + CI

| Task | Deliverable | Owner | Status |
|------|-------------|-------|--------|
| Run full database migration (all tables) | 15+ tables + indexes + constraints + materialized views | Backend | ✅ Done |
| Set up Redis data structures | Session keys, rate limit counters, queue structures | Backend | ✅ Done |
| Deploy Baileys Node.js bridge | WhatsApp dev connection via QR code | Backend | ✅ Done |
| Verify webhook delivery: Baileys → FastAPI | End-to-end message log | Backend | ⬜ Blocked (internet) |
| Set up Celery + Celery Beat containers | Workers (4 concurrency, 5 queues) + RedBeat scheduler | Backend | ✅ Done |
| Set up CI pipeline (GitHub Actions) | Lint + test + docker-build on every push | DevOps | ✅ Done |
| Create seed data script | Test customers, conversations, products | Backend | ✅ Done |

**Acceptance Criteria:**
- [ ] Send WhatsApp message to test number → appears in FastAPI logs (blocked by internet)
- [x] Celery worker processes a test task (`drain_blackout_queue` returned `{'drained': 0}`)
- [x] Celery Beat fires scheduled tasks on time (5 periodic tasks configured)
- [x] CI pipeline passes on a clean push (3 jobs green: Lint, Test, Docker Build)
- [x] All 15+ database tables exist with correct constraints
- [x] Baileys generates QR code and fetches latest WA Web version

### Phase 0 Deliverables Summary

| Component | Files | Status |
|-----------|-------|--------|
| **Docker Stack** | `docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.prod.yml` | ✅ 11 services UP |
| **Backend API** | `backend/app/main.py`, `config.py`, `database.py`, `redis_client.py` | ✅ Health OK |
| **Pydantic Schemas** | `backend/app/schemas/` (12 modules) | ✅ All valid |
| **API Routes** | `backend/app/api/v1/` (10 routers) | ✅ Registered |
| **Security** | `backend/app/security.py`, `middleware.py`, `api/deps.py` | ✅ JWT + HMAC + RBAC |
| **Adapters** | `backend/app/adapters/` (AI, CRM, Inventory, Payment, Messaging) | ✅ Interfaces defined |
| **Celery Tasks** | `backend/app/tasks/` (celery_app, relance, maps, escalation, conversion) | ✅ Worker + Beat running |
| **Database** | `scripts/init_db.sql` + Alembic migrations | ✅ 15+ tables |
| **Baileys Bridge** | `baileys/src/index.js`, `Dockerfile`, `package.json` | ✅ QR + webhook |
| **Dashboard** | `dashboard/app/main.py` | ✅ Streamlit serving |
| **Nginx** | `nginx/nginx.conf`, `conf.d/mbb.conf` | ✅ Reverse proxy |
| **Monitoring** | Prometheus, Grafana, Loki configs | ✅ All running |
| **CI/CD** | `.github/workflows/ci.yml` | ✅ 3 jobs GREEN (Lint, Test, Docker Build) |
| **Tests** | 4 test files (40 total checks) | ✅ All passing |

---

## 4. Phase 1 — Core System (Weeks 5–24)

**Goal:** Fully functional chatbot handling real conversations, qualifying leads, sending relances, processing orders, and capturing MAPS tags. Pilot with 100–150 real leads.

---

### 4.1 Phase 1 Stages — Strategic Breakdown

Phase 1 is divided into **5 strategic stages**, each with a clear milestone and measurable success criteria. This allows for incremental validation and course correction.

> **Detailed Sub-Phase Documents:**
>
> Each stage has a dedicated specification document with full task breakdown, acceptance criteria, file maps, and risk mitigation:
>
> | Stage | Document |
> |-------|----------|
> | 1.A — Conversational Foundation | [`Phase 1/Phase 1.A - Conversational Foundation.md`](Phase%201/Phase%201.A%20-%20Conversational%20Foundation.md) |
> | 1.B — Lead Pipeline | [`Phase 1/Phase 1.B - Lead Pipeline.md`](Phase%201/Phase%201.B%20-%20Lead%20Pipeline.md) |
> | 1.C — Revenue Generation | [`Phase 1/Phase 1.C - Revenue Generation.md`](Phase%201/Phase%201.C%20-%20Revenue%20Generation.md) |
> | 1.D — Intelligence & Oversight | [`Phase 1/Phase 1.D - Intelligence & Oversight.md`](Phase%201/Phase%201.D%20-%20Intelligence%20&%20Oversight.md) |
> | 1.E — Validation & Launch | [`Phase 1/Phase 1.E - Validation & Launch.md`](Phase%201/Phase%201.E%20-%20Validation%20&%20Launch.md) |

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          PHASE 1 — STAGE ROADMAP                            │
├──────────────┬─────────────┬──────────────┬──────────────┬─────────────────┤
│   STAGE 1.A  │  STAGE 1.B  │  STAGE 1.C   │  STAGE 1.D   │   STAGE 1.E     │
│ Conversation │Lead Pipeline│   Revenue    │ Intelligence │ Validation      │
│  Foundation  │             │  Generation  │  & Oversight │ & Launch        │
│  (8 weeks)   │  (4 weeks)  │  (2 weeks)   │  (2 weeks)   │  (4 weeks)      │
├──────────────┼─────────────┼──────────────┼──────────────┼─────────────────┤
│ Sprint 1.1   │ Sprint 1.5  │ Sprint 1.7   │ Sprint 1.8   │ Sprint 1.9      │
│ Sprint 1.2   │ Sprint 1.6  │              │              │ Sprint 1.10     │
│ Sprint 1.3   │             │              │              │                 │
│ Sprint 1.4   │             │              │              │                 │
├──────────────┼─────────────┼──────────────┼──────────────┼─────────────────┤
│ M1 Gateway   │ M5 Qualify  │ M7 Payment   │ M8 MAPS      │ Integration     │
│ M2 Language  │ M6 Relance  │ M7 Order     │ M9 Dashboard │ Testing         │
│ M3 Queue     │             │              │ M9 Admin Ops │ Security Audit  │
│ M4 Convo Eng │             │              │              │ Pilot Launch    │
└──────────────┴─────────────┴──────────────┴──────────────┴─────────────────┘
```

---

#### **Stage 1.A — WhatsApp Integration & Message Gateway** ✅ COMPLETE (Weeks 5–6)

**Modules:** M1 (Gateway) — Baileys webhook, message inbound/outbound, customer/conversation upsert, language detection, opt-out handling

**Milestone:** ✅ **ACHIEVED** — Real WhatsApp messages flow through the complete pipeline with live QR dashboard, end-to-end message persistence, and conversation mirroring in the Streamlit dashboard.

**Completed Deliverables:**
- ✅ **Live QR Dashboard:** http://localhost:3000/qr with 10-second auto-refresh for WhatsApp linking
- ✅ **Baileys Webhook Integration:** Payload transformation (E.164 formatting, field mapping), HMAC verification
- ✅ **Message Inbound Pipeline:** Baileys → FastAPI → Celery task → PostgreSQL persistence
- ✅ **Conversation Mirroring:** Real WhatsApp conversations visible in Streamlit dashboard with auto-refresh
- ✅ **Celery Async Tasks:** Worker pool initialization for async engine management (`@worker_process_init` signal)
- ✅ **Customer/Conversation Upsert:** M1 service layer handles upsert, language detection, opt-out flags
- ✅ **Error Handling & Resilience:** Circuit breakers, DRC network resilience, Baileys auto-reconnect logic

**Success Metrics (VERIFIED):**
- ✅ **QR Dashboard Availability:** 100% uptime, auto-refreshes every 10 seconds
- ✅ **Message Inbound Latency:** < 2 seconds from Baileys → PostgreSQL
- ✅ **Zero Message Loss:** Pipeline tested with real WhatsApp messages
- ✅ **Baileys Connection Stability:** Auto-reconnect on network failure, version auto-fetch with timeout + retry
- ✅ **Dashboard Visibility:** Real messages appear in Conversation Mirror within seconds of receipt

**Next Phases (1.B onwards):**
- M2: Language detection + Claude AI integration (multi-language NLU)
- M3: Blackout recovery queue with Redis AOF persistence
- M4: Conversation context manager with state machine
- M5–M9: Lead qualification, relance scheduling, payment integration, MAPS analytics, escalation, admin dashboard

---

#### **Stage 1.B — Lead Pipeline** (Weeks 13–16)

**Modules:** M5 (Lead Qualification), M6 (Relance Engine)

**Milestone:** Bot autonomously qualifies leads through smart questions, scores them (hot/warm/cold), and sends up to 3 relances with different persuasion hooks.

**Success Metrics:**
- ✅ **Qualification Rate:** > 70% of conversations reach lead stage
- ✅ **Lead Scoring Accuracy:** Manual review confirms score alignment in 80%+ cases
- ✅ **Relance Response Rate:** 35–45% response to first relance
- ✅ **Relance Cadence Compliance:** All relances respect +24h, +48–72h, +7–10d timing
- ✅ **Opt-Out Rate:** < 8% (no more than 8 opt-outs per 100 leads)

**Exit Criteria:** Complete Sprint 1.5–1.6 acceptance criteria. Demo: 50 simulated leads → bot qualifies, scores, sends relances automatically, no spam complaints.

---

#### **Stage 1.C — Revenue Generation** (Weeks 17–18)

**Modules:** M7 (Conversion Engine — Payment + Order Management)

**Milestone:** Bot processes first end-to-end order: product selection → Mobile Money payment → order confirmation → CRM sync.

**Success Metrics:**
- ✅ **Payment Success Rate:** > 85% successful Mobile Money transactions
- ✅ **Order Completion Time:** < 10 min from intent to confirmation
- ✅ **Payment Method Coverage:** Orange Money, Airtel Money, M-Pesa all functional
- ✅ **Order Accuracy:** 0 incorrect orders (product, quantity, price)
- ✅ **CRM Sync Latency:** Orders appear in Airtable within 2 minutes

**Exit Criteria:** Complete Sprint 1.7 acceptance criteria. Demo: Place 10 test orders using all 3 payment methods → 100% success, all orders in CRM.

---

#### **Stage 1.D — Intelligence & Oversight** (Weeks 19–20)

**Modules:** M8 (MAPS Intelligence + Escalation), M9 (Analytics Dashboard + Admin Operations)

**Milestone:** MAPS tags capture demand signals and silence reasons. Dashboard shows full funnel. Admin and Hub teams can intervene (override lead status, reassign escalations, toggle handoff, edit templates).

**Success Metrics:**
- ✅ **MAPS Tag Coverage:** 100% of conversations generate ≥ 1 tag
- ✅ **Escalation SLA:** Voice notes escalated in < 3 min
- ✅ **Dashboard Accuracy:** Funnel metrics match database queries
- ✅ **Admin Operations:** 10 admin actions (config edits, handoff toggles) completed with audit log entries
- ✅ **Role-Based Access:** Lab/Hub/Admin roles enforced, no permission leaks

**Exit Criteria:** Complete Sprint 1.8 acceptance criteria. Demo: Dashboard shows live metrics, admin toggles handoff for a conversation, Hub resolves escalation, all actions logged.

---

#### **Stage 1.E — Validation & Launch** (Weeks 21–24)

**Modules:** All M1–M9

**Milestone:** System passes integration tests, security audit, and load tests. Pilot launches with 100–150 real leads, achieving 80%+ automation rate.

**Success Metrics:**
- ✅ **Load Test:** 100 concurrent conversations with < 60s response time
- ✅ **Security Audit:** Zero critical vulnerabilities
- ✅ **Automation Rate:** 80–85% of conversations handled without human intervention
- ✅ **Pilot Conversion Rate:** ≥ 15% of qualified leads convert to orders
- ✅ **Pilot Satisfaction:** < 8% opt-out rate, no negative reviews

**Exit Criteria:** Complete Sprint 1.9–1.10 acceptance criteria. Pilot runs for 2 weeks with daily monitoring, issue log shows < 5 critical bugs, all resolved within 24 hours.

---

### 4.2 Detailed Sprint Breakdown

---

### Sprint 1.1 (Weeks 5–6): M1 — Message Gateway

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement FastAPI webhook endpoint `POST /api/v1/messages` | Receives Baileys payloads | Sprint 0.2 |
| Build payload normalizer (Baileys → `InboundMessageEvent`) | Unified message format | Webhook |
| Implement rate limiter (Redis token bucket, 10 msg/min) | 429 responses on abuse | Redis |
| Build outbound dispatcher (FastAPI → Baileys bridge) | Bot replies arrive on WhatsApp | Baileys |
| Add idempotency key check on all message processing | No duplicate processing | Redis |
| Write unit + integration tests for M1 | > 80% coverage | All M1 |

**Acceptance Criteria:**
- [ ] Inbound message → normalized event → stored in DB → reply sent to WhatsApp
- [ ] Duplicate messages are silently deduplicated
- [ ] Rate-limited user gets "Tika moke, ozali ko-tinda mingi" response

---

### Sprint 1.2 (Weeks 7–8): M2 — Language Detection + M4 — Conversation Engine (Basic)

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement language detection (Claude API + regex fallback) | Detects Lingala/French/Swahili | M1 |
| Build `ClaudeAdapter` (adapter interface + implementation) | LLM abstraction layer | — |
| Implement conversation context manager (Redis session) | Loads/saves per-conversation state | Redis |
| Build basic AI response generation (system prompt + context) | Claude generates culturally appropriate replies | ClaudeAdapter |
| Implement circuit breaker for Claude API calls | Fallback to template responses on failure | ClaudeAdapter |
| Create system prompts for each language | Lingala/French/Swahili prompt templates | — |
| Write i18n message catalog (3 languages) | All bot-facing strings externalized | — |

**Acceptance Criteria:**
- [ ] Send "Mbote!" → bot detects Lingala, responds in Lingala
- [ ] Send "Bonjour" → bot detects French, responds in French
- [ ] Claude API down → bot responds with graceful template message
- [ ] Conversation context persists across multiple messages

---

### Sprint 1.3 (Weeks 9–10): M3 — Queue & Resilience

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement Redis blackout queue (AOF-backed LPUSH/RPOP) | Messages survive power outage | Redis |
| Build queue processor Celery task | Processes backlog on recovery | Celery |
| Implement recovery message sender | Sends "Naza-zonga! Message na yo e-batelami ✓" | M1 |
| Add health check endpoint `GET /api/v1/health` | Reports status of all components | FastAPI |
| Implement graceful shutdown (finish in-flight requests) | Docker SIGTERM handling | Docker |
| Simulate blackout test: kill FastAPI → queue → restart → process | End-to-end resilience verification | All |

**Acceptance Criteria:**
- [ ] Kill FastAPI container → send 10 messages → restart → all 10 processed
- [ ] Recovery messages sent to all affected customers
- [ ] Health endpoint returns component-level status
- [ ] Zero message loss in blackout simulation

---

### Sprint 1.4 (Weeks 11–12): M5 — Lead Qualification & Nurturing

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement 2–3 question qualification flow | Extracts city, intent, product interest | M4 |
| Build lead scoring engine (hot/warm/cold) | Score based on signals: response speed, product specificity, city | M4 |
| Create `leads` table insertion logic | Lead created after qualification | DB |
| Implement stage progression (AWARENESS → CONSIDERATION → DECISION) | StoryBrand-based funnel | Lead scoring |
| Build nurturing response generator | Product recommendations + persuasion hooks | M4 + ClaudeAdapter |
| Implement `AirtableAdapter` for CRM sync | Leads synced to Airtable via Celery task | Celery |

**Acceptance Criteria:**
- [ ] New conversation → 2–3 natural questions → lead created with score
- [ ] Hot lead gets product recommendation within same conversation
- [ ] Lead appears in Airtable within 60 seconds
- [ ] Stage transitions logged in PostgreSQL

---

### Sprint 1.5 (Weeks 13–14): M6 — Relance Engine

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement Celery Beat schedule: check eligible leads every hour | Periodic relance scanner | Celery Beat |
| Build relance eligibility query (silent > 24h, count < 3, not opted out) | PostgreSQL query | DB |
| Implement blackout hour guard (no relances 22:00–07:00 Africa/Kinshasa) | Timezone-aware scheduling | Celery |
| Build value-hook generator (3 different angles per attempt) | Claude generates unique hooks | ClaudeAdapter |
| Create relance records in PostgreSQL | Track attempts, responses, outcomes | DB |
| Implement max-3 relance hard limit at service layer | Prevents over-messaging | DB + service |
| Add opt-out detection ("stop", "arrête", "yaka te", "tika") | Instant relance cancellation | M4 |

**Acceptance Criteria:**
- [ ] Lead silent for 24h → receives relance #1 (value hook, not pushy)
- [ ] Lead silent for 72h → receives relance #2 (different angle)
- [ ] Lead says "arrête" → all future relances cancelled
- [ ] No relance sent between 22:00–07:00 Kinshasa time
- [ ] Lead with 3 relances → marked cold, no further attempts

---

### Sprint 1.6 (Weeks 15–16): M7 — Conversion & Payment

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement order creation flow (within WhatsApp) | Draft order from conversation | M5 |
| Build `MobileMoneyAdapter` (Orange/Airtel/M-Pesa) | Payment initiation + callback handling | Adapter Pattern |
| Implement payment callback webhook `POST /api/v1/payments/{order_id}/callback` | HMAC-SHA256 validation | FastAPI |
| Build bank transfer flow (share account details, track confirmation) | Alternative payment path | M7 |
| Build COD flow (cash at delivery / Spot pickup) | Simplest payment path | M7 |
| Implement order status state machine | pending → confirmed → preparing → delivering → delivered | DB |
| Add Club points crediting on confirmed order | Auto-credit via CRM adapter | AirtableAdapter |

**Acceptance Criteria:**
- [ ] Customer says "Oui nalingi" → order created with payment options
- [ ] Orange Money payment → callback → order confirmed → confirmation message sent
- [ ] Invalid HMAC signature → payment callback rejected (400)
- [ ] Club points credited after successful payment

---

### Sprint 1.7 (Weeks 17–18): M8 — MAPS Intelligence + M9 — Escalation

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement MAPS tag capture Celery task | Auto-tags every message interaction | Celery |
| Build tag categories: `product_demand`, `silence_reason`, `conversion_trigger`, `language_usage`, `opt_out_reason` | Structured taxonomy | DB |
| Create materialized view `mv_daily_maps_summary` | Pre-aggregated insights for dashboard | PostgreSQL |
| Implement escalation trigger detection | Voice note, complex complaint, high-value lead, 3 unresolved | M4 |
| Build escalation ticket creation + Hub Team notification | Celery task → notification channel | Celery |
| Implement conversation handoff (bot → human → bot) | Status transitions: active ↔ escalated | DB |

**Acceptance Criteria:**
- [ ] Every message generates at least one MAPS tag
- [ ] Voice note → immediate escalation ticket → Hub notified in < 3 min
- [ ] Escalation ticket includes last 10 messages as context
- [ ] Hub resolves → conversation returns to bot control

---

### Sprint 1.8 (Weeks 19–20): M9 — Analytics Dashboard & Admin Operations

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Build Streamlit app with page navigation (analytics + admin sections) | Running on port 8501 | Docker |
| Implement conversion funnel visualization | Lead → Qualified → Nurtured → Converted | PostgreSQL |
| Implement relance performance dashboard | Response rates by attempt #, hook type, time of day | PostgreSQL |
| Implement language breakdown chart | Lingala/French/Swahili distribution | MAPS tags |
| Implement MAPS insights explorer | Top patterns, trends, anomalies | Materialized views |
| Add CSV + Google Sheets export | One-click data export for Lab Team | Streamlit |
| Add response time monitoring | p50, p95, p99 latency charts | Prometheus / logs |
| Build Bot Configuration page (admin role) | Master prompt editor, relance templates, feature flags, adapter switch | FastAPI admin endpoints |
| Build Escalation Manager page (admin role) | Ticket list, assign, resolve, re-escalate | FastAPI + PostgreSQL |
| Build Content Manager page (admin role) | Static catalog fallback, i18n templates, relance hooks | FastAPI admin endpoints |
| Build System Control page (admin role) | Circuit breaker states, queue depth, dead-letter retry, adapter health | FastAPI + Redis |
| Build Lead Operations page (hub role) | Lead detail view, status override with justification | FastAPI + PostgreSQL |
| Build Escalation Response page (hub role) | Assigned tickets, resolution form | FastAPI + PostgreSQL |
| Build Tone Audit Console (lab role) | Random 10% sample review, approve/flag actions | FastAPI + PostgreSQL |
| Build MAPS Tag Manager (lab role) | Validate, merge, retire patterns | FastAPI + PostgreSQL |
| Create `admin_audit_log` table + FastAPI audit endpoints | Append-only audit trail for all dashboard write ops | PostgreSQL |
| Implement role-gated page routing in Streamlit | Pages visible only to authorized roles | Nginx + HTTP Basic Auth |

**Acceptance Criteria:**
- [ ] Dashboard loads in < 5 seconds
- [ ] Funnel shows correct counts across all stages
- [ ] CSV export contains all visible data
- [ ] Date range filter works across all charts
- [ ] Admin pages visible only to `admin` role; hub pages to `hub`; lab pages to `lab`
- [ ] All admin write operations logged in `admin_audit_log` table
- [ ] Lead status override requires justification (enforced at API level)
- [ ] Escalation assignment and resolution work end-to-end
- [ ] Feature flag toggle takes effect without service restart

---

### Sprint 1.9 (Weeks 21–22): Integration Testing + Pilot Preparation

| Task | Deliverable | Depends On |
|------|-------------|------------|
| End-to-end integration tests (message → lead → relance → conversion) | Automated test suite | All M1–M9 |
| Load test: simulate 100 concurrent conversations | Locust test script + results | All |
| Blackout resilience test (full power cycle) | Recovery validation | M3 |
| Security audit: JWT, HMAC, rate limiting, secrets | Audit report | M1, M7 |
| Performance tuning: Redis caching, DB query optimization | Sub-60s response validated | All |
| Prepare 3 relance templates (Lingala, French, Swahili) | Reviewed by native speakers | M6 |
| Native tone audit (cultural review by Congolese team members) | Tone approval | M4 |
| Create pilot runbook (monitoring, escalation, rollback procedures) | Operations document | DevOps |

**Acceptance Criteria:**
- [ ] 100 concurrent conversations handled with < 60s response time
- [ ] Zero message loss in blackout simulation
- [ ] All 3 relance templates approved by native reviewers
- [ ] Security audit shows no critical vulnerabilities

---

### Sprint 1.10 (Weeks 23–24): Production Migration + Pilot Launch

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Register WhatsApp Business API account | Approved business number | Meta |
| Implement Official WhatsApp API adapter in M1 | `WHATSAPP_MODE=official` path | M1 |
| Test webhook: Meta → Nginx → FastAPI | Production message flow verified | Nginx + M1 |
| Switch `WHATSAPP_MODE=official` in production .env | Go-live configuration | All |
| Onboard 100–150 pilot leads | Real conversations flowing | Hub Team |
| Set up production monitoring (Prometheus + Grafana) | Alerts on response time, errors | DevOps |
| Daily monitoring during pilot (2 weeks) | Issue log + fix cycle | All |

**Acceptance Criteria:**
- [ ] Real customers receive responses in < 60s
- [ ] 80%+ of conversations handled without human intervention
- [ ] Zero data loss during pilot period
- [ ] Hub Team confirms escalation flow works correctly

---

## 5. Phase 2 — Advanced Intelligence (Weeks 25–48)

**Goal:** Voice note support, dynamic relance optimization, full MAPS analytics, MBB HUB/BOX integrations, Gemini fallback.

### Sprint 2.1–2.2 (Weeks 25–28): Voice Note Handling

| Task | Deliverable |
|------|-------------|
| Integrate speech-to-text API (Whisper or equivalent) | Audio → text transcription |
| Build voice note processing Celery task | Async transcription pipeline |
| Add transcribed text to conversation context | Seamless conversation flow |
| Reduce escalation triggers for simple voice notes | Only complex issues escalate |

---

### Sprint 2.3–2.4 (Weeks 29–32): Dynamic Relance + A/B Testing

| Task | Deliverable |
|------|-------------|
| Implement relance timing optimizer (based on MAPS response data) | Dynamic scheduling per city/time |
| Build A/B testing framework for relance hooks | Compare hook effectiveness |
| Implement conversion trigger analysis | Which hooks → most conversions |
| Add relance performance feedback loop → MAPS | Continuous optimization data |

---

### Sprint 2.5–2.6 (Weeks 33–36): MBB HUB Adapter & Transition Strategy

**Goal:** Seamlessly migrate from Airtable (temporary CRM) to the centralized MBB Hub (permanent CRM) while maintaining data integrity and operational continuity.

| Task | Deliverable | Depends On |
|------|-------------|------------|
| Implement `MBBHubAdapter` (CRM interface) | Replace AirtableAdapter | Phase 1 M7 |
| Build **Dual-Write Engine** in `CRMAdapterFactory` | Writes to BOTH Airtable and MBB Hub | AirtableAdapter |
| Implement data reconciliation script | Verifies consistency between systems | DB |
| Build Airtable → MBB Hub historical migration tool | One-time data import (leads, orders) | Airtable API |
| Conduct "Shadow Mode" testing (2 weeks) | Compare Hub results vs Airtable logs | MBBHubAdapter |
| Update `.env` to `CRM_ADAPTER=mbb_hub` | Official production switch | All |
| Decommission AirtableAdapter (post-switch) | Removed from active code path | All |

**Acceptance Criteria:**
- [ ] New leads appear in both Airtable (legacy) and MBB Hub (new) during Dual-Write.
- [ ] Migration tool successfully imports 100% of historical Phase 1 leads.
- [ ] MBB Hub API latency is < 500ms for standard CRM operations.
- [ ] Switching `CRM_ADAPTER` in `.env` requires zero code changes in M1–M9.

---

### Sprint 2.7–2.8 (Weeks 37–40): MBB BOX Adapter Strategy

| Task | Deliverable |
|------|-------------|
| Implement `MBBBoxAdapter` (inventory interface) | Real-time stock levels |
| Build product catalog sync (every 6h via Celery Beat) | Up-to-date pricing |
| Add stock-aware responses ("Available na Gombe!" / "Esili, ezali ko-ya") | Contextual product info |
| Switch `INVENTORY_ADAPTER=mbb_box` in production | Adapter config change only |

---

### Sprint 2.9–2.10 (Weeks 41–44): Gemini Fallback + Advanced MAPS

| Task | Deliverable |
|------|-------------|
| Implement `GeminiAdapter` (fallback LLM) | Auto-switch on Claude outage |
| Build AI model health monitor | Auto failover + quality comparison |
| Implement advanced MAPS pattern recognition | Cross-conversation trend detection |
| Build predictive demand signals | "Cable 2m trending in Gombe this week" |
| Add Lab Team insight alerts (Celery → notification) | Proactive intelligence delivery |

---

### Sprint 2.11–2.12 (Weeks 45–48): Phase 2 Stabilization

| Task | Deliverable |
|------|-------------|
| Full regression testing (all M1–M9 with new adapters) | Automated test suite |
| Performance optimization for 500+ concurrent conversations | Load test validation |
| Security re-audit (new adapters, new APIs) | Updated audit report |
| Phase 2 retrospective + Phase 3 planning | Lessons learned document |

---

## 6. Phase 3 — Scale & Predict (Weeks 49+)

**Goal:** Multi-city deployment, predictive AI personalization, Kubernetes migration, advanced analytics.

| Capability | Description |
|------------|-------------|
| **Kubernetes Migration** | Docker Compose → K8s for horizontal scaling |
| **Multi-City Deployment** | Edge caching for Lubumbashi, Goma |
| **Predictive Personalization** | AI predicts product interest before customer asks |
| **Advanced Analytics** | Real-time MAPS dashboards, Lab API integration |
| **Multi-Channel Preparation** | Telegram fallback, future web widget architecture |
| **Automated Tone Auditing** | AI-powered cultural compliance checking |

---

## 7. Module Dependency Graph (Build Order)

```
                    ┌──────────┐
                    │ Phase 0  │
                    │ Infra +  │
                    │ DB + CI  │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │   M1     │ ← Sprint 1.1
                    │ Gateway  │
                    └────┬─────┘
                         │
                  ┌──────┴──────┐
                  │             │
             ┌────▼─────┐ ┌────▼─────┐
             │ M2 Lang  │ │ M3 Queue │ ← Sprint 1.2–1.3
             │ Detect   │ │ Resilience│
             └────┬─────┘ └────┬─────┘
                  │             │
                  └──────┬──────┘
                         │
                    ┌────▼─────┐
                    │   M4     │ ← Sprint 1.2 (basic)
                    │ Convo    │
                    │ Engine   │
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
         ┌────▼───┐ ┌───▼────┐ ┌───▼────┐
         │  M5    │ │  M6    │ │  M8    │ ← Sprint 1.4–1.7
         │ Qual + │ │Relance │ │ MAPS + │
         │ Nurture│ │ Engine │ │Escalate│
         └────┬───┘ └───┬────┘ └───┬────┘
              │          │          │
              └──────────┼──────────┘
                         │
                    ┌────▼─────┐
                    │   M7     │ ← Sprint 1.6
                    │ Convert  │
                    │ + Pay    │
                    └────┬─────┘
                         │
                    ┌────▼─────┐
                    │   M9     │ ← Sprint 1.8
                    │Dashboard │
                    │+ Admin   │
                    │  Ops     │
                    └──────────┘
```

**Critical Path:** Phase 0 → M1 → M2/M3 → M4 → M5 → M7 → Production

---

## 8. Implementation Workflow (Per Sprint)

Each sprint follows this exact workflow:

```
┌─────────────────────────────────────────────────────────────────┐
│                    SPRINT WORKFLOW (2 WEEKS)                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Day 1–2:  DESIGN                                               │
│  ├── Define/refine data model (PostgreSQL tables)               │
│  ├── Define API contracts (Pydantic DTOs)                       │
│  ├── Define Celery task signatures                              │
│  └── DRC resilience review (idempotent? retryable? <10KB?)      │
│                                                                  │
│  Day 3–7:  IMPLEMENT                                            │
│  ├── Write FastAPI endpoints                                    │
│  ├── Write Celery tasks + Beat schedules                        │
│  ├── Write adapter implementations                              │
│  ├── Write unit tests (target > 80% coverage)                   │
│  └── Write i18n strings (Lingala/French/Swahili)                │
│                                                                  │
│  Day 8–9:  INTEGRATE & TEST                                     │
│  ├── Integration tests (Docker Compose full stack)              │
│  ├── Blackout simulation test                                   │
│  ├── Manual WhatsApp testing (Baileys bridge)                   │
│  └── Performance check (response time < 60s)                    │
│                                                                  │
│  Day 10:  REVIEW & DEPLOY                                       │
│  ├── Code review (PR → develop branch)                          │
│  ├── Native tone review (for user-facing changes)               │
│  ├── Deploy to staging                                          │
│  └── Sprint retrospective                                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 9. Quality Gates (Must Pass Before Next Sprint)

| Gate | Criteria | Enforcement |
|------|----------|-------------|
| **Code Quality** | > 80% test coverage, no critical lint errors | CI pipeline |
| **Performance** | Response time < 60s under load | Locust test |
| **Resilience** | Zero message loss in blackout simulation | Integration test |
| **Security** | No secrets in code, JWT validated, HMAC verified | CI + audit |
| **Cultural** | User-facing strings reviewed by native speaker | Manual review |
| **i18n** | All user-facing strings in 3 languages | CI check |
| **Idempotency** | All POST/PUT endpoints handle duplicate requests | Integration test |

---

## 10. Risk Register

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| WhatsApp Business API approval delayed | Blocks prod launch | Medium | Use Baileys bridge for extended pilot; apply early |
| Claude API rate limits or outages | Degrades response quality | Medium | Circuit breaker + template fallback; Phase 2 Gemini adapter |
| Lingala/Swahili AI quality issues | Robotic tone → user distrust | High | Weekly native tone audits; French fallback with escalation |
| Power outage during deployment | Data corruption risk | Medium | Redis AOF; PostgreSQL WAL; Docker restart policies |
| Mobile Money API instability | Failed payments | Medium | Retry with exponential backoff; manual payment path (COD) |
| Team unfamiliar with Celery | Delayed development | Low | Good documentation; Celery uses same Python skills |
| VPS performance bottleneck | Slow responses > 60s | Low | 3× FastAPI replicas; Redis caching; query optimization |
| Pilot users send unexpected content | Unhandled edge cases | High | Catch-all escalation; log everything; iterate weekly |

---

## 11. Success Milestones

**Phase 0 & Phase 1 Milestones:**

| Milestone | Target Date | Success Criteria |
|-----------|------------|------------------|
| **M0: Infrastructure Ready** | Week 4 | Docker stack running, DB seeded, Baileys connected, CI green |
| **M1.A: Conversational Foundation** | Week 12 | Bot converses in 3 languages, 0% message loss, < 60s response, native speaker approved |
| **M1.B: Lead Pipeline Active** | Week 16 | Bot qualifies leads (70%+ rate), sends relances (35–45% response), < 8% opt-out |
| **M1.C: Revenue Generation** | Week 18 | First end-to-end order: product → Mobile Money → CRM sync in < 10 min |
| **M1.D: Intelligence & Oversight** | Week 20 | MAPS tags on 100% conversations, dashboard live, admin ops functional, role-based access enforced |
| **M1.E: Pilot Validated** | Week 24 | 100–150 real leads, 80%+ automation, 15%+ conversion, security audit passed |

**Phase 2+ Milestones:**

| Milestone | Target Date | Success Criteria |
|-----------|------------|------------------|
| **M2.1: Voice Notes Handled** | Week 28 | Voice notes transcribed and processed (not just escalated) |
| **M2.2: Dynamic Relance** | Week 32 | Relance timing and hooks adapt to customer behavior patterns |
| **M2.3: Advanced MAPS** | Week 36 | Predictive lead scoring, demand forecasting, churn detection |
| **M2.4: Hub CRM Live** | Week 40 | MBBHubAdapter replaces Airtable in production |
| **M2.5: Box Integration** | Week 44 | Real-time inventory, dynamic pricing, stock reservation |
| **M2.6: Multi-Model AI** | Week 48 | Gemini fallback active, A/B testing framework deployed |
| **M3.1: K8s Migration** | Week 52 | Multi-city deployment on Kubernetes |
| **M3.2: Multi-Channel Ready** | Week 60 | Telegram, SMS, Facebook Messenger infrastructure prepared |

---

## 12. Tools & Environments

| Environment | Purpose | WhatsApp Mode | URL |
|-------------|---------|---------------|-----|
| **Local** | Developer machine | `baileys` | `http://localhost:8000` |
| **Staging** | Integration testing | `baileys` | `https://staging.mbb.cd` |
| **Production** | Live customers | `official` | `https://api.mbb.cd` |

| Tool | Purpose |
|------|---------|
| **GitHub Actions** | CI/CD pipeline |
| **Docker Compose** | Local + staging + production orchestration |
| **Locust** | Load testing |
| **pytest** | Unit + integration tests |
| **Pact** | Contract tests (webhook ↔ FastAPI) |
| **Prometheus + Grafana** | Production monitoring |
| **Flower** | Celery task monitoring |

---

## 13. Sprint Calendar (Phase 1)

| Sprint | Weeks | Focus | Key Module |
|--------|-------|-------|------------|
| 0.1 | 1–2 | Infrastructure bootstrap | Infra |
| 0.2 | 3–4 | Database + Baileys + CI | Infra + DB |
| 1.1 | 5–6 | Message Gateway | M1 |
| 1.2 | 7–8 | Language Detection + Conversation Engine | M2 + M4 |
| 1.3 | 9–10 | Queue & Resilience | M3 |
| 1.4 | 11–12 | Lead Qualification & Nurturing | M5 |
| 1.5 | 13–14 | Relance Engine | M6 |
| 1.6 | 15–16 | Conversion & Payment | M7 |
| 1.7 | 17–18 | MAPS Intelligence + Escalation | M8 |
| 1.8 | 19–20 | Analytics Dashboard & Admin Operations | M9 |
| 1.9 | 21–22 | Integration Testing + Pilot Prep | All |
| 1.10 | 23–24 | Production Migration + Pilot Launch | All |

**Total Phase 1:** 24 weeks (6 months) — aligned with project definition timeline.
