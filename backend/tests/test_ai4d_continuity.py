from __future__ import annotations

import uuid

import pytest

from app.ai.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    StrictCapabilityModel,
)
from app.ai.commercial_state import (
    COMMERCIAL_STATE_FINALIZER,
    CommercialConcernKind,
    CommercialState,
    DecisionConstraint,
    apply_commercial_state_update,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService

CONVERSATION_ID = uuid.UUID("4d000000-0000-4000-8000-000000000001")
ITEM_ID = uuid.UUID("4d000000-0000-4000-8000-000000000101")


class _DetailsInput(StrictCapabilityModel):
    sellable_item_id: str


class _SearchInput(StrictCapabilityModel):
    query: str
    max_budget: int | None = None
    budget_currency: str = "USD"


class _FixtureOutput(StrictCapabilityModel):
    fixture: str


class _ScriptedAdapter:
    def __init__(self, *results: ProviderTurnResult) -> None:
        self.results = list(results)
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        return self.results.pop(0)


def _tool(name: str, arguments: dict, call_id: str) -> ProviderTurnResult:
    return ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id=call_id,
                capability_name=name,
                arguments=arguments,
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )


def _final(response: str, update: dict) -> ProviderTurnResult:
    return _tool(
        COMMERCIAL_STATE_FINALIZER,
        {"response_text": response, "state_update": update},
        "final-state",
    )


def _turn(content: str, *, history=(), allowed=()) -> AITurn:
    return AITurn(
        user_content=content,
        language="french",
        expected_ownership_version=3,
        conversation_id=CONVERSATION_ID,
        source_message_id=uuid.uuid4(),
        history=history,
        allowed_capabilities=allowed,
    )


async def _allowed(_context) -> bool:
    return True


def _registry(observed: list[tuple[str, object]]) -> CapabilityRegistry:
    async def details(_context, arguments):
        observed.append(("get_product_details", arguments))
        return {"fixture": "current exact product details"}

    async def search(_context, arguments):
        observed.append(("search_products", arguments))
        return {"fixture": "current constrained search"}

    return CapabilityRegistry(
        (
            CapabilityDefinition(
                name="get_product_details",
                description="Get exact fictional product details.",
                input_model=_DetailsInput,
                output_model=_FixtureOutput,
                handler=details,
            ),
            CapabilityDefinition(
                name="search_products",
                description="Search fictional products with current constraints.",
                input_model=_SearchInput,
                output_model=_FixtureOutput,
                handler=search,
            ),
        )
    )


@pytest.mark.asyncio
async def test_goal_budget_and_need_continuity_prevents_repeated_clarification():
    saved = CommercialState(
        revision=4,
        current_goal="find an air fryer",
        expressed_needs=["serves five people"],
        decision_constraints=[DecisionConstraint(kind="budget", value="maximum $60")],
        open_questions=[],
    )

    async def load(_conversation_id):
        return saved

    adapter = _ScriptedAdapter(
        _final(
            "Oui, je garde le besoin pour cinq personnes et le plafond de 60 USD.",
            {},
        )
    )
    finalized = await AITurnService(
        adapter,
        commercial_state_loader=load,
    ).generate_finalized(_turn("Montre-moi la meilleure option."))

    prompt = adapter.calls[0].messages[0].content
    assert "find an air fryer" in prompt
    assert "serves five people" in prompt
    assert "maximum $60" in prompt
    assert "combien de personnes" not in finalized.text.casefold()
    assert finalized.commercial_state_update is not None
    assert not finalized.commercial_state_update.model_fields_set
    assert finalized.commercial_state_snapshot_revision == 4


