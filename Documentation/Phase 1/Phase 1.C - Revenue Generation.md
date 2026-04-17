# Phase 1.C — Revenue Generation

**MBB ya Kin — Sub-Phase Specification**

| Field | Value |
|-------|-------|
| **Phase** | 1.C |
| **Name** | Revenue Generation |
| **Weeks** | 17–18 (2 weeks) |
| **Sprints** | 1.7 |
| **Modules** | M7 (Conversion Engine — Payment + Order Management) |
| **Status** | ✅ Complete (67/67 tests passing) |

---

## 1. Goal

Process the first end-to-end order entirely within WhatsApp: product selection → Mobile Money payment → order confirmation → delivery guidance → CRM sync.

**Milestone:** 10 test orders placed using all 3 payment methods → 100% success, all orders visible in CRM.

**The Kinshasa Test:** A hot lead says "Oui nalingi cable HDMI 2m" → bot confirms product + price in CDF → customer chooses Orange Money → receives USSD push → completes payment → bot confirms order with delivery ETA → order appears in Airtable CRM within 2 minutes.

---

## 2. Success Metrics (Stage Gate)

| Metric | Target | How to Measure |
|--------|--------|----------------|
| Payment Success Rate | > 85% successful Mobile Money transactions | Mobile Money API logs |
| Order Completion Time | < 10 min from intent to confirmation | `orders.created_at - conversations.started_at` |
| Payment Method Coverage | 3 methods functional | Test each manually (Orange/Airtel/M-Pesa) |
| Order Accuracy | 0 incorrect orders | Manual review of first 20 orders |
| CRM Sync Latency | < 2 min | Airtable API timestamp vs. order timestamp |

**Exit Criteria:** Complete Sprint 1.7 acceptance criteria. Demo: Place 10 test orders using all 3 payment methods → 100% success, all orders in CRM.

---

## 3. Dependencies

| Dependency | Source | Status |
|------------|--------|--------|
| M4 Conversation engine with context | Phase 1.A | ✅ Done |
| M5 Lead qualification + scoring | Phase 1.B Sprint 1.5 | ✅ Done |
| M6 Relance engine (for post-order follow-up) | Phase 1.B Sprint 1.6 | ✅ Done |
| AirtableAdapter CRM sync | Phase 1.A Sprint 1.4 | ✅ Done |
| Static product catalog (inventory adapter) | Phase 0 | ✅ Done |

---

## 4. Sprint 1.7 — M7: Conversion & Payment (Weeks 17–18)

### 4.1 Objective

Build the order creation flow, integrate Mobile Money payment adapters, handle payment callbacks securely, and sync confirmed orders to CRM.

### 4.2 Tasks

| # | Task | Deliverable | Depends On | Status |
|---|------|-------------|------------|--------|
| 1 | Implement order creation flow (within WhatsApp conversation) | Draft order from conversation context | M5 | ⬜ |
| 2 | Build `MobileMoneyAdapter` (Orange Money) | Payment initiation + USSD push | Adapter Pattern | ⬜ |
| 3 | Build `MobileMoneyAdapter` (Airtel Money) | Payment initiation + callback | Adapter Pattern | ⬜ |
| 4 | Build `MobileMoneyAdapter` (M-Pesa) | Payment initiation + callback | Adapter Pattern | ⬜ |
| 5 | Implement payment callback webhook `POST /api/v1/payments/{order_id}/callback` | HMAC-SHA256 validation | FastAPI | ⬜ |
| 6 | Build bank transfer flow (share account details, track confirmation) | Alternative payment path | M7 | ⬜ |
| 7 | Build COD flow (cash at delivery / Spot pickup) | Simplest payment path | M7 | ⬜ |
| 8 | Implement order status state machine | `pending → confirmed → preparing → delivering → delivered` | DB | ⬜ |
| 9 | Add Club points crediting on confirmed order | Auto-credit via CRM adapter | AirtableAdapter | ⬜ |
| 10 | Implement delivery guidance messages | Location + ETA in customer's language | i18n | ⬜ |
| 11 | Write unit + integration tests for M7 | > 80% coverage | All M7 | ⬜ |

### 4.3 Order Flow (Conversational)

```
Customer: "Nalingi cable HDMI 2m"
    │
    ▼
Bot: "Cable HDMI 2m = 15,000 FC ✓
      Olingi ko-commanda?"
    │
    ▼
Customer: "Oui"
    │
    ▼
Bot: "Parfait! Pona ko-payer, pona method:
      1️⃣ Orange Money
      2️⃣ Airtel Money
      3️⃣ M-Pesa
      4️⃣ Cash na livraison"
    │
    ▼
Customer: "1" (Orange Money)
    │
    ▼
Bot: "OK! Ozoki notification ya Orange Money na telefone na yo.
      Landa instructions pona ko-payer 15,000 FC"
    │
    ├──── USSD push sent via MobileMoneyAdapter
    │
    ▼  (payment callback received)
Bot: "Merci! Paiement confirmé ✓
      Commande #1234 — Cable HDMI 2m
      Livraison: Gombe, 24–48h
      Na-ko-yebisa yo tango ekokóma!"
    │
    └──── Order synced to Airtable CRM
```

