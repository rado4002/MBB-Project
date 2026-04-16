"""Sprint 0.1 — Database schema validation test.

Verifies that all 10 ORM models match the database design spec without
requiring a live database connection. Inspects SQLAlchemy metadata directly.

Run: python tests/test_migration.py
"""
import sys
import os

sys.path.insert(0, ".")

# Minimal env vars so config.py can initialise without secrets
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_DB", "mbb")
os.environ.setdefault("POSTGRES_USER", "mbb")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("REDIS_HOST", "localhost")
os.environ.setdefault("JWT_SECRET", "testsecret32charslongpadding12345")
os.environ.setdefault("CLAUDE_API_KEY", "test")
os.environ.setdefault("AIRTABLE_API_KEY", "test")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test")

PASS = "\u2713"
FAIL = "\u2717"
errors: list[str] = []


def check(condition: bool, msg: str) -> None:
    if condition:
        print(f"  {PASS} {msg}")
    else:
        print(f"  {FAIL} {msg}")
        errors.append(msg)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Import models and build metadata
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1/5] Importing ORM models...")
from app.database import Base  # noqa: E402
import app.models  # noqa: F401, E402 — registers all classes on Base.metadata

tables = Base.metadata.tables  # keyed by "schema.tablename" or "tablename"


def get_table(name: str):
    """Return table object; look up with and without schema prefix."""
    result = tables.get(f"mbb.{name}")
    if result is None:
        result = tables.get(name)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. All 10 tables registered
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2/5] Checking table registration...")

EXPECTED_TABLES = [
    "customers", "conversations", "messages", "leads", "relances",
    "maps_tags", "orders", "payments", "escalation_tickets", "admin_audit_log",
]

for tname in EXPECTED_TABLES:
    t = get_table(tname)
    check(t is not None, f"Table mbb.{tname} registered on Base.metadata")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Column presence per table
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3/5] Checking column presence...")

COLUMN_SPEC: dict[str, list[str]] = {
    "customers": [
        "phone_number", "name", "city", "preferred_language",
        "opt_out_flag", "opt_out_at", "consent_given", "consent_timestamp",
        "club_member", "club_points", "created_at", "last_interaction",
    ],
    "conversations": [
        "conversation_id", "customer_id", "start_time", "last_message_time",
        "status", "language_detected", "context", "message_count",
        "created_at", "updated_at",
    ],
    "messages": [
        "message_id", "conversation_id", "timestamp", "direction",
        "content", "content_type", "language", "persuasion_hook",
        "processing_time_ms", "whatsapp_message_id", "created_at",
    ],
    "leads": [
        "lead_id", "customer_id", "conversation_id", "score", "score_value",
        "stage", "intent", "product_interest", "source", "relance_count",
        "qualified_at", "last_nurture_at", "converted_at", "created_at", "updated_at",
    ],
    "relances": [
        "relance_id", "lead_id", "attempt_number", "scheduled_at",
        "delivered_at", "value_hook", "hook_type", "response_received",
        "response_time_minutes", "cancelled", "created_at",
    ],
    "maps_tags": [
        "tag_id", "message_id", "conversation_id", "customer_id",
        "category", "pattern", "trigger_event", "city", "language",
        "metadata", "created_at",
    ],
    "orders": [
        "order_id", "lead_id", "customer_id", "items", "total_amount",
        "currency", "payment_type", "delivery_zone", "delivery_method",
        "status", "hub_crm_synced", "hub_crm_order_id", "club_points_credited",
        "created_at", "confirmed_at", "delivered_at", "updated_at",
    ],
    "payments": [
        "payment_id", "order_id", "method", "amount", "currency",
        "provider_transaction_id", "provider_response", "status",
        "failure_reason", "created_at", "completed_at",
    ],
    "escalation_tickets": [
        "ticket_id", "lead_id", "conversation_id", "customer_id",
        "priority", "reason", "assigned_to", "status", "resolution_notes",
        "transcript_snapshot", "maps_tags_snapshot",
        "created_at", "assigned_at", "resolved_at",
    ],
    "admin_audit_log": [
        "audit_id", "user_name", "user_role", "action", "target_entity",
        "target_id", "old_value", "new_value", "justification",
        "ip_address", "created_at",
    ],
}

