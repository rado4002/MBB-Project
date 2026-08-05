from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models.conversation import Conversation
from app.models.message import Message
from app.modules.m4_conversation.operator_replies import (
    ReplyIdempotencyConflict,
    ReplyNotEligible,
    ReplyOwnershipConflict,
    ReplyOwnershipVersionConflict,
    accept_operator_reply,
    mark_operator_reply_accepted,
)
from app.tasks import operator_replies as delivery


def _conversation(
    *, owner_id: uuid.UUID, version: int = 2, status: str = "active"
) -> Conversation:
    now = datetime.now(timezone.utc)
    return Conversation(
        conversation_id=uuid.uuid4(),
        customer_id="+243812345678",
        start_time=now,
        last_message_time=now,
        status=status,
        language_detected="french",
        context={},
        message_count=1,
        owner_type="human",
        human_owner_account_id=owner_id,
        ai_execution_state="paused",
        ownership_version=version,
        ownership_updated_at=now,
        created_at=now,
        updated_at=now,
    )


class _AcceptanceSession:
    def __init__(self, conversation: Conversation) -> None:
        self.conversation = conversation
        self.messages: dict[uuid.UUID, Message] = {}
        self.pending: Message | None = None
        self.commit_count = 0

    async def get(self, _model, message_id):
        return self.messages.get(message_id)

    async def scalar(self, _statement):
        return self.conversation

    def add(self, message):
        self.pending = message

    async def flush(self):
        return None

    async def execute(self, _statement):
        return SimpleNamespace(rowcount=1)

    async def commit(self):
        self.commit_count += 1
        if self.pending is not None:
            self.messages[self.pending.message_id] = self.pending
            self.pending = None

    async def rollback(self):
        self.pending = None

    async def refresh(self, message):
        if message.delivery_state is None:
            message.delivery_state = "accepted"
            message.delivery_state_timestamp = datetime.now(timezone.utc)


async def _accept(
    session: _AcceptanceSession,
    *, actor_id: uuid.UUID,
    key: uuid.UUID,
    text: str = "Bonjour",
    conversation_id: uuid.UUID | None = None,
    version: int = 2,
):
    return await accept_operator_reply(
        session,
        conversation_id=conversation_id or session.conversation.conversation_id,
        text=text,
        expected_ownership_version=version,
        actor_account_id=actor_id,
        actor_display_name="Omar Operator",
        actor_role="operator",
        message_id=key,
        messaging_adapter="whatsapp",
        whatsapp_mode="baileys",
    )


@pytest.mark.asyncio
async def test_one_logical_submission_persists_authorship_and_exact_retry_replays() -> None:
    actor_id = uuid.uuid4()
    session = _AcceptanceSession(_conversation(owner_id=actor_id))
    key = uuid.uuid4()

    first = await _accept(session, actor_id=actor_id, key=key)
    retry = await _accept(session, actor_id=actor_id, key=key)

    assert first.replayed is False
    assert retry.replayed is True
    assert retry.message is first.message
    assert len(session.messages) == 1
    assert first.message.message_id == key
    assert first.message.operator_author_account_id == actor_id
    assert first.message.author_display_name == "Omar Operator"
    assert first.message.accepted_ownership_version == 2
    assert first.message.delivery_state is None

    accepted = await mark_operator_reply_accepted(session, key)
    assert accepted.delivery_state == "accepted"
    assert accepted.delivery_state_timestamp is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["text", "conversation", "actor", "version"])
async def test_same_key_with_changed_logical_request_conflicts(changed: str) -> None:
    actor_id = uuid.uuid4()
    session = _AcceptanceSession(_conversation(owner_id=actor_id))
    key = uuid.uuid4()
    await _accept(session, actor_id=actor_id, key=key)
    kwargs = {
        "actor_id": actor_id,
        "key": key,
        "text": "Bonjour",
        "conversation_id": session.conversation.conversation_id,
        "version": 2,
    }
    if changed == "text":
        kwargs["text"] = "Different"
    elif changed == "conversation":
        kwargs["conversation_id"] = uuid.uuid4()
    elif changed == "actor":
        kwargs["actor_id"] = uuid.uuid4()
    else:
        kwargs["version"] = 3
    with pytest.raises(ReplyIdempotencyConflict):
        await _accept(session, **kwargs)
    assert len(session.messages) == 1


