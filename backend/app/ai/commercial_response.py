"""Bounded commercial response plans validated and rendered by MBB."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.ai.capabilities import (
    AIProductDetail,
    AIProductSearchItem,
    GetProductDetailsOutput,
    SearchProductsInput,
    SearchProductsOutput,
)
from app.ai.provider_contract import ProviderToolCall, ProviderToolResult
from app.i18n.messages import t

_PRODUCT_CAPABILITIES = frozenset({"get_product_details", "search_products"})
_MAX_PRODUCT_REFS = 5
_MAX_RENDERED_DETAIL_CHARS = 500


class ResponseKind(str, Enum):
    clarify = "CLARIFY"
    product_list = "PRODUCT_LIST"
    recommendation = "RECOMMENDATION"
    product_details = "PRODUCT_DETAILS"
    comparison = "COMPARISON"
    no_match = "NO_MATCH"
    unsupported_commercial_request = "UNSUPPORTED_COMMERCIAL_REQUEST"


class FactField(str, Enum):
    name = "NAME"
    model = "MODEL"
    current_price = "CURRENT_PRICE"
    current_availability = "CURRENT_AVAILABILITY"
    current_sellability = "CURRENT_SELLABILITY"
    approved_product_detail = "APPROVED_PRODUCT_DETAIL"


class RecommendationReason(str, Enum):
    none = "NONE"
    budget_fit = "BUDGET_FIT"
    available_now = "AVAILABLE_NOW"


class NextAction(str, Enum):
    search_more = "SEARCH_MORE"
    show_details = "SHOW_DETAILS"
    compare_products = "COMPARE_PRODUCTS"
    request_human_handoff = "REQUEST_HUMAN_HANDOFF"
    none = "NONE"


class Clarification(str, Enum):
    ask_product_type = "ASK_PRODUCT_TYPE"
    ask_budget = "ASK_BUDGET"
    ask_model = "ASK_MODEL"
    none = "NONE"


class Tone(str, Enum):
    warm = "WARM"
    concise = "CONCISE"


class CommercialResponsePlan(BaseModel):
    """Provider-selected references and presentation intent, never business values."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    response_kind: ResponseKind
    product_refs: tuple[uuid.UUID, ...] = Field(
        default=(),
        max_length=_MAX_PRODUCT_REFS,
    )
    fact_fields: tuple[FactField, ...] = Field(default=(), max_length=len(FactField))
    recommendation_ref: uuid.UUID | None = None
    recommendation_reason: RecommendationReason = RecommendationReason.none
    next_action: NextAction = NextAction.none
    clarification: Clarification = Clarification.none
    tone: Tone = Tone.concise

    @model_validator(mode="after")
    def validate_shape(self) -> CommercialResponsePlan:
        if len(self.product_refs) != len(set(self.product_refs)):
            raise ValueError("duplicate product references")
        if len(self.fact_fields) != len(set(self.fact_fields)):
            raise ValueError("duplicate fact fields")

        product_count = len(self.product_refs)
        product_kind = self.response_kind in {
            ResponseKind.product_list,
            ResponseKind.recommendation,
            ResponseKind.product_details,
            ResponseKind.comparison,
        }
        if product_kind and (product_count == 0 or FactField.name not in self.fact_fields):
            raise ValueError("product responses require references and NAME")
        if not product_kind and (self.product_refs or self.fact_fields):
            raise ValueError("non-product responses cannot include product facts")
        if self.response_kind == ResponseKind.product_details and product_count != 1:
            raise ValueError("product details require one product")
        if (
            FactField.approved_product_detail in self.fact_fields
            and self.response_kind != ResponseKind.product_details
        ):
            raise ValueError("approved details require PRODUCT_DETAILS")
        if self.response_kind == ResponseKind.comparison and not 2 <= product_count <= 3:
            raise ValueError("comparison requires two or three products")

        if self.response_kind == ResponseKind.recommendation:
            if (
                self.recommendation_ref is None
                or self.recommendation_ref not in self.product_refs
                or self.recommendation_reason == RecommendationReason.none
            ):
                raise ValueError("recommendation requires a referenced target and reason")
        elif (
            self.recommendation_ref is not None
            or self.recommendation_reason != RecommendationReason.none
        ):
            raise ValueError("recommendation fields require RECOMMENDATION")

        if self.response_kind == ResponseKind.clarify:
            if self.clarification == Clarification.none:
                raise ValueError("clarification response requires a question")
            if self.next_action != NextAction.none:
                raise ValueError("clarification cannot include a separate next action")
        elif self.clarification != Clarification.none:
            raise ValueError("clarification is only valid for CLARIFY")
        return self


