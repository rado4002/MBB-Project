from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1 import operator_conversations
from app.models.internal_note import InternalNote
from app.modules.m4_conversation.internal_notes import (
    InternalNoteConversationNotFound,
    InternalNoteIdempotencyConflict,
    InternalNoteResult,
    InternalNoteUnavailable,
)
from test_operator_escalation_api import _client, _headers


def _note(
    *,
    note_id: uuid.UUID,
    conversation_id: uuid.UUID,
    account_id: uuid.UUID,
    display_name: str,
    content: str,
) -> InternalNote:
    return InternalNote(
        note_id=note_id,
        conversation_id=conversation_id,
        author_account_id=account_id,
        author_display_name=display_name,
        content=content,
        created_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_authorized_roles_create_exact_text_without_delivery(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    client, _database, account, redis_client, csrf, _calls = await _client(
        monkeypatch, role=role
    )
    conversation_id = uuid.uuid4()
    key = uuid.uuid4()
    content = "  Ligne une — 你好\n<script>alert(1)</script>  "
    service_calls: list[dict] = []
    published = False

    async def _create(_db, **kwargs):
        service_calls.append(kwargs)
        return InternalNoteResult(
            _note(
                note_id=kwargs["note_id"],
                conversation_id=kwargs["conversation_id"],
                account_id=kwargs["actor_account_id"],
                display_name=kwargs["actor_display_name"],
                content=kwargs["content"],
            ),
            replayed=False,
        )

    def _publish(_message_id):
        nonlocal published
        published = True

    monkeypatch.setattr(operator_conversations, "create_internal_note", _create)
    monkeypatch.setattr(operator_conversations, "_publish_operator_reply", _publish)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/notes",
            json={"text": content},
            headers=_headers(csrf, key),
        )
        assert response.status_code == 201
        assert response.headers["idempotent-replayed"] == "false"
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "kind": "internal_note",
            "note_id": str(key),
            "occurred_at": response.json()["occurred_at"],
            "author": {
                "account_id": str(account.account_id),
                "display_name": account.display_name,
            },
            "text": content,
        }
        assert service_calls[0]["content"] == content
        assert service_calls[0]["actor_account_id"] == account.account_id
        assert service_calls[0]["actor_role"] == role
        assert service_calls[0]["request_id"] == "e2-api-request"
        assert published is False
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_exact_replay_is_200(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _database, account, redis_client, csrf, _calls = await _client(monkeypatch)
    key = uuid.uuid4()
    conversation_id = uuid.uuid4()

    async def _create(_db, **kwargs):
        return InternalNoteResult(
            _note(
                note_id=key,
                conversation_id=conversation_id,
                account_id=account.account_id,
                display_name=account.display_name,
                content=kwargs["content"],
            ),
            replayed=True,
        )

    monkeypatch.setattr(operator_conversations, "create_internal_note", _create)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/notes",
            json={"text": "same"},
            headers=_headers(csrf, key),
        )
        assert response.status_code == 200
        assert response.headers["idempotent-replayed"] == "true"
        assert response.json()["note_id"] == str(key)
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_notes_require_csrf_origin_and_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    async def _create(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("note service must not be called")

    monkeypatch.setattr(operator_conversations, "create_internal_note", _create)
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
                f"/api/v1/operator/conversations/{uuid.uuid4()}/notes",
                json={"text": "private"},
                headers=headers,
            )
            assert response.status_code == 403
            assert response.json()["error"]["code"] == expected_code
        finally:
            await client.aclose()
            await redis_client.aclose()
    assert called is False


@pytest.mark.asyncio
async def test_blank_oversize_and_non_uuid_are_rejected_before_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(monkeypatch)
    called = False

    async def _create(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(operator_conversations, "create_internal_note", _create)
    try:
        for text, key in (
            ("   \n", str(uuid.uuid4())),
            ("x" * 4097, str(uuid.uuid4())),
            ("ok", "not-a-uuid"),
        ):
            response = await client.post(
                f"/api/v1/operator/conversations/{uuid.uuid4()}/notes",
                json={"text": text},
                headers={**_headers(csrf), "Idempotency-Key": key},
            )
            assert response.status_code == 422
        assert called is False
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (InternalNoteConversationNotFound(), 404, "CONVERSATION_NOT_FOUND"),
        (InternalNoteIdempotencyConflict(), 409, "IDEMPOTENCY_CONFLICT"),
        (InternalNoteUnavailable(), 503, "SERVICE_UNAVAILABLE"),
    ],
)
async def test_safe_service_error_mapping(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    status_code: int,
    code: str,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(monkeypatch)

    async def _create(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(operator_conversations, "create_internal_note", _create)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/notes",
            json={"text": "private"},
            headers=_headers(csrf),
        )
        assert response.status_code == status_code
        assert response.json()["error"]["code"] == code
    finally:
        await client.aclose()
        await redis_client.aclose()
