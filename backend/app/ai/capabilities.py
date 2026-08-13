"""Strict provider-neutral boundary for future MBB AI capabilities."""
from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from app.models.catalog import normalize_category_code
from app.schemas.commerce_admin import AttributeValue
from app.schemas.product_offer import ProductOfferResponse

_CAPABILITY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_DESCRIPTION_LENGTH = 200
_MODEL_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "allowed_tools",
        "conversation_id",
        "turn_id",
        "ownership_version",
        "expected_ownership_version",
        "human_owner_account_id",
        "owner_type",
        "customer_id",
        "business_id",
        "tenant_id",
        "actor_id",
        "permissions",
        "owner_id",
        "internal_account_id",
    }
)


class StrictCapabilityModel(BaseModel):
    """Base contract for validated capability inputs and safe outputs."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        protected_namespaces=(),
    )


@dataclass(frozen=True)
class TrustedCapabilityContext:
    """MBB-supplied business scope that model arguments cannot modify."""

    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    expected_ownership_version: int

    def __post_init__(self) -> None:
        if self.expected_ownership_version <= 0:
            raise ValueError("expected ownership version must be positive")


CapabilityHandler = Callable[
    [TrustedCapabilityContext, StrictCapabilityModel],
    Awaitable[object],
]


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    description: str
    input_model: type[StrictCapabilityModel]
    output_model: type[StrictCapabilityModel]
    handler: CapabilityHandler

    def __post_init__(self) -> None:
        if not _CAPABILITY_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("capability name must be stable lower_snake_case")
        description = self.description.strip()
        if not description or len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError("capability description must be 1-200 characters")
        object.__setattr__(self, "description", description)
        if not issubclass(self.input_model, StrictCapabilityModel):
            raise TypeError("capability input model must be strict")
        if not issubclass(self.output_model, StrictCapabilityModel):
            raise TypeError("capability output model must be strict")


@dataclass(frozen=True)
class CapabilitySpecification:
    name: str
    description: str
    input_schema: Mapping[str, Any]


class DuplicateCapabilityName(ValueError):
    pass


class CapabilityRegistry:
    """Immutable registry built only from explicit MBB code definitions."""

    def __init__(self, definitions: Iterable[CapabilityDefinition]) -> None:
        registered: dict[str, CapabilityDefinition] = {}
        for definition in definitions:
            if definition.name in registered:
                raise DuplicateCapabilityName(definition.name)
            registered[definition.name] = definition
        self._definitions = MappingProxyType(registered)

    def __len__(self) -> int:
        return len(self._definitions)

    def resolve(self, name: str) -> CapabilityDefinition | None:
        return self._definitions.get(name)

    def specifications(
        self,
        allowed_capabilities: Iterable[str],
    ) -> tuple[CapabilitySpecification, ...]:
        allowed = frozenset(allowed_capabilities)
        return tuple(
            CapabilitySpecification(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_model.model_json_schema(),
            )
            for name, definition in sorted(self._definitions.items())
            if name in allowed
        )


class CapabilityErrorCategory(str, Enum):
    unknown_tool = "unknown_tool"
    tool_not_allowed = "tool_not_allowed"
    invalid_arguments = "invalid_arguments"
    execution_failed = "execution_failed"


class SafeCapabilityError(Exception):
    """Code-controlled domain failure safe to classify across the AI boundary."""

    def __init__(self, safe_code: str) -> None:
        if not _SAFE_CODE_PATTERN.fullmatch(safe_code):
            raise ValueError("safe capability error code is invalid")
        super().__init__("capability execution failed safely")
        self.safe_code = safe_code


@dataclass(frozen=True)
class CapabilitySuccess:
    capability_name: str
    output: StrictCapabilityModel
    succeeded: bool = True


@dataclass(frozen=True)
class CapabilityFailure:
    error: CapabilityErrorCategory
    safe_code: str | None = None
    succeeded: bool = False


CapabilityExecutionResult = CapabilitySuccess | CapabilityFailure


class CapabilityExecutor:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        *,
        requested_name: str,
        model_arguments: object,
        allowed_capabilities: Iterable[str],
        context: TrustedCapabilityContext,
    ) -> CapabilityExecutionResult:
        if not isinstance(requested_name, str):
            return CapabilityFailure(CapabilityErrorCategory.unknown_tool)
        definition = self._registry.resolve(requested_name)
        if definition is None:
            return CapabilityFailure(CapabilityErrorCategory.unknown_tool)

        allowed = frozenset(allowed_capabilities)
        if requested_name not in allowed:
            return CapabilityFailure(CapabilityErrorCategory.tool_not_allowed)

        if not isinstance(model_arguments, Mapping):
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)
        if _MODEL_FORBIDDEN_ARGUMENTS.intersection(model_arguments):
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)

        try:
            validated_input = definition.input_model.model_validate(
                model_arguments,
                strict=True,
            )
        except ValidationError:
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)

        try:
            raw_output = await definition.handler(context, validated_input)
            validated_output = definition.output_model.model_validate(
                raw_output,
                strict=True,
            )
        except SafeCapabilityError as exc:
            return CapabilityFailure(
                CapabilityErrorCategory.execution_failed,
                safe_code=exc.safe_code,
            )
        except Exception:
            return CapabilityFailure(CapabilityErrorCategory.execution_failed)

        return CapabilitySuccess(
            capability_name=definition.name,
            output=validated_output,
        )


class RequestHumanHandoffInput(StrictCapabilityModel):
    reason_category: Literal[
        "customer_requested_human",
        "unsupported_action",
        "policy_exception",
        "insufficient_business_evidence",
        "repeated_misunderstanding",
        "required_capability_unavailable",
    ]


class RequestHumanHandoffOutput(StrictCapabilityModel):
    state: Literal["waiting_for_human"]
    ownership_version: int = Field(gt=0)
    escalation_ticket_id: uuid.UUID
    replayed: bool


class SearchProductsInput(StrictCapabilityModel):
    query: str | None = Field(default=None, min_length=1, max_length=120)
    category_code: str | None = Field(default=None, min_length=1, max_length=50)
    max_budget: Decimal | None = Field(
        default=None,
        gt=0,
        max_digits=18,
        decimal_places=2,
    )
    budget_currency: Literal["USD", "CDF"] = "USD"
    search_mode: Literal["SELLABLE_ONLY", "INCLUDE_UNAVAILABLE"] = "SELLABLE_ONLY"
    limit: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def _normalize_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain searchable text")
        return normalized

    @field_validator("max_budget", mode="before")
    @classmethod
    def _parse_budget(cls, value: object) -> object:
        if value is None or isinstance(value, Decimal):
            return value
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("max_budget must be a decimal value")
        try:
            parsed = Decimal(str(value))
        except ArithmeticError as exc:
            raise ValueError("max_budget must be a decimal value") from exc
        if not parsed.is_finite():
            raise ValueError("max_budget must be finite")
        return parsed

    @field_validator("category_code")
    @classmethod
    def _normalize_category(cls, value: str | None) -> str | None:
        return None if value is None else normalize_category_code(value)


class GetProductDetailsInput(StrictCapabilityModel):
    sellable_item_id: str = Field(
        pattern=(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )


class AIProductMedia(StrictCapabilityModel):
    media_id: uuid.UUID
    alt_text: str | None = Field(default=None, max_length=500)
    source_scope: Literal["product", "sellable_item"]


class AIProductCdfQuote(StrictCapabilityModel):
    currency: Literal["CDF"] = "CDF"
    amount: Decimal


class AIProductSearchItem(StrictCapabilityModel):
    product_id: uuid.UUID
    sellable_item_id: uuid.UUID
    name: str
    model_label: str | None
    category_code: str
    attributes: dict[str, AttributeValue]
    current_usd_price: Decimal | None
    price_currency: Literal["USD"] = "USD"
    cdf_quote_status: Literal["available", "cdf_quote_unavailable"]
    derived_cdf_quote: AIProductCdfQuote | None
    availability: Literal["available", "out_of_stock", "unknown"]
    offer_status: Literal[
        "sellable_now",
        "availability_unconfirmed",
        "out_of_stock",
        "price_unavailable",
        "inactive",
    ]
    is_sellable_now: bool
    primary_media: AIProductMedia | None


class AIProductDetail(AIProductSearchItem):
    description: str
    sku: str | None


class SearchProductsOutput(StrictCapabilityModel):
    items: list[AIProductSearchItem] = Field(max_length=10)


class GetProductDetailsOutput(StrictCapabilityModel):
    product: AIProductDetail


def _project_media(offer: ProductOfferResponse) -> dict[str, object] | None:
    media = offer.primary_media
    if media is None:
        return None
    return {
        "media_id": media.media_id,
        "alt_text": media.alt_text,
        "source_scope": media.source_scope,
    }


def _project_offer(offer: ProductOfferResponse, *, include_details: bool) -> dict[str, object]:
    projected: dict[str, object] = {
        "product_id": offer.product_id,
        "sellable_item_id": offer.sellable_item_id,
        "name": offer.product_name,
        "model_label": offer.model_label,
        "category_code": offer.category_code,
        "attributes": offer.attributes,
        "current_usd_price": offer.current_usd_price,
        "price_currency": offer.price_currency,
        "cdf_quote_status": offer.cdf_quote_status,
        "derived_cdf_quote": (
            None
            if offer.derived_cdf_quote is None
            else {
                "currency": offer.derived_cdf_quote.currency,
                "amount": offer.derived_cdf_quote.cdf_amount,
            }
        ),
        "availability": offer.inventory_status,
        "offer_status": offer.offer_status,
        "is_sellable_now": offer.is_sellable_now,
        "primary_media": _project_media(offer),
    }
    if include_details:
        projected.update(description=offer.description, sku=offer.sku)
    return projected


async def _search_products(
    _context: TrustedCapabilityContext,
    arguments: StrictCapabilityModel,
) -> object:
    from app.database import async_session_factory
    from app.modules.product_offer.service import (
        ProductOfferCdfQuoteUnavailable,
        search_product_offers,
    )

    assert isinstance(arguments, SearchProductsInput)
    budget_arguments: dict[str, Decimal | None] = {
        "max_budget_usd": None,
        "max_budget_cdf": None,
    }
    if arguments.max_budget is not None:
        budget_field = (
            "max_budget_usd"
            if arguments.budget_currency == "USD"
            else "max_budget_cdf"
        )
        budget_arguments[budget_field] = arguments.max_budget

    async with async_session_factory() as session:
        try:
            offers = await search_product_offers(
                session,
                query=arguments.query,
                category_code=arguments.category_code,
                search_mode=arguments.search_mode.lower(),
                limit=arguments.limit,
                **budget_arguments,
            )
        except ProductOfferCdfQuoteUnavailable as exc:
            raise SafeCapabilityError("cdf_quote_unavailable") from exc
        except ValueError as exc:
            raise SafeCapabilityError("invalid_search") from exc

    return {
        "items": [
            _project_offer(offer, include_details=False) for offer in offers
        ]
    }


async def _get_product_details(
    _context: TrustedCapabilityContext,
    arguments: StrictCapabilityModel,
) -> object:
    from app.database import async_session_factory
    from app.modules.product_offer.service import (
        ProductOfferNotFound,
        require_product_offer,
    )

    assert isinstance(arguments, GetProductDetailsInput)
    async with async_session_factory() as session:
        try:
            offer = await require_product_offer(
                session,
                uuid.UUID(arguments.sellable_item_id),
            )
        except ProductOfferNotFound as exc:
            raise SafeCapabilityError("sellable_item_not_found") from exc

    return {"product": _project_offer(offer, include_details=True)}


async def _request_human_handoff(
    context: TrustedCapabilityContext,
    _arguments: StrictCapabilityModel,
) -> object:
    from app.database import async_session_factory
    from app.modules.m4_conversation.ai_handoff import (
        AIHandoffConversationNotFound,
        AIHandoffUnavailable,
        StaleAIAuthority,
        request_human_handoff,
    )

    async with async_session_factory() as session:
        try:
            result = await request_human_handoff(
                session,
                conversation_id=context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )
        except AIHandoffConversationNotFound as exc:
            raise SafeCapabilityError("conversation_not_found") from exc
        except StaleAIAuthority as exc:
            raise SafeCapabilityError("stale_ai_authority") from exc
        except AIHandoffUnavailable as exc:
            raise SafeCapabilityError("handoff_unavailable") from exc

    return {
        "state": "waiting_for_human",
        "ownership_version": result.ownership_version,
        "escalation_ticket_id": result.escalation_ticket_id,
        "replayed": result.replayed,
    }


AI_CAPABILITY_REGISTRY = CapabilityRegistry(
    (
        CapabilityDefinition(
            name="get_product_details",
            description=(
                "Retrieve current authoritative details for one exact MBB Sellable "
                "Item. Treat non-sellable states as truthful business results."
            ),
            input_model=GetProductDetailsInput,
            output_model=GetProductDetailsOutput,
            handler=_get_product_details,
        ),
        CapabilityDefinition(
            name="request_human_handoff",
            description="Pause AI and request Human attention for this conversation.",
            input_model=RequestHumanHandoffInput,
            output_model=RequestHumanHandoffOutput,
            handler=_request_human_handoff,
        ),
        CapabilityDefinition(
            name="search_products",
            description=(
                "Search MBB's authoritative current product offers. Use returned "
                "price, availability and sellability exactly; never invent facts."
            ),
            input_model=SearchProductsInput,
            output_model=SearchProductsOutput,
            handler=_search_products,
        ),
    )
)