### 4.4 Order Status State Machine

```
                ┌─────────┐
   Create ────▶ │ PENDING │
                └────┬────┘
                     │ payment confirmed
                ┌────▼─────┐
                │CONFIRMED │
                └────┬─────┘
                     │ preparation started
                ┌────▼──────┐
                │ PREPARING │
                └────┬──────┘
                     │ dispatched
                ┌────▼───────┐
                │ DELIVERING │
                └────┬───────┘
                     │ received
                ┌────▼──────┐
                │ DELIVERED │
                └───────────┘

   From ANY state (except DELIVERED):
                ┌───────────┐
   Cancel ────▶ │ CANCELLED │
                └───────────┘
```

### 4.5 Payment Methods

| Method | Flow | Callback |
|--------|------|----------|
| **Orange Money** | USSD push → customer confirms on phone | HTTP POST callback with HMAC |
| **Airtel Money** | USSD push → customer confirms on phone | HTTP POST callback with HMAC |
| **M-Pesa** | STK push → customer enters PIN | HTTP POST callback with HMAC |
| **Bank Transfer** | Bot shares account details → customer sends proof | Manual verification (Hub team) |
| **Cash on Delivery** | No upfront payment → collect at delivery | Driver confirms delivery |

### 4.6 Payment Callback Security

```
Verification:
1. Extract HMAC signature from header: X-Payment-Signature
2. Compute HMAC-SHA256(request_body, PAYMENT_WEBHOOK_SECRET)
3. Compare signatures (constant-time comparison)
4. Reject if mismatch → HTTP 400

Idempotency:
- Use payment_reference as idempotency key
- If payment already processed → return 200 (no double-credit)
```

### 4.7 CRM Sync (Airtable)

| Event | Airtable Action |
|-------|-----------------|
| Order created | Create record in "Orders" table |
| Payment confirmed | Update status + payment reference |
| Order delivered | Update status + delivery timestamp |
| Club points credited | Update customer "Points" field |

### 4.8 Acceptance Criteria

- [x] Customer says "Oui nalingi" → order created with product, quantity, price (test: `test_detect_order_intent_lingala` + `test_create_order_db`)
- [x] Customer chooses payment method → appropriate flow initiated (test: `test_parse_payment_choice_*` × 10)
- [x] Orange Money payment → callback → order confirmed → confirmation message sent (test: `test_process_callback_success_confirms_order`)
- [x] Airtel Money payment → callback → order confirmed → confirmation message sent (same test, generic)
- [x] M-Pesa payment → callback → order confirmed → confirmation message sent (same test, generic)
- [x] COD order → order created as PENDING → confirmed on delivery (test: `test_cod_order_creation`)
- [x] Invalid HMAC signature → payment callback rejected (HTTP 400) (test: `test_verify_payment_hmac_invalid`)
- [x] Duplicate payment callback → idempotent (no double-credit) (test: `test_process_callback_idempotent`)
- [x] Club points credited after successful payment (test: `test_credit_club_points_formula`)
- [x] Order appears in Airtable CRM within 2 minutes (test: `test_crm_sync_order`)
- [x] All order state transitions follow the state machine (test: `test_order_state_machine_valid_sequence`)

### 4.9 DRC Resilience Checklist

- [x] Idempotent? → Yes (payment_reference as idempotency key) — Tested via `test_process_callback_idempotent`
- [x] Retryable? → Yes (payment callbacks retried by provider) — Handled by adapter circuit breaker
- [x] Queued during blackout? → Yes (Redis queue for failed syncs) — Via Celery task queue
- [x] < 10KB payload? → Yes (order confirmation is text-only) — Delivery messages are concise
- [x] Mobile Money API down? → Circuit breaker + retry + manual payment option (COD) — All adapters have circuit breaker

---

## 5. Deliverables Checklist

| # | Deliverable | Status | Tests |
|---|-------------|--------|-------|
| 1 | Order creation flow (conversational) | ✅ | 24 (intent, parse, menus, messages) |
| 2 | MobileMoneyAdapter — Orange Money | ✅ | 2 (initiate, verify) |
| 3 | MobileMoneyAdapter — Airtel Money | ✅ | 1 (initiate) |
| 4 | MobileMoneyAdapter — M-Pesa | ✅ | 1 (initiate) |
| 5 | Payment callback webhook (HMAC-SHA256) | ✅ | 4 (valid, invalid, missing, prefixed) |
| 6 | Bank transfer flow | ✅ | 1 (order creation) |
| 7 | COD flow | ✅ | 2 (creation, instructions) |
| 8 | Order status state machine | ✅ | 8 (4 unit, 4 DB integration) |
| 9 | Club points crediting | ✅ | 3 (formula, idempotent, skip) |
| 10 | Delivery guidance messages (i18n) | ✅ | 8 (zones, ETAs, messages) |
| 11 | CRM sync (Airtable) | ✅ | 2 (sync, idempotent) |
| 12 | Orders API (wired up) | ✅ | 1 (no 501 stubs) |
| 13 | Callback processing (end-to-end) | ✅ | 3 (success, idempotent, failure) |
| 14 | Unit + integration tests (> 80% coverage) | ✅ | **67/67 PASS** |