class CommercialResponseError(ValueError):
    """Fail-closed plan error carrying only a bounded audit-safe code."""

    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(safe_code)


@dataclass(frozen=True)
class _ProductEvidence:
    item: AIProductSearchItem
    has_details: bool
    source_call_ids: frozenset[str]


@dataclass(frozen=True)
class _SearchBudget:
    currency: str
    amount: Decimal
    product_refs: frozenset[uuid.UUID]


@dataclass(frozen=True)
class _Authority:
    products: dict[uuid.UUID, _ProductEvidence]
    search_budgets: tuple[_SearchBudget, ...]
    successful_search: bool


def commercial_response_mode(allowed_capabilities: Iterable[str]) -> bool:
    """Activate only for the existing product/commercial AI path."""
    return bool(_PRODUCT_CAPABILITIES.intersection(allowed_capabilities))


def commercial_plan_instruction() -> str:
    """Compact provider instruction for the final non-tool response."""
    return (
        "For the final non-tool commercial response, output exactly one JSON object "
        "and no prose or Markdown. Keys: response_kind, product_refs, fact_fields, "
        "recommendation_ref, recommendation_reason, next_action, clarification, tone. "
        "Use only these values: response_kind=CLARIFY|PRODUCT_LIST|RECOMMENDATION|"
        "PRODUCT_DETAILS|COMPARISON|NO_MATCH|UNSUPPORTED_COMMERCIAL_REQUEST; "
        "fact_fields=NAME|MODEL|CURRENT_PRICE|CURRENT_AVAILABILITY|"
        "CURRENT_SELLABILITY|APPROVED_PRODUCT_DETAIL; recommendation_reason="
        "NONE|BUDGET_FIT|AVAILABLE_NOW; next_action=SEARCH_MORE|SHOW_DETAILS|"
        "COMPARE_PRODUCTS|REQUEST_HUMAN_HANDOFF|NONE; clarification="
        "ASK_PRODUCT_TYPE|ASK_BUDGET|ASK_MODEL|NONE; tone=WARM|CONCISE. "
        "Use sellable_item_id references only from successful MBB tool output. "
        "Never include copied prices, stock values, free-text explanations, or actions."
    )


def parse_commercial_response_plan(raw_content: str) -> CommercialResponsePlan:
    """Parse the entire provider content as one strict JSON plan."""
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise CommercialResponseError("commercial_plan_invalid")
    try:
        return CommercialResponsePlan.model_validate_json(raw_content, strict=True)
    except (ValidationError, ValueError):
        raise CommercialResponseError("commercial_plan_invalid") from None


def validate_and_render_commercial_response(
    raw_content: str,
    *,
    language: str,
    exposed_capabilities: Iterable[str],
    tool_calls: Sequence[ProviderToolCall],
    tool_results: Sequence[ProviderToolResult],
) -> str:
    """Validate one complete plan against MBB evidence and render safe text."""
    plan = parse_commercial_response_plan(raw_content)
    authority = _build_authority(tool_calls, tool_results)
    products = _validate_plan(plan, authority, frozenset(exposed_capabilities))
    return _render(plan, products, language)


def commercial_response_fallback(language: str) -> str:
    """Reuse the established localized technical fallback."""
    return t("error_fallback", language)


def _build_authority(
    tool_calls: Sequence[ProviderToolCall],
    tool_results: Sequence[ProviderToolResult],
) -> _Authority:
    calls = {call.call_id: call for call in tool_calls}
    products: dict[uuid.UUID, _ProductEvidence] = {}
    budgets: list[_SearchBudget] = []
    successful_search = False

    for result in tool_results:
        if result.status != "success":
            continue
        call = calls.get(result.call_id)
        if call is None or call.capability_name != result.capability_name:
            raise CommercialResponseError("commercial_plan_invalid")
        assert result.output is not None
        if result.capability_name == "search_products":
            successful_search = True
            try:
                output = SearchProductsOutput.model_validate_json(
                    json.dumps(result.output)
                )
                arguments = SearchProductsInput.model_validate(call.arguments, strict=True)
            except (TypeError, ValueError, ValidationError):
                raise CommercialResponseError("commercial_plan_invalid") from None
            refs = frozenset(item.sellable_item_id for item in output.items)
            if arguments.max_budget is not None:
                budgets.append(
                    _SearchBudget(
                        currency=arguments.budget_currency,
                        amount=arguments.max_budget,
                        product_refs=refs,
                    )
                )
            for item in output.items:
                _merge_product(products, item, result.call_id, has_details=False)
        elif result.capability_name == "get_product_details":
            try:
                output = GetProductDetailsOutput.model_validate_json(
                    json.dumps(result.output)
                )
            except (TypeError, ValueError, ValidationError):
                raise CommercialResponseError("commercial_plan_invalid") from None
            _merge_product(products, output.product, result.call_id, has_details=True)

    return _Authority(
        products=products,
        search_budgets=tuple(budgets),
        successful_search=successful_search,
    )


