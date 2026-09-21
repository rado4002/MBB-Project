"""Best-effort, content-free operational observations; never business authority.

The existing PrintLogger can block on stdout. A bounded stdlib QueueListener
keeps that I/O off the caller. No flush/join, delivery guarantee, or exporter.
Only this stream uses the explicit processors below (no ambient log context).
"""

from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from logging.handlers import QueueListener

import structlog

from app.config import get_settings

EVENTS = frozenset(
    {
        "acceptance",
        "publication",
        "worker",
        "turn",
        "provider_call",
        "provider_attempt",
        "capability",
        "persistence",
        "grounding",
        "fallback",
        "send",
    }
)
OUTCOMES = frozenset(
    {
        "accepted",
        "duplicate",
        "rate_limited",
        "unconfirmed",
        "not_applicable",
        "started",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
        "response_generated",
        "handoff_requested",
        "order_draft_presented",
        "committed",
        "rolled_back",
        "retry",
        "denied",
        "selected",
        "passed",
        "rejected",
        "stale",
        "confirmed",
        "skipped",
        "uncertain",
    }
)
REASONS = frozenset(
    {
        "unknown",
        "configuration",
        "authentication",
        "permission",
        "rate_limit",
        "timeout",
        "unavailable",
        "invalid_request",
        "malformed_response",
        "provider_failure",
        "budget_exceeded",
        "commercial_grounding_failed",
        "stale_ai_authority",
        "ai_authority_changed",
        "newer_customer_evidence",
        "commercial_state_changed",
        "whatsapp_send_disabled",
        "unverified_persisted_ack",
        "unconfirmed_provider_id",
        "persistence_failed",
        "transaction_retry",
        "capability_failed",
        "unknown_tool",
        "tool_not_allowed",
        "invalid_arguments",
    }
)
CAPABILITIES = frozenset(
    {
        "get_product_details",
        "search_products",
        "prepare_order_draft",
        "request_human_handoff",
    }
)
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "reasoning_tokens",
)
IDS = ("task_id", "turn_id", "source_message_id", "outbound_message_id")
WORKER_STATUSES = frozenset(
    {
        "processed",
        "duplicate_ignored",
        "persistence_failed",
        "waiting_for_human",
        "human_controlled",
        "opt_out",
        "escalated_voice_note",
        "stale_order_draft_authority",
        "awaiting_order_draft_confirmation",
        *(
            "order_draft_" + state
            for state in (
                "confirmed",
                "already_confirmed",
                "cancelled",
                "already_cancelled",
                "invalidated",
                "refreshed",
            )
        ),
    }
)
COUNTS = {
    "provider_call": "provider_calls",
    "provider_attempt": "provider_attempts",
    "capability": "logical_capabilities",
    "persistence": "persistence_attempts",
}
TURN_COUNTS = (*COUNTS.values(), "tool_rounds")
_context = ContextVar("ai_ops_context", default=None)
_counts = ContextVar("ai_ops_counts", default=None)
_buffer = None
_listener = None
_pid = None
_init_lock = threading.Lock()
_BUFFER_SIZE = 256


def _after_fork():
    global _buffer, _listener, _pid, _init_lock
    _buffer = _listener = _pid = None
    _init_lock = threading.Lock()
    _context.set(None)
    _counts.set(None)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class _Output:
    """Give the telemetry PrintLogger its own lock, sharing only stdout I/O."""

    def __init__(self):
        self.write = sys.stdout.write
        self.flush = sys.stdout.flush


class _Sink:
    def __init__(self):
        self.logger = structlog.wrap_logger(
            structlog.PrintLogger(_Output()),
            processors=[structlog.processors.JSONRenderer()],
            wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        )

    def handle(self, record):
        try:
            self.logger.info("ai_ops.v1", **record)
        except BaseException:
            pass  # No recursive logging, even when the sink is broken.


def _enqueue(record):
    global _buffer, _listener, _pid
    pid = os.getpid()
    if _pid != pid or _buffer is None:
        if not _init_lock.acquire(blocking=False):
            return
        try:
            if _pid != pid or _buffer is None:
                buffer = queue.Queue(maxsize=_BUFFER_SIZE)
                listener = QueueListener(buffer, _Sink())
                listener.start()
                _buffer, _listener, _pid = buffer, listener, pid
        finally:
            _init_lock.release()
    _buffer.put_nowait(record)


