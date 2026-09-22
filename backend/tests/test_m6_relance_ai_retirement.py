"""Regression coverage for retirement of legacy Relance AI execution."""

from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.adapters as adapters
from app.api.v1 import relances as relance_api
from app.i18n import messages as i18n
from app.modules.m6_relance import hooks
from app.modules.m6_relance import service as relance_service
from app.schemas.common import Language
from app.tasks import relance as relance_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_number", "language", "hook_type"),
    [
        (1, Language.french, "reciprocity"),
        (2, Language.lingala, "social_proof"),
        (3, Language.swahili, "scarcity"),
    ],
)
async def test_relance_hook_uses_existing_deterministic_fallback(
    monkeypatch: pytest.MonkeyPatch,
    attempt_number: int,
    language: Language,
    hook_type: str,
) -> None:
    def fail_if_resolved():
        pytest.fail("legacy Relance resolved an AI provider")

    monkeypatch.setattr(adapters, "get_ai_adapter", fail_if_resolved)

    hook_text, actual_hook_type = await hooks.generate_relance_hook(
        attempt_number=attempt_number,
        language=language,
        product_interest="ignored product",
        city="ignored city",
        customer_name="ignored name",
        previous_hooks=["ignored prior hook"],
    )

    assert hook_text == i18n.t(f"relance_fallback_{attempt_number}", language)
    assert actual_hook_type == hook_type


@pytest.mark.asyncio
async def test_relance_service_preserves_non_ai_scheduling(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Mock()
    lead = SimpleNamespace(
        lead_id=uuid.uuid4(),
        relance_count=0,
        product_interest=["cable"],
    )
    conversation = SimpleNamespace(
        language_detected=Language.french,
        last_message_time=datetime(2026, 9, 1, 10, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        relance_service,
        "_get_previous_hook_texts",
        AsyncMock(return_value=[]),
    )

    relance = await relance_service.create_and_schedule_relance(
        session,
        lead=lead,
        conversation=conversation,
    )

    assert relance is not None
    assert relance.value_hook == i18n.t("relance_fallback_1", Language.french)
    assert relance.hook_type == "reciprocity"
    assert relance.attempt_number == 1
    assert lead.relance_count == 1
    session.add.assert_called_once_with(relance)


@pytest.mark.parametrize(
    "runtime_module",
    [relance_api, relance_tasks, relance_service, hooks],
)
def test_relance_runtime_chain_contains_no_provider_execution(runtime_module) -> None:
    source = inspect.getsource(runtime_module)

    assert "get_ai_adapter" not in source
    assert "get_provider_turn_adapter" not in source
    assert ".generate(" not in source
