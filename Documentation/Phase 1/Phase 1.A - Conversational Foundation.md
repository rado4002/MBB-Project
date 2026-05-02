# Phase 1.A — Conversational Foundation

**MBB ya Kin — Sub-Phase Specification**

| Field | Value |
|-------|-------|
| **Phase** | 1.A |
| **Name** | WhatsApp Integration & M1 Gateway |
| **Weeks** | 5–6 (2 weeks) |
| **Sprints** | 1.1 (WhatsApp Baileys Integration) |
| **Modules** | M1 (Gateway) — Baileys webhook, message inbound/outbound, customer/conversation upsert, language detection, opt-out handling |
| **Status** | ✅ Complete |
| **Completed** | Live QR dashboard, Baileys payload transformation, Celery async task processing, end-to-end message pipeline, conversation mirroring in dashboard |

---

## 1. Goal

**Phase 1.A Goal (Sprint 1.1 Focus):**
Build the foundational WhatsApp integration layer: receive real WhatsApp messages via Baileys bridge → normalize payloads → persist to database → enable reply dispatch → visible in Streamlit dashboard.

**Completed Milestone:** 
✅ Live QR dashboard for WhatsApp linking
✅ Real WhatsApp messages flow: Baileys → FastAPI → Celery → PostgreSQL → Dashboard
✅ Message pipeline is end-to-end functional with proper error handling and HMAC verification

**Next Phases (1.B onwards):**
- M2: Language detection + Claude AI integration
- M3: Blackout recovery queue (Redis AOF)
- M4: Conversation context & state machine
- M5–M9: Lead qualification, relance, payment, MAPS, escalation, dashboard

---

## 2. Success Metrics (Stage Gate)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Response Time | < 60s (95th percentile) | Prometheus histogram |
| Language Detection Accuracy | > 92% correct | Manual audit of 100 random messages |
| Blackout Recovery | 0% message loss | Simulate power outage, count recovered messages |
| Conversation Context | 5 messages retained | Send 10 messages, verify bot recalls message #5 |
| Cultural Tone | Native approval | 3 native speakers approve 100+ responses |

**Exit Criteria:** Complete all sprint acceptance criteria. Demo: Send 20 back-and-forth messages in mixed Lingala/French → bot responds coherently, simulate blackout → all messages recovered.

---

## 3. Dependencies

| Dependency | Source | Status |
|------------|--------|--------|
| Docker stack running (all 11 services) | Phase 0 | ✅ Done |
| PostgreSQL schema deployed (15+ tables) | Phase 0 | ✅ Done |
| Redis running with AOF persistence | Phase 0 | ✅ Done |
| Baileys bridge connected + QR generation | Phase 0 | ✅ Done |
| Celery worker + Beat operational | Phase 0 | ✅ Done |
| CI pipeline green | Phase 0 | ✅ Done |

---

## 4. Sprint 1.1 — M1: Message Gateway (Weeks 5–6)

### 4.1 Objective

Implement the full inbound/outbound message pipeline: WhatsApp → webhook → normalization → database → response dispatch.

### 4.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement FastAPI webhook endpoint `POST /api/v1/messages` | Receives Baileys payloads | Phase 0 | ✅ |
| 2 | Build payload normalizer (Baileys → `InboundMessageEvent`) | Unified message format | Task 1 | ✅ |
| 3 | Implement rate limiter (Redis token bucket, 10 msg/min) | 429 responses on abuse | Redis | ✅ |
| 4 | Build outbound dispatcher (FastAPI → Baileys bridge) | Bot replies arrive on WhatsApp | Baileys | ✅ |
| 5 | Add idempotency key check on all message processing | No duplicate processing | Redis | ✅ |
| 6 | Write unit + integration tests for M1 | > 80% coverage | All M1 | ✅ |

### 4.3 Data Model Focus

- `messages` table: Verify schema covers all inbound/outbound fields
- `conversations` table: Auto-create on first message from new phone
- `customers` table: Upsert on first contact

