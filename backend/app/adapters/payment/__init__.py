"""
app/adapters/payment — Mobile Money payment adapter factory.

Usage:
    from app.adapters.payment import get_payment_adapter
    adapter = get_payment_adapter("orange_money")
    result = await adapter.initiate_payment(...)

Switching adapters: APP_ENV=development → MockPaymentAdapter (no real API calls).
"""
from __future__ import annotations

from app.adapters.base import BasePaymentAdapter
from app.config import get_settings

settings = get_settings()

# Provider → adapter module mapping
_PROVIDER_MAP = {
    "orange_money": "app.adapters.payment.orange_money.OrangeMoneyAdapter",
    "airtel_money": "app.adapters.payment.airtel_money.AirtelMoneyAdapter",
    "mpesa": "app.adapters.payment.mpesa.MPesaAdapter",
}

# Module-level singletons (one instance per provider)
_instances: dict[str, BasePaymentAdapter] = {}


def get_payment_adapter(provider: str) -> BasePaymentAdapter:
    """
    Return the correct PaymentAdapter for a given provider string.

    Args:
        provider: "orange_money" | "airtel_money" | "mpesa"

    Returns:
        Adapter instance implementing BasePaymentAdapter.
    """
    if provider not in _instances:
        if provider not in _PROVIDER_MAP:
            raise ValueError(f"Unknown payment provider: {provider!r}. "
                             f"Valid: {list(_PROVIDER_MAP)}")
        module_path, class_name = _PROVIDER_MAP[provider].rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        cls = getattr(mod, class_name)
        _instances[provider] = cls()
    return _instances[provider]
