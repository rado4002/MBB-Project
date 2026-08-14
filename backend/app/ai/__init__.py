"""MBB-owned, provider-neutral AI application boundary."""

from typing import Any

__all__ = [
    "AI_SYSTEM_POLICY_VERSION",
    "AISystemPolicy",
    "AITurn",
    "AITurnService",
    "FinalizedAITurnResult",
    "ProviderCapability",
    "ProviderContinuationState",
    "ProviderErrorCategory",
    "ProviderFinishReason",
    "ProviderIdentity",
    "ProviderMessage",
    "ProviderReasoningProfile",
    "ProviderToolCall",
    "ProviderTurnError",
    "ProviderTurnRequest",
    "ProviderTurnResult",
    "ProviderUsage",
    "get_ai_turn_service",
    "get_system_policy",
]


def __getattr__(name: str) -> Any:
    if name in {"AI_SYSTEM_POLICY_VERSION", "AISystemPolicy", "get_system_policy"}:
        from app.ai import policy

        return getattr(policy, name)
    if name in {
        "ProviderCapability",
        "ProviderContinuationState",
        "ProviderErrorCategory",
        "ProviderFinishReason",
        "ProviderIdentity",
        "ProviderMessage",
        "ProviderReasoningProfile",
        "ProviderToolCall",
        "ProviderTurnError",
        "ProviderTurnRequest",
        "ProviderTurnResult",
        "ProviderUsage",
    }:
        from app.ai import provider_contract

        return getattr(provider_contract, name)
    if name in {
        "AITurn",
        "AITurnService",
        "FinalizedAITurnResult",
        "get_ai_turn_service",
    }:
        from app.ai import turn

        return getattr(turn, name)
    raise AttributeError(f"module 'app.ai' has no attribute {name!r}")
