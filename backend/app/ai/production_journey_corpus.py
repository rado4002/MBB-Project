"""Current evaluation contract for the single production journey harness."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

from app.ai.evaluation_corpus import get_mbb_evaluation_corpus

PRODUCTION_JOURNEY_CORPUS_VERSION = "mbb-production-journey-v1"


class JourneyScenario(str, Enum):
    direct_response = "direct_response"
    product_search = "product_search"
    product_details_continuation = "product_details_continuation"
    capability_failure = "capability_failure"
    product_offer_truth = "product_offer_truth"
    order_draft_preparation = "order_draft_preparation"
    exact_confirm_cancel = "exact_confirm_cancel"
    human_handoff = "human_handoff"
    stale_authority_rejection = "stale_authority_rejection"
    fallback = "fallback"
    timeout_recovery = "timeout_recovery"


class ProductionJourneyContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: str = PRODUCTION_JOURNEY_CORPUS_VERSION
    scenarios: tuple[JourneyScenario, ...] = tuple(JourneyScenario)
    evaluated_languages: tuple[str, ...] = ("french", "lingala", "swahili")
    source_corpus_version: str = "mbb-ai-eval-v1"
    production_capabilities: tuple[str, ...] = (
        "search_products",
        "get_product_details",
        "prepare_order_draft",
        "request_human_handoff",
    )
    scenario_case_ids: dict[JourneyScenario, tuple[str, ...]] = {
        JourneyScenario.direct_response: ("product.discovery.normal",),
        JourneyScenario.product_search: ("product.discovery.normal",),
        JourneyScenario.product_details_continuation: (
            "product.truth.available",
            "product.truth.current_price",
        ),
        JourneyScenario.capability_failure: ("evidence.capability_error",),
        JourneyScenario.product_offer_truth: (
            "product.truth.available",
            "product.truth.out_of_stock",
        ),
        JourneyScenario.order_draft_preparation: ("journey.order_draft",),
        JourneyScenario.exact_confirm_cancel: ("journey.confirm_cancel",),
        JourneyScenario.human_handoff: ("handoff.explicit_human",),
        JourneyScenario.stale_authority_rejection: ("journey.stale_authority",),
        JourneyScenario.fallback: ("evidence.no_matching_product",),
        JourneyScenario.timeout_recovery: ("journey.timeout_recovery",),
    }


PRODUCTION_JOURNEY_CONTRACT = ProductionJourneyContract()


def get_production_journey_contract() -> ProductionJourneyContract:
    """Return the current contract while retaining the scoring corpus source."""
    # Import-time validation keeps the contract tied to the existing scorer corpus.
    get_mbb_evaluation_corpus()
    return PRODUCTION_JOURNEY_CONTRACT