def _merge_product(
    products: dict[uuid.UUID, _ProductEvidence],
    item: AIProductSearchItem,
    call_id: str,
    *,
    has_details: bool,
) -> None:
    existing = products.get(item.sellable_item_id)
    if existing is not None and _core_product(existing.item) != _core_product(item):
        raise CommercialResponseError("commercial_plan_invalid")
    chosen = item if has_details or existing is None else existing.item
    source_ids = {call_id}
    if existing is not None:
        source_ids.update(existing.source_call_ids)
    products[item.sellable_item_id] = _ProductEvidence(
        item=chosen,
        has_details=has_details or (existing.has_details if existing else False),
        source_call_ids=frozenset(source_ids),
    )


def _core_product(item: AIProductSearchItem) -> dict[str, object]:
    return item.model_dump(exclude={"description", "sku"})


def _validate_plan(
    plan: CommercialResponsePlan,
    authority: _Authority,
    exposed: frozenset[str],
) -> tuple[_ProductEvidence, ...]:
    try:
        products = tuple(authority.products[ref] for ref in plan.product_refs)
    except KeyError:
        raise CommercialResponseError("commercial_plan_unknown_product") from None

    for product in products:
        _require_facts(plan.fact_fields, product)
    _validate_recommendation(plan, authority)
    _validate_next_action(plan, exposed)

    if plan.response_kind == ResponseKind.no_match and (
        not authority.successful_search or authority.products
    ):
        raise CommercialResponseError("commercial_plan_invalid")
    return products


def _require_facts(
    fields: Sequence[FactField],
    product: _ProductEvidence,
) -> None:
    item = product.item
    missing = (
        (FactField.model in fields and item.model_label is None)
        or (FactField.current_price in fields and item.current_usd_price is None)
        or (
            FactField.approved_product_detail in fields
            and (
                not product.has_details
                or not isinstance(item, AIProductDetail)
                or len(item.description) > _MAX_RENDERED_DETAIL_CHARS
            )
        )
    )
    if missing:
        raise CommercialResponseError("commercial_plan_missing_fact")


def _validate_recommendation(
    plan: CommercialResponsePlan,
    authority: _Authority,
) -> None:
    if plan.recommendation_ref is None:
        return
    product = authority.products[plan.recommendation_ref].item
    if plan.recommendation_reason == RecommendationReason.available_now:
        if product.availability != "available" or not product.is_sellable_now:
            raise CommercialResponseError("commercial_plan_invalid")
        return
    if plan.recommendation_reason == RecommendationReason.budget_fit:
        if not any(
            plan.recommendation_ref in budget.product_refs
            and _fits_budget(product, budget)
            for budget in authority.search_budgets
        ):
            raise CommercialResponseError("commercial_plan_invalid")


def _fits_budget(product: AIProductSearchItem, budget: _SearchBudget) -> bool:
    if budget.currency == "USD":
        return (
            product.current_usd_price is not None
            and product.current_usd_price <= budget.amount
        )
    quote = product.derived_cdf_quote
    return quote is not None and quote.amount <= budget.amount


def _validate_next_action(
    plan: CommercialResponsePlan,
    exposed: frozenset[str],
) -> None:
    action = plan.next_action
    allowed = action == NextAction.none
    if action == NextAction.search_more:
        allowed = "search_products" in exposed
    elif action == NextAction.show_details:
        allowed = "get_product_details" in exposed and bool(plan.product_refs)
    elif action == NextAction.compare_products:
        allowed = "search_products" in exposed and len(plan.product_refs) >= 2
    elif action == NextAction.request_human_handoff:
        allowed = "request_human_handoff" in exposed
    if not allowed:
        raise CommercialResponseError("commercial_plan_unsupported_action")


