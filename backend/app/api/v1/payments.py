"""
EP-14: POST /api/v1/payments/callback
HMAC-SHA256 verified webhook from Mobile Money providers.
"""
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import DBSession
from app.config import get_settings
from app.schemas.payments import PaymentCallback, PaymentCallbackResponse
from app.security import require_hmac

log = structlog.get_logger()
settings = get_settings()
router = APIRouter(prefix="/payments", tags=["M7 — Payment"])


@router.post(
    "/callback",
    response_model=PaymentCallbackResponse,
    status_code=status.HTTP_200_OK,
)
async def payment_callback(
    request: Request,
    db: DBSession,
):
    """
    Receive payment status callback from Mobile Money provider.

    HMAC-SHA256 signature is validated before the body is parsed.
    The raw body is consumed for HMAC verification; Pydantic parses JSON after.
    """
    # HMAC verification (uses raw body before Pydantic parsing)
    await require_hmac(request, settings.payment_webhook_secret)

    body_bytes = await request.body()
    import json
    try:
        data = json.loads(body_bytes)
        payload = PaymentCallback(**data)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    log.info(
        "payment.callback",
        order_id=str(payload.order_id),
        provider=payload.provider,
        status=payload.status,
        tx_id=payload.transaction_id,
    )

    # Dispatch to M7 Celery task
    from app.tasks.celery_app import celery_app

    celery_app.send_task(
        "m7.process_payment_callback",
        kwargs={
            "transaction_id": payload.transaction_id,
            "order_id": str(payload.order_id),
            "provider": payload.provider.value,
            "status": payload.status.value,
            "amount_cdf": str(payload.amount_cdf),
            "customer_phone": payload.customer_phone,
            "timestamp": payload.timestamp.isoformat(),
        },
        queue="conversion",
    )

    return PaymentCallbackResponse(
        order_id=payload.order_id,
        status=payload.status,
        acknowledged_at=datetime.now(timezone.utc),
    )
