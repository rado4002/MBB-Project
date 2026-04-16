# Phase 1.B — Lead Pipeline

**MBB ya Kin — Sub-Phase Specification**

| Field | Value |
|-------|-------|
| **Phase** | 1.B |
| **Name** | Lead Pipeline |
| **Weeks** | 13–16 (4 weeks) |
| **Sprints** | 1.5, 1.6 |
| **Modules** | M5 (Lead Qualification & Nurturing), M6 (Relance Engine) |
| **Status** | ✅ COMPLETE (Sprint 1.5 ✅, Sprint 1.6 ✅) |

---

## 1. Goal

Bot autonomously qualifies leads through smart questions, scores them (hot/warm/cold), and sends up to 3 relances with different persuasion hooks — all while respecting opt-out and quiet hours.

**Milestone:** 50 simulated leads → bot qualifies, scores, sends relances automatically, no spam complaints.

**The Kinshasa Test:** A customer asks about a product at 14h → goes silent → bot sends relance #1 at 14h+24 with a value hook in their language → customer replies → bot nurtures with recommendation → customer goes silent again → relance #2 at 72h with different angle → still silent → final relance #3 at 7 days → customer says "arrête" → bot stops immediately, forever.

---

## 2. Success Metrics (Stage Gate)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Qualification Rate | > 70% of conversations reach lead stage | `SELECT COUNT(*) FROM leads / COUNT(*) FROM conversations` |
| Lead Scoring Accuracy | 80%+ manual alignment | Hub team reviews 50 random leads |
| Relance Response Rate | 35–45% reply to 1st relance | Track `replied_to_relance_1` field |
| Relance Cadence Compliance | 100% on-time | Celery Beat logs vs. expected schedule |
| Opt-Out Rate | < 8% | Count opt-outs / total relances sent |

**Exit Criteria:** Complete Sprint 1.5–1.6 acceptance criteria. Demo: 50 simulated leads → bot qualifies, scores, sends relances automatically, zero spam complaints.

---

## 3. Dependencies

| Dependency | Source | Status |
|------------|--------|--------|
| M1 Gateway operational (inbound + outbound) | Phase 1.A Sprint 1.1 | ✅ Done |
| M2 Language detection working | Phase 1.A Sprint 1.2 | ✅ Done |
| M4 Conversation engine with context | Phase 1.A Sprint 1.2 | ✅ Done |
| M3 Blackout queue resilience | Phase 1.A Sprint 1.3 | ✅ Done |
| Basic lead creation from Sprint 1.4 | Phase 1.A Sprint 1.4 | ✅ Done |
| Celery Beat scheduler running | Phase 0 | ✅ Done |

---

## 4. Sprint 1.5 — M5: Lead Qualification & Nurturing (Full) (Weeks 13–14)

### 4.1 Objective

Complete the lead qualification pipeline: detect buying signals → ask 2–3 smart questions → score the lead → trigger nurturing with product recommendations and persuasion hooks.

### 4.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Complete qualification flow (2–3 natural questions) | Extracts city, intent, product interest, budget range | Sprint 1.4 | ✅ |
| 2 | Implement full lead scoring engine | Score based on: response speed, product specificity, city, engagement | Sprint 1.4 | ✅ |
| 3 | Build stage progression (AWARENESS → CONSIDERATION → DECISION) | StoryBrand-based funnel transitions | Lead scoring | ✅ |
| 4 | Build nurturing response generator | Product recommendations + persuasion hooks | M4 + ClaudeAdapter | ✅ |
| 5 | Implement follow-up product suggestions | Based on lead profile + conversation history | ClaudeAdapter | ✅ |
| 6 | Implement lead stage transition logging | PostgreSQL audit trail | DB | ✅ |
| 7 | Write unit + integration tests for M5 | > 80% coverage | All M5 | ✅ |

### 4.3 Lead Scoring Logic