for tname, cols in COLUMN_SPEC.items():
    t = get_table(tname)
    if t is None:
        check(False, f"mbb.{tname}: table not found — skipping column check")
        continue
    actual_cols = {c.name for c in t.columns}
    for col in cols:
        check(col in actual_cols, f"mbb.{tname}.{col} exists")


# ─────────────────────────────────────────────────────────────────────────────
# 4. CHECK constraints by name
# ─────────────────────────────────────────────────────────────────────────────
print("\n[4/5] Checking CHECK constraint names...")

CONSTRAINT_SPEC: dict[str, list[str]] = {
    "customers": ["chk_language", "chk_phone_format", "chk_club_points"],
    "conversations": ["chk_conv_status", "chk_conv_language"],
    "messages": ["chk_msg_direction", "chk_msg_content_type"],
    "leads": ["chk_lead_score", "chk_lead_stage", "chk_lead_relance", "chk_score_value"],
    "relances": ["chk_relance_attempt", "chk_relance_hook_type"],
    "maps_tags": ["chk_maps_category"],
    "orders": ["chk_order_payment", "chk_order_status", "chk_order_delivery", "chk_order_amount"],
    "payments": ["chk_payment_method", "chk_payment_status", "chk_payment_amount"],
    "escalation_tickets": ["chk_esc_priority", "chk_esc_reason", "chk_esc_status"],
    "admin_audit_log": ["chk_audit_role"],
}

for tname, constraint_names in CONSTRAINT_SPEC.items():
    t = get_table(tname)
    if t is None:
        continue
    actual = {c.name for c in t.constraints}
    for cname in constraint_names:
        check(cname in actual, f"mbb.{tname}: constraint {cname} present")


# ─────────────────────────────────────────────────────────────────────────────
# 5. Index presence by name
# ─────────────────────────────────────────────────────────────────────────────
print("\n[5/5] Checking index names...")

INDEX_SPEC: dict[str, list[str]] = {
    "customers": [
        "idx_customers_city", "idx_customers_opt_out",
        "idx_customers_last_interaction", "idx_customers_club",
    ],
    "conversations": [
        "idx_conv_customer", "idx_conv_status", "idx_conv_last_msg", "idx_conv_context",
    ],
    "messages": [
        "idx_msg_conversation", "idx_msg_timestamp", "idx_msg_direction", "idx_msg_content_type",
    ],
    "leads": [
        "idx_leads_customer", "idx_leads_score", "idx_leads_stage",
        "idx_leads_relance", "idx_leads_source", "idx_leads_qualified",
    ],
    "relances": [
        "idx_relance_lead", "idx_relance_scheduled", "idx_relance_performance",
    ],
    "maps_tags": [
        "idx_maps_message", "idx_maps_category", "idx_maps_pattern",
        "idx_maps_created", "idx_maps_city_category", "idx_maps_metadata",
    ],
    "orders": [
        "idx_orders_lead", "idx_orders_customer", "idx_orders_status",
        "idx_orders_created", "idx_orders_hub_sync",
    ],
    "payments": ["idx_payments_order", "idx_payments_status", "idx_payments_provider"],
    "escalation_tickets": [
        "idx_esc_conversation", "idx_esc_customer", "idx_esc_status", "idx_esc_priority",
    ],
    "admin_audit_log": [
        "idx_audit_user", "idx_audit_action", "idx_audit_target", "idx_audit_created",
    ],
}

for tname, index_names in INDEX_SPEC.items():
    t = get_table(tname)
    if t is None:
        continue
    actual = {i.name for i in t.indexes}
    for iname in index_names:
        check(iname in actual, f"mbb.{tname}: index {iname} present")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
print()
if errors:
    print(f"SCHEMA VALIDATION FAILED — {len(errors)} issue(s):")
    for e in errors:
        print(f"  {FAIL} {e}")
    sys.exit(1)
else:
    total = sum(len(v) for v in COLUMN_SPEC.values()) + \
            sum(len(v) for v in CONSTRAINT_SPEC.values()) + \
            sum(len(v) for v in INDEX_SPEC.values()) + \
            len(EXPECTED_TABLES)
    print(f"ALL {total} SCHEMA CHECKS PASSED {PASS}")
    sys.exit(0)