### 4.4 API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/v1/messages` | Receive inbound webhook from Baileys/Official |
| `POST` | `/api/v1/messages/send` | Dispatch outbound message to WhatsApp |
| `GET` | `/api/v1/messages/{conversation_id}` | Retrieve message history |

### 4.5 Acceptance Criteria

- [x] Inbound message → normalized event → stored in DB → reply sent to WhatsApp
- [x] Duplicate messages are silently deduplicated (idempotency key)
- [x] Rate-limited user gets "Tika moke, ozali ko-tinda mingi" response
- [x] Payload size < 10KB for all responses
- [x] All operations are idempotent and retryable

### 4.6 DRC Resilience Checklist

- [x] Idempotent? → Yes (idempotency key on message_id)
- [x] Retryable? → Yes (Celery retry with exponential backoff)
- [x] Queued during blackout? → Yes (Redis LPUSH with AOF)
- [x] < 10KB payload? → Yes (text-only responses)

---

## 5. Sprint 1.2 — M2: Language Detection + M4: Conversation Engine (Basic) (Weeks 7–8)

### 5.1 Objective

Detect message language (Lingala/French/Swahili), build the Claude AI integration, and implement basic multi-turn conversation with context memory.

### 5.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement language detection (Claude API + regex fallback) | Detects Lingala/French/Swahili | M1 | ✅ |
| 2 | Build `ClaudeAdapter` (adapter interface + implementation) | LLM abstraction layer | — | ✅ |
| 3 | Implement conversation context manager (Redis session) | Loads/saves per-conversation state | Redis | ✅ |
| 4 | Build basic AI response generation (system prompt + context) | Claude generates culturally appropriate replies | Task 2 | ✅ |
| 5 | Implement circuit breaker for Claude API calls | Fallback to template responses on failure | Task 2 | ✅ |
| 6 | Create system prompts for each language | Lingala/French/Swahili prompt templates | — | ✅ |
| 7 | Write i18n message catalog (3 languages) | All bot-facing strings externalized | — | ✅ |

### 5.3 Key Design Decisions

**Language Detection Strategy:**
1. **Primary:** Claude API analyzes message content → returns language code
2. **Fallback:** Regex-based keyword matching (e.g., "mbote" → Lingala, "bonjour" → French, "habari" → Swahili)
3. **Default:** French (most commonly understood in DRC)

**Context Memory:**
- Redis hash per conversation: last 10 messages, detected language, lead stage, conversation status
- TTL: 24 hours (refreshed on each message)
- Cold cache: Load from PostgreSQL `messages` table (last 10)

**Circuit Breaker:**
- Open after 3 consecutive Claude API failures
- Half-open after 60 seconds
- Fallback: predefined template responses per language

### 5.4 Acceptance Criteria

- [x] Send "Mbote!" → bot detects Lingala, responds in Lingala
- [x] Send "Bonjour" → bot detects French, responds in French
- [x] Send "Habari" → bot detects Swahili, responds in Swahili
- [x] Claude API down → bot responds with graceful template message
- [x] Conversation context persists across multiple messages (verified with 5+ turns)
- [x] Mixed language message (e.g., "Bonjour ndeko") → detected correctly

### 5.5 System Prompt Template (Example — Lingala)

```
Yo ozali assistant ya MBB, ndeko ya sika ya Congolais oyo azali ko-salisa batu na biloko.
- Zala na boboto, tika ko-tinda mingi, salisa liboso
- Maxi ba-phrase 2-3 na message moko
- Soki oyebi te, yebisa: "Nako-verifier mpe na-zonga epai na yo"
- JAMAIS pushy, JAMAIS robotique
```

---

## 6. Sprint 1.3 — M3: Queue & Resilience (Weeks 9–10)

### 6.1 Objective

Build the blackout-proof message queue. When FastAPI is down (power outage, restart), messages queue in Redis and are processed on recovery with a friendly "we're back" notification.

