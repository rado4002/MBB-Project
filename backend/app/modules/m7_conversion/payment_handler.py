"""
app/modules/m7_conversion/payment_handler.py — Payment callback processing.

Handles:
  1. HMAC-SHA256 signature verification (security boundary)
  2. Idempotency check (payment_reference deduplication)
  3. Dispatching callback to service layer
"""
from __future__ import annotations

import hmac
import hashlib
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.payment import Payment
from app.modules.m7_conversion.service import PaymentPrerequisiteUnavailable
from app.schemas.payments import PaymentCallback, PaymentCallbackResponse

log = structlog.get_logger(__name__)
settings = get_settings()


class HMACVerificationError(ValueError):
    """Raised when X-Payment-Signature header fails HMAC-SHA256 verification."""


class DuplicateCallbackError(ValueError):
    """Raised when the same payment_reference has already been processed."""


def verify_payment_hmac(body_bytes: bytes, signature_header: str) -> None:
    """
    Verify HMAC-SHA256 signature from Mobile Money provider.

    Raises HMACVerificationError if signature is missing or invalid.
    Uses constant-time comparison to prevent timing attacks.
    """
    if not settings.payment_webhook_secret:
        raise HMACVerificationError("Payment webhook verification is not configured")
    if not signature_header:
        raise HMACVerificationError("Missing X-Payment-Signature header")

    expected = hmac.new(
        settings.payment_webhook_secret.encode(),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    # Strip any "sha256=" prefix that some providers add
    received = signature_header.removeprefix("sha256=")

    if not hmac.compare_digest(expected, received):
        log.warning("payment.hmac.mismatch", received=received[:8] + "...")
        raise HMACVerificationError("HMAC-SHA256 signature verification failed")


async def check_idempotency(
    session: AsyncSession,
    transaction_id: str,
) -> Payment | None:
    """
    Check if this payment callback has already been processed.

    Returns existing Payment record if found (idempotent return),
    None if this is a new callback.
    """
    result = await session.execute(
        select(Payment).where(Payment.provider_transaction_id == transaction_id)
    )
    return result.scalar_one_or_none()


async def process_payment_callback(
    session: AsyncSession,
    callback: PaymentCallback,
) -> PaymentCallbackResponse:
    """Do not settle via this legacy provider-neutral callback processor."""
    raise PaymentPrerequisiteUnavailable(
        "Payment completion requires authoritative provider verification."
    )
