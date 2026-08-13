"""Provider-neutral MBB AI turn contract and service."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.adapters.base import ProviderTurnAdapter
from app.ai.policy import get_system_policy
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderTurnError,
    ProviderTurnRequest,
)

_MAX_RESPONSE_TOKENS = 512
_HISTORY_LIMIT = 6


@dataclass(frozen=True)
class AITurn:
    """The runtime context MBB currently needs for one assistant reply."""

    user_content: str
    language: str
    expected_ownership_version: int
    history: Sequence[Mapping[str, str]] = ()
    turn_id: uuid.UUID = field(default_factory=uuid.uuid4, init=False)

    def __post_init__(self) -> None:
        if self.expected_ownership_version <= 0:
            raise ValueError("expected ownership version must be positive")


class AITurnService:
    """Apply MBB policy and adapt an AI turn to the configured provider boundary."""

    def __init__(self, adapter: ProviderTurnAdapter) -> None:
        self._adapter = adapter

    async def generate(self, turn: AITurn) -> str:
        policy = get_system_policy(turn.language)
        result = await self._adapter.generate_turn(
            ProviderTurnRequest(
                messages=(
                    ProviderMessage(role="user", content=_build_runtime_prompt(turn)),
                ),
                system_instruction=policy.text,
                max_output_tokens=_MAX_RESPONSE_TOKENS,
                reasoning_profile=ProviderReasoningProfile.default,
            )
        )
        if result.tool_calls or result.text is None:
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        return result.text


def get_ai_turn_service() -> AITurnService:
    """Build the service using the repository's existing adapter factory."""
    from app.adapters import get_provider_turn_adapter

    return AITurnService(get_provider_turn_adapter())


def _build_runtime_prompt(turn: AITurn) -> str:
    """Keep customer-controlled content in runtime data, outside system policy."""
    if not turn.history:
        return turn.user_content

    language_label = {
        "lingala": "Lingala",
        "french": "Français",
        "swahili": "Kiswahili",
    }.get(turn.language, "Français")
    history_lines = []
    for message in turn.history[-_HISTORY_LIMIT:]:
        role = "Client" if message.get("direction") == "inbound" else "Moi (bot)"
        history_lines.append(f"{role}: {message.get('content', '')}")

    return (
        f"Historique récent ({language_label}):\n"
        f"{'\n'.join(history_lines)}\n\n"
        f"Message actuel du client:\n{turn.user_content}"
    )
