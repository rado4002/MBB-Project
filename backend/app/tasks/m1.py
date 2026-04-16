"""
app/tasks/m1.py — Celery tasks for M1 Message Gateway.

Tasks:
  - process_inbound_message : Full M1 processing flow (AI response + send)
  - drain_blackout_queue    : Drain Redis blackout queue on power recovery
"""
from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

import structlog
from celery import Task

from app.tasks.celery_app import celery_app

log = structlog.get_logger(__name__)

# ── i18n acknowledgment messages ─────────────────────────────────────────────
_VOICE_ACK = {
    "lingala": (
        "Mbote! 🎙️ Na-yoki voice note na yo. "
        "Mosali na biso azali ko-vérifier surtout. "
        "Tika nga nakozela yo mwa minuti."
    ),
    "swahili": (
        "Habari! 🎙️ Nimepokea ujumbe wako wa sauti. "
        "Mtu wa timu yetu atawasiliana nawe hivi karibuni. "
        "Asante kwa subira yako."
    ),
    "french": (
        "Salut! 🎙️ J'ai bien reçu ton message vocal. "
        "Un membre de l'équipe va te contacter très bientôt. "
        "Merci de ta patience! 🙏"
    ),
}

_OPT_OUT_ACK = {
    "lingala": "OK ndeko, nakosala te lisusu. Ozali libre kozongela nge ntango olingi. 🙏",
    "swahili": "Sawa, sitawasiliana nawe tena. Unaweza kurudi wakati wowote. 🙏",
    "french": "D'accord, je ne t'enverrai plus de messages. Tu peux revenir quand tu veux. 🙏",
}

_RECOVERY_MSG = {
    "lingala": "Naza-zonga! Message na yo e-batelami ✓",
    "swahili": "Namerudi! Ujumbe wako umehifadhiwa ✓",
    "french": "Je suis de retour! Ton message a été conservé ✓",
}


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 30


# ── System prompt template ────────────────────────────────────────────────────

def _build_system_prompt(language: str, history: list[dict]) -> str:
    """Build Claude system prompt with conversation history context."""
    lang_label = {"lingala": "Lingala", "french": "Français", "swahili": "Swahili"}.get(
        language, "Français"
    )
    history_text = ""
    for h in history[-6:]:   # last 3 exchanges (6 messages max)
        role = "Client" if h.get("direction") == "inbound" else "Moi (bot)"
        history_text += f"{role}: {h.get('content', '')}\n"

    return (
        f"Tu es l'assistant commercial de MBB ya Kin — une boutique WhatsApp à Kinshasa.\n"
        f"Langue du client: {lang_label}.\n"
        f"Réponds TOUJOURS dans la langue du client ({lang_label}).\n"
        f"Historique récent:\n{history_text}\n"
        "Aide le client à trouver ce dont il a besoin. "
        "Pose UNE question de qualification si le besoin n'est pas clair. "
        "2-3 phrases maximum."
    )


# ── Main processing task ──────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="m1.process_inbound_message",
    queue="default",
    acks_late=True,
)
def process_inbound_message(
    self: Task,
    *,
    message_id: str,
    customer_phone: str,
    content: str,
    content_type: str,
    timestamp: str,
    whatsapp_message_id: str,
) -> dict:
    """
    Full M1 processing flow executed by a Celery worker:

    1. Open DB session
    2. Upsert customer + conversation, detect language, persist inbound message
    3. Voice note → escalation ticket + ack → return
    4. Opt-out → ack → return
    5. Build Claude prompt from session cache + DB history
    6. Call Claude API (with circuit breaker)
    7. Persist outbound message
    8. Dispatch MAPS tag generation (async Celery task)
    9. Update Redis session cache
    10. Send response via messaging adapter
    """
    return asyncio.run(
        _process(
            task=self,
            message_id=message_id,
            customer_phone=customer_phone,
            content=content,
            content_type=content_type,
            timestamp=timestamp,
            whatsapp_message_id=whatsapp_message_id,
        )
    )


