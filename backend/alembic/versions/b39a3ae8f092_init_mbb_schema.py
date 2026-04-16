"""init_mbb_schema — Create mbb schema and all Phase 1 tables.

Revision ID: b39a3ae8f092
Revises:
Create Date: 2026-04-16

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b39a3ae8f092'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Extensions (idempotent — init_db.sql also runs these on Docker init) ──
    # Required even outside Docker so `alembic upgrade head` works standalone.
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "pg_trgm"')
    op.execute('CREATE EXTENSION IF NOT EXISTS "unaccent"')

    # ── Schema ────────────────────────────────────────────────────────────────
    op.execute("CREATE SCHEMA IF NOT EXISTS mbb")

    # ── customers ─────────────────────────────────────────────────────────────
    op.create_table(
        "customers",
        sa.Column("phone_number", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("city", sa.String(50), nullable=False),
        sa.Column("preferred_language", sa.String(10), nullable=False, server_default="french"),
        sa.Column("opt_out_flag", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("opt_out_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("consent_given", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("consent_timestamp", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("club_member", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("club_points", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_interaction", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("preferred_language IN ('lingala', 'french', 'swahili')", name="chk_language"),
        sa.CheckConstraint(r"phone_number ~ '^\+243[0-9]{9}$'", name="chk_phone_format"),
        sa.CheckConstraint("club_points >= 0", name="chk_club_points"),
        schema="mbb",
    )
    op.create_index("idx_customers_city", "customers", ["city"], schema="mbb")
    op.create_index(
        "idx_customers_opt_out", "customers", ["opt_out_flag"],
        schema="mbb", postgresql_where=sa.text("opt_out_flag = TRUE"),
    )
    op.create_index("idx_customers_last_interaction", "customers", ["last_interaction"], schema="mbb")
    op.create_index(
        "idx_customers_club", "customers", ["club_member"],
        schema="mbb", postgresql_where=sa.text("club_member = TRUE"),
    )

    # ── conversations ─────────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.String(20), sa.ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"), nullable=False),
        sa.Column("start_time", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_message_time", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("language_detected", sa.String(10), nullable=False),
        sa.Column("context", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("message_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "status IN ('active', 'qualifying', 'nurturing', 'escalated', 'converted', 'dormant')",
            name="chk_conv_status",
        ),
        sa.CheckConstraint("language_detected IN ('lingala', 'french', 'swahili')", name="chk_conv_language"),
        schema="mbb",
    )
    op.create_index("idx_conv_customer", "conversations", ["customer_id"], schema="mbb")
    op.create_index("idx_conv_status", "conversations", ["status"], schema="mbb")
    op.create_index("idx_conv_last_msg", "conversations", ["last_message_time"], schema="mbb")
    op.create_index("idx_conv_context", "conversations", ["context"], schema="mbb", postgresql_using="gin")

    # ── messages ──────────────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("timestamp", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("direction", sa.String(10), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_type", sa.String(20), nullable=False, server_default="text"),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("persuasion_hook", sa.String(50), nullable=True),
        sa.Column("processing_time_ms", sa.Integer, nullable=True),
        sa.Column("whatsapp_message_id", sa.String(100), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("direction IN ('inbound', 'outbound')", name="chk_msg_direction"),
        sa.CheckConstraint("content_type IN ('text', 'voice_note', 'image')", name="chk_msg_content_type"),
        schema="mbb",
    )
    op.create_index("idx_msg_conversation", "messages", ["conversation_id"], schema="mbb")
    op.create_index("idx_msg_timestamp", "messages", ["timestamp"], schema="mbb")
    op.create_index("idx_msg_direction", "messages", ["direction"], schema="mbb")
    op.create_index(
        "idx_msg_content_type", "messages", ["content_type"],
        schema="mbb", postgresql_where=sa.text("content_type != 'text'"),
    )
    op.execute(
        "CREATE INDEX idx_msg_fts ON mbb.messages USING GIN (to_tsvector('french', content))"
    )

    # ── leads ─────────────────────────────────────────────────────────────────
    op.create_table(
        "leads",
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.String(20), sa.ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("score", sa.String(10), nullable=False),
        sa.Column("score_value", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("stage", sa.String(20), nullable=False, server_default="awareness"),
        sa.Column("intent", sa.String(30), nullable=False),
        sa.Column("product_interest", postgresql.ARRAY(sa.String), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("relance_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("qualified_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_nurture_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("score IN ('hot', 'warm', 'cold')", name="chk_lead_score"),
        sa.CheckConstraint("stage IN ('awareness', 'consideration', 'decision')", name="chk_lead_stage"),
        sa.CheckConstraint("relance_count BETWEEN 0 AND 3", name="chk_lead_relance"),
        sa.CheckConstraint("score_value BETWEEN 0 AND 10", name="chk_score_value"),
        sa.UniqueConstraint("conversation_id", name="uq_lead_conversation"),
        schema="mbb",
    )
    op.create_index("idx_leads_customer", "leads", ["customer_id"], schema="mbb")
    op.create_index("idx_leads_score", "leads", ["score"], schema="mbb")
    op.create_index("idx_leads_stage", "leads", ["stage"], schema="mbb")
    op.create_index(
        "idx_leads_relance", "leads", ["relance_count"],
        schema="mbb", postgresql_where=sa.text("relance_count < 3"),
    )
    op.create_index("idx_leads_source", "leads", ["source"], schema="mbb")
    op.create_index("idx_leads_qualified", "leads", ["qualified_at"], schema="mbb")

    # ── relances ──────────────────────────────────────────────────────────────
    op.create_table(
        "relances",
        sa.Column("relance_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.leads.lead_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer, nullable=False),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("value_hook", sa.Text, nullable=False),
        sa.Column("hook_type", sa.String(30), nullable=False),
        sa.Column("response_received", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("response_time_minutes", sa.Integer, nullable=True),
        sa.Column("cancelled", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("attempt_number BETWEEN 1 AND 3", name="chk_relance_attempt"),
        sa.CheckConstraint(
            "hook_type IN ('reciprocity', 'social_proof', 'scarcity', 'loyalty')",
            name="chk_relance_hook_type",
        ),
        sa.UniqueConstraint("lead_id", "attempt_number", name="uq_relance_lead_attempt"),
        schema="mbb",
    )
    op.create_index("idx_relance_lead", "relances", ["lead_id"], schema="mbb")
    op.create_index(
        "idx_relance_scheduled", "relances", ["scheduled_at"],
        schema="mbb",
        postgresql_where=sa.text("delivered_at IS NULL AND cancelled = FALSE"),
    )
    op.create_index("idx_relance_performance", "relances", ["attempt_number", "response_received"], schema="mbb")

    # ── maps_tags ─────────────────────────────────────────────────────────────
    op.create_table(
        "maps_tags",
        sa.Column("tag_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.messages.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("customer_id", sa.String(20), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("pattern", sa.String(200), nullable=False),
        sa.Column("trigger_event", sa.String(100), nullable=True),
        sa.Column("city", sa.String(50), nullable=True),
        sa.Column("language", sa.String(10), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "category IN ('product_demand', 'silence_reason', 'conversion_trigger', 'language_usage', 'opt_out_reason')",
            name="chk_maps_category",
        ),
        schema="mbb",
    )
    op.create_index("idx_maps_message", "maps_tags", ["message_id"], schema="mbb")
    op.create_index("idx_maps_category", "maps_tags", ["category"], schema="mbb")
    op.create_index("idx_maps_pattern", "maps_tags", ["pattern"], schema="mbb")
    op.create_index("idx_maps_created", "maps_tags", ["created_at"], schema="mbb")
    op.create_index("idx_maps_city_category", "maps_tags", ["city", "category"], schema="mbb")
    op.create_index("idx_maps_metadata", "maps_tags", ["metadata"], schema="mbb", postgresql_using="gin")

    # ── orders ────────────────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.leads.lead_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("customer_id", sa.String(20), sa.ForeignKey("mbb.customers.phone_number", ondelete="RESTRICT"), nullable=False),
        sa.Column("items", postgresql.JSONB, nullable=False),
        sa.Column("total_amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CDF"),
        sa.Column("payment_type", sa.String(20), nullable=False),
        sa.Column("delivery_zone", sa.String(50), nullable=False),
        sa.Column("delivery_method", sa.String(20), nullable=False, server_default="moto_taxi"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("hub_crm_synced", sa.Boolean, nullable=False, server_default=sa.text("FALSE")),
        sa.Column("hub_crm_order_id", sa.String(50), nullable=True),
        sa.Column("club_points_credited", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("payment_type IN ('mobile_money', 'bank_transfer', 'cod')", name="chk_order_payment"),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'preparing', 'delivering', 'delivered', 'cancelled')",
            name="chk_order_status",
        ),
        sa.CheckConstraint("delivery_method IN ('moto_taxi', 'spot_pickup')", name="chk_order_delivery"),
        sa.CheckConstraint("total_amount > 0", name="chk_order_amount"),
        schema="mbb",
    )
    op.create_index("idx_orders_lead", "orders", ["lead_id"], schema="mbb")
    op.create_index("idx_orders_customer", "orders", ["customer_id"], schema="mbb")
    op.create_index("idx_orders_status", "orders", ["status"], schema="mbb")
    op.create_index("idx_orders_created", "orders", ["created_at"], schema="mbb")
    op.create_index(
        "idx_orders_hub_sync", "orders", ["hub_crm_synced"],
        schema="mbb", postgresql_where=sa.text("hub_crm_synced = FALSE"),
    )

    # ── payments ──────────────────────────────────────────────────────────────
    op.create_table(
        "payments",
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.orders.order_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="CDF"),
        sa.Column("provider_transaction_id", sa.String(100), nullable=True),
        sa.Column("provider_response", postgresql.JSONB, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "method IN ('orange_money', 'airtel_money', 'mpesa', 'bank_transfer', 'cash')",
            name="chk_payment_method",
        ),
        sa.CheckConstraint("status IN ('pending', 'completed', 'failed', 'refunded')", name="chk_payment_status"),
        sa.CheckConstraint("amount > 0", name="chk_payment_amount"),
        schema="mbb",
    )
    op.create_index("idx_payments_order", "payments", ["order_id"], schema="mbb")
    op.create_index("idx_payments_status", "payments", ["status"], schema="mbb")
    op.create_index(
        "idx_payments_provider", "payments", ["provider_transaction_id"],
        schema="mbb",
        postgresql_where=sa.text("provider_transaction_id IS NOT NULL"),
    )

    # ── escalation_tickets ────────────────────────────────────────────────────
    op.create_table(
        "escalation_tickets",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.leads.lead_id", ondelete="SET NULL"), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"), nullable=False),
        sa.Column("customer_id", sa.String(20), sa.ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("assigned_to", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("resolution_notes", sa.Text, nullable=True),
        sa.Column("transcript_snapshot", postgresql.JSONB, nullable=False),
        sa.Column("maps_tags_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("priority IN ('high', 'medium', 'low')", name="chk_esc_priority"),
        sa.CheckConstraint(
            "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', 'unresolved_3x', 'sav_issue')",
            name="chk_esc_reason",
        ),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'resolved', 'closed')", name="chk_esc_status"),
        schema="mbb",
    )
    op.create_index("idx_esc_conversation", "escalation_tickets", ["conversation_id"], schema="mbb")
    op.create_index("idx_esc_customer", "escalation_tickets", ["customer_id"], schema="mbb")
    op.create_index(
        "idx_esc_status", "escalation_tickets", ["status"],
        schema="mbb",
        postgresql_where=sa.text("status IN ('open', 'in_progress')"),
    )
    op.create_index("idx_esc_priority", "escalation_tickets", ["priority", "created_at"], schema="mbb")

    # ── admin_audit_log ───────────────────────────────────────────────────────
    op.create_table(
        "admin_audit_log",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_name", sa.String(50), nullable=False),
        sa.Column("user_role", sa.String(10), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target_entity", sa.String(50), nullable=False),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("old_value", postgresql.JSONB, nullable=True),
        sa.Column("new_value", postgresql.JSONB, nullable=True),
        sa.Column("justification", sa.Text, nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("user_role IN ('admin', 'hub', 'lab')", name="chk_audit_role"),
        schema="mbb",
    )
    op.create_index("idx_audit_user", "admin_audit_log", ["user_name"], schema="mbb")
    op.create_index("idx_audit_action", "admin_audit_log", ["action"], schema="mbb")
    op.create_index("idx_audit_target", "admin_audit_log", ["target_entity", "target_id"], schema="mbb")
    op.create_index("idx_audit_created", "admin_audit_log", ["created_at"], schema="mbb")


def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table("admin_audit_log", schema="mbb")
    op.drop_table("escalation_tickets", schema="mbb")
    op.drop_table("payments", schema="mbb")
    op.drop_table("orders", schema="mbb")
    op.drop_table("maps_tags", schema="mbb")
    op.drop_table("relances", schema="mbb")
    op.drop_table("leads", schema="mbb")
    op.drop_table("messages", schema="mbb")
    op.drop_table("conversations", schema="mbb")
    op.drop_table("customers", schema="mbb")
    op.execute("DROP SCHEMA IF EXISTS mbb CASCADE")
