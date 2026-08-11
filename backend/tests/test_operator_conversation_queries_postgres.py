from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi import Response
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.browser_auth_deps import BrowserPrincipal, BrowserSessionContext
from app.api.v1.operator_conversations import (
    get_operator_conversation,
    get_operator_message_history,
    list_operator_conversations,
)
from app.config import Settings
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.operator_identity.browser_auth import BrowserAuthState

DATABASE_URL = os.environ.get("E1_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="E1_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


class CountingSession:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.statements: list[Any] = []

    async def execute(self, statement):
        self.statements.append(statement)
        return await self.session.execute(statement)

    async def scalar(self, statement):
        self.statements.append(statement)
        return await self.session.scalar(statement)

    def reset(self) -> None:
        self.statements.clear()


def _principal() -> BrowserPrincipal:
    now = datetime.now(timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="query.operator",
        display_name="Query Operator",
        email_normalized=None,
        password_hash="not-used-for-query-evidence",
        role="operator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    state = BrowserAuthState(
        redis_client=object(),
        settings=Settings(
            browser_session_hmac_secret="s" * 32,
            browser_csrf_hmac_secret="c" * 32,
        ),
    )
    return BrowserPrincipal(
        account=account,
        session=BrowserSessionContext(
            raw_token="not-used",
            record=object(),
            state=state,
        ),
        capabilities=frozenset({"conversation.read", "message.read"}),
    )


async def _seed_disposable_rows(session: AsyncSession) -> list[uuid.UUID]:
    base_time = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    conversation_ids = [uuid.uuid4() for _ in range(250)]
    phones = [f"+2438{index:08d}" for index in range(250)]
    await session.execute(
        insert(Customer),
        [
            {
                "phone_number": phone,
                "name": f"Disposable Customer {index}",
                "city": "Kinshasa",
                "preferred_language": ("french", "lingala", "swahili")[index % 3],
            }
            for index, phone in enumerate(phones)
        ],
    )
    await session.execute(
        insert(Conversation),
        [
            {
                "conversation_id": conversation_id,
                "customer_id": phones[index],
                "last_message_time": base_time - timedelta(minutes=index // 2),
                "status": (
                    "active",
                    "qualifying",
                    "nurturing",
                    "escalated",
                )[index % 4],
                "language_detected": ("french", "lingala", "swahili")[index % 3],
                "message_count": 3,
                "context": {"must_not_be_selected": FULL_CONTEXT_SENTINEL},
            }
            for index, conversation_id in enumerate(conversation_ids)
        ],
    )
    messages: list[dict[str, Any]] = []
    for index, conversation_id in enumerate(conversation_ids):
        latest = base_time - timedelta(minutes=index // 2)
        for offset in range(3):
            messages.append(
                {
                    "message_id": uuid.uuid4(),
                    "conversation_id": conversation_id,
                    "timestamp": latest - timedelta(seconds=2 - offset),
                    "direction": "inbound" if offset == 2 else "outbound",
                    "content": f"Disposable message {index}-{offset}",
                    "content_type": "text",
                    "language": ("french", "lingala", "swahili")[index % 3],
                }
            )
    await session.execute(insert(Message), messages)
    await session.execute(
        insert(Lead),
        [
            {
                "lead_id": uuid.uuid4(),
                "customer_id": phones[index],
                "conversation_id": conversation_ids[index],
                "score": "hot",
                "score_value": 8,
                "stage": "consideration",
                "intent": "product_inquiry",
                "product_interest": ["display-safe-a", "display-safe-b"],
                "source": "disposable-e1",
            }
            for index in range(0, 250, 5)
        ],
    )
    await session.execute(
        insert(EscalationTicket),
        [
            {
                "ticket_id": uuid.uuid4(),
                "conversation_id": conversation_ids[index],
                "customer_id": phones[index],
                "priority": "medium",
                "reason": "complex_complaint",
                "status": "open",
                "transcript_snapshot": [],
            }
            for index in range(0, 250, 7)
        ],
    )
    await session.flush()
    return conversation_ids


FULL_CONTEXT_SENTINEL = "raw-context-must-not-load"


async def _explain(
    session: AsyncSession,
    statement,
    dialect,
) -> dict[str, Any]:
    compiled = statement.compile(
        dialect=dialect,
        compile_kwargs={"literal_binds": True},
    )
    result = await session.scalar(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {compiled}")
    )
    return result[0]["Plan"]


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    node_types: list[str] = []

    def visit(node: dict[str, Any]) -> None:
        node_types.append(node["Node Type"])
        for child in node.get("Plans", []):
            visit(child)

    visit(plan)
    return {
        "root": plan["Node Type"],
        "actual_rows": plan.get("Actual Rows"),
        "total_cost": plan.get("Total Cost"),
        "node_types": sorted(set(node_types)),
    }


@pytest.mark.asyncio
async def test_realistic_query_counts_and_postgresql_plans() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    principal = _principal()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                conversation_ids = await _seed_disposable_rows(session)
                await session.execute(
                    update(Conversation)
                    .where(Conversation.conversation_id == conversation_ids[0])
                    .values(ai_execution_state="paused", ownership_version=2)
                )
                counted = CountingSession(session)

                queue = await list_operator_conversations(
                    response=Response(),
                    principal=principal,
                    db=counted,
                    limit=25,
                    cursor=None,
                    conversation_status=None,
                    escalation_state=None,
                    language=None,
                )
                assert len(queue.items) == 25
                assert queue.next_cursor is not None
                assert len(counted.statements) == 1
                queue_statement = counted.statements[0]

                counted.reset()
                detail = await get_operator_conversation(
                    conversation_id=conversation_ids[0],
                    response=Response(),
                    principal=principal,
                    db=counted,
                )
                assert detail.conversation_id == conversation_ids[0]
                assert detail.ownership.owner_type == "ai"
                assert detail.ownership.human_owner is None
                assert detail.ownership.ai_execution_state == "paused"
                assert detail.ownership.version == 2
                assert detail.open_escalation.exists is True
                assert len(counted.statements) == 1
                detail_statement = counted.statements[0]

                counted.reset()
                history = await get_operator_message_history(
                    conversation_id=conversation_ids[0],
                    response=Response(),
                    principal=principal,
                    _message_principal=principal,
                    db=counted,
                    limit=30,
                    before=None,
                )
                assert len(history.items) == 3
                assert len(counted.statements) == 2
                access_statement, history_statement = counted.statements

                plans = {
                    "queue": _plan_summary(
                        await _explain(session, queue_statement, engine.dialect)
                    ),
                    "detail": _plan_summary(
                        await _explain(session, detail_statement, engine.dialect)
                    ),
                    "message_access": _plan_summary(
                        await _explain(session, access_statement, engine.dialect)
                    ),
                    "message_history": _plan_summary(
                        await _explain(session, history_statement, engine.dialect)
                    ),
                }
                assert all(summary["actual_rows"] is not None for summary in plans.values())
                print(
                    "E1_QUERY_EVIDENCE="
                    + json.dumps(
                        {
                            "disposable_conversations": 250,
                            "disposable_messages": 750,
                            "data_query_counts": {
                                "queue": 1,
                                "detail": 1,
                                "message_history": 2,
                            },
                            "plans": plans,
                        },
                        sort_keys=True,
                    )
                )
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