def _render(
    plan: CommercialResponsePlan,
    products: Sequence[_ProductEvidence],
    language: str,
) -> str:
    if language not in {"english", "french"}:
        raise CommercialResponseError("commercial_plan_language_review_required")
    french = language == "french"
    warm = plan.tone == Tone.warm
    if plan.response_kind == ResponseKind.clarify:
        questions = {
            Clarification.ask_product_type: (
                "Quel type de produit cherches-tu ?" if french else "What type of product are you looking for?"
            ),
            Clarification.ask_budget: (
                "Quel est ton budget ?" if french else "What is your budget?"
            ),
            Clarification.ask_model: (
                "Quel modèle t'intéresse ?" if french else "Which model interests you?"
            ),
        }
        return questions[plan.clarification]
    if plan.response_kind == ResponseKind.no_match:
        lead = "Je n'ai trouvé aucun produit correspondant." if french else "I found no matching product."
        return _with_action(lead, plan.next_action, french)
    if plan.response_kind == ResponseKind.unsupported_commercial_request:
        lead = (
            "Je ne peux pas traiter cette demande ici."
            if french
            else "That commercial request is not supported here."
        )
        return _with_action(lead, plan.next_action, french)

    lines = [_render_product(product, plan.fact_fields, french) for product in products]
    if plan.response_kind in {ResponseKind.product_list, ResponseKind.comparison}:
        lead = (
            "Voici les options vérifiées :"
            if french and warm
            else "Options vérifiées :"
            if french
            else "Here are the verified options:"
            if warm
            else "Verified options:"
        )
        text = lead + "\n" + "\n".join(f"• {line}" for line in lines)
    elif plan.response_kind == ResponseKind.recommendation:
        target = plan.product_refs.index(plan.recommendation_ref)  # type: ignore[arg-type]
        reason = (
            "Ce produit respecte le budget indiqué."
            if french and plan.recommendation_reason == RecommendationReason.budget_fit
            else "Il est disponible maintenant."
            if french
            else "It fits the stated budget."
            if plan.recommendation_reason == RecommendationReason.budget_fit
            else "It is available now."
        )
        text = (
            f"Je recommande {lines[target]}. {reason}"
            if french
            else f"I recommend {lines[target]}. {reason}"
        )
    else:
        text = lines[0]
    return _with_action(text, plan.next_action, french)


def _render_product(
    product: _ProductEvidence,
    fields: Sequence[FactField],
    french: bool,
) -> str:
    item = product.item
    identity = item.name
    if FactField.model in fields and item.model_label not in identity:
        identity = f"{identity} ({item.model_label})"
    facts: list[str] = []
    if FactField.current_price in fields:
        facts.append(f"{item.current_usd_price} {item.price_currency}")
    if FactField.current_availability in fields:
        available_text = (
            "disponible maintenant"
            if item.is_sellable_now and french
            else "available now"
            if item.is_sellable_now
            else "indiqué en stock, mais pas disponible à la vente actuellement"
            if french
            else "stock reported available, but not currently sellable"
        )
        availability = {
            "available": available_text,
            "out_of_stock": "en rupture de stock" if french else "out of stock",
            "unknown": "disponibilité à vérifier" if french else "availability needs verification",
        }
        facts.append(availability[item.availability])
    if FactField.current_sellability in fields:
        facts.append(
            "disponible à la vente maintenant" if french and item.is_sellable_now
            else "pas disponible à la vente actuellement" if french
            else "sellable now" if item.is_sellable_now
            else "not currently sellable"
        )
    if FactField.approved_product_detail in fields:
        assert isinstance(item, AIProductDetail)
        facts.append(item.description)
    return identity if not facts else f"{identity} — {', '.join(facts)}"


def _with_action(text: str, action: NextAction, french: bool) -> str:
    actions = {
        NextAction.search_more: (
            "Je peux chercher d'autres options."
            if french
            else "I can search for more current options."
        ),
        NextAction.show_details: (
            "Je peux te montrer les détails vérifiés."
            if french
            else "I can show the verified details."
        ),
        NextAction.compare_products: (
            "Je peux comparer ces produits."
            if french
            else "I can compare these products."
        ),
        NextAction.request_human_handoff: (
            "Je peux demander l'aide d'un conseiller humain."
            if french
            else "I can ask a human adviser to take over."
        ),
    }
    suffix = actions.get(action)
    return text if suffix is None else f"{text} {suffix}"
