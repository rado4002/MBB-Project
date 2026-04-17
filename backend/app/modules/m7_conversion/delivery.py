"""
app/modules/m7_conversion/delivery.py — Delivery guidance messages.

Provides localized delivery messages with ETA estimates by zone.
All messages follow DRC tone: warm, casual, 2-3 sentences max.
"""
from __future__ import annotations

from app.i18n.messages import t

# Delivery ETA by zone (rough estimates for DRC context)
_ZONE_ETA: dict[str, str] = {
    "gombe": "24–48h",
    "lingwala": "24–48h",
    "barumbu": "24–48h",
    "kinshasa": "24–48h",
    "kalamu": "24–48h",
    "kasa-vubu": "24–48h",
    "ngaliema": "24–48h",
    "lemba": "48–72h",
    "limete": "48–72h",
    "ndjili": "48–72h",
    "matete": "48–72h",
    "kintambo": "48–72h",
    "bumbu": "48–72h",
    "masina": "48–72h",
    "kisenso": "72h",
    "maluku": "72–96h",
    "nsele": "72–96h",
    "mont ngafula": "72–96h",
    "lubumbashi": "3–5 jours",
    "goma": "3–5 jours",
    "bukavu": "4–6 jours",
    "kisangani": "5–7 jours",
}

_DEFAULT_ETA = "24–72h"


def estimate_delivery_time(delivery_zone: str) -> str:
    """
    Return a human-readable ETA string for the given delivery zone.

    Case-insensitive match against known zones; falls back to default.
    """
    normalized = delivery_zone.lower().strip()
    # Exact match
    if normalized in _ZONE_ETA:
        return _ZONE_ETA[normalized]
    # Partial match (e.g. "Gombe, Kinshasa" → "gombe")
    for zone, eta in _ZONE_ETA.items():
        if zone in normalized:
            return eta
    return _DEFAULT_ETA


def get_delivery_guidance(
    *,
    language: str,
    delivery_zone: str,
    order_id: str,
) -> str:
    """
    Build a delivery guidance message in the customer's language.

    Includes the ETA and a warm DRC-tone message.
    """
    eta = estimate_delivery_time(delivery_zone)
    return t("delivery_guidance", language).format(
        order_id=order_id,
        zone=delivery_zone,
        eta=eta,
    )


def get_payment_patience_message(language: str) -> str:
    """
    Message sent when payment callback is delayed > 5 min.

    Reassures the customer without being pushy.
    """
    return t("payment_patience", language)


def get_cod_instructions(
    *,
    language: str,
    delivery_zone: str,
    order_id: str,
) -> str:
    """
    Instructions for cash-on-delivery orders.

    Confirms the order and explains when/how payment is collected.
    """
    eta = estimate_delivery_time(delivery_zone)
    return t("cod_instructions", language).format(
        order_id=order_id,
        zone=delivery_zone,
        eta=eta,
    )
