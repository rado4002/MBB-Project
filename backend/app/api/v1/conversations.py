"""
EP-02: GET  /api/v1/conversations/{conversation_id}
EP-03: PUT  /api/v1/conversations/{conversation_id}/context
EP-17: GET  /api/v1/conversations   (list, paginated)
EP-18: PUT  /api/v1/conversations/{conversation_id}/status
EP-19: POST /api/v1/conversations/{conversation_id}/escalate
A-13:  PUT  /api/v1/conversations/{conversation_id}/handoff
"""
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DBSession, RedisClient, get_current_role, require_role
from app.schemas.admin import HandoffToggle, HandoffToggleResponse
from app.schemas.conversations import (
    ConversationContextResponse,
    ConversationContextUpdate,
    ConversationResponse,
)
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
    """Retrieve full conversation history. Used by Hub Team escalation view."""
    log.info("conversation.get", conversation_id=str(conversation_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


@router.put(
    "/{conversation_id}/context",
    response_model=ConversationContextResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_conversation_context(
    conversation_id: uuid.UUID,
    body: ConversationContextUpdate,
    db: DBSession,
):
    """Manually update conversation JSONB context (Celery tasks or Hub Team)."""
    log.info("conversation.context.update", conversation_id=str(conversation_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


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
    log.info("conversation.list", status_filter=status_filter)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


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
    log.info("conversation.status.update", conversation_id=str(conversation_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M4 not yet implemented")


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
):
    """Manually escalate a conversation to the Hub Team (EP-19)."""
    log.info("conversation.escalate", conversation_id=str(conversation_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M8 not yet implemented")


@router.put(
    "/{conversation_id}/handoff",
    response_model=HandoffToggleResponse,
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def toggle_handoff(
    conversation_id: uuid.UUID,
    body: HandoffToggle,
    db: DBSession,
):
    """Switch conversation between bot-controlled and human-controlled (A-13)."""
    log.info("conversation.handoff", conversation_id=str(conversation_id), mode=body.mode)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A-13 not yet implemented")
