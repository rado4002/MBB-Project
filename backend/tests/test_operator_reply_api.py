from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1 import operator_conversations
from app.models.message import Message
from app.modules.m4_conversation.operator_replies import (
    OperatorReplyResult,
    ReplyIdempotencyConflict,
    ReplyNotEligible,
    ReplyOwnershipConflict,
    ReplyOwnershipVersionConflict,
)
from test_operator_escalation_api import _client, _headers


def _accepted_message(
    *, conversation_id: uuid.UUID, account_id: uuid.UUID, display_name: str,
    message_id: uuid.UUID, text: str, version: int,
) -> Message:
    now = datetime.now(timezone.utc)
    return Message(
        message_id=message_id,
        conversation_id=conversation_id,
        timestamp=now,
        direction="outbound",
        content=text,
        content_type="text",
        language="french",
        operator_author_account_id=account_id,
        author_display_name=display_name,
        accepted_ownership_version=version,
        delivery_state=None,
        delivery_state_timestamp=None,
        created_at=now,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_reply_uses_authenticated_actor_and_publishes_persisted_uuid(
    monkeypatch: pytest.MonkeyPatch, role: str,
) -> None:
    client, _database, account, redis_client, csrf, _calls = await _client(
        monkeypatch, role=role
    )
    conversation_id = uuid.uuid4()
    key = uuid.uuid4()
    service_calls: list[dict] = []
    published: list[uuid.UUID] = []

    async def _accept(_db, **kwargs):
        service_calls.append(kwargs)
        return OperatorReplyResult(
            _accepted_message(
                conversation_id=conversation_id,
                account_id=account.account_id,
                display_name=account.display_name,
                message_id=key,
                text=kwargs["text"],
                version=kwargs["expected_ownership_version"],
            ),
            replayed=False,
        )

    async def _mark(_db, _message_id):
        message = service_calls and _accepted_message(
            conversation_id=conversation_id,
            account_id=account.account_id,
            display_name=account.display_name,
            message_id=key,
            text=service_calls[0]["text"],
            version=service_calls[0]["expected_ownership_version"],
        )
        assert message
        message.delivery_state = "accepted"
        message.delivery_state_timestamp = datetime.now(timezone.utc)
        return message

    monkeypatch.setattr(operator_conversations, "accept_operator_reply", _accept)
    monkeypatch.setattr(operator_conversations, "mark_operator_reply_accepted", _mark)
    monkeypatch.setattr(operator_conversations, "_publish_operator_reply", published.append)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/replies",
            json={"text": "Bonjour Marie", "expected_ownership_version": 2},
            headers=_headers(csrf, key),
        )
        assert response.status_code == 202
        assert response.headers["idempotent-replayed"] == "false"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["message_id"] == str(key)
        assert response.json()["sender_type"] == "operator"
        assert response.json()["operator_author"] == {
            "account_id": str(account.account_id),
            "display_name": account.display_name,
        }
        assert response.json()["delivery_state"] == "accepted"
        assert published == [key]
        assert service_calls[0]["actor_account_id"] == account.account_id
        assert service_calls[0]["actor_role"] == role
        assert service_calls[0]["message_id"] == key
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_reply_requires_csrf_origin_and_reply_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _accept(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("reply service must not be called")

    monkeypatch.setattr(operator_conversations, "accept_operator_reply", _accept)
    for role, header_change, expected_code in (
        ("operator", {"X-CSRF-Token": "wrong"}, "csrf_invalid"),
        ("operator", {"Origin": "https://evil.example"}, "origin_invalid"),
        ("analyst", {}, "FORBIDDEN"),
    ):
        client, _database, _account, redis_client, csrf, _calls = await _client(
            monkeypatch, role=role
        )
        headers = _headers(csrf)
        headers.update(header_change)
        try:
            response = await client.post(
                f"/api/v1/operator/conversations/{uuid.uuid4()}/replies",
                json={"text": "Bonjour", "expected_ownership_version": 2},
                headers=headers,
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == expected_code
        finally:
            await client.aclose()
            await redis_client.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_reply_rejects_blank_oversize_and_non_uuid_key_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(monkeypatch)
    called = False

    async def _accept(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(operator_conversations, "accept_operator_reply", _accept)
    try:
        for text, key in (("   \n", str(uuid.uuid4())), ("x" * 4097, str(uuid.uuid4())), ("ok", "not-a-uuid")):
            response = await client.post(
                f"/api/v1/operator/conversations/{uuid.uuid4()}/replies",
                json={"text": text, "expected_ownership_version": 2},
                headers={**_headers(csrf), "Idempotency-Key": key},
            )
            assert response.status_code == 422
        assert called is False
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ReplyOwnershipConflict(), "OWNERSHIP_CONFLICT"),
        (ReplyOwnershipVersionConflict(), "OWNERSHIP_VERSION_CONFLICT"),
        (ReplyNotEligible(), "CONVERSATION_NOT_REPLY_ELIGIBLE"),
        (ReplyIdempotencyConflict(), "IDEMPOTENCY_CONFLICT"),
    ],
)
async def test_reply_conflicts_are_safe_and_do_not_publish(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(monkeypatch)
    published = False

    async def _accept(*_args, **_kwargs):
        raise error

    def _publish(_message_id):
        nonlocal published
        published = True

    monkeypatch.setattr(operator_conversations, "accept_operator_reply", _accept)
    monkeypatch.setattr(operator_conversations, "_publish_operator_reply", _publish)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/replies",
            json={"text": "Bonjour", "expected_ownership_version": 2},
            headers=_headers(csrf),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert published is False
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_publication_failure_is_retryable_with_same_message_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, account, redis_client, csrf, _calls = await _client(monkeypatch)
    conversation_id = uuid.uuid4()
    key = uuid.uuid4()
    message = _accepted_message(
        conversation_id=conversation_id,
        account_id=account.account_id,
        display_name=account.display_name,
        message_id=key,
        text="Retry unchanged",
        version=2,
    )
    calls = 0

    async def _accept(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return OperatorReplyResult(message, replayed=calls > 1)

    async def _mark(*_args, **_kwargs):
        message.delivery_state = "accepted"
        message.delivery_state_timestamp = datetime.now(timezone.utc)
        return message

    publish_calls = 0

    def _publish(_message_id):
        nonlocal publish_calls
        publish_calls += 1
        if publish_calls == 1:
            raise ConnectionError("broker unavailable")

    monkeypatch.setattr(operator_conversations, "accept_operator_reply", _accept)
    monkeypatch.setattr(operator_conversations, "mark_operator_reply_accepted", _mark)
    monkeypatch.setattr(operator_conversations, "_publish_operator_reply", _publish)
    try:
        first = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/replies",
            json={"text": "Retry unchanged", "expected_ownership_version": 2},
            headers=_headers(csrf, key),
        )
        second = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/replies",
            json={"text": "Retry unchanged", "expected_ownership_version": 2},
            headers=_headers(csrf, key),
        )
        assert first.status_code == 503
        assert first.json()["error"]["code"] == "REPLY_PUBLICATION_FAILED"
        assert second.status_code == 200
        assert second.json()["message_id"] == str(key)
        assert second.headers["idempotent-replayed"] == "true"
        assert calls == 2
        assert publish_calls == 2
    finally:
        await client.aclose()
        await redis_client.aclose()
