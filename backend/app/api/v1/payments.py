"""
EP-14: POST /api/v1/payments/callback
Legacy generic callback is disabled pending provider-specific verification.
"""
from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import DBSession
from app.schemas.payments import PaymentCallbackResponse

router = APIRouter(prefix="/payments", tags=["M7 — Payment"])


@router.post(
    "/callback",
    response_model=PaymentCallbackResponse,
    status_code=status.HTTP_200_OK,
    responses={503: {"description": "No approved provider verification contract is active."}},
)
async def payment_callback(
    request: Request,
    db: DBSession,
):
    """Reject generic callbacks until a verified DRC provider contract exists."""
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="payment_provider_verification_unavailable",
    )