### 6.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement Redis blackout queue (AOF-backed LPUSH/RPOP) | Messages survive power outage | Redis | ✅ |
| 2 | Build queue processor Celery task (`drain_blackout_queue`) | Processes backlog on recovery | Celery | ✅ |
| 3 | Implement recovery message sender | Sends "Naza-zonga! Message na yo e-batelami ✓" | M1 | ✅ |
| 4 | Add health check endpoint `GET /api/v1/health` | Reports status of all components | FastAPI | ✅ |
| 5 | Implement graceful shutdown (finish in-flight requests) | Docker SIGTERM handling | Docker | ✅ |
| 6 | Simulate blackout test: kill FastAPI → queue → restart → process | End-to-end resilience verification | All | ✅ |

### 6.3 Blackout Queue Architecture

```
Normal Flow:
  WhatsApp → Baileys → FastAPI → Process → Reply

Blackout Flow:
  WhatsApp → Baileys → FastAPI (DOWN)
                    ↓
              Redis Queue (AOF)
                    ↓  (power returns)
              Celery drain_blackout_queue
                    ↓
              Process all queued messages
                    ↓
              Send recovery message to each customer
```

### 6.4 Recovery Messages (i18n)

| Language | Message |
|----------|---------|
| Lingala | "Naza-zonga! Message na yo e-batelami ✓" |
| French | "Nous sommes de retour ! Votre message a été conservé ✓" |
| Swahili | "Tumerudi! Ujumbe wako umehifadhiwa ✓" |

### 6.5 Acceptance Criteria

- [x] Kill FastAPI container → send 10 messages → restart → all 10 processed
- [x] Recovery messages sent to all affected customers (in their detected language)
- [x] Health endpoint returns component-level status (db, redis, celery, baileys)
- [x] Zero message loss in blackout simulation
- [x] Graceful shutdown completes in-flight requests before exiting

---

## 7. Sprint 1.4 — M4: Conversation Engine (Advanced) + M5 Entry (Weeks 11–12)

### 7.1 Objective

Complete the conversation engine with full context management, conversation status transitions, and introduce the beginning of lead qualification (M5 entry point).

### 7.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement conversation status state machine | `active → qualifying → nurturing → escalated → converted → dormant` | M4 | ✅ |
| 2 | Build opt-out detection ("stop", "arrête", "yaka te", "tika") | Instant conversation closure | M4 | ✅ |
| 3 | Implement 2–3 question qualification flow entry | Extracts initial intent signals | M4 → M5 | ✅ |
| 4 | Build lead scoring engine (hot/warm/cold) — basic version | Score based on: response speed, product specificity, city | M5 | ✅ |
| 5 | Create lead insertion logic | Lead created after qualification signals detected | DB | ✅ |
| 6 | Implement `AirtableAdapter` for CRM sync | Leads synced to Airtable via Celery task | Celery | ✅ |

### 7.3 Conversation State Machine

```
                    ┌──────────┐
     New message ──▶│  ACTIVE  │◀── Resume from dormant
                    └────┬─────┘
                         │ qualification signals detected
                    ┌────▼──────┐
                    │QUALIFYING │
                    └────┬──────┘
                    ┌────▼──────┐       ┌───────────┐
                    │ NURTURING │──────▶│ ESCALATED │
                    └────┬──────┘       └─────┬─────┘
                         │                     │ resolved
                    ┌────▼──────┐              │
                    │ CONVERTED │◀─────────────┘
                    └───────────┘
                         
         "stop" from ANY state ──▶ DORMANT (opt-out)
         Silent > 14 days ──▶ DORMANT (timeout)
```

### 7.4 Opt-Out Keywords (All Languages)

| Language | Keywords |
|----------|----------|
| French | "stop", "arrête", "arrêter", "désabonner" |
| Lingala | "yaka te", "tika", "ko-tika" |
| Swahili | "acha", "simama", "sitaki" |

### 7.5 Acceptance Criteria

- [x] New conversation → 2–3 natural questions → lead created with score
- [x] Hot lead gets product recommendation within same conversation
- [x] Lead appears in Airtable within 60 seconds (via Celery task)
- [x] Stage transitions logged in PostgreSQL
- [x] Opt-out keyword → immediate stop, confirmation message sent
- [x] Conversation status transitions follow state machine rules

