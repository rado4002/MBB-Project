"""
app/modules/m7_conversion/order_flow.py — Conversational order flow helpers.

Handles:
  1. Detecting order intent from customer message
  2. Building the payment method menu (multilingual)
  3. Parsing the customer's payment method choice
  4. Building order summary messages
"""
from __future__ import annotations

import re

from app.i18n.messages import t
from app.schemas.common import PaymentMethod

# Signals that indicate a customer wants to place an order
_ORDER_SIGNALS = [
    r"\bnalingi\b",          # Lingala: "I want"
    r"\boui\b.*\bnalingi\b",
    r"\bje (veux|voudrais|commande)\b",
    r"\bcommand(er|e)\b",
    r"\bacheter\b",
    r"\bnunua\b",            # Swahili: buy
    r"\btaka\b.*\bnunua\b",
    r"\bconfirm\b",
    r"\boui\b.*\bcommande\b",
    r"\byes\b.*\border\b",
    r"\bprendre\b",
    r"\bj'en veux\b",
    r"\bok.*commande\b",
]

# Valid payment method keywords (customer types "1", "orange", etc.)
_METHOD_ALIASES: dict[str, PaymentMethod] = {
    "1": PaymentMethod.orange_money,
    "orange": PaymentMethod.orange_money,
    "orange money": PaymentMethod.orange_money,
    "om": PaymentMethod.orange_money,
    "2": PaymentMethod.airtel_money,
    "airtel": PaymentMethod.airtel_money,
    "airtel money": PaymentMethod.airtel_money,
    "am": PaymentMethod.airtel_money,
    "3": PaymentMethod.mpesa,
    "mpesa": PaymentMethod.mpesa,
    "m-pesa": PaymentMethod.mpesa,
    "mpesa kongo": PaymentMethod.mpesa,
    "4": PaymentMethod.cash,
    "cash": PaymentMethod.cash,
    "cod": PaymentMethod.cash,
    "livraison": PaymentMethod.cash,
    "cash livraison": PaymentMethod.cash,
    "livrer": PaymentMethod.cash,
    "5": PaymentMethod.bank_transfer,
    "virement": PaymentMethod.bank_transfer,
    "banque": PaymentMethod.bank_transfer,
    "bank": PaymentMethod.bank_transfer,
    "transfer": PaymentMethod.bank_transfer,
}


def detect_order_intent(text: str) -> bool:
    """
    Return True if the customer message signals a purchase intent.

    Case-insensitive. Lingala / French / Swahili patterns covered.
    """
    normalized = text.lower().strip()
    return any(re.search(pattern, normalized) for pattern in _ORDER_SIGNALS)


def parse_payment_choice(text: str) -> PaymentMethod | None:
    """
    Extract payment method from customer's response to the payment menu.

    Returns PaymentMethod enum or None if not recognized.
    """
    normalized = text.lower().strip()
    # Try exact match first, then prefix match
    if normalized in _METHOD_ALIASES:
        return _METHOD_ALIASES[normalized]
    for alias, method in _METHOD_ALIASES.items():
        if normalized.startswith(alias) or alias in normalized:
            return method
    return None


def build_payment_menu(language: str) -> str:
    """Return the payment method selection menu in the customer's language."""
    return t("payment_menu", language)


def build_order_summary(
    *,
    product_name: str,
    quantity: int,
    unit_price_cdf: float,
    language: str,
) -> str:
    """
    Build a confirmation message showing the product and price.

    Keeps it under 3 sentences per DRC tone rules.
    """
    total = int(unit_price_cdf * quantity)
    lines = [
        t("order_confirm_product", language).format(
            product=product_name,
            qty=quantity,
            price=f"{total:,} FC",
        ),
        t("order_confirm_question", language),
    ]
    return "\n".join(lines)


def build_payment_initiated_message(
    *,
    method: PaymentMethod,
    amount_cdf: float,
    language: str,
) -> str:
    """Message sent after USSD/STK push is initiated."""
    amount_str = f"{int(amount_cdf):,} FC"
    key = f"payment_initiated_{method.value}"
    msg = t(key, language)
    if msg == key:  # Key missing in template — fallback
        msg = t("payment_initiated_fallback", language)
    return msg.format(amount=amount_str)


def build_order_confirmed_message(
    *,
    order_number: str,
    product_summary: str,
    delivery_zone: str,
    language: str,
) -> str:
    """Message sent after payment callback confirms success."""
    return t("order_confirmed", language).format(
        order_id=order_number,
        product=product_summary,
        zone=delivery_zone,
    )
