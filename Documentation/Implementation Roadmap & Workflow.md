# Implementation Roadmap & Workflow

**MBB ya Kin — Multi-Language Lead Nurturer Bot**

Date: April 2026
Version: 1.0
Status: Planning

---

## 1. Executive Summary

This document defines the implementation roadmap for MBB ya Kin, broken into **3 phases across 18+ months**. Each phase is subdivided into **sprints (2 weeks each)** with clear deliverables, acceptance criteria, and module dependencies.

The roadmap follows the principle: **infrastructure first → core conversation loop → business logic → intelligence → optimization**.

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

---

## 3. Phase 0 — Foundation (Weeks 1–4)

**Goal:** Deployable skeleton — Docker stack running, database seeded, Baileys bridge connected, CI pipeline green.

### Sprint 0.1 (Weeks 1–2): Infrastructure Bootstrap

| Task | Deliverable | Owner | Depends On |
|------|-------------|-------|------------|
| Provision VPS (4 vCPU, 16GB RAM, 100GB SSD) | Running Ubuntu 22.04 server | DevOps | — |
| Install Docker + Docker Compose | `docker-compose.yml` with all services | DevOps | VPS |
| Configure Nginx reverse proxy | SSL termination, rate limiting | DevOps | Docker |
| Set up PostgreSQL 16 container | Running DB with `mbb` schema | Backend | Docker |
| Set up Redis 7 container (AOF enabled) | Running Redis with persistence | Backend | Docker |
| Create `.env.example` + Docker Secrets | All secrets documented, no plain-text | DevOps | — |
| Set up Git repository + branch strategy | `main`, `develop`, `feature/*`, `release/*` | All | — |

**Acceptance Criteria:**
- [ ] `docker-compose up` starts all containers in < 2 min
- [ ] PostgreSQL accepts connections on port 5432
- [ ] Redis responds to PING on port 6379
- [ ] Nginx serves HTTPS on port 443

### Sprint 0.2 (Weeks 3–4): Database + Dev Channel + CI

| Task | Deliverable | Owner | Depends On |
|------|-------------|-------|------------|
| Run full database migration (all tables) | 10 core tables + indexes + constraints (includes `admin_audit_log`) | Backend | PostgreSQL |
| Set up Redis data structures | Session keys, rate limit counters, queue structures | Backend | Redis |
| Deploy Baileys Node.js bridge | WhatsApp dev connection via QR code | Backend | Docker |
| Verify webhook delivery: Baileys → FastAPI | End-to-end message log | Backend | Baileys + FastAPI |
| Set up Celery + Celery Beat containers | Workers + scheduler running | Backend | Redis |
| Set up CI pipeline (GitHub Actions) | Lint + test + build on every push | DevOps | Git |
| Create seed data script | Test customers, conversations, products | Backend | DB |

**Acceptance Criteria:**
- [ ] Send WhatsApp message to test number → appears in FastAPI logs
- [ ] Celery worker processes a test task
- [ ] Celery Beat fires a scheduled task on time
- [ ] CI pipeline passes on a clean push
- [ ] All 9 database tables exist with correct constraints

---

## 4. Phase 1 — Core System (Weeks 5–24)

**Goal:** Fully functional chatbot handling real conversations, qualifying leads, sending relances, processing orders, and capturing MAPS tags. Pilot with 100–150 real leads.

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
| Update `.env` to `CRM_PROVIDER=mbb_hub` | Official production switch | All |
| Decommission AirtableAdapter (post-switch) | Removed from active code path | All |

**Acceptance Criteria:**
- [ ] New leads appear in both Airtable (legacy) and MBB Hub (new) during Dual-Write.
- [ ] Migration tool successfully imports 100% of historical Phase 1 leads.
- [ ] MBB Hub API latency is < 500ms for standard CRM operations.
- [ ] Switching `CRM_PROVIDER` in `.env` requires zero code changes in M1–M9.

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

| Milestone | Target Date | Success Criteria |
|-----------|------------|------------------|
| **M0: Infrastructure Ready** | Week 4 | Docker stack running, DB seeded, Baileys connected, CI green |
| **M1: First Bot Reply** | Week 6 | Real WhatsApp message → AI-generated reply in < 60s |
| **M2: Lead Qualified** | Week 12 | Conversation → 2–3 questions → scored lead in PostgreSQL |
| **M3: First Relance Sent** | Week 14 | Silent lead → automatic value-first relance on schedule |
| **M4: First Order** | Week 16 | Full flow: message → qualification → order → Mobile Money payment |
| **M5: MAPS Tag Captured** | Week 18 | Every interaction generates structured intelligence tags |
| **M6: Dashboard Live** | Week 20 | Streamlit showing real funnel data with export + admin operations pages live |
| **M7: Pilot Launch** | Week 24 | 100–150 real leads, 80% automation, < 60s response, Hub approval |
| **M8: Voice Notes** | Week 28 | Voice notes transcribed and processed (not just escalated) |
| **M9: Hub CRM Live** | Week 36 | MBBHubAdapter replaces Airtable in production |
| **M10: Full Intelligence** | Week 48 | Dynamic relance, MAPS predictions, Gemini fallback active |

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