def _project(fields):
    """Never stringify arbitrary objects or pass through arbitrary keys."""
    result = {}
    for key in IDS:
        value = fields.get(key)
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif type(value) is str:
            try:
                result[key] = str(uuid.UUID(value))
            except ValueError:
                pass
    for key, allowed in (
        ("route", {"broker", "blackout", "blackout_replay"}),
        ("worker_status", WORKER_STATUSES),
        ("worker_send_result", {"confirmed", "skipped", "uncertain"}),
        (
            "draft_state",
            {
                "confirmed",
                "already_confirmed",
                "cancelled",
                "already_cancelled",
                "invalidated",
                "refreshed",
            },
        ),
        ("provider", {"deepseek", "claude", "disabled"}),
        ("capability", CAPABILITIES),
        ("transaction_outcome", {"committed", "rolled_back"}),
        (
            "turn_outcome",
            {
                "response_generated",
                "fallback_used",
                "handoff_requested",
                "order_draft_presented",
            },
        ),
        (
            "finish_reason",
            {"completed", "tool_call", "max_output", "stopped", "error", "unknown"},
        ),
        ("boundary", {"ordinary", "terminal", "handoff", "draft_reply"}),
    ):
        if key in fields:
            value = fields[key]
            result[key] = (
                value if type(value) is str and value in allowed else "unknown"
            )
    # Arbitrary configured model names may contain secrets. Defer model export.
    for key in (
        *USAGE_FIELDS,
        *TURN_COUNTS,
        "provider_call_index",
        "capability_index",
        "attempt_index",
        "returned_tool_calls",
        "task_retries",
    ):
        value = fields.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[key] = value
    if type(fields.get("redelivered")) is bool:
        result["redelivered"] = fields["redelivered"]
    return result


def emit(event, outcome, *, reason=None, duration_ms=None, **fields):
    """Fail open on configuration, projection, clock, queue or logging failure."""
    try:
        if (
            not get_settings().ai_ops_enabled
            or event not in EVENTS
            or outcome not in OUTCOMES
        ):
            return
        record = _project(fields)
        record.update(
            schema="ai_ops.v1",
            observation=event,
            outcome=outcome,
            observed_at=datetime.now(timezone.utc).isoformat(),
            duration_ms=(
                duration_ms
                if type(duration_ms) in (int, float) and 0 <= duration_ms < float("inf")
                else None
            ),
            reason=reason if type(reason) is str and reason in REASONS else None,
        )
        if event in {"provider_call", "provider_attempt"}:
            for key in USAGE_FIELDS:
                record.setdefault(key, None)
        if event == "persistence":
            record.setdefault("transaction_outcome", None)
        if event == "send":
            record["send_result"] = (
                None
                if outcome == "started"
                else outcome
                if outcome in {"confirmed", "skipped"}
                else "uncertain"
            )
        _enqueue(record)
    except BaseException:
        pass


def _safe_emit(*args, **kwargs):
    # Also isolates a replaced/injected emitter, not just the default sink.
    try:
        emit(*args, **kwargs)
    except BaseException:
        pass


def _failure(error):
    if isinstance(error, asyncio.CancelledError):
        return "cancelled", None
    if not isinstance(error, Exception):
        return "interrupted", None
    code = getattr(error, "safe_code", None)
    audit = getattr(error, "audit_record", None)
    if audit is not None:
        code = audit.safe_code
    names = {
        "StaleAITurnAuthority": "stale_ai_authority",
        "CommercialGroundingError": "commercial_grounding_failed",
        "AITurnPersistenceError": "persistence_failed",
        "AITurnBudgetExceeded": "budget_exceeded",
        "AITurnDeadlineExceeded": "timeout",
        "TimeoutException": "timeout",
        "ReadTimeout": "timeout",
        "APITimeoutError": "timeout",
        "APIConnectionError": "unavailable",
    }
    code = names.get(type(error).__name__, code)
    if type(error).__name__ == "APIStatusError":
        status = getattr(error, "status_code", None)
        code = {
            400: "invalid_request",
            401: "authentication",
            403: "permission",
            408: "timeout",
            429: "rate_limit",
        }.get(status, "unknown")
        if type(status) is int and 500 <= status <= 599:
            code = "unavailable"
    if type(error).__name__ == "Retry":
        return "retry", None
    if code == "stale_ai_authority":
        return "stale", code
    if code == "commercial_grounding_failed":
        return "rejected", code
    return "failed", code if type(code) is str and code in REASONS else "unknown"


