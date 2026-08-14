"""
app/tasks/m1.py — Celery tasks for M1 Message Gateway.

Tasks:
  - process_inbound_message : Full M1 processing flow (AI/local fallback response + send)
  - drain_blackout_queue    : Drain Redis blackout queue on power recovery
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

import structlog
from celery import Task
from sqlalchemy.exc import IntegrityError

from app.ai.audit import AITurnAuditRecord, AITurnOutcome, append_ai_turn_audit
from app.config import get_settings
from app.i18n.messages import t
from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)
settings = get_settings()

_INBOUND_WHATSAPP_UNIQUE_INDEX = "uq_messages_inbound_whatsapp_message_id"
_M1_AI_CAPABILITIES = (
    "get_product_details",
    "request_human_handoff",
    "search_products",
)


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 30


async def _ai_may_reply(
    session,
    conversation_id: uuid.UUID,
    *,
    lock: bool = False,
    expected_ownership_version: int | None = None,
) -> bool:
    from app.modules.m4_conversation.ownership import ai_may_reply

    return await ai_may_reply(
        session,
        conversation_id,
        lock=lock,
        expected_ownership_version=expected_ownership_version,
    )


async def _ai_reply_ownership_version(
    session,
    conversation_id: uuid.UUID,
) -> int | None:
    from app.modules.m4_conversation.ownership import ai_reply_ownership_version

    return await ai_reply_ownership_version(session, conversation_id)


async def _ai_is_waiting_for_human(
    session,
    conversation_id: uuid.UUID,
) -> bool:
    from app.modules.m4_conversation.ownership import ai_is_waiting_for_human

    return await ai_is_waiting_for_human(session, conversation_id)


def _integrity_constraint_name(exc: IntegrityError) -> str | None:
    """Extract a PostgreSQL constraint name without classifying other errors."""
    original = exc.orig
    for candidate in (original, getattr(original, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return name
        diagnostic = getattr(candidate, "diag", None)
        name = getattr(diagnostic, "constraint_name", None)
        if name:
            return name
    return None


async def _duplicate_result(session, whatsapp_message_id: str) -> dict:
    from app.models.message import Message
    from sqlalchemy import select

    result = await session.execute(
        select(Message).where(
            Message.direction == "inbound",
            Message.whatsapp_message_id == whatsapp_message_id,
        )
    )
    existing = result.scalar_one_or_none()
    return {
        "status": "duplicate_ignored",
        "whatsapp_message_id": whatsapp_message_id,
        "existing_message_id": str(existing.message_id) if existing else None,
        "conversation_id": str(existing.conversation_id) if existing else None,
    }


def _dispatch_maps_fanout(*, conversation_id: str, message_id: str, content: str,
                          language: str, content_type: str) -> None:
    if not settings.m1_maps_fanout_enabled:
        log.info("m1.maps_fanout_skipped_safety_gate")
        return

    celery_app.send_task(
        "app.tasks.maps.tag_event",
        kwargs={
            "conversation_id": conversation_id,
            "event_type": "inbound_message",
            "payload": {
                "message_id": message_id,
                "content": content[:200],
                "language": language,
                "content_type": content_type,
            },
        },
        queue="maps",
    )


async def _persist_outbound(
    *,
    conversation_id: uuid.UUID,
    content: str,
    language: str,
    processing_time_ms: int,
    expected_ownership_version: int,
    source_message_id: uuid.UUID | None = None,
    audit_record: AITurnAuditRecord | None = None,
) -> uuid.UUID | None:
    """Commit one response and its optional AI audit before any send attempt."""
    from app.database import async_session_factory
    from app.modules.m1_gateway.service import persist_outbound

    async with async_session_factory() as session:
        try:
            if not await _ai_may_reply(
                session,
                conversation_id,
                lock=True,
                expected_ownership_version=expected_ownership_version,
            ):
                await session.rollback()
                log.info(
                    "m1.persist_outbound.skipped",
                    conversation_id=str(conversation_id),
                    reason="ai_authority_changed",
                )
                return None
            outbound_id = await persist_outbound(
                session=session,
                conversation_id=conversation_id,
                content=content,
                language=language,
                processing_time_ms=processing_time_ms,
            )
            if audit_record is not None:
                if source_message_id is None:
                    raise ValueError("AI audit requires an authoritative source message")
                audit_values = audit_record.model_dump()
                audit_values.update(
                    source_message_id=source_message_id,
                    outbound_message_id=outbound_id,
                )
                await append_ai_turn_audit(
                    session,
                    AITurnAuditRecord.model_validate(audit_values, strict=True),
                )
            await session.commit()
            return outbound_id
        except Exception as exc:
            await session.rollback()
            log.error(
                "m1.persist_outbound.failed_closed",
                conversation_id=str(conversation_id),
                error_type=type(exc).__name__,
            )
            return None


def _persistence_failure_result(conversation_id: str) -> dict:
    return {
        "status": "persistence_failed",
        "conversation_id": conversation_id,
        "send_status": "unknown_or_failed",
    }


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
    5. Build response prompt from session cache + DB history
    6. Call the MBB AI turn service or use fallback when AI is disabled/unavailable
    7. Persist outbound message
    8. Dispatch MAPS tag generation (async Celery task)
    9. Update Redis session cache
    10. Send response via messaging adapter
    """
    return run_async(
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
    from app.modules.m1_gateway.service import process_inbound
    from app.modules.m1_gateway.session_cache import get_session, save_session, SessionState
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
            if inbound.is_duplicate:
                await session.rollback()
                return {
                    "status": "duplicate_ignored",
                    "whatsapp_message_id": whatsapp_message_id,
                    "existing_message_id": (
                        str(inbound.existing_message_id)
                        if inbound.existing_message_id else None
                    ),
                    "conversation_id": str(inbound.conversation_id),
                }
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if _integrity_constraint_name(exc) == _INBOUND_WHATSAPP_UNIQUE_INDEX:
                log.info("m1.inbound_duplicate_conflict", wa_id=whatsapp_message_id)
                return await _duplicate_result(session, whatsapp_message_id)
            log.error("m1.process_inbound.integrity_error", error_type=type(exc).__name__)
            raise task.retry(exc=exc, countdown=2 ** task.request.retries * 30)
        except Exception as exc:
            await session.rollback()
            log.error("m1.process_inbound.error", phone=customer_phone, error=str(exc))
            raise task.retry(exc=exc, countdown=2 ** task.request.retries * 30)

        conv_id = str(inbound.conversation_id)
        language = inbound.language
        expected_ownership_version = await _ai_reply_ownership_version(
            session,
            inbound.conversation_id,
        )
        if expected_ownership_version is None:
            waiting_for_human = await _ai_is_waiting_for_human(
                session,
                inbound.conversation_id,
            )
            await session.rollback()
            log.info(
                "m1.autonomous_reply.skipped",
                conv_id=conv_id,
                reason="ai_not_eligible",
            )
            return {
                "status": (
                    "waiting_for_human"
                    if waiting_for_human
                    else "human_controlled"
                ),
                "conversation_id": conv_id,
                "send_status": "skipped",
            }

        # ── Opt-out ────────────────────────────────────────────────────────────
        if inbound.is_opted_out:
            response_text = t("opt_out_ack", language)
            outbound_id = await _persist_outbound(
                conversation_id=inbound.conversation_id,
                content=response_text,
                language=language,
                processing_time_ms=int((time.monotonic() - t0) * 1000),
                expected_ownership_version=expected_ownership_version,
            )
            if outbound_id is None:
                return _persistence_failure_result(conv_id)
            send_result = await _send_safe(
                customer_phone,
                response_text,
                idempotency_key=str(outbound_id),
                conversation_id=inbound.conversation_id,
                expected_ownership_version=expected_ownership_version,
            )
            return {
                "status": "opt_out",
                "phone": customer_phone,
                "outbound_message_id": str(outbound_id),
                "send_status": send_result["status"],
            }

        # ── Voice note escalation ──────────────────────────────────────────────
        if inbound.is_voice_note:
            await _handle_voice_note(
                session=session,
                customer_phone=customer_phone,
                conversation_id=inbound.conversation_id,
                language=language,
            )
            response_text = t("voice_note_ack", language)
            outbound_id = await _persist_outbound(
                conversation_id=inbound.conversation_id,
                content=response_text,
                language=language,
                processing_time_ms=int((time.monotonic() - t0) * 1000),
                expected_ownership_version=expected_ownership_version,
            )
            if outbound_id is None:
                return _persistence_failure_result(conv_id)
            send_result = await _send_safe(
                customer_phone,
                response_text,
                idempotency_key=str(outbound_id),
                conversation_id=inbound.conversation_id,
                expected_ownership_version=expected_ownership_version,
            )
            return {
                "status": "escalated_voice_note",
                "conversation_id": conv_id,
                "outbound_message_id": str(outbound_id),
                "send_status": send_result["status"],
            }

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

        # End ownership/history reads before any provider inference begins.
        await session.rollback()

        # ── Step 6: Generate response or use local fallback ───────────────────
        from app.ai.turn import (
            AITurn,
            AITurnExecutionError,
            AITurnPersistenceError,
            get_ai_turn_service,
        )

        ai_turn_service = get_ai_turn_service()
        ai_turn = AITurn(
            user_content=content,
            language=language,
            expected_ownership_version=expected_ownership_version,
            conversation_id=inbound.conversation_id,
            source_message_id=inbound.message_id,
            history=history,
            allowed_capabilities=_M1_AI_CAPABILITIES,
        )
        audit_record = None
        try:
            finalized_turn = await ai_turn_service.generate_finalized(ai_turn)
        except AITurnPersistenceError:
            log.error("m1.ai_action.failed_closed", conv_id=conv_id)
            return _persistence_failure_result(conv_id)
        except AITurnExecutionError as exc:
            log.warning("m1.ai_fallback.used", conv_id=conv_id, error=str(exc))
            ai_response = t("error_fallback", language)
            audit_values = exc.audit_record.model_dump()
            audit_values["outcome"] = AITurnOutcome.fallback_used
            audit_record = AITurnAuditRecord.model_validate(
                audit_values,
                strict=True,
            )
        except Exception as exc:
            log.error(
                "m1.ai_turn.unfinalized_failed_closed",
                conv_id=conv_id,
                error_type=type(exc).__name__,
            )
            return _persistence_failure_result(conv_id)
        else:
            if finalized_turn.text is None:
                return {
                    "status": "waiting_for_human",
                    "conversation_id": conv_id,
                    "send_status": "skipped",
                }
            ai_response = finalized_turn.text
            audit_record = finalized_turn.audit_record

        processing_ms = int((time.monotonic() - t0) * 1000)

        # ── Step 7: Persist outbound message ──────────────────────────────────
        out_msg_id = await _persist_outbound(
            conversation_id=inbound.conversation_id,
            content=ai_response,
            language=language,
            processing_time_ms=processing_ms,
            expected_ownership_version=expected_ownership_version,
            source_message_id=inbound.message_id,
            audit_record=audit_record,
        )
        if out_msg_id is None:
            return _persistence_failure_result(conv_id)

        # ── Step 8: Dispatch MAPS tag generation ──────────────────────────────
        try:
            _dispatch_maps_fanout(
                conversation_id=conv_id,
                message_id=str(msg_uuid),
                content=content,
                language=language,
                content_type=content_type,
            )
        except Exception as exc:
            log.warning("m1.maps_dispatch.failed", conv_id=conv_id, error=str(exc))

        # ── Step 8b: Lead qualification check ─────────────────────────────────
        try:
            from app.modules.m4_conversation.engine import detect_qualification_signals
            from app.modules.m5_qualification.service import qualify_and_create_lead

            if detect_qualification_signals(content):
                async with async_session_factory() as session3:
                    lead = await qualify_and_create_lead(
                        session=session3,
                        customer_phone=customer_phone,
                        conversation_id=inbound.conversation_id,
                        message_text=content,
                        msg_count=session_state.msg_count,
                    )
                    if lead:
                        # Transition conversation to qualifying
                        from app.modules.m4_conversation.engine import can_transition
                        from sqlalchemy import update as sa_update
                        from app.models.conversation import Conversation as ConvModel

                        current_status = session_state.stage
                        if can_transition(current_status, "qualifying"):
                            await session3.execute(
                                sa_update(ConvModel)
                                .where(ConvModel.conversation_id == inbound.conversation_id)
                                .values(status="qualifying")
                            )
                            session_state.stage = "qualifying"
                    await session3.commit()
        except Exception as exc:
            log.warning("m1.qualification.failed", conv_id=conv_id, error=str(exc))

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
        send_result = await _send_safe(
            customer_phone,
            ai_response,
            idempotency_key=str(out_msg_id),
            conversation_id=inbound.conversation_id,
            expected_ownership_version=expected_ownership_version,
        )

        log.info(
            "m1.processed",
            conv_id=conv_id,
            lang=language,
            processing_ms=processing_ms,
            send_status=send_result["status"],
        )
        result = {
            "status": "processed",
            "conversation_id": conv_id,
            "outbound_message_id": str(out_msg_id),
            "language": language,
            "processing_ms": processing_ms,
            "send_status": send_result["status"],
        }
        if send_result.get("provider_message_id"):
            result["provider_message_id"] = send_result["provider_message_id"]
        return result


async def _send_safe(
    phone: str,
    text: str,
    *,
    idempotency_key: str,
    conversation_id: uuid.UUID | None = None,
    expected_ownership_version: int | None = None,
) -> dict[str, str]:
    """Send once through the adapter and report only confirmed outcomes."""
    from app.adapters import get_messaging_adapter
    from app.database import async_session_factory

    if not settings.whatsapp_send_enabled:
        log.info("m1.send_message.skipped", reason="whatsapp_send_disabled")
        return {"status": "skipped"}

    try:
        if conversation_id is None:
            adapter = get_messaging_adapter()
            provider_message_id = await adapter.send_message(
                phone,
                text,
                idempotency_key=idempotency_key,
            )
        else:
            async with async_session_factory() as session:
                if expected_ownership_version is None or not await _ai_may_reply(
                    session,
                    conversation_id,
                    lock=True,
                    expected_ownership_version=expected_ownership_version,
                ):
                    await session.rollback()
                    log.info(
                        "m1.send_message.skipped",
                        reason="ai_authority_changed",
                    )
                    return {"status": "skipped"}
                adapter = get_messaging_adapter()
                provider_message_id = await adapter.send_message(
                    phone,
                    text,
                    idempotency_key=idempotency_key,
                )
                await session.commit()
        if not isinstance(provider_message_id, str) or not provider_message_id.strip():
            log.error(
                "m1.send_message.unknown_or_failed",
                error_type="UnconfirmedProviderMessageId",
            )
            return {"status": "unknown_or_failed"}
        log.info("m1.send_message.sent")
        return {
            "status": "sent",
            "provider_message_id": provider_message_id.strip(),
        }
    except Exception as exc:
        log.error(
            "m1.send_message.unknown_or_failed",
            error_type=type(exc).__name__,
        )
        return {"status": "unknown_or_failed"}


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

_BLACKOUT_REQUIRED_FIELDS = (
    "message_id",
    "customer_phone",
    "content",
    "content_type",
    "timestamp",
    "whatsapp_message_id",
)


def _blackout_raw_ref(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _decode_blackout_payload(raw: str) -> tuple[dict | None, str | None]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid_json"
    if not isinstance(payload, dict):
        return None, "not_object"
    if any(field not in payload for field in _BLACKOUT_REQUIRED_FIELDS):
        return None, "missing_required_field"
    if any(not isinstance(payload[field], str) for field in _BLACKOUT_REQUIRED_FIELDS):
        return None, "invalid_field_type"
    if any(not payload[field] for field in _BLACKOUT_REQUIRED_FIELDS):
        return None, "empty_required_field"
    try:
        uuid.UUID(payload["message_id"])
        datetime.fromisoformat(payload["timestamp"])
    except (ValueError, TypeError):
        return None, "invalid_identifier_or_timestamp"
    if payload["content_type"] not in {"text", "voice_note", "image"}:
        return None, "invalid_content_type"
    wa_id = payload["whatsapp_message_id"]
    if not wa_id.strip() or wa_id != wa_id.strip() or len(wa_id) > 100:
        return None, "invalid_whatsapp_message_id"
    return payload, None

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
    return run_async(_drain(self))


async def _drain(task: Task) -> dict:
    del task
    from app.redis_client import (
        blackout_acknowledge,
        blackout_acquire_drain_lock,
        blackout_claim_one,
        blackout_depths,
        blackout_quarantine,
        blackout_recover_processing,
        blackout_release_drain_lock,
        new_blackout_drain_owner,
    )

    counts = {
        "recovered": 0,
        "claimed": 0,
        "published": 0,
        "acknowledged": 0,
        "quarantined": 0,
        "failed": 0,
    }
    owner = new_blackout_drain_owner()
    lock_acquired = False
    status = "completed"
    MAX_ITEMS = 50

    try:
        lock_acquired = await blackout_acquire_drain_lock(owner)
    except Exception as exc:
        log.error("blackout.drain.lock_failed", error_type=type(exc).__name__)
        return {
            "status": "redis_error",
            **counts,
            "pending_depth": -1,
            "processing_depth": -1,
            "quarantine_depth": -1,
            "lock_acquired": False,
        }

    if not lock_acquired:
        return {
            "status": "already_running",
            **counts,
            "pending_depth": -1,
            "processing_depth": -1,
            "quarantine_depth": -1,
            "lock_acquired": False,
        }

    try:
        counts["recovered"] = await blackout_recover_processing()
        for _ in range(MAX_ITEMS):
            raw = await blackout_claim_one()
            if raw is None:
                break
            counts["claimed"] += 1
            payload, reason = _decode_blackout_payload(raw)
            if reason is not None:
                raw_ref = _blackout_raw_ref(raw)
                try:
                    quarantined = await blackout_quarantine(raw)
                except Exception as exc:
                    counts["failed"] += 1
                    status = "redis_error"
                    log.error(
                        "blackout.quarantine_failed",
                        reason=reason,
                        raw_ref=raw_ref,
                        error_type=type(exc).__name__,
                    )
                    break
                if not quarantined:
                    counts["failed"] += 1
                    status = "redis_error"
                    log.error(
                        "blackout.quarantine_failed",
                        reason=reason,
                        raw_ref=raw_ref,
                        error_type="RedisWriteNotConfirmed",
                    )
                    break
                counts["quarantined"] += 1
                log.warning("blackout.payload_quarantined", reason=reason, raw_ref=raw_ref)
                continue

            try:
                celery_app.send_task(
                    "m1.process_inbound_message",
                    kwargs=payload,
                    queue="default",
                )
                counts["published"] += 1
            except Exception as exc:
                counts["failed"] += 1
                status = "publication_failed"
                log.error(
                    "blackout.drain.publish_failed",
                    wa_ref=_blackout_raw_ref(payload["whatsapp_message_id"]),
                    error_type=type(exc).__name__,
                )
                break

            try:
                acknowledged = await blackout_acknowledge(raw)
            except Exception as exc:
                acknowledged = False
                log.error(
                    "blackout.drain.ack_failed",
                    wa_ref=_blackout_raw_ref(payload["whatsapp_message_id"]),
                    error_type=type(exc).__name__,
                )
            if not acknowledged:
                counts["failed"] += 1
                status = "acknowledgement_failed"
                break
            counts["acknowledged"] += 1
    except Exception as exc:
        counts["failed"] += 1
        status = "redis_error"
        log.error("blackout.drain.redis_failed", error_type=type(exc).__name__)
    finally:
        try:
            await blackout_release_drain_lock(owner)
        except Exception as exc:
            log.error("blackout.drain.lock_release_failed", error_type=type(exc).__name__)

    try:
        depths = await blackout_depths()
    except Exception as exc:
        depths = {"pending_depth": -1, "processing_depth": -1, "quarantine_depth": -1}
        log.error("blackout.drain.depth_failed", error_type=type(exc).__name__)
    result = {"status": status, **counts, **depths, "lock_acquired": True}
    log.info("m1.drain_blackout_queue.done", **result)
    return result
