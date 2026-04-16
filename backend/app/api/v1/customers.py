"""
EP-20: GET /api/v1/customers/{phone_number}
EP-21: PUT /api/v1/customers/{phone_number}/opt-out
EP-22: PUT /api/v1/customers/{phone_number}/language
"""
import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DBSession, get_current_role
from app.schemas.customers import (
    CustomerLanguageResponse,
    CustomerLanguageUpdate,
    CustomerResponse,
    OptOutResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/customers", tags=["M4 — Customers"])


@router.get(
    "/{phone_number:path}",
    response_model=CustomerResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_customer(phone_number: str, db: DBSession):
    """Retrieve customer profile by DRC phone number."""
    log.info("customer.get", phone=phone_number)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


@router.put(
    "/{phone_number:path}/opt-out",
    response_model=OptOutResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_current_role)],
)
async def opt_out_customer(phone_number: str, db: DBSession):
    """
    Register customer opt-out. Immediately stops all automated messages.
    Signals: 'stop', 'arrête', 'yaka te', 'tika' are also auto-detected in M2.
    """
    log.info("customer.opt_out", phone=phone_number)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


@router.put(
    "/{phone_number:path}/language",
    response_model=CustomerLanguageResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_customer_language(
    phone_number: str,
    body: CustomerLanguageUpdate,
    db: DBSession,
):
    """Update preferred communication language for a customer."""
    log.info("customer.language.update", phone=phone_number, language=body.language)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")
