# SQLAlchemy ORM models — imported here so Alembic autogenerate can discover them
from app.models.customer import Customer
from app.models.conversation import Conversation
from app.models.conversation_ownership_idempotency import (
    ConversationOwnershipIdempotency,
)
from app.models.message import Message
from app.models.internal_note import InternalNote
from app.models.lead import Lead
from app.models.relance import Relance
from app.models.maps_tag import MapsTag
from app.models.order import Order
from app.models.payment import Payment
from app.models.escalation_ticket import EscalationTicket
from app.models.admin_audit_log import AdminAuditLog
from app.models.lead_stage_transition import LeadStageTransition
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import (
    OperatorAuditEvent,
    OperatorAuditSecurityMetadata,
)
from app.models.ai_turn_audit import AITurnAudit
from app.models.operator_escalation_idempotency import (
    OperatorEscalationIdempotency,
)
from app.models.catalog import Product, SellableItem
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.models.inventory import InventoryRecord

__all__ = [
    "Customer",
    "Conversation",
    "ConversationOwnershipIdempotency",
    "Message",
    "InternalNote",
    "Lead",
    "Relance",
    "MapsTag",
    "Order",
    "Payment",
    "EscalationTicket",
    "AdminAuditLog",
    "LeadStageTransition",
    "OperatorAccount",
    "OperatorAuditEvent",
    "OperatorAuditSecurityMetadata",
    "AITurnAudit",
    "OperatorEscalationIdempotency",
    "Product",
    "SellableItem",
    "SellableItemPrice",
    "ExchangeRate",
    "InventoryRecord",
]
