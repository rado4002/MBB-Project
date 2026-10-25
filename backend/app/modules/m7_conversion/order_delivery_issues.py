"""Orders-owned reporting and Administrator triage of delivery problems."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.order_delivery_confirmation import OrderDeliveryConfirmation
from app.models.order_delivery_issue import OrderDeliveryIssue
from app.models.stock_movement import StockMovement
from app.modules.m4_conversation.order_delivery_evidence import (
    CustomerReceiptEvidenceInvalid,
    CustomerReceiptEvidenceMissing,
    read_order_delivery_issue_evidence,
)
from app.modules.m7_conversion.order_fulfillment import (
    UnsupportedOrderPreparation,
    _single_line,
)
from app.operator_identity.audit import append_operator_audit_event

_REPORT_AUDIT_ACTION = "order.delivery_issue.reported"
_CLOSE_AUDIT_ACTION = "order.delivery_issue.closed"
_FAILED_REASONS = frozenset(
    {"recipient_unavailable", "recipient_refused", "address_problem", "courier_problem", "other"}
)
_DISPUTED_REASONS = frozenset(
    {"receipt_disputed", "condition_disputed", "item_disputed", "other"}
)
_SOURCES = frozenset(
    {"customer_message", "customer_call", "courier_report", "staff_observation", "other"}
)
_RESOLUTIONS = frozenset({"false_alarm", "delivery_continuing"})


class OrderDeliveryIssueError(Exception):
    """Base error for stable delivery issue failures."""


class OrderDeliveryIssueForbidden(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueNotFound(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueStale(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueStateConflict(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueAlreadyOpen(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueDispatchRequired(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueEvidenceMissing(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueEvidenceInvalid(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueIdempotencyConflict(OrderDeliveryIssueError):
    pass


class OrderDeliveryIssueUnavailable(OrderDeliveryIssueError):
    pass


@dataclass(frozen=True)
class OrderDeliveryIssueResult:
    issue: OrderDeliveryIssue
    replayed: bool


def _digest(purpose: bytes, value: bytes) -> str:
    return hashlib.sha256(purpose + value).hexdigest()


def _canonical_digest(purpose: bytes, payload: dict) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return _digest(purpose, encoded)


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


def _normalize_text(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not 10 <= len(normalized) <= 500:
        raise ValueError(f"{label} must contain between 10 and 500 characters")
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise ValueError(f"{label} contains unsupported control characters")
    return normalized


def _report_evidence(
    *,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    expected_order_updated_at: datetime,
    issue_type: str,
    reason_code: str,
    source: str,
    summary: str,
    evidence_message_id: uuid.UUID | None,
    actor_account_id: uuid.UUID,
) -> tuple[str, str]:
    key_digest = _digest(
        b"order-delivery-issue-report-key:v1:", str(idempotency_key).encode("ascii")
    )
    fingerprint = _canonical_digest(
        b"order-delivery-issue-report-request:v1:",
        {
            "actor_account_id": str(actor_account_id),
            "evidence_message_id": str(evidence_message_id) if evidence_message_id else None,
            "expected_order_updated_at": expected_order_updated_at.isoformat(),
            "issue_type": issue_type,
            "order_id": str(order_id),
            "reason_code": reason_code,
            "source": source,
            "summary": summary,
        },
    )
    return key_digest, fingerprint


def _close_evidence(
    *,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    issue_id: uuid.UUID,
    resolution_code: str,
    resolution_reason: str,
    actor_account_id: uuid.UUID,
) -> tuple[str, str]:
    key_digest = _digest(
        b"order-delivery-issue-close-key:v1:", str(idempotency_key).encode("ascii")
    )
    fingerprint = _canonical_digest(
        b"order-delivery-issue-close-request:v1:",
        {
            "actor_account_id": str(actor_account_id),
            "issue_id": str(issue_id),
            "order_id": str(order_id),
            "resolution_code": resolution_code,
            "resolution_reason": resolution_reason,
        },
    )
    return key_digest, fingerprint


async def list_order_delivery_issues(
    session: AsyncSession, *, order_id: uuid.UUID, limit: int = 20
) -> tuple[OrderDeliveryIssue, ...]:
    if not 1 <= limit <= 20:
        raise ValueError("delivery issue history limit is invalid")
    return tuple(
        (
            await session.scalars(
                select(OrderDeliveryIssue)
                .where(OrderDeliveryIssue.order_id == order_id)
                .order_by(
                    OrderDeliveryIssue.reported_at.desc(),
                    OrderDeliveryIssue.issue_id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )


async def has_open_order_delivery_issue(
    session: AsyncSession, *, order_id: uuid.UUID
) -> bool:
    issue_id = await session.scalar(
        select(OrderDeliveryIssue.issue_id).where(
            OrderDeliveryIssue.order_id == order_id,
            OrderDeliveryIssue.status == "open",
        )
    )
    return issue_id is not None


async def report_order_delivery_issue(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_order_updated_at: datetime,
    issue_type: str,
    reason_code: str,
    source: str,
    summary: str,
    evidence_message_id: uuid.UUID | None,
    idempotency_key: uuid.UUID,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OrderDeliveryIssueResult:
    if actor_role not in {"administrator", "operator"}:
        raise OrderDeliveryIssueForbidden("delivery issue reporting is not permitted")
    if expected_order_updated_at.tzinfo is None:
        raise ValueError("expected_order_updated_at must include a timezone")
    if not isinstance(idempotency_key, uuid.UUID):
        raise ValueError("idempotency_key must be a UUID")
    if not actor_display_name or len(actor_display_name) > 100:
        raise ValueError("actor_display_name is invalid")
    if issue_type not in {"failed", "disputed"}:
        raise ValueError("unsupported delivery issue type")
    allowed_reasons = _FAILED_REASONS if issue_type == "failed" else _DISPUTED_REASONS
    if reason_code not in allowed_reasons:
        raise ValueError("reason code does not match delivery issue type")
    if source not in _SOURCES:
        raise ValueError("unsupported delivery issue source")
    normalized_summary = _normalize_text(summary, label="summary")
    key_digest, request_fingerprint = _report_evidence(
        idempotency_key=idempotency_key,
        order_id=order_id,
        expected_order_updated_at=expected_order_updated_at,
        issue_type=issue_type,
        reason_code=reason_code,
        source=source,
        summary=normalized_summary,
        evidence_message_id=evidence_message_id,
        actor_account_id=actor_account_id,
    )
    try:
        await session.execute(select(func.pg_advisory_xact_lock(_advisory_key(key_digest))))
        prior = await session.scalar(
            select(OrderDeliveryIssue).where(
                OrderDeliveryIssue.report_operation_key_digest == key_digest
            )
        )
        if prior is not None:
            if prior.order_id != order_id or not hmac.compare_digest(
                prior.report_request_fingerprint, request_fingerprint
            ):
                raise OrderDeliveryIssueIdempotencyConflict(
                    "idempotency key was already used for another delivery issue report"
                )
            await session.commit()
            return OrderDeliveryIssueResult(issue=prior, replayed=True)

        order = await session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderDeliveryIssueNotFound("Order not found")
        if order.updated_at != expected_order_updated_at:
            raise OrderDeliveryIssueStale("Order changed since it was reviewed")
        if order.status not in {"delivering", "delivered"}:
            raise OrderDeliveryIssueStateConflict(
                "only dispatched Orders can have a delivery issue"
            )
        if issue_type == "failed" and order.status != "delivering":
            raise OrderDeliveryIssueStateConflict(
                "a delivered Order can only receive a dispute"
            )
        try:
            sellable_item_id, quantity = _single_line(order)
        except UnsupportedOrderPreparation as exc:
            raise OrderDeliveryIssueDispatchRequired(str(exc)) from exc
        movement = await session.scalar(
            select(StockMovement).where(
                StockMovement.order_id == order_id,
                StockMovement.operation_kind == "dispatch",
            )
        )
        if (
            movement is None
            or movement.sellable_item_id != sellable_item_id
            or movement.dispatched_quantity != quantity
        ):
            raise OrderDeliveryIssueDispatchRequired(
                "matching immutable dispatch evidence is required"
            )
        active = await session.scalar(
            select(OrderDeliveryIssue.issue_id).where(
                OrderDeliveryIssue.order_id == order_id,
                OrderDeliveryIssue.status == "open",
            )
        )
        if active is not None:
            raise OrderDeliveryIssueAlreadyOpen(
                "an unresolved delivery issue already exists for this Order"
            )

        evidence = None
        if evidence_message_id is not None:
            try:
                evidence = await read_order_delivery_issue_evidence(
                    session,
                    order_id=order_id,
                    customer_id=order.customer_id,
                    evidence_message_id=evidence_message_id,
                    dispatched_at=movement.occurred_at,
                )
            except CustomerReceiptEvidenceMissing as exc:
                raise OrderDeliveryIssueEvidenceMissing(str(exc)) from exc
            except CustomerReceiptEvidenceInvalid as exc:
                raise OrderDeliveryIssueEvidenceInvalid(str(exc)) from exc
        confirmation = await session.scalar(
            select(OrderDeliveryConfirmation).where(
                OrderDeliveryConfirmation.order_id == order_id
            )
        )
        reported_at = await session.scalar(select(func.clock_timestamp()))
        if reported_at is None:
            raise OrderDeliveryIssueUnavailable("delivery issue timestamp is unavailable")
        issue = OrderDeliveryIssue(
            order_id=order_id,
            dispatch_movement_id=movement.movement_id,
            delivery_confirmation_id=(
                confirmation.confirmation_id if confirmation is not None else None
            ),
            issue_type=issue_type,
            reason_code=reason_code,
            source=source,
            summary=normalized_summary,
            evidence_message_id=(evidence.message.message_id if evidence else None),
            evidence_conversation_id=(evidence.conversation_id if evidence else None),
            evidence_received_at=(evidence.message.created_at if evidence else None),
            reported_by_account_id=actor_account_id,
            reporter_display_name=actor_display_name,
            reported_at=reported_at,
            report_operation_key_digest=key_digest,
            report_request_fingerprint=request_fingerprint,
            status="open",
        )
        session.add(issue)
        await session.flush()
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action=_REPORT_AUDIT_ACTION,
            target_type="order_delivery_issue",
            target_id=str(issue.issue_id),
            reason_code=reason_code,
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "order_id": str(order_id),
                "dispatch_movement_id": str(movement.movement_id),
                "delivery_confirmation_id": (
                    str(confirmation.confirmation_id) if confirmation else None
                ),
                "issue_type": issue_type,
                "source": source,
                "evidence_message_id": (
                    str(evidence.message.message_id) if evidence else None
                ),
                "request_fingerprint": request_fingerprint,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=reported_at,
        )
        await session.commit()
        await session.refresh(issue)
        return OrderDeliveryIssueResult(issue=issue, replayed=False)
    except OrderDeliveryIssueError:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderDeliveryIssueUnavailable(
            "delivery issue persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderDeliveryIssueUnavailable(
            "delivery issue could not be committed"
        ) from exc


async def close_order_delivery_issue(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    issue_id: uuid.UUID,
    resolution_code: str,
    resolution_reason: str,
    idempotency_key: uuid.UUID,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OrderDeliveryIssueResult:
    if actor_role != "administrator":
        raise OrderDeliveryIssueForbidden("delivery issue closure is not permitted")
    if not actor_display_name or len(actor_display_name) > 100:
        raise ValueError("actor_display_name is invalid")
    if resolution_code not in _RESOLUTIONS:
        raise ValueError("unsupported delivery issue resolution")
    normalized_reason = _normalize_text(resolution_reason, label="resolution_reason")
    key_digest, request_fingerprint = _close_evidence(
        idempotency_key=idempotency_key,
        order_id=order_id,
        issue_id=issue_id,
        resolution_code=resolution_code,
        resolution_reason=normalized_reason,
        actor_account_id=actor_account_id,
    )
    try:
        await session.execute(select(func.pg_advisory_xact_lock(_advisory_key(key_digest))))
        prior = await session.scalar(
            select(OrderDeliveryIssue).where(
                OrderDeliveryIssue.close_operation_key_digest == key_digest
            )
        )
        if prior is not None:
            if (
                prior.order_id != order_id
                or prior.issue_id != issue_id
                or not hmac.compare_digest(
                    prior.close_request_fingerprint or "", request_fingerprint
                )
            ):
                raise OrderDeliveryIssueIdempotencyConflict(
                    "idempotency key was already used for another issue closure"
                )
            await session.commit()
            return OrderDeliveryIssueResult(issue=prior, replayed=True)

        order = await session.scalar(
            select(Order).where(Order.order_id == order_id).with_for_update()
        )
        if order is None:
            raise OrderDeliveryIssueNotFound("Order not found")
        issue = await session.scalar(
            select(OrderDeliveryIssue)
            .where(
                OrderDeliveryIssue.issue_id == issue_id,
                OrderDeliveryIssue.order_id == order_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if issue is None:
            raise OrderDeliveryIssueNotFound("delivery issue not found")
        if issue.status != "open":
            raise OrderDeliveryIssueStateConflict("delivery issue is already closed")
        if resolution_code == "delivery_continuing" and order.status != "delivering":
            raise OrderDeliveryIssueStateConflict(
                "delivery can continue only while the Order is delivering"
            )
        closed_at = await session.scalar(
            select(
                func.greatest(
                    func.clock_timestamp(),
                    issue.reported_at + timedelta(microseconds=1),
                )
            )
        )
        if closed_at is None:
            raise OrderDeliveryIssueUnavailable("delivery issue closure timestamp is unavailable")
        issue.status = "closed"
        issue.resolution_code = resolution_code
        issue.resolution_reason = normalized_reason
        issue.closed_by_account_id = actor_account_id
        issue.closed_by_display_name = actor_display_name
        issue.closed_at = closed_at
        issue.close_operation_key_digest = key_digest
        issue.close_request_fingerprint = request_fingerprint
        await session.flush()
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action=_CLOSE_AUDIT_ACTION,
            target_type="order_delivery_issue",
            target_id=str(issue.issue_id),
            reason_code=resolution_code,
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "order_id": str(order_id),
                "issue_type": issue.issue_type,
                "previous_status": "open",
                "new_status": "closed",
                "request_fingerprint": request_fingerprint,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=closed_at,
        )
        await session.commit()
        await session.refresh(issue)
        return OrderDeliveryIssueResult(issue=issue, replayed=False)
    except OrderDeliveryIssueError:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderDeliveryIssueUnavailable(
            "delivery issue persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderDeliveryIssueUnavailable(
            "delivery issue closure could not be committed"
        ) from exc
