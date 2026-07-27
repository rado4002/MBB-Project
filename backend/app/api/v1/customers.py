"""
EP-20: GET /api/v1/customers/{phone_number}
EP-21: PUT /api/v1/customers/{phone_number}/opt-out
EP-22: PUT /api/v1/customers/{phone_number}/language
A-10:  GET /api/v1/customers (admin list)
"""
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.models.customer import Customer
from app.modules.m6_relance.service import cancel_all_relances
from app.schemas.customers import (
    CustomerLanguageResponse,
    CustomerLanguageUpdate,
    CustomerResponse,
    OptOutResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/customers", tags=["M4 — Customers"])


def _customer_response_payload(
    customer: Customer,
    *,
    conversation_count: int | None = None,
    lead_count: int | None = None,
) -> dict:
    payload = {
        "phone_number": customer.phone_number,
        "name": customer.name,
        "city": customer.city,
        "language": customer.preferred_language,
        "preferred_language": customer.preferred_language,
        "opt_out": customer.opt_out_flag,
        "opted_out": customer.opt_out_flag,
        "club_member": customer.club_member,
        "club_points": customer.club_points,
        "created_at": customer.created_at,
        "updated_at": customer.last_interaction,
    }
    if conversation_count is not None:
        payload["conversation_count"] = conversation_count
    if lead_count is not None:
        payload["lead_count"] = lead_count
    return payload


@router.get(
    "/{phone_number:path}",
    response_model=CustomerResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_customer(phone_number: str, db: DBSession):
    """Retrieve a customer profile by canonical international E.164 number."""
    log.info("customer.get", phone=phone_number)
    
    customer = await db.get(Customer, phone_number)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    # Count conversations and leads
    from app.models.conversation import Conversation
    from app.models.lead import Lead
    
    conv_count = (await db.execute(
        select(func.count()).select_from(Conversation).where(Conversation.customer_id == phone_number)
    )).scalar() or 0
    
    lead_count = (await db.execute(
        select(func.count()).select_from(Lead).where(Lead.customer_id == phone_number)
    )).scalar() or 0
    
    return CustomerResponse(**_customer_response_payload(
        customer,
        conversation_count=conv_count,
        lead_count=lead_count,
    ))


@router.put(
    "/{phone_number:path}/opt-out",
    response_model=OptOutResponse,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(get_current_role)],
)
async def opt_out_customer(phone_number: str, db: DBSession, idempotency_key: IdempotencyKey):
    """
    Register customer opt-out. Immediately stops all automated messages.
    Signals: 'stop', 'arrête', 'yaka te', 'tika' are also auto-detected in M2.
    Idempotent via phone_number PK.
    """
    log.info("customer.opt_out", phone=phone_number)
    
    customer = await db.get(Customer, phone_number)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    # Idempotency: already opted out
    if customer.opted_out:
        log.debug("customer.already_opted_out", phone=phone_number)
        return OptOutResponse(
            phone_number=customer.phone_number,
            opted_out=True,
            opted_out_at=customer.updated_at,
        )
    
    # Mark as opted out
    customer.opted_out = True
    customer.updated_at = datetime.now(timezone.utc)
    
    # Cancel all pending relances for this customer's leads
    from app.models.lead import Lead
    leads_result = await db.execute(
        select(Lead).where(Lead.customer_id == phone_number)
    )
    leads = leads_result.scalars().all()
    
    total_cancelled = 0
    for lead in leads:
        cancelled = await cancel_all_relances(session=db, lead_id=lead.lead_id)
        total_cancelled += cancelled
    
    await db.commit()
    
    log.info(
        "customer.opted_out_success",
        phone=phone_number,
        relances_cancelled=total_cancelled,
    )
    
    return OptOutResponse(
        phone_number=customer.phone_number,
        opted_out=True,
        opted_out_at=customer.updated_at,
    )


@router.put(
    "/{phone_number:path}/language",
    response_model=CustomerLanguageResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_customer_language(
    phone_number: str,
    body: CustomerLanguageUpdate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Update preferred communication language for a customer. Idempotent via phone_number PK."""
    log.info("customer.language.update", phone=phone_number, language=body.language)
    
    customer = await db.get(Customer, phone_number)
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    
    # Update language (idempotent: overwriting is safe)
    customer.preferred_language = body.language
    customer.updated_at = datetime.now(timezone.utc)
    await db.commit()
    
    return CustomerLanguageResponse(
        phone_number=customer.phone_number,
        preferred_language=customer.preferred_language,
        updated_at=customer.updated_at,
    )


@router.get(
    "",
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def list_customers(
    db: DBSession,
    city: str | None = Query(default=None),
    language: str | None = Query(default=None),
    opted_out: bool | None = Query(default=None),
    club_member: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Admin: list all customers with filters (A-10)."""
    log.info("customer.list", city=city, language=language, limit=limit, offset=offset)
    
    # Build query with filters
    q = select(Customer).order_by(Customer.created_at.desc())
    count_q = select(func.count()).select_from(Customer)
    
    if city:
        q = q.where(Customer.city == city)
        count_q = count_q.where(Customer.city == city)
    
    if language:
        q = q.where(Customer.preferred_language == language)
        count_q = count_q.where(Customer.preferred_language == language)
    
    if opted_out is not None:
        q = q.where(Customer.opt_out_flag == opted_out)
        count_q = count_q.where(Customer.opt_out_flag == opted_out)
    
    if club_member is not None:
        q = q.where(Customer.club_member == club_member)
        count_q = count_q.where(Customer.club_member == club_member)
    
    # Execute queries
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(q.offset(offset).limit(limit))
    customers = result.scalars().all()
    
    # Format response
    items = []
    for c in customers:
        # Count conversations and leads
        from app.models.conversation import Conversation
        from app.models.lead import Lead
        
        conv_count = (await db.execute(
            select(func.count()).select_from(Conversation).where(Conversation.customer_id == c.phone_number)
        )).scalar() or 0
        
        lead_count = (await db.execute(
            select(func.count()).select_from(Lead).where(Lead.customer_id == c.phone_number)
        )).scalar() or 0
        
        payload = _customer_response_payload(
            c,
            conversation_count=conv_count,
            lead_count=lead_count,
        )
        payload["created_at"] = payload["created_at"].isoformat()
        payload["updated_at"] = payload["updated_at"].isoformat()
        items.append(payload)
    
    return {
        "customers": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