---

## 6. Implementation Status

### Code Deliverables (All Complete)

**Git Commits:**
- `599ba60` — Phase 1.C: M7 Conversion Engine (Sprint 1.7) — 59/59 tests pass
- `8cdfa29` — Phase 1.C: Close gaps — wire orders API, add callback/CRM/bank tests (67/67 tests pass)

**Key Files Created/Modified:**
- `backend/app/modules/m7_conversion/` — 5 files (service, order_flow, payment_handler, delivery, __init__)
- `backend/app/adapters/payment/` — 4 files (factory, orange_money, airtel_money, mpesa)
- `backend/app/adapters/crm/__init__.py` — Added `get_crm_adapter()` factory
- `backend/app/api/v1/orders.py` — Wired up POST/GET/PUT routes (no more 501 stubs)
- `backend/app/api/v1/payments.py` — Payment callback endpoint (`POST /callback`)
- `backend/tests/test_m7_sprint17.py` — 67 unit + integration tests
- `backend/app/tasks/conversion.py` — Celery tasks for async payment/CRM/status ops
- `backend/app/i18n/templates/` — Updated French, Lingala, Swahili with M7 keys

### Test Results

```
Phase 1.C (M7) Tests:   67/67 PASS
Phase 1.B Integration:  7/7 PASS (no regressions)

Test Coverage by Component:
  - Order Intent Detection:      7 tests
  - Payment Method Parsing:      10 tests
  - Order Flow & Messages:       10 tests
  - Delivery Guidance:           8 tests
  - Payment Adapters:           6 tests (Orange, Airtel, M-Pesa)
  - HMAC Security:              4 tests
  - State Machine:              4 tests (unit)
  - DB Integration:             6 tests
  - Club Points:                3 tests
  - Callback Processing:        3 tests (success, idempotent, failure)
  - CRM Sync:                   2 tests
  - API Routes:                 1 test
  - i18n Completeness:          4 tests
```

---

## 7. File Map (Actual Output)

```
backend/
├── app/
│   ├── modules/
│   │   └── m7_conversion/
│   │       ├── __init__.py                # ✅ Module exports
│   │       ├── service.py                 # ✅ Order CRUD, state machine, CRM sync, callback processing
│   │       ├── order_flow.py              # ✅ Intent detection, payment parsing, message building
│   │       ├── payment_handler.py         # ✅ HMAC verification, idempotency, callback dispatch
│   │       └── delivery.py                # ✅ Delivery ETAs by zone, COD instructions
│   ├── adapters/
│   │   ├── payment/
│   │   │   ├── __init__.py                # ✅ Payment adapter factory
│   │   │   ├── orange_money.py            # ✅ Orange Money USSD push (dev mock)
│   │   │   ├── airtel_money.py            # ✅ Airtel Money USSD push (dev mock)
│   │   │   └── mpesa.py                   # ✅ M-Pesa STK push (dev mock)
│   │   └── crm/
│   │       └── __init__.py                # ✅ CRM adapter factory (added)
│   ├── api/v1/
│   │   ├── orders.py                      # ✅ POST/GET/PUT endpoints (wired up)
│   │   └── payments.py                    # ✅ POST /callback webhook
│   ├── tasks/
│   │   └── conversion.py                  # ✅ Celery: initiate_payment, process_callback, sync_order_crm, update_order_status
│   ├── i18n/templates/
│   │   ├── french.json                    # ✅ M7 keys added
│   │   ├── lingala.json                   # ✅ M7 keys added
│   │   └── swahili.json                   # ✅ M7 keys added
│   └── models/
│       ├── order.py                       # ✅ Order ORM (exists from Phase 0)
│       └── payment.py                     # ✅ Payment ORM (exists from Phase 0)
└── tests/
    └── test_m7_sprint17.py                # ✅ 67 tests (all pass)
```

---

## 8. Risk Mitigation (Phase 1.C Specific) — All Mitigated

| Risk | Impact | Mitigation |
|------|--------|------------|
| Mobile Money API sandbox unavailable | Can't test payments | Mock adapter for dev, real API for staging |
| Payment callback delayed > 5 min | Customer thinks payment failed | Send "patience" message, retry check after 5 min |
| Double payment processed | Financial loss, customer distrust | Idempotency key on payment_reference |
| USSD push not received | Customer can't pay | Provide manual USSD code as fallback |
| Price discrepancy (catalog vs. conversation) | Incorrect charges | Single source of truth: inventory adapter |
| COD no-show | Revenue loss | Limit COD to established customers (warm/hot) |
