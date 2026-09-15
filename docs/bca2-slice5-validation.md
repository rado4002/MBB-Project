# BCA-2 Slice 5 — legacy Order creation retired

Validated locally on 2026-09-15 against baseline
`88531d0ac6c1cd6f3bcd20ae88fd5254990ccd5d`.

## Inspection and changes

Repository-wide searches found one runtime caller of
`backend/app/modules/m7_conversion/service.py:create_order`: the POST handler
in `backend/app/api/v1/orders.py`. No supported in-repository consumer calls
that endpoint. External consumers are unclear from the codebase.
The other function callers were in `backend/tests/test_m7_sprint17.py`.
The legacy path accepted caller CDF prices, calculated totals from them,
immediately inserted a pending Payment, and accepted but never used its
purported idempotency argument.

Removed that handler, service function, creation-only imports/constants, and
`OrderCreate`. Retained GET `/api/v1/orders/{order_id}` and PUT
`/api/v1/orders/{order_id}/status`, authentication, transition checks, callback,
loyalty, CRM, messaging helpers, and other M7 behavior. No payment initiation
was added. Existing ORM models, migration history, database constraints, and
draft-confirmation implementation are unchanged.

GET now derives `payment_method` from the actual Payment row. It returns null
when no Payment exists and preserves each historical provider when one exists.
It no longer maps every mobile-money Order to Orange Money or defaults to cash.
The response schema permits null and documents that a method is not payment
success and a recorded delivery zone is not a delivery commitment. Pending
Orders still read as pending; no fulfillment or payment-success field is added.
AI-6's existing database payment/delivery placeholders remain internal storage
values, not provider or fulfillment authority.

Removed three creation-only tests. Other M7 tests insert historical fixtures
directly and continue testing lifecycle, callbacks, loyalty and CRM behavior.
Their stale score 75 fixture now uses valid score 9. Four mock-adapter tests
call existing mock helpers directly, so disabled send gates need not be enabled.
No runtime adapter or safety gate was changed.

## Validation

### Recovered state and resumed acceptance

At recovery, HEAD, local `origin/main`, and remote `refs/heads/main` were all
`88531d0ac6c1cd6f3bcd20ae88fd5254990ccd5d`; main was 0 ahead / 0 behind.
Eight tracked Slice 5 files were modified, this report was untracked, and
nothing was staged. All interrupted implementation and connected-test edits
were preserved. The session interruption itself demonstrated no code defect.

Docker Desktop was initially stopped. After starting it for inspection, all
retained `bot-*` containers were stopped and none auto-started. Contrary to
the earlier cleanup statement in this report, `mbb-slice5-postgres` still
existed, stopped, with anonymous volume
`5c79b32d223edf2347489a2cabf294ba5e46990fd9bc2c5a3f08ea656f18bc85`.
That container, retained Slice 3 containers, native PostgreSQL processes,
and all retained data were left untouched.

A fresh ephemeral PostgreSQL 16 container named `mbb-slice5-resume-postgres`,
bound only to 127.0.0.1:19544 with synthetic database `slice5_acceptance`, ran the unchanged migrations
through `a3b4c5d6e7f8`. No existing application database was used for tests.
AI providers, WhatsApp/CRM/payment sends, relance, scheduling and M1 MAPS fanout
were explicitly disabled. No worker, Beat or application service was started.
M1 tests intercept external calls and assert none occur; historical CRM/callback
tests use mocks, and payment adapter tests invoke local mock helpers.

Commands from `backend/`, with test database environment pointing to the
isolated fixture:

- `python -m alembic upgrade head`: passed; database revision verified.
- `python -X utf8 -m pytest tests/test_order_drafts.py tests/test_order_drafts_postgres.py tests/test_m7_sprint17.py tests/test_ai_evaluation.py -q --tb=short --disable-warnings`:
  **128 passed, 49 warnings in 41.54s** on the resumed final run.
- `python -X utf8 tests/test_schema_api_validation.py`: **all 12 checks passed**.
- `python -X utf8 -c "import runpy; runpy.run_path('tests/conftest.py'); runpy.run_path('tests/test_project_setup.py')"`:
  Order schema and critical route checks passed. The complete script reports
  the already documented unrelated Celery task-route expectation mismatch
  (`operator_replies.*` missing from its expected set). Left unchanged.