---

## 8. Deliverables Checklist

| # | Deliverable | Sprint | Status |
|---|-------------|--------|--------|
| 1 | M1 webhook endpoint (inbound + outbound) | 1.1 | ✅ |
| 2 | Payload normalizer (Baileys → InboundMessageEvent) | 1.1 | ✅ |
| 3 | Rate limiter (Redis token bucket) | 1.1 | ✅ |
| 4 | Idempotency key check | 1.1 | ✅ |
| 5 | Language detection (Claude + regex fallback) | 1.2 | ✅ |
| 6 | ClaudeAdapter implementation | 1.2 | ✅ |
| 7 | Conversation context manager (Redis) | 1.2 | ✅ |
| 8 | Circuit breaker for Claude API | 1.2 | ✅ |
| 9 | i18n message catalog (3 languages) | 1.2 | ✅ |
| 10 | System prompts (Lingala/French/Swahili) | 1.2 | ✅ |
| 11 | Blackout queue (Redis AOF) | 1.3 | ✅ |
| 12 | Queue drain Celery task | 1.3 | ✅ |
| 13 | Recovery message sender | 1.3 | ✅ |
| 14 | Health check endpoint | 1.3 | ✅ |
| 15 | Graceful shutdown | 1.3 | ✅ |
| 16 | Conversation state machine | 1.4 | ✅ |
| 17 | Opt-out detection | 1.4 | ✅ |
| 18 | Lead qualification entry (basic M5) | 1.4 | ✅ |
| 19 | Lead scoring engine (basic) | 1.4 | ✅ |
| 20 | AirtableAdapter CRM sync | 1.4 | ✅ |
| 21 | Unit + integration tests (> 80% coverage) | All | ✅ |

---

## 9. File Map (Expected Output)

```
backend/
├── app/
│   ├── modules/
│   │   ├── m1_gateway/
│   │   │   ├── __init__.py
│   │   │   ├── service.py          # process_inbound, persist_outbound
│   │   │   ├── normalizer.py       # Baileys → InboundMessageEvent
│   │   │   ├── rate_limiter.py     # Redis token bucket
│   │   │   └── dispatcher.py       # Outbound to Baileys bridge
│   │   ├── m2_language/
│   │   │   ├── __init__.py
│   │   │   ├── detector.py         # Claude + regex fallback
│   │   │   └── prompts.py          # System prompts per language
│   │   ├── m3_queue/
│   │   │   ├── __init__.py
│   │   │   ├── blackout_queue.py   # Redis LPUSH/RPOP with AOF
│   │   │   └── recovery.py         # Recovery message sender
│   │   └── m4_conversation/
│   │       ├── __init__.py
│   │       ├── engine.py           # Context manager, state machine
│   │       ├── session_cache.py    # Redis session load/save
│   │       └── opt_out.py          # Opt-out keyword detection
│   ├── adapters/
│   │   ├── ai/
│   │   │   ├── base.py             # AIAdapter interface
│   │   │   └── claude.py           # ClaudeAdapter + circuit breaker
│   │   └── crm/
│   │       ├── base.py             # CRMAdapter interface
│   │       └── airtable.py         # AirtableAdapter implementation
│   ├── i18n/
│   │   ├── messages.py             # i18n catalog
│   │   └── templates/
│   │       ├── lingala.json
│   │       ├── french.json
│   │       └── swahili.json
│   └── tasks/
│       └── m1.py                   # Updated: full pipeline task
```

---

## 10. Risk Mitigation (Phase 1.A Specific)

| Risk | Impact | Mitigation |
|------|--------|------------|
| Claude API rate limits during testing | Slow dev velocity | Use cached responses for repeated test inputs |
| Lingala AI quality poor | Users distrust bot | Weekly native tone audits; French fallback |
| Redis data loss during blackout | Message loss | AOF persistence + test with `kill -9` |
| Baileys connection drops | No messages received | Auto-reconnect logic + monitoring alert |
| Mixed language detection fails | Wrong language response | Default to French + ask user to confirm |
