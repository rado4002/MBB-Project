"""Human-specific delivery task for accepted browser replies."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select

from app.config import get_settings
from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)
settings = get_settings()


def _now() -> datetime:
    return datetime.now(timezone.utc)


@celery_app.task(
    name="operator_replies.deliver",
    queue="default",
    acks_late=True,
    max_retries=0,
)
def deliver_operator_reply(*, message_id: str) -> dict[str, str]:
    return run_async(_deliver_operator_reply(uuid.UUID(message_id)))


async def _deliver_operator_reply(message_id: uuid.UUID) -> dict[str, str]:
    from app.adapters import get_messaging_adapter
    from app.database import async_session_factory
    from app.models.conversation import Conversation
    from app.models.message import Message
    from app.modules.m4_conversation.operator_replies import REPLY_ELIGIBLE_STATUSES

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(Message, Conversation)
                .join(
                    Conversation,
                    Conversation.conversation_id == Message.conversation_id,
                )
                .where(Message.message_id == message_id)
                .with_for_update(of=(Message, Conversation))
            )
        ).one_or_none()
        if row is None:
            return {"status": "missing", "message_id": str(message_id)}
        message, conversation = row
        if message.operator_author_account_id is None:
            return {"status": "not_operator_reply", "message_id": str(message_id)}
        if message.delivery_state not in {None, "accepted"}:
            return {
                "status": message.delivery_state or "legacy",
                "message_id": str(message_id),
            }

        blocked = (
            conversation.owner_type != "human"
            or conversation.human_owner_account_id
            != message.operator_author_account_id
            or conversation.ai_execution_state != "paused"
            or conversation.ownership_version
            != message.accepted_ownership_version
            or conversation.status not in REPLY_ELIGIBLE_STATUSES
            or settings.messaging_adapter != "whatsapp"
            or settings.whatsapp_mode != "baileys"
        )
        if blocked:
            message.delivery_state = "failed"
            message.delivery_state_timestamp = _now()
            await session.commit()
            log.info(
                "operator_reply.delivery_blocked",
                message_id=str(message_id),
                conversation_id=str(conversation.conversation_id),
            )
            return {"status": "failed", "message_id": str(message_id)}

        try:
            adapter = get_messaging_adapter()
        except Exception as exc:
            message.delivery_state = "failed"
            message.delivery_state_timestamp = _now()
            await session.commit()
            log.error(
                "operator_reply.adapter_unavailable",
                message_id=str(message_id),
                error_type=type(exc).__name__,
            )
            return {"status": "failed", "message_id": str(message_id)}

        try:
            provider_message_id = await adapter.send_message(
                conversation.customer_id,
                message.content,
                idempotency_key=str(message.message_id),
            )
        except Exception as exc:
            message.delivery_state = "uncertain"
            message.delivery_state_timestamp = _now()
            await session.commit()
            log.error(
                "operator_reply.delivery_uncertain",
                message_id=str(message_id),
                error_type=type(exc).__name__,
            )
            return {"status": "uncertain", "message_id": str(message_id)}

        if (
            not isinstance(provider_message_id, str)
            or not provider_message_id.strip()
        ):
            message.delivery_state = "failed"
            message.delivery_state_timestamp = _now()
            await session.commit()
            return {"status": "failed", "message_id": str(message_id)}
        if len(provider_message_id.strip()) > 100:
            message.delivery_state = "uncertain"
            message.delivery_state_timestamp = _now()
            await session.commit()
            return {"status": "uncertain", "message_id": str(message_id)}

        message.whatsapp_message_id = provider_message_id.strip()
        message.delivery_state = "sent"
        message.delivery_state_timestamp = _now()
        await session.commit()
        log.info(
            "operator_reply.sent",
            message_id=str(message_id),
            conversation_id=str(conversation.conversation_id),
        )
        return {"status": "sent", "message_id": str(message_id)}