```
Score = weighted_sum(signals)

Signals:
  ┌─────────────────────────┬────────┬──────────────────────────────────────┐
  │ Signal                  │ Weight │ Example                              │
  ├─────────────────────────┼────────┼──────────────────────────────────────┤
  │ Response speed          │ 25%    │ < 5 min = hot, < 1h = warm, > 1h = cold │
  │ Product specificity     │ 30%    │ "Cable HDMI 2m" = hot, "cables" = warm │
  │ City mentioned          │ 15%    │ Kinshasa/Lubumbashi = higher intent │
  │ Price inquiry           │ 20%    │ Asks price = strong buying signal   │
  │ Engagement depth        │ 10%    │ 5+ messages = warm, 10+ = hot      │
  └─────────────────────────┴────────┴──────────────────────────────────────┘

  HOT:  score ≥ 70  →  immediate product recommendation
  WARM: score 40–69 →  nurture with value content
  COLD: score < 40  →  light touch, schedule relance
```

### 4.4 Qualification Questions (Natural, Not Interrogative)

| # | Purpose | Example (Lingala) | Example (French) |
|---|---------|-------------------|-------------------|
| 1 | Intent | "Ozali ko-luka nini exactement?" | "Tu cherches quoi exactement ?" |
| 2 | Location | "Ozali wapi na Kin? To-ko-deliver" | "T'es dans quel coin ? On peut livrer" |
| 3 | Urgency | "Olingi yango nini mbala moko?" | "T'en as besoin pour quand ?" |

### 4.5 Acceptance Criteria

- [x] New conversation with buying signals → 2–3 natural questions → lead created with score
- [x] Hot lead gets immediate product recommendation
- [x] Warm lead enters nurturing flow with value content
- [x] Cold lead scheduled for relance (handled in Sprint 1.6)
- [x] Stage transitions (AWARENESS → CONSIDERATION → DECISION) logged with timestamps
- [x] Lead data synced to Airtable within 60 seconds

---

## 5. Sprint 1.6 — M6: Relance Engine (Weeks 15–16)

### 5.1 Objective

Build the automated follow-up system: detect silent leads, schedule relances at proper intervals, generate unique value hooks for each attempt, respect quiet hours and opt-out.

### 5.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement Celery Beat schedule: check eligible leads every hour | Periodic relance scanner | Celery Beat | ✅ |
| 2 | Build relance eligibility query | Silent > 24h, count < 3, not opted out, not converted | DB | ✅ |
| 3 | Implement blackout hour guard (22:00–07:00 Africa/Kinshasa) | Timezone-aware scheduling | Celery | ✅ |
| 4 | Build value-hook generator (3 different angles per attempt) | Claude generates unique hooks | ClaudeAdapter | ✅ |
| 5 | Create relance records in PostgreSQL | Track attempts, responses, outcomes | DB | ✅ |
| 6 | Implement max-3 relance hard limit at service layer | Prevents over-messaging | DB + service | ✅ |
| 7 | Add opt-out detection integrated with relance cancellation | Instant relance cancellation on "stop" | M4 | ✅ |
| 8 | Write unit + integration tests for M6 | > 80% coverage | All M6 | ✅ |

### 5.3 Relance Schedule

```
Timeline (from last customer message):

  ┌───────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ Msg   │    │Relance #1│    │Relance #2│    │Relance #3│
  │ Recv  │───▶│  +24h    │───▶│ +48–72h  │───▶│ +7–10d   │───▶ Mark COLD
  └───────┘    └──────────┘    └──────────┘    └──────────┘
                    │               │               │
                    ▼               ▼               ▼
              Value hook #1   Different angle   Final attempt
              "Hey! Biloko     "Ndeko, batu      "Dernière fois,
               ya sika..."     mingi ba-zali      promo spéciale
                                ko-zua..."        pour yo..."

  ⛔ HARD RULES:
  • Max 3 relances per lead (NEVER exceed)
  • No relances 22:00–07:00 Africa/Kinshasa
  • Opt-out = immediate permanent stop
  • Different hook angle each time (NEVER repeat)
```

### 5.4 Relance Hook Angles

| Attempt | Angle | Tone |
|---------|-------|------|
| #1 (+24h) | Value reminder | "Hey! The [product] you were looking at is still available" |
| #2 (+48–72h) | Social proof | "Others in [city] are loving this..." |
| #3 (+7–10d) | Exclusive offer | "Last chance — special offer just for you" |

### 5.5 Quiet Hours Logic

```python
# Pseudocode
kinshasa_tz = ZoneInfo("Africa/Kinshasa")
now_kin = datetime.now(kinshasa_tz)
if now_kin.hour >= 22 or now_kin.hour < 7:
    # Reschedule to 07:00 next day
    next_send = now_kin.replace(hour=7, minute=0) + timedelta(days=1 if now_kin.hour >= 22 else 0)
    task.apply_async(eta=next_send)
```