class Observation:
    def __init__(self, event, fields):
        self.event, self.fields = event, fields
        self.outcome, self.reason = "succeeded", None
        self.started = None
        self.tokens = []

    def __enter__(self):
        try:
            if not self.fields.pop("active", True) or not get_settings().ai_ops_enabled:
                return self
            self.started = time.monotonic()
            context = dict(_context.get() or {})
            context.update(_project(self.fields))
            if self.event == "turn":
                self.tokens.append(
                    (_counts, _counts.set(dict.fromkeys(TURN_COUNTS, 0)))
                )
            counts = _counts.get()
            if counts is not None and self.event in COUNTS:
                name = COUNTS[self.event]
                counts[name] += 1
                if self.event in {"provider_call", "capability"}:
                    key = (
                        "provider_call_index"
                        if self.event == "provider_call"
                        else "capability_index"
                    )
                    context[key] = counts[name]
            self.fields = context
            self.tokens.append((_context, _context.set(context)))
            _safe_emit(self.event, "started", **self.fields)
        except BaseException:
            pass
        return self

    def set(self, outcome=None, reason=None, **fields):
        try:
            if outcome in OUTCOMES:
                self.outcome = outcome
            if reason in REASONS:
                self.reason = reason
            self.fields.update(_project(fields))
        except BaseException:
            pass

    def published(self, task):
        try:
            self.set("confirmed")
            self.set(task_id=task.id)
        except BaseException:
            pass

    def worker_result(self, result):
        try:
            self.set(
                worker_status=result.get("status"),
                outbound_message_id=result.get("outbound_message_id"),
            )
            if "send_status" in result and result.get("status") != "persistence_failed":
                self.set(
                    worker_send_result={
                        "sent": "confirmed",
                        "skipped": "skipped",
                        "unknown_or_failed": "uncertain",
                    }.get(result["send_status"], "unknown")
                )
        except BaseException:
            pass

    def usage(self, response):
        try:
            usage = getattr(response, "usage", None)
            self.set(**{key: getattr(usage, key, None) for key in USAGE_FIELDS})
            finish_reason = getattr(response, "finish_reason", None)
            if finish_reason is not None:
                self.set(finish_reason=finish_reason.value)
            calls = getattr(response, "tool_calls", None)
            if type(calls) is tuple:
                self.set(returned_tool_calls=len(calls))
        except BaseException:
            pass

    def __exit__(self, error_type, error, traceback):
        try:
            if self.started is not None:
                if error is not None:
                    self.outcome, self.reason = _failure(error)
                    if self.event == "publication" and self.outcome == "failed":
                        self.outcome = "unconfirmed"
                if self.event == "turn":
                    self.fields.update(_counts.get() or {})
                _safe_emit(
                    self.event,
                    self.outcome,
                    reason=self.reason,
                    duration_ms=(time.monotonic() - self.started) * 1000,
                    **self.fields,
                )
        except BaseException:
            pass
        finally:
            for variable, token in reversed(self.tokens):
                try:
                    variable.reset(token)
                except BaseException:
                    pass
        return False  # Preserve the exact business exception, including cancellation.


def observe(event, **fields):
    return Observation(event, fields)


def tool_round():
    try:
        counts = _counts.get()
        if counts is not None:
            counts["tool_rounds"] += 1
    except BaseException:
        pass


def note(event, outcome, *, reason=None, **fields):
    try:
        context = dict(_context.get() or {})
        context.update(fields)
        _safe_emit(event, outcome, reason=reason, **context)
    except BaseException:
        pass


def worker(function):
    """Observe the complete M1 invocation without changing its return/exception."""

    @wraps(function)
    async def wrapped(*args, **kwargs):
        fields = {}
        try:
            fields["source_message_id"] = kwargs.get("message_id")
            request = kwargs["task"].request
            fields.update(task_id=request.id, task_retries=request.retries)
            fields["redelivered"] = (request.delivery_info or {}).get("redelivered")
        except BaseException:
            pass
        with observe("worker", **fields) as observation:
            result = await function(*args, **kwargs)
            observation.worker_result(result)
            return result

    return wrapped
