# Phase 1.D — Intelligence & Oversight

**MBB ya Kin — Sub-Phase Specification**

| Field | Value |
|-------|-------|
| **Phase** | 1.D |
| **Name** | Intelligence & Oversight |
| **Weeks** | 19–20 (2 weeks) |
| **Sprints** | 1.8 |
| **Modules** | M8 (MAPS Intelligence + Escalation), M9 (Analytics Dashboard + Admin Operations) |
| **Status** | ⬜ Not Started |

---

## 1. Goal

Capture demand intelligence (MAPS tags) from every conversation, build the escalation pipeline for complex issues, and deliver a full Streamlit dashboard with role-based admin operations.

**Milestone:** MAPS tags on 100% of conversations. Dashboard shows live funnel metrics. Admin toggles handoff. Hub resolves escalation. All actions audit-logged.

**The Kinshasa Test:** A customer sends a voice note about a complex product issue → bot escalates to Hub team within 3 minutes with full context (last 10 messages) → Hub resolves → conversation returns to bot → MAPS tags capture the demand signal + silence reason → Lab team views trend on dashboard → admin edits relance template → change takes effect immediately.

---

## 2. Success Metrics (Stage Gate)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| MAPS Tag Coverage | 100% conversations tagged | `SELECT COUNT(*) FROM maps_tags / COUNT(*) FROM conversations` |
| Escalation SLA | < 3 min for voice notes | `escalation_tickets.created_at - messages.created_at` |
| Dashboard Accuracy | 100% metric match | Compare dashboard charts to raw SQL queries |
| Admin Operations | 10+ actions logged | Count `admin_audit_log` entries |
| Role Enforcement | 0 permission leaks | Attempt unauthorized actions with each role |

**Exit Criteria:** Complete Sprint 1.8 acceptance criteria. Demo: Dashboard shows live metrics, admin toggles handoff, Hub resolves escalation, all actions logged.

---

## 3. Dependencies

| Dependency | Source | Status |
|------------|--------|--------|
| M1–M4 conversational pipeline | Phase 1.A | ⬜ |
| M5 Lead qualification + scoring | Phase 1.B | ⬜ |
| M6 Relance engine | Phase 1.B | ⬜ |
| M7 Order + payment pipeline | Phase 1.C | ⬜ |
| Materialized views in PostgreSQL | Phase 0 (schema) | ✅ Done |
| Streamlit container running | Phase 0 | ✅ Done |

---

## 4. Sprint 1.8 — M8: MAPS Intelligence + Escalation (Part 1, Week 19)

### 4.1 Objective

Auto-tag every message interaction with MAPS categories, build escalation triggers, and implement the Hub team notification + handoff flow.

### 4.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement MAPS tag capture Celery task | Auto-tags every message interaction | Celery | ⬜ |
| 2 | Build tag categories: `product_demand`, `silence_reason`, `conversion_trigger`, `language_usage`, `opt_out_reason` | Structured taxonomy | DB | ⬜ |
| 3 | Create/refresh materialized view `mv_daily_maps_summary` | Pre-aggregated insights for dashboard | PostgreSQL | ⬜ |
| 4 | Implement escalation trigger detection | Voice note, complex complaint, high-value lead, 3+ unresolved | M4 | ⬜ |
| 5 | Build escalation ticket creation + Hub Team notification | Celery task → notification | Celery | ⬜ |
| 6 | Implement conversation handoff (bot → human → bot) | Status: `active ↔ escalated` | DB | ⬜ |
| 7 | Write unit + integration tests for M8 | > 80% coverage | All M8 | ⬜ |

### 4.3 MAPS Tag Categories

