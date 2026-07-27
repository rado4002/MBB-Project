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
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.order import Order
from app.models.payment import Payment
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
    """
    Process a Mobile Money payment callback.

    Idempotent: safe to call multiple times with the same transaction_id.

    Steps:
    1. Check idempotency (skip if already processed)
    2. Find the Payment record for this order
    3. Update Payment.status → "completed" or "failed"
    4. If completed: update Order.status → "confirmed"

    Returns:
        PaymentCallbackResponse with current order status
    """
    now = datetime.now(timezone.utc)

    # Step 1: Idempotency check
    existing = await check_idempotency(session, str(callback.transaction_id))
    if existing is not None:
        log.info(
            "payment.callback.duplicate",
            transaction_id=callback.transaction_id,
            existing_status=existing.status,
        )
        # Return current state without re-processing
        order_result = await session.execute(
            select(Order).where(Order.order_id == callback.order_id)
        )
        order = order_result.scalar_one()
        from app.schemas.common import PaymentStatus
        return PaymentCallbackResponse(
            order_id=order.order_id,
            status=PaymentStatus(existing.status),
            acknowledged_at=now,
        )

    # Step 2: Fetch the Payment record
    payment_result = await session.execute(
        select(Payment).where(Payment.order_id == callback.order_id)
    )
    payment = payment_result.scalar_one_or_none()

    if payment is None:
        log.error("payment.callback.no_payment_found", order_id=str(callback.order_id))
        raise ValueError(f"No payment record for order {callback.order_id}")

    # Step 3: Update payment record
    payment.status = callback.status.value
    payment.provider_transaction_id = callback.transaction_id
    payment.provider_response = {
        "provider": callback.provider.value,
        "amount_cdf": float(callback.amount_cdf),
        "timestamp": callback.timestamp.isoformat(),
        "reference": callback.provider_reference,
    }
    if callback.status.value == "completed":
        payment.completed_at = now

    # Step 4: Update order status if payment succeeded
    order_result = await session.execute(
        select(Order).where(Order.order_id == callback.order_id)
    )
    order = order_result.scalar_one()

    if callback.status.value == "completed" and order.status == "pending":
        order.status = "confirmed"
        order.confirmed_at = now
        order.updated_at = now
        log.info(
            "payment.callback.order_confirmed",
            order_id=str(order.order_id),
            method=callback.provider.value,
        )

    await session.commit()

    from app.schemas.common import PaymentStatus
    return PaymentCallbackResponse(
        order_id=order.order_id,
        status=PaymentStatus(payment.status),
        acknowledged_at=now,
    )
