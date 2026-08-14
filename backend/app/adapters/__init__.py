"""
Adapter factory.

Usage:
    from app.adapters import get_ai_adapter, get_crm_adapter

    ai = get_ai_adapter()
    response = await ai.generate(prompt, system, max_tokens)

Switch provider-turn adapters by changing env vars — no module code changes required:
    AI_TURN_PROVIDER=disabled | deepseek | claude
Legacy get_ai_adapter callers remain controlled by AI_ADAPTER=disabled | claude.
    CRM_ADAPTER=airtable | mbb_hub
    INVENTORY_ADAPTER=static | mbb_box
    PAYMENT_ADAPTER=mobile_money
    MESSAGING_ADAPTER=whatsapp
"""
from functools import lru_cache
from typing import Literal

from app.adapters.base import (
    BaseAIAdapter,
    BaseCRMAdapter,
    BaseInventoryAdapter,
    BaseMessagingAdapter,
    BasePaymentAdapter,
    ProviderTurnAdapter,
)
from app.config import get_settings

settings = get_settings()


def _build_ai_adapter(configured_name: str) -> BaseAIAdapter:
    if configured_name in {"disabled", "local"}:
        from app.adapters.ai.disabled_adapter import DisabledAIAdapter
        return DisabledAIAdapter()
    if configured_name == "claude":
        from app.adapters.ai.claude_adapter import ClaudeAdapter
        return ClaudeAdapter()
    raise ValueError(f"Unknown AI adapter: {configured_name}")


def _build_provider_turn_adapter(configured_name: str) -> ProviderTurnAdapter:
    if configured_name == "deepseek":
        from app.adapters.ai.deepseek_adapter import DeepSeekAdapter

        return DeepSeekAdapter(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout_s=settings.deepseek_timeout_s,
        )
    return _build_ai_adapter(configured_name)


@lru_cache()
def get_ai_adapter() -> BaseAIAdapter:
    return _build_ai_adapter(settings.ai_adapter)


@lru_cache()
def get_provider_turn_adapter() -> ProviderTurnAdapter:
    return _build_provider_turn_adapter(settings.ai_turn_provider)


def ai_adapter_eligibility(
    configured_name: str,
) -> Literal["eligible", "disabled", "unavailable"]:
    """Resolve local adapter usability without making a provider request."""
    try:
        adapter = _build_provider_turn_adapter(configured_name)
    except (ValueError, ImportError, RuntimeError):
        return "unavailable"

    from app.adapters.ai.disabled_adapter import DisabledAIAdapter

    return "disabled" if isinstance(adapter, DisabledAIAdapter) else "eligible"


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