```
MAPS Intelligence Framework:
┌──────────────────────────────────────────────────────────────┐
│                     MAPS TAG TAXONOMY                        │
├─────────────────────┬────────────────────────────────────────┤
│ product_demand      │ What products are people asking about? │
│                     │ Tags: product_name, category, price_rg │
├─────────────────────┼────────────────────────────────────────┤
│ silence_reason      │ Why did the customer go silent?        │
│                     │ Tags: price_high, no_stock, slow_reply │
│                     │       competitor, distrust, blackout   │
├─────────────────────┼────────────────────────────────────────┤
│ conversion_trigger  │ What made the customer buy?            │
│                     │ Tags: promo, recommendation, urgency,  │
│                     │       social_proof, relance_hook       │
├─────────────────────┼────────────────────────────────────────┤
│ language_usage      │ Language distribution across convos    │
│                     │ Tags: lingala, french, swahili, mixed  │
├─────────────────────┼────────────────────────────────────────┤
│ opt_out_reason      │ Why did the customer opt out?          │
│                     │ Tags: too_many_messages, not_interested│
│                     │       wrong_product, privacy_concern   │
└─────────────────────┴────────────────────────────────────────┘
```

### 4.4 Escalation Triggers

| Trigger | Priority | Action |
|---------|----------|--------|
| Voice note received | HIGH | Immediate escalation + context |
| Complex complaint detected (AI classification) | HIGH | Escalation with complaint summary |
| High-value lead (score ≥ 80) | MEDIUM | Notify Hub for personal follow-up |
| 3+ unresolved questions in conversation | MEDIUM | Escalation with question list |
| Customer requests human ("parler à quelqu'un") | HIGH | Immediate handoff |

### 4.5 Handoff Flow

```
Normal (Bot handles):
  Customer ──▶ Bot ──▶ Response

Escalation:
  Customer ──▶ Bot detects trigger
                 │
                 ▼
           Create escalation ticket
                 │
                 ▼
           Notify Hub Team (via dashboard + optional webhook)
                 │
                 ▼
           Bot sends: "Ndeko moko ya équipe na biso akoyamba yo noki ✓"
                 │
                 ▼
           Conversation status → ESCALATED
                 │
                 ▼
           Hub responds via dashboard → message sent to customer
                 │
                 ▼
           Hub resolves ticket → status → ACTIVE (back to bot)
```

### 4.6 Acceptance Criteria (M8)

- [ ] Every message generates at least one MAPS tag
- [ ] Voice note → immediate escalation ticket → Hub notified in < 3 min
- [ ] Escalation ticket includes last 10 messages as context
- [ ] Hub resolves ticket → conversation returns to bot control
- [ ] Customer requesting human ("parler à quelqu'un") → immediate handoff
- [ ] Materialized view refreshes on schedule (Celery Beat)

---

## 5. Sprint 1.8 — M9: Analytics Dashboard + Admin Operations (Part 2, Week 20)

### 5.1 Objective

Build the Streamlit dashboard with analytics visualizations and role-based admin pages for Lab, Hub, and Admin teams.

### 5.2 Tasks — Analytics Dashboard

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Build Streamlit app with page navigation | Running on port 8501 | Docker | ⬜ |
| 2 | Implement conversion funnel visualization | Lead → Qualified → Nurtured → Converted | PostgreSQL | ⬜ |
| 3 | Implement relance performance dashboard | Response rates by attempt #, hook type, time of day | PostgreSQL | ⬜ |
| 4 | Implement language breakdown chart | Lingala/French/Swahili distribution | MAPS tags | ⬜ |
| 5 | Implement MAPS insights explorer | Top patterns, trends, anomalies | Materialized views | ⬜ |
| 6 | Add CSV + Google Sheets export | One-click data export | Streamlit | ⬜ |
| 7 | Add response time monitoring | p50, p95, p99 latency charts | Prometheus / logs | ⬜ |

### 5.3 Tasks — Admin Operations (Role-Based)