- `git diff --check`: passed.

Evidence covers absent legacy route/function/request schema, the sole runtime
Order constructor in the draft domain, rejected caller-price POSTs with zero
Orders/Payments, rejection of model-provided prices/rates, explicit confirmation,
one pending Order, sequential and concurrent replay, zero Payment rows,
reconfirmation after changed authoritative terms, historical reads for all five
payment methods and no Payment, valid cancellation, invalid-transition 409,
missing-order 404, and retained historical lifecycle/callback behavior.

The first combined run found the stale M7 fixtures described above (80 passed,
16 failed); the earlier 128-test run passed after those test-only corrections.
On recovery, the extended connected test initially failed (127 passed,
1 failed): its synthetic ambiguous reply explicitly assigned host-clock
`created_at`, while M1 uses PostgreSQL's receipt-time default. Mixing those
clocks let an earlier fixture message sort after the first M1 confirmation,
correctly triggering stale-authority rejection. Only the test helper was
repaired to use the same database default as M1. No runtime safety check was
changed. The focused connected test then passed (1 passed, 2 warnings).

The connected test
`tests/test_order_drafts_postgres.py::test_real_ai_turn_terminal_draft_uses_authoritative_offer`
starts with a qualified hot decision-stage lead and purchase intent. A local
scripted adapter exercises the real AI-turn service and durable draft path;
it is not live provider evidence. The authoritative Product Offer supplies
product/item/price identity, USD 55, rate 2800, CDF 154000 per unit, and
inventory authority. Quantity 2 yields a CDF 308000 draft and pending Order.
Bare `OUI` creates no Order. Exact `OUI <code>` through M1 creates one pending
Order; replay through M1 returns the same ID. Counts assert exactly one Order
and zero Payments; inventory status and update time are unchanged. CRM sync
fields remain unset and confirmed/delivered timestamps remain null.

HTTP GET through the real Orders router with an isolated database and an
overridden admin dependency returns that same pending Order's authoritative
items and total, with `payment_method: null`. This field is the existing
method/provider representation; there is no separate provider field.
No payment-success or delivery-method field is exposed. The schema explains
that a recorded delivery zone is not a commitment. The historical HTTP test
verifies actual stored Payment methods for Orange Money, Airtel Money,
M-Pesa, cash and bank transfer, plus null when no Payment exists.
The connected test's external-call guard remains empty: provider inference,
FX provider, CRM, inventory/payment/messaging adapters and Celery dispatch
are intercepted and fail the test if invoked. Both confirmation sends are
skipped. No delivery job or service is started. HTTP is exercised in-process,
not against a public deployment; authentication itself is not revalidated here.

No external business effect occurred during validation. This is local/codebase
evidence, not production, pilot or public-deployment certification. Historical
design documents may still describe the retired API; they do not authorize it.

## Changed files

- `backend/app/api/v1/orders.py`
- `backend/app/modules/m7_conversion/service.py`
- `backend/app/schemas/orders.py`
- `backend/tests/test_m7_sprint17.py`
- `backend/tests/test_order_drafts.py`
- `backend/tests/test_order_drafts_postgres.py`
- `backend/tests/test_project_setup.py`
- `backend/tests/test_schema_api_validation.py`
- `docs/bca2-slice5-validation.md`

## State and next step

The fresh `mbb-slice5-resume-postgres` container and its disposable anonymous
volume were removed after acceptance. No containers were running afterward.
The recovered `mbb-slice5-postgres` container remains stopped and preserved.
Docker Desktop was stopped again to restore its initial state.
All nine files listed above form the complete Slice 5 commit.
The protected stabilization tag still targets
`cb39748deecf8ebe28c6ce3cded734754becbeb1`.
The connected local acceptance journey is now exercised. Conversation-authority
consolidation may be scoped as the next controlled task after this Slice 5
commit; it is not implemented or authorized for execution by this report.
Payment redesign, conversation consolidation and relance were not begun.
No push is part of this acceptance run.
