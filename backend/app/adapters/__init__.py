"""
Adapter factory.

Usage:
    from app.adapters import get_ai_adapter, get_crm_adapter

    ai = get_ai_adapter()
    response = await ai.generate(prompt, system, max_tokens)

Switch adapters by changing env vars — no module code changes required:
    AI_ADAPTER=disabled | claude
    CRM_ADAPTER=airtable | mbb_hub
    INVENTORY_ADAPTER=static | mbb_box
    PAYMENT_ADAPTER=mobile_money
    MESSAGING_ADAPTER=whatsapp
"""
from functools import lru_cache

from app.adapters.base import (
    BaseAIAdapter,
    BaseCRMAdapter,
    BaseInventoryAdapter,
    BaseMessagingAdapter,
    BasePaymentAdapter,
)
from app.config import get_settings

settings = get_settings()


@lru_cache()
def get_ai_adapter() -> BaseAIAdapter:
    if settings.ai_adapter in {"disabled", "local"}:
        from app.adapters.ai.disabled_adapter import DisabledAIAdapter
        return DisabledAIAdapter()
    if settings.ai_adapter == "claude":
        from app.adapters.ai.claude_adapter import ClaudeAdapter
        return ClaudeAdapter()
    raise ValueError(f"Unknown AI adapter: {settings.ai_adapter}")


@lru_cache()
def get_crm_adapter() -> BaseCRMAdapter:
    if settings.crm_adapter == "airtable":
        from app.adapters.crm.airtable_adapter import AirtableAdapter
        return AirtableAdapter()
    raise ValueError(f"Unknown CRM adapter: {settings.crm_adapter}")


@lru_cache()
def get_inventory_adapter() -> BaseInventoryAdapter:
    if settings.inventory_adapter == "static":
        from app.adapters.inventory.static_adapter import StaticInventoryAdapter
        return StaticInventoryAdapter()
    raise ValueError(f"Unknown inventory adapter: {settings.inventory_adapter}")


@lru_cache()
def get_payment_adapter() -> BasePaymentAdapter:
    if settings.payment_adapter == "mobile_money":
        from app.adapters.payment.mobile_money_adapter import MobileMoneyAdapter
        return MobileMoneyAdapter()
    raise ValueError(f"Unknown payment adapter: {settings.payment_adapter}")


@lru_cache()
def get_messaging_adapter() -> BaseMessagingAdapter:
    if settings.messaging_adapter == "whatsapp":
        if settings.whatsapp_mode == "baileys":
            from app.adapters.messaging.baileys_adapter import BaileysAdapter
            return BaileysAdapter()
        from app.adapters.messaging.whatsapp_official_adapter import WhatsAppOfficialAdapter
        return WhatsAppOfficialAdapter()
    raise ValueError(f"Unknown messaging adapter: {settings.messaging_adapter}")