| # | Task | Role | Deliverable | Status |
|---|------|------|-------------|--------|
| 8 | Bot Configuration page | admin | Master prompt editor, relance templates, feature flags, adapter switch | ⬜ |
| 9 | Escalation Manager page | admin | Ticket list, assign, resolve, re-escalate | ⬜ |
| 10 | Content Manager page | admin | Static catalog fallback, i18n templates, relance hooks | ⬜ |
| 11 | System Control page | admin | Circuit breaker states, queue depth, dead-letter retry, adapter health | ⬜ |
| 12 | Lead Operations page | hub | Lead detail view, status override with justification | ⬜ |
| 13 | Escalation Response page | hub | Assigned tickets, resolution form | ⬜ |
| 14 | Tone Audit Console | lab | Random 10% sample review, approve/flag actions | ⬜ |
| 15 | MAPS Tag Manager | lab | Validate, merge, retire patterns | ⬜ |
| 16 | Create `admin_audit_log` entries | all | Append-only audit trail for all write ops | ⬜ |
| 17 | Implement role-gated page routing | all | Pages visible only to authorized roles | ⬜ |

### 5.4 Dashboard Layout

```
┌──────────────────────────────────────────────────────────────┐
│  MBB ya Kin — Dashboard                     [Role: admin ▼] │
├──────────────┬───────────────────────────────────────────────┤
│ NAVIGATION   │                                               │
│              │  📊 ANALYTICS                                 │
│ 📊 Analytics │  ┌─────────┬─────────┬─────────┬──────────┐  │
│   Funnel     │  │ Leads   │Qualified│Nurtured │Converted │  │
│   Relance    │  │  342    │  245    │  156    │   47     │  │
│   Languages  │  └─────────┴─────────┴─────────┴──────────┘  │
│   MAPS       │                                               │
│   Response ⏱ │  📈 Relance Performance                      │
│              │  ┌──────────────────────────────────────────┐ │
│ 🔧 Admin     │  │ Attempt #1: 42% response rate           │ │
│   Bot Config │  │ Attempt #2: 28% response rate           │ │
│   Escalation │  │ Attempt #3: 15% response rate           │ │
│   Content    │  └──────────────────────────────────────────┘ │
│   System     │                                               │
│              │  🌍 Language Distribution                     │
│ 👥 Hub       │  ┌──────────────────────────────────────────┐ │
│   Leads      │  │ French: 52% | Lingala: 35% | Swahili: 13%│ │
│   Escalation │  └──────────────────────────────────────────┘ │
│              │                                               │
│ 🔬 Lab       │  📅 Date Range: [2026-04-01] → [2026-04-17] │
│   Tone Audit │  📥 Export: [CSV] [Google Sheets]            │
│   MAPS Tags  │                                               │
└──────────────┴───────────────────────────────────────────────┘
```

### 5.5 Role-Based Access Matrix

| Page | admin | hub | lab | Description |
|------|-------|-----|-----|-------------|
| Analytics — Funnel | ✅ | ✅ | ✅ | Read-only funnel metrics |
| Analytics — Relance | ✅ | ✅ | ✅ | Read-only relance performance |
| Analytics — Languages | ✅ | ✅ | ✅ | Read-only language distribution |
| Analytics — MAPS | ✅ | ❌ | ✅ | Read-only MAPS insights |
| Analytics — Response Time | ✅ | ❌ | ❌ | System performance |
| Bot Configuration | ✅ | ❌ | ❌ | Write: prompts, templates, flags |
| Escalation Manager | ✅ | ❌ | ❌ | Write: assign, resolve tickets |
| Content Manager | ✅ | ❌ | ❌ | Write: catalog, i18n, hooks |
| System Control | ✅ | ❌ | ❌ | Write: circuit breakers, queues |
| Lead Operations | ❌ | ✅ | ❌ | Write: status override |
| Escalation Response | ❌ | ✅ | ❌ | Write: resolve assigned tickets |
| Tone Audit Console | ❌ | ❌ | ✅ | Write: approve/flag responses |
| MAPS Tag Manager | ❌ | ❌ | ✅ | Write: validate/merge/retire |

### 5.6 Acceptance Criteria (M9)