async def _process(
    *,
    task: Task,
    message_id: str,
    customer_phone: str,
    content: str,
    content_type: str,
    timestamp: str,
    whatsapp_message_id: str,
) -> dict:
    from app.database import async_session_factory
    from app.modules.m1_gateway.service import process_inbound, persist_outbound
    from app.modules.m1_gateway.session_cache import get_session, save_session, SessionState
    from app.adapters import get_messaging_adapter, get_ai_adapter
    from sqlalchemy import select

    t0 = time.monotonic()

    parsed_ts = datetime.fromisoformat(timestamp)
    msg_uuid = uuid.UUID(message_id)

    async with async_session_factory() as session:
        # ── Steps 2-4 ─────────────────────────────────────────────────────────
        try:
            inbound = await process_inbound(
                session=session,
                customer_phone=customer_phone,
                content=content,
                content_type=content_type,
                timestamp=parsed_ts,
                whatsapp_message_id=whatsapp_message_id,
                message_id=msg_uuid,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            log.error("m1.process_inbound.error", phone=customer_phone, error=str(exc))
            raise task.retry(exc=exc, countdown=2 ** task.request.retries * 30)

        conv_id = str(inbound.conversation_id)
        language = inbound.language

        # ── Opt-out ────────────────────────────────────────────────────────────
        if inbound.is_opted_out:
            ack = _OPT_OUT_ACK.get(language, _OPT_OUT_ACK["french"])
            await _send_safe(customer_phone, ack)
            return {"status": "opt_out", "phone": customer_phone}

        # ── Voice note escalation ──────────────────────────────────────────────
        if inbound.is_voice_note:
            await _handle_voice_note(
                session=session,
                customer_phone=customer_phone,
                conversation_id=inbound.conversation_id,
                language=language,
            )
            ack = _VOICE_ACK.get(language, _VOICE_ACK["french"])
            await _send_safe(customer_phone, ack)
            return {"status": "escalated_voice_note", "conversation_id": conv_id}

        # ── Step 5: Load Redis session cache ──────────────────────────────────
        session_state = await get_session(conv_id)

        # Load recent DB history if cache is cold
        history: list[dict] = []
        if session_state is None:
            from app.models.message import Message
            msg_result = await session.execute(
                select(Message)
                .where(Message.conversation_id == inbound.conversation_id)
                .order_by(Message.timestamp.desc())
                .limit(10)
            )
            db_msgs = list(reversed(msg_result.scalars().all()))
            history = [
                {"direction": m.direction, "content": m.content, "language": m.language}
                for m in db_msgs
            ]
            # Seed new session state from DB
            session_state = SessionState(
                customer_id=customer_phone,
                language=language,
                history=history,
            )
        else:
            history = session_state.history

        # ── Step 6: Call Claude ───────────────────────────────────────────────
        ai = get_ai_adapter()
        system_prompt = _build_system_prompt(language, history)
        try:
            ai_response = await ai.generate(
                prompt=content,
                system=system_prompt,
                max_tokens=512,
            )
        except Exception as exc:
            log.error("m1.claude.error", conv_id=conv_id, error=str(exc))
            # Fallback message in customer language
            ai_response = {
                "lingala": "Pole ndeko, na-zali na problème mwa moke. Kozongela ye mwa ntango. 🙏",
                "swahili": "Pole, nina tatizo kidogo. Tafadhali jaribu tena baadaye. 🙏",
                "french": "Désolé, j'ai un petit problème technique. Réessaie dans un moment. 🙏",
            }.get(language, "Désolé, réessaie dans un moment. 🙏")

        processing_ms = int((time.monotonic() - t0) * 1000)

        # ── Step 7: Persist outbound message ──────────────────────────────────
        async with async_session_factory() as session2:
            try:
                out_msg_id = await persist_outbound(
                    session=session2,
                    conversation_id=inbound.conversation_id,
                    content=ai_response,
                    language=language,
                    processing_time_ms=processing_ms,
                )
                await session2.commit()
            except Exception as exc:
                await session2.rollback()
                log.error("m1.persist_outbound.error", conv_id=conv_id, error=str(exc))
                out_msg_id = uuid.uuid4()  # fallback — don't fail the whole task

        # ── Step 8: Dispatch MAPS tag generation ──────────────────────────────
        try:
            celery_app.send_task(
                "app.tasks.maps.tag_event",
                kwargs={
                    "conversation_id": conv_id,
                    "event_type": "inbound_message",
                    "payload": {
                        "message_id": str(msg_uuid),
                        "content": content[:200],
                        "language": language,
                        "content_type": content_type,
                    },
                },
                queue="maps",
            )
        except Exception as exc:
            log.warning("m1.maps_dispatch.failed", conv_id=conv_id, error=str(exc))

        # ── Step 9: Update Redis session cache ────────────────────────────────
        session_state.language = language
        session_state.last_msg_time = datetime.now(timezone.utc).isoformat()
        session_state.msg_count += 1
        session_state.history.append(
            {"direction": "inbound", "content": content, "language": language}
        )
        session_state.history.append(
            {"direction": "outbound", "content": ai_response, "language": language}
        )
        await save_session(conv_id, session_state)

        # ── Step 10: Send response ─────────────────────────────────────────────
        await _send_safe(customer_phone, ai_response)

        log.info(
            "m1.processed",
            phone=customer_phone,
            conv_id=conv_id,
            lang=language,
            processing_ms=processing_ms,
        )
        return {
            "status": "processed",
            "conversation_id": conv_id,
            "outbound_message_id": str(out_msg_id),
            "language": language,
            "processing_ms": processing_ms,
        }


async def _send_safe(phone: str, text: str) -> None:
    """Send a message, swallowing errors (message is already persisted in DB)."""
    from app.adapters import get_messaging_adapter
    try:
        adapter = get_messaging_adapter()
        await adapter.send_message(phone, text)
    except Exception as exc:
        log.error("m1.send_message.failed", phone=phone, error=str(exc))


async def _handle_voice_note(
    *,
    session,
    customer_phone: str,
    conversation_id: uuid.UUID,
    language: str,
) -> None:
    """Create an escalation ticket for a voice note message."""
    from app.models.escalation_ticket import EscalationTicket

    ticket = EscalationTicket(
        conversation_id=conversation_id,
        customer_id=customer_phone,
        reason="voice_note",
        priority="high",
        status="open",
        transcript_snapshot=[],
        maps_tags_snapshot=None,
    )
    session.add(ticket)
    try:
        await session.commit()
        log.info(
            "m1.voice_note_escalation_created",
            phone=customer_phone,
            conv_id=str(conversation_id),
        )
    except Exception as exc:
        await session.rollback()
        log.error("m1.voice_note_escalation.error", phone=customer_phone, error=str(exc))


# ── Blackout queue drainer ────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="m1.drain_blackout_queue",
    queue="default",
    acks_late=True,
)
def drain_blackout_queue(self: Task) -> dict:
    """
    Drain the Redis blackout queue (DB3 list `blackout:queue`) on power recovery.
    Each item is a JSON-serialised inbound message payload.
    Beat-scheduled every 5 minutes.
    """
    return asyncio.run(_drain(self))


async def _drain(task: Task) -> dict:
    from app.redis_client import blackout_dequeue_batch

    processed = 0
    errors = 0
    MAX_BATCH = 50  # avoid hogging the worker for too long

    messages = await blackout_dequeue_batch(batch_size=MAX_BATCH)
    for payload in messages:
        try:
            celery_app.send_task(
                "m1.process_inbound_message",
                kwargs=payload,
                queue="default",
            )
            processed += 1
        except Exception as exc:
            log.error("m1.drain.item_failed", error=str(exc))
            errors += 1

    log.info("m1.drain_blackout_queue.done", processed=processed, errors=errors)
    return {"processed": processed, "errors": errors}
