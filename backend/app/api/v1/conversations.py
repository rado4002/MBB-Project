"""
EP-02: GET  /api/v1/conversations/{conversation_id}
EP-03: PUT  /api/v1/conversations/{conversation_id}/context
EP-17: GET  /api/v1/conversations   (list, paginated)
EP-18: PUT  /api/v1/conversations/{conversation_id}/status
EP-19: POST /api/v1/conversations/{conversation_id}/escalate
A-13:  PUT  /api/v1/conversations/{conversation_id}/handoff
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.modules.m8_maps.escalation import create_ticket, handoff_conversation
from app.schemas.admin import HandoffToggle, HandoffToggleResponse
from app.schemas.conversations import (
    ConversationContextResponse,
    ConversationContextUpdate,
    ConversationResponse,
    CustomerSummary,
    LeadInConversation,
    MessageItem,
)
from app.schemas.common import PaginationMeta
from app.schemas.escalations import EscalationCreate, EscalationResponse

log = structlog.get_logger()
router = APIRouter(prefix="/conversations", tags=["M4 — Conversation Engine"])


@router.get(
    "/{conversation_id}",
    response_model=ConversationResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_conversation(
    conversation_id: uuid.UUID,
    db: DBSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    include_context: bool = Query(default=True),
):
    """Retrieve full conversation history with messages."""
    conv = await db.get(Conversation, conversation_id, options=[
        selectinload(Conversation.customer),
        selectinload(Conversation.lead),
    ])
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Fetch messages with pagination
    msg_q = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(msg_q)
    msgs = result.scalars().all()

    total_msgs = (await db.execute(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )).scalar() or 0

    cust = conv.customer
    customer_summary = CustomerSummary(
        phone_number=cust.phone_number,
        name=cust.name,
        city=cust.city,
        language=cust.preferred_language,
        club_member=cust.club_member,
    ) if cust else CustomerSummary(phone_number=conv.customer_id)

    lead_info = None
    if conv.lead:
        lead_info = LeadInConversation(
            lead_id=conv.lead.lead_id,
            score=conv.lead.score,
            stage=conv.lead.stage,
            relance_count=conv.lead.relance_count,
        )

    return ConversationResponse(
        conversation_id=conv.conversation_id,
        customer=customer_summary,
        status=conv.status,
        language_detected=conv.language_detected,
        context=conv.context if include_context else None,
        message_count=conv.message_count,
        messages=[
            MessageItem(
                message_id=m.message_id,
                timestamp=m.timestamp,
                direction=m.direction,
                content=m.content,
                content_type=m.content_type,
                language=m.language,
            )
            for m in msgs
        ],
        lead=lead_info,
        pagination=PaginationMeta(total=total_msgs, limit=limit, offset=offset),
    )


@router.put(
    "/{conversation_id}/context",
    response_model=ConversationContextResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_conversation_context(
    conversation_id: uuid.UUID,
    body: ConversationContextUpdate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Manually update conversation JSONB context (Celery tasks or Hub Team). Idempotent via key tracking."""
    conv = await db.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.context = {**conv.context, **body.context}
    await db.flush()
    return ConversationContextResponse(
        conversation_id=conv.conversation_id,
        context=conv.context,
        updated_at=conv.updated_at,
    )


@router.get(
    "",
    dependencies=[Depends(get_current_role)],
)
async def list_conversations(
    db: DBSession,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """List conversations with optional status filter (EP-17)."""
    q = select(Conversation).order_by(Conversation.last_message_time.desc())
    count_q = select(func.count()).select_from(Conversation)

    if status_filter:
        q = q.where(Conversation.status == status_filter)
        count_q = count_q.where(Conversation.status == status_filter)

    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(q.offset(offset).limit(limit).options(
        selectinload(Conversation.customer),
        selectinload(Conversation.lead),
    ))
    convs = result.scalars().all()

    items = []
    for c in convs:
        cust = c.customer
        items.append({
            "conversation_id": str(c.conversation_id),
            "customer_phone": c.customer_id,
            "customer_name": cust.name if cust else None,
            "status": c.status,
            "language_detected": c.language_detected,
            "message_count": c.message_count,
            "last_message_time": c.last_message_time.isoformat() if c.last_message_time else None,
            "lead_score": c.lead.score if c.lead else None,
        })

    return {
        "conversations": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.put(
    "/{conversation_id}/status",
    dependencies=[Depends(get_current_role)],
)
async def update_conversation_status(
    conversation_id: uuid.UUID,
    db: DBSession,
    new_status: str | None = None,
):
    """Update conversation status lifecycle (EP-18)."""
    conv = await db.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if new_status:
        conv.status = new_status
        await db.flush()
    return {"conversation_id": str(conv.conversation_id), "status": conv.status}


@router.post(
    "/{conversation_id}/escalate",
    response_model=EscalationResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_role)],
)
async def escalate_conversation(
    conversation_id: uuid.UUID,
    body: EscalationCreate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Manually escalate a conversation to the Hub Team (EP-19). Idempotent via key tracking."""
    conv = await db.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = await create_ticket(
        db,
        conversation_id=conversation_id,
        customer_id=conv.customer_id,
        reason=body.reason,
        priority=body.priority,
        lead_id=getattr(body, "lead_id", None),
    )
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    return EscalationResponse(
        escalation_id=result["escalation_id"],
        conversation_id=conversation_id,
        priority=result["priority"],
        status="open",
        reason=result["reason"],
        escalation_type=body.escalation_type,
        assigned_to=None,
        created_at=now,
        updated_at=now,
    )


@router.put(
    "/{conversation_id}/handoff",
    response_model=HandoffToggleResponse,
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def toggle_handoff(
    conversation_id: uuid.UUID,
    body: HandoffToggle,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Switch conversation between bot-controlled and human-controlled (A-13). Idempotent via key tracking."""
    try:
        result = await handoff_conversation(db, conversation_id, body.mode)
    except ValueError:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return HandoffToggleResponse(
        conversation_id=conversation_id,
        mode=result["mode"],
        updated_at=result["updated_at"],
    )