- [ ] Dashboard loads in < 5 seconds
- [ ] Funnel shows correct counts across all stages
- [ ] CSV export contains all visible data
- [ ] Date range filter works across all charts
- [ ] Admin pages visible only to `admin` role
- [ ] Hub pages visible only to `hub` role
- [ ] Lab pages visible only to `lab` role
- [ ] All admin write operations logged in `admin_audit_log` table
- [ ] Lead status override requires justification (enforced at API level)
- [ ] Escalation assignment and resolution work end-to-end
- [ ] Feature flag toggle takes effect without service restart

---

## 6. Deliverables Checklist

| # | Deliverable | Module | Status |
|---|-------------|--------|--------|
| 1 | MAPS tag capture Celery task | M8 | ⬜ |
| 2 | 5 tag categories with taxonomy | M8 | ⬜ |
| 3 | Materialized view refresh | M8 | ⬜ |
| 4 | Escalation trigger detection | M8 | ⬜ |
| 5 | Escalation ticket creation + notification | M8 | ⬜ |
| 6 | Conversation handoff (bot ↔ human) | M8 | ⬜ |
| 7 | Streamlit dashboard (7 analytics pages) | M9 | ⬜ |
| 8 | Admin operations pages (4 pages) | M9 | ⬜ |
| 9 | Hub operations pages (2 pages) | M9 | ⬜ |
| 10 | Lab operations pages (2 pages) | M9 | ⬜ |
| 11 | Role-gated page routing | M9 | ⬜ |
| 12 | Admin audit log (append-only) | M9 | ⬜ |
| 13 | CSV + Google Sheets export | M9 | ⬜ |
| 14 | Unit + integration tests (> 80% coverage) | Both | ⬜ |

---

## 7. File Map (Expected Output)

```
backend/
├── app/
│   ├── modules/
│   │   ├── m8_maps/
│   │   │   ├── __init__.py
│   │   │   ├── tagger.py           # MAPS tag capture logic
│   │   │   ├── categories.py       # Tag taxonomy definitions
│   │   │   └── escalation.py       # Trigger detection + ticket creation
│   │   └── m9_admin/
│   │       ├── __init__.py
│   │       ├── audit.py            # Audit log operations
│   │       └── config.py           # Feature flags, prompt management
│   └── tasks/
│       ├── maps.py                 # Updated: full MAPS tagging tasks
│       └── escalation.py           # Updated: notification + handoff

dashboard/
├── app/
│   ├── main.py                     # Streamlit entry point with navigation
│   ├── pages/
│   │   ├── analytics/
│   │   │   ├── funnel.py
│   │   │   ├── relance.py
│   │   │   ├── languages.py
│   │   │   ├── maps_insights.py
│   │   │   └── response_time.py
│   │   ├── admin/
│   │   │   ├── bot_config.py
│   │   │   ├── escalation_manager.py
│   │   │   ├── content_manager.py
│   │   │   └── system_control.py
│   │   ├── hub/
│   │   │   ├── lead_operations.py
│   │   │   └── escalation_response.py
│   │   └── lab/
│   │       ├── tone_audit.py
│   │       └── maps_tag_manager.py
│   └── utils/
│       ├── auth.py                 # Role-based access control
│       ├── db.py                   # Database connection
│       └── export.py               # CSV + Sheets export helpers
```

---

## 8. Risk Mitigation (Phase 1.D Specific)

| Risk | Impact | Mitigation |
|------|--------|------------|
| MAPS tagging slows response time | > 60s response | Tag asynchronously via Celery (don't block response) |
| Hub team slow to respond to escalations | Customer frustration | Auto-reassign after 30 min; fallback bot response |
| Dashboard DB queries too slow | > 5s page load | Use materialized views; add indexes; cache with Redis |
| Too many MAPS tags (noise) | Insights buried | Claude-powered relevance filtering; merge similar tags |
| Role bypass via direct API calls | Unauthorized actions | JWT role enforcement at FastAPI middleware level |
