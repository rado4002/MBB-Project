from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from test_operator_conversation_reads import _authenticated_client


def _message_row(item_id: uuid.UUID, occurred_at: datetime) -> dict:
    return {
        "kind": "message",
        "item_id": item_id,
        "occurred_at": occurred_at,
        "direction": "inbound",
        "content_type": "text",
        "content": "Bonjour",
        "language": "french",
        "author_account_id": None,
        "author_display_name": None,
        "delivery_state": None,
        "delivery_state_timestamp": None,
    }


def _note_row(
    item_id: uuid.UUID,
    occurred_at: datetime,
    account_id: uuid.UUID,
) -> dict:
    return {
        "kind": "internal_note",
        "item_id": item_id,
        "occurred_at": occurred_at,
        "direction": None,
        "content_type": None,
        "content": "Private — 你好\n<script>alert(1)</script>",
        "language": None,
        "author_account_id": account_id,
        "author_display_name": "Operator One",
        "delivery_state": None,
        "delivery_state_timestamp": None,
    }


@pytest.mark.asyncio
async def test_timeline_combines_discriminated_items_with_stable_cursor() -> None:
    client, database, account, redis_client = await _authenticated_client()
    conversation_id = uuid.uuid4()
    tied = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    newest = uuid.uuid4()
    boundary = uuid.uuid4()
    older = uuid.uuid4()
    database.message_rows = [
        _message_row(newest, tied),
        _note_row(boundary, tied, account.account_id),
        _message_row(older, tied),
    ]
    try:
        response = await client.get(
            f"/api/v1/operator/conversations/{conversation_id}/timeline?limit=2"
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        body = response.json()
        assert [item["kind"] for item in body["items"]] == [
            "internal_note",
            "message",
        ]
        assert body["items"][0]["note_id"] == str(boundary)
        assert body["items"][0]["author"] == {
            "account_id": str(account.account_id),
            "display_name": "Operator One",
        }
        assert body["items"][0]["text"].endswith("<script>alert(1)</script>")
        assert "delivery_state" not in body["items"][0]
        assert body["items"][1]["message_id"] == str(newest)
        assert body["next_older_cursor"]

        statement = database.statements[-1]
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "UNION ALL" in sql
        assert "mbb.internal_notes" in sql
        assert "mbb.messages" in sql
        assert "ORDER BY operator_timeline.occurred_at DESC" in sql
        assert "operator_timeline.kind DESC" in sql
        assert "operator_timeline.item_id DESC" in sql

        database.message_rows = [_message_row(older, tied)]
        older_response = await client.get(
            f"/api/v1/operator/conversations/{conversation_id}/timeline",
            params={"before": body["next_older_cursor"]},
        )
        assert older_response.status_code == 200
        assert older_response.json()["items"][0]["message_id"] == str(older)

        wrong_conversation = await client.get(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/timeline",
            params={"before": body["next_older_cursor"]},
        )
        assert wrong_conversation.status_code == 400
        assert wrong_conversation.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_timeline_attributes_only_audited_outbound_to_mbb_ai() -> None:
    client, database, _account, redis_client = await _authenticated_client()
    conversation_id = uuid.uuid4()
    occurred_at = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    ai_row = _message_row(uuid.uuid4(), occurred_at)
    ai_row.update(
        direction="outbound",
        content="Je transmets ta demande.",
        ai_actor_display_name="MBB AI Assistant",
    )
    legacy_row = _message_row(uuid.uuid4(), occurred_at)
    legacy_row.update(direction="outbound", content="Legacy outbound")
    database.message_rows = [legacy_row, ai_row]
    try:
        response = await client.get(
            f"/api/v1/operator/conversations/{conversation_id}/timeline"
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["sender_type"] for item in items] == ["ai", "unknown"]
        assert items[0]["sender_display_name"] == "MBB AI Assistant"
        assert items[1]["sender_display_name"] is None
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_timeline_is_404_safe_and_analyst_cannot_read_notes() -> None:
    client, database, _account, redis_client = await _authenticated_client()
    database.conversation_accessible = False
    try:
        response = await client.get(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/timeline"
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
        assert database.execute_count == 0
    finally:
        await client.aclose()
        await redis_client.aclose()

    analyst, analyst_db, _account, analyst_redis = await _authenticated_client(
        role="analyst"
    )
    try:
        denied = await analyst.get(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/timeline"
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "FORBIDDEN"
        assert analyst_db.execute_count == 0
    finally:
        await analyst.aclose()
        await analyst_redis.aclose()
