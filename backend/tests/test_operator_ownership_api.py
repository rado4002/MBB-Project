from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.api.v1 import operator_conversations
from app.modules.m4_conversation.ownership import (
    OwnershipConflict,
    OwnershipSnapshot,
    OwnershipTransitionResult,
    ReturnToAIDisabled,
)
from test_operator_escalation_api import _client, _headers


def _snapshot(
    conversation_id: uuid.UUID,
    *,
    owner_type: str,
    account_id: uuid.UUID | None = None,
    display_name: str | None = None,
    version: int = 2,
) -> OwnershipSnapshot:
    return OwnershipSnapshot(
        conversation_id=conversation_id,
        owner_type=owner_type,
        human_owner_account_id=account_id,
        human_owner_display_name=display_name,
        ai_execution_state="paused" if owner_type == "human" else "eligible",
        version=version,
        updated_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_authorized_human_takeover_uses_authenticated_actor(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    client, _database, account, redis_client, csrf, _calls = await _client(
        monkeypatch, role=role
    )
    conversation_id = uuid.uuid4()
    calls = []

    async def _transition(_db, **kwargs):
        calls.append(kwargs)
        return OwnershipTransitionResult(
            _snapshot(
                conversation_id,
                owner_type="human",
                account_id=account.account_id,
                display_name=account.display_name,
            ),
            replayed=False,
        )

    monkeypatch.setattr(operator_conversations, "transition_ownership", _transition)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/ownership",
            json={"target_owner_type": "human", "expected_version": 1},
            headers=_headers(csrf),
        )
        assert response.status_code == 200
        assert response.headers["idempotent-replayed"] == "false"
        assert response.json()["ownership"]["owner_type"] == "human"
        assert response.json()["ownership"]["ai_execution_state"] == "paused"
        assert response.json()["ownership"]["human_owner"] == {
            "account_id": str(account.account_id),
            "display_name": account.display_name,
        }
        assert calls[0]["actor_account_id"] == account.account_id
        assert calls[0]["actor_role"] == role
        assert calls[0]["expected_version"] == 1
        assert calls[0]["target_owner_type"] == "human"
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_ownership_conflict_returns_safe_current_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(
        monkeypatch
    )
    conversation_id = uuid.uuid4()

    async def _conflict(*_args, **_kwargs):
        raise OwnershipConflict(
            _snapshot(
                conversation_id,
                owner_type="human",
                account_id=uuid.uuid4(),
                display_name="Alice Operator",
            )
        )

    monkeypatch.setattr(operator_conversations, "transition_ownership", _conflict)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/ownership",
            json={"target_owner_type": "human", "expected_version": 1},
            headers=_headers(csrf),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "OWNERSHIP_CONFLICT"
        assert response.json()["error"]["message"] == (
            "This conversation is now controlled by Alice Operator."
        )
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_return_to_ai_disabled_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(
        monkeypatch
    )

    async def _disabled(*_args, **_kwargs):
        raise ReturnToAIDisabled

    monkeypatch.setattr(operator_conversations, "transition_ownership", _disabled)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/ownership",
            json={"target_owner_type": "ai", "expected_version": 2},
            headers=_headers(csrf),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "AI_DISABLED"
        assert "remains under human control" in response.json()["error"]["message"]
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_analyst_cannot_change_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account, redis_client, csrf, _calls = await _client(
        monkeypatch, role="analyst"
    )
    called = False

    async def _transition(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("service must not be called")

    monkeypatch.setattr(operator_conversations, "transition_ownership", _transition)
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/ownership",
            json={"target_owner_type": "human", "expected_version": 1},
            headers=_headers(csrf),
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "FORBIDDEN"
        assert called is False
    finally:
        await client.aclose()
        await redis_client.aclose()