@pytest.mark.asyncio
async def test_only_current_paused_owner_with_current_version_and_eligible_status_can_reply() -> None:
    actor_id = uuid.uuid4()
    key = uuid.uuid4()

    other_owner = _AcceptanceSession(_conversation(owner_id=uuid.uuid4()))
    with pytest.raises(ReplyOwnershipConflict):
        await _accept(other_owner, actor_id=actor_id, key=key)

    ai_owned_conversation = _conversation(owner_id=actor_id)
    ai_owned_conversation.owner_type = "ai"
    ai_owned_conversation.human_owner_account_id = None
    ai_owned_conversation.ai_execution_state = "eligible"
    with pytest.raises(ReplyOwnershipConflict):
        await _accept(_AcceptanceSession(ai_owned_conversation), actor_id=actor_id, key=key)

    stale = _AcceptanceSession(_conversation(owner_id=actor_id, version=3))
    with pytest.raises(ReplyOwnershipVersionConflict):
        await _accept(stale, actor_id=actor_id, key=key, version=2)

    dormant = _AcceptanceSession(_conversation(owner_id=actor_id, status="dormant"))
    with pytest.raises(ReplyNotEligible):
        await _accept(dormant, actor_id=actor_id, key=key)


class _TaskResult:
    def __init__(self, row) -> None:
        self.row = row

    def one_or_none(self):
        return self.row


class _TaskSession:
    def __init__(self, message: Message, conversation: Conversation) -> None:
        self.message = message
        self.conversation = conversation
        self.commit_count = 0

    async def execute(self, _statement):
        return _TaskResult((self.message, self.conversation))

    async def commit(self):
        self.commit_count += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def _accepted(actor_id: uuid.UUID, conversation: Conversation) -> Message:
    now = datetime.now(timezone.utc)
    return Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="outbound",
        content="Human reply",
        content_type="text",
        language="french",
        operator_author_account_id=actor_id,
        author_display_name="Omar Operator",
        accepted_ownership_version=conversation.ownership_version,
        delivery_state="accepted",
        delivery_state_timestamp=now,
        created_at=now,
    )


@pytest.mark.asyncio
async def test_final_ownership_recheck_blocks_stale_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid.uuid4()
    conversation = _conversation(owner_id=actor_id)
    message = _accepted(actor_id, conversation)
    conversation.ownership_version += 1
    session = _TaskSession(message, conversation)
    monkeypatch.setattr("app.database.async_session_factory", lambda: session)
    adapter_called = False

    def _adapter():
        nonlocal adapter_called
        adapter_called = True
        raise AssertionError("adapter must not be called")

    monkeypatch.setattr("app.adapters.get_messaging_adapter", _adapter)
    result = await delivery._deliver_operator_reply(message.message_id)
    assert result["status"] == "failed"
    assert message.delivery_state == "failed"
    assert adapter_called is False


@pytest.mark.asyncio
async def test_delivery_uses_message_uuid_and_records_only_confirmed_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid.uuid4()
    conversation = _conversation(owner_id=actor_id)
    message = _accepted(actor_id, conversation)
    session = _TaskSession(message, conversation)
    monkeypatch.setattr("app.database.async_session_factory", lambda: session)
    calls = []

    class _Adapter:
        async def send_message(self, phone, text, *, idempotency_key):
            calls.append((phone, text, idempotency_key))
            return "provider-confirmed"

    monkeypatch.setattr("app.adapters.get_messaging_adapter", lambda: _Adapter())
    result = await delivery._deliver_operator_reply(message.message_id)
    assert result["status"] == "sent"
    assert calls == [(conversation.customer_id, message.content, str(message.message_id))]
    assert message.delivery_state == "sent"
    assert message.whatsapp_message_id == "provider-confirmed"

    replay = await delivery._deliver_operator_reply(message.message_id)
    assert replay["status"] == "sent"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_external_exception_records_uncertain_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor_id = uuid.uuid4()
    conversation = _conversation(owner_id=actor_id)
    message = _accepted(actor_id, conversation)
    session = _TaskSession(message, conversation)
    monkeypatch.setattr("app.database.async_session_factory", lambda: session)
    calls = 0

    class _Adapter:
        async def send_message(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            raise TimeoutError("outcome unknown")

    monkeypatch.setattr("app.adapters.get_messaging_adapter", lambda: _Adapter())
    first = await delivery._deliver_operator_reply(message.message_id)
    second = await delivery._deliver_operator_reply(message.message_id)
    assert first["status"] == "uncertain"
    assert second["status"] == "uncertain"
    assert message.delivery_state == "uncertain"
    assert calls == 1