@pytest.mark.asyncio
async def test_known_selected_product_uses_details_instead_of_broad_search():
    saved = CommercialState(
        revision=2,
        current_goal="find an air fryer",
        selected_sellable_item_ids=[ITEM_ID],
    )
    observed: list[tuple[str, object]] = []

    async def load(_conversation_id):
        return saved

    adapter = _ScriptedAdapter(
        _tool(
            "get_product_details",
            {"sellable_item_id": str(ITEM_ID)},
            "details-1",
        ),
        _final("Voici les détails actuels du modèle retenu.", {}),
    )
    finalized = await AITurnService(
        adapter,
        capability_registry=_registry(observed),
        authority_checker=_allowed,
        commercial_state_loader=load,
    ).generate_finalized(
        _turn(
            "Rappelle-moi ses détails actuels.",
            allowed=("get_product_details", "search_products"),
        )
    )

    assert finalized.text == "Voici les détails actuels du modèle retenu."
    assert [name for name, _arguments in observed] == ["get_product_details"]
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_explicit_budget_replacement_causes_constrained_research_and_latest_wins():
    saved = CommercialState(
        revision=5,
        current_goal="find an air fryer",
        decision_constraints=[DecisionConstraint(kind="budget", value="maximum $70")],
    )
    observed: list[tuple[str, object]] = []

    async def load(_conversation_id):
        return saved

    adapter = _ScriptedAdapter(
        _tool(
            "search_products",
            {
                "query": "air fryer",
                "max_budget": 50,
                "budget_currency": "USD",
            },
            "search-1",
        ),
        _final(
            "Je cherche maintenant uniquement jusqu'à 50 USD.",
            {
                "decision_constraints": [
                    {"kind": "budget", "value": "maximum $50"}
                ]
            },
        ),
    )
    finalized = await AITurnService(
        adapter,
        capability_registry=_registry(observed),
        authority_checker=_allowed,
        commercial_state_loader=load,
    ).generate_finalized(
        _turn(
            "Mon maximum était 70 USD, en fait c'est 50 USD.",
            allowed=("search_products",),
        )
    )

    assert [name for name, _arguments in observed] == ["search_products"]
    search_arguments = observed[0][1]
    assert isinstance(search_arguments, _SearchInput)
    assert search_arguments.max_budget == 50
    assert finalized.commercial_state_update is not None
    after = apply_commercial_state_update(
        saved,
        expected_revision=5,
        state_update=finalized.commercial_state_update,
    )
    assert after is not None
    assert [constraint.value for constraint in after.decision_constraints] == [
        "maximum $50"
    ]


@pytest.mark.asyncio
async def test_vague_price_objection_changes_concern_without_inventing_hard_budget():
    saved = CommercialState(
        revision=1,
        current_goal="find an air fryer",
        decision_constraints=[DecisionConstraint(kind="budget", value="maximum $70")],
    )

    async def load(_conversation_id):
        return saved

    adapter = _ScriptedAdapter(
        _final(
            "Je comprends, 55 USD te semble un peu cher.",
            {
                "current_concern": {
                    "kind": "price",
                    "detail": "$55 is a little expensive",
                }
            },
        )
    )
    finalized = await AITurnService(
        adapter,
        commercial_state_loader=load,
    ).generate_finalized(_turn("55 USD, c'est un peu cher."))
    assert finalized.commercial_state_update is not None
    after = apply_commercial_state_update(
        saved,
        expected_revision=1,
        state_update=finalized.commercial_state_update,
    )
    assert after is not None
    assert [constraint.value for constraint in after.decision_constraints] == [
        "maximum $70"
    ]
    assert after.current_concern is not None
    assert after.current_concern.kind is CommercialConcernKind.price


@pytest.mark.asyncio
async def test_current_message_and_recent_visible_history_follow_saved_state_in_prompt():
    saved = CommercialState(revision=1, current_goal="find an air fryer")

    async def load(_conversation_id):
        return saved

    adapter = _ScriptedAdapter(_final("Je passe au smart lock.", {}))
    await AITurnService(
        adapter,
        commercial_state_loader=load,
    ).generate_finalized(
        _turn(
            "En fait, je cherche un smart lock.",
            history=(
                {"direction": "inbound", "content": "Ancien besoin visible."},
            ),
        )
    )

    prompt = adapter.calls[0].messages[0].content
    assert prompt.index("find an air fryer") < prompt.index("Ancien besoin visible")
    assert prompt.index("Ancien besoin visible") < prompt.index("smart lock")
