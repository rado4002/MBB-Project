import asyncio
import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.adapters.messaging import baileys_adapter as module


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class _Client:
    def __init__(self, capture, response=None, error=None, **_kwargs):
        self.capture = capture
        self.response = response
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, json):
        self.capture.append((url, json))
        if self.error:
            raise self.error
        return _Response(self.response)


def _run(coro):
    return asyncio.run(coro)


def _configure(monkeypatch, *, enabled=True, attempts=1):
    settings = SimpleNamespace(
        baileys_url="http://baileys.invalid:3000",
        whatsapp_send_enabled=enabled,
        baileys_send_max_attempts=attempts,
    )
    monkeypatch.setattr(module, "settings", settings)
    return settings


def _install_client(monkeypatch, capture, *, response=None, error=None):
    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: _Client(
            capture,
            response=response,
            error=error,
            **kwargs,
        ),
    )


def test_recovery_attempt_default_is_one_and_minimum_is_enforced():
    assert Settings().baileys_send_max_attempts == 1
    with pytest.raises(ValidationError):
        Settings(baileys_send_max_attempts=0)


def test_same_persisted_uuid_is_sent_once_and_valid_response_is_returned(monkeypatch):
    _configure(monkeypatch)
    capture = []
    outbound_id = str(uuid.uuid4())
    _install_client(
        monkeypatch,
        capture,
        response={
            "success": True,
            "status": "sent",
            "provider_message_id": "provider-123",
        },
    )

    result = _run(
        module.BaileysAdapter().send_message(
            "+243812345678",
            "message text",
            idempotency_key=outbound_id,
        )
    )

    assert result == "provider-123"
    assert len(capture) == 1
    assert capture[0][1]["idempotency_key"] == outbound_id


def test_configured_single_attempt_does_not_retry_an_ambiguous_transport_error(monkeypatch):
    _configure(monkeypatch, attempts=1)
    capture = []
    _install_client(
        monkeypatch,
        capture,
        error=httpx.ReadTimeout("ambiguous"),
    )

    with pytest.raises(module.MessagingAdapterError):
        _run(
            module.BaileysAdapter().send_message(
                "+243812345678",
                "message text",
                idempotency_key=str(uuid.uuid4()),
            )
        )
    assert len(capture) == 1


def test_multiple_configured_attempts_reuse_the_same_idempotency_key(monkeypatch):
    _configure(monkeypatch, attempts=2)
    capture = []
    outbound_id = str(uuid.uuid4())
    responses = iter(
        (
            httpx.ReadTimeout("ambiguous"),
            {
                "success": True,
                "status": "sent",
                "duplicate": True,
                "provider_message_id": "provider-replayed",
            },
        )
    )

    class _SequencedClient(_Client):
        async def post(self, url, *, json):
            self.capture.append((url, json))
            outcome = next(responses)
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: _SequencedClient(capture, **kwargs),
    )

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", no_delay)

    result = _run(
        module.BaileysAdapter().send_message(
            "+243812345678",
            "message text",
            idempotency_key=outbound_id,
        )
    )

    assert result == "provider-replayed"
    assert len(capture) == 2
    assert [payload["idempotency_key"] for _, payload in capture] == [
        outbound_id,
        outbound_id,
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"success": False, "skipped": True},
        {"success": False, "status": "sent", "provider_message_id": "provider-1"},
        {"success": True, "status": "in_progress", "provider_message_id": "provider-1"},
        {"success": True, "status": "unknown", "provider_message_id": "provider-1"},
        {"success": True, "status": "sent"},
        {"success": True, "status": "sent", "provider_message_id": ""},
        [],
        "truncated",
        json.JSONDecodeError("bad", "", 0),
    ],
)
def test_malformed_or_inconclusive_responses_are_never_sent(monkeypatch, body):
    _configure(monkeypatch)
    capture = []
    _install_client(monkeypatch, capture, response=body)

    with pytest.raises(module.MessagingAdapterError):
        _run(
            module.BaileysAdapter().send_message(
                "+243812345678",
                "message text",
                idempotency_key=str(uuid.uuid4()),
            )
        )
    assert len(capture) == 1


def test_disabled_adapter_is_skipped_without_http_or_key_generation(monkeypatch):
    _configure(monkeypatch, enabled=False)
    capture = []
    _install_client(monkeypatch, capture, response=AssertionError("HTTP called"))

    assert _run(
        module.BaileysAdapter().send_message("+243812345678", "message text")
    ) == ""
    assert capture == []


def test_adapter_logs_do_not_contain_phone_text_key_or_provider_object(monkeypatch):
    _configure(monkeypatch)
    capture = []
    records = []
    outbound_id = str(uuid.uuid4())
    sensitive_phone = "+243899999999"
    sensitive_text = "private-message-value"
    _install_client(
        monkeypatch,
        capture,
        response={
            "success": True,
            "status": "sent",
            "provider_message_id": "provider-sensitive",
            "complete": {"private": sensitive_text},
        },
    )
    monkeypatch.setattr(
        module,
        "log",
        SimpleNamespace(
            info=lambda *args, **kwargs: records.append(("info", args, kwargs)),
            warning=lambda *args, **kwargs: records.append(("warning", args, kwargs)),
            error=lambda *args, **kwargs: records.append(("error", args, kwargs)),
        ),
    )

    _run(
        module.BaileysAdapter().send_message(
            sensitive_phone,
            sensitive_text,
            idempotency_key=outbound_id,
        )
    )

    serialized = repr(records)
    for sensitive in (
        sensitive_phone,
        sensitive_text,
        outbound_id,
        "provider-sensitive",
    ):
        assert sensitive not in serialized