### 5.6 Acceptance Criteria

- [x] Lead silent for 24h → receives relance #1 (value hook, not pushy)
- [x] Lead silent for 72h → receives relance #2 (different angle)
- [x] Lead silent for 7 days → receives relance #3 (final, exclusive offer)
- [x] Lead says "arrête" → all future relances cancelled permanently
- [x] Lead says "tika" (Lingala) → same instant cancellation
- [x] No relance sent between 22:00–07:00 Kinshasa time
- [x] Lead with 3 relances and no response → marked COLD, no further attempts
- [x] Each relance has a different hook (verified by reviewing content)
- [x] Relance records stored in PostgreSQL with timestamps and outcomes

---

## 6. Deliverables Checklist

| # | Deliverable | Sprint | Status |
|---|-------------|--------|--------|
| 1 | Full qualification flow (2–3 questions) | 1.5 | ✅ |
| 2 | Lead scoring engine (hot/warm/cold) | 1.5 | ✅ |
| 3 | Stage progression (StoryBrand funnel) | 1.5 | ✅ |
| 4 | Nurturing response generator | 1.5 | ✅ |
| 5 | AirtableAdapter CRM sync (full) | 1.5 | ✅ |
| 6 | Relance eligibility scanner (Celery Beat) | 1.6 | ✅ |
| 7 | Blackout hour guard (22:00–07:00) | 1.6 | ✅ |
| 8 | Value-hook generator (3 angles) | 1.6 | ✅ |
| 9 | Max-3 relance hard limit | 1.6 | ✅ |
| 10 | Opt-out → relance cancellation | 1.6 | ✅ |
| 11 | Relance records in PostgreSQL | 1.6 | ✅ |
| 12 | Unit + integration tests (> 80% coverage) | Both | ✅ M5: 18/18, M6: 7/7 |

---

## 7. File Map (Expected Output)

```
backend/
├── app/
│   ├── modules/
│   │   ├── m5_qualification/
│   │   │   ├── __init__.py
│   │   │   ├── service.py          # ✅ qualification flow orchestration
│   │   │   ├── scorer.py           # ✅ lead scoring engine (0-100 weighted)
│   │   │   ├── nurturing.py        # ✅ product recommendations, hooks
│   │   │   └── stages.py           # ✅ StoryBrand stage transitions
│   │   └── m6_relance/
│   │       ├── __init__.py         # ✅ (Sprint 1.6)
│   │       ├── service.py          # ✅ relance orchestration
│   │       ├── eligibility.py      # ✅ eligible lead query
│   │       ├── hooks.py            # ✅ value-hook generator (3 angles)
│   │       └── scheduler.py        # ✅ quiet hours + cadence logic
│   ├── models/
│   │   └── lead_stage_transition.py # ✅ audit trail table
│   ├── i18n/
│   │   └── templates/
│   │       ├── french.json         # ✅ updated (nurturing + relance templates)
│   │       ├── lingala.json        # ✅ updated (nurturing + relance templates)
│   │       └── swahili.json        # ✅ updated (nurturing + relance templates)
│   └── tasks/
│       ├── m5.py                   # ✅ CRM sync task (from Sprint 1.4)
│       └── relance.py              # ✅ (Sprint 1.6) full relance Celery tasks
└── tests/
    ├── test_m5_sprint15.py         # ✅ 18/18 tests passing
    ├── test_m6_sprint16.py         # ✅ 20 unit tests (pytest format)
    └── test_m6_quick.py            # ✅ 7/7 validation tests passing
```

---

## 8. Risk Mitigation (Phase 1.B Specific)

| Risk | Impact | Mitigation |
|------|--------|------------|
| Relances perceived as spam | High opt-out rate, brand damage | Max 3 hard limit, value-first hooks, native tone review |
| Lead scoring inaccurate | Wasted effort on cold leads | Weekly manual review of 50 leads, adjust weights |
| Quiet hours miscalculated | Relances at 3 AM → instant opt-out | Use `Africa/Kinshasa` timezone explicitly, test edge cases |
| Airtable API rate limits | CRM sync delayed | Batch sync via Celery (max 5 req/sec), retry with backoff |
| Claude generates repetitive hooks | All 3 relances sound same | Include previous hooks in prompt context, enforce variation |
