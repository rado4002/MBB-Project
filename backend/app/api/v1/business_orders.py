"""Browser-session Orders workspace and guarded operational actions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    UUID4,
    ValidationError,
    field_validator,
)
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    require_recent_reauthentication,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.modules.m7_conversion.order_cancellation import (
    OrderCancellationForbidden,
    OrderCancellationIdempotencyConflict,
    OrderCancellationPaymentConflict,
    OrderCancellationStale,
    OrderCancellationStateConflict,
    OrderCancellationUnavailable,
    OrderNotFound,
    cancel_pending_order,
)
from app.modules.m7_conversion.order_fulfillment import (
    OrderPreparationAllocationRequired,
    OrderPreparationForbidden,
    OrderPreparationIdempotencyConflict,
    OrderPreparationNotFound,
    OrderPreparationPaymentRequired,
    OrderPreparationStale,
    OrderPreparationStateConflict,
    OrderPreparationUnavailable,
    UnsupportedOrderPreparation,
    get_order_preparation_eligibility,
    prepare_confirmed_order,
)
from app.modules.m7_conversion.order_dispatch import (
    OrderDispatchAllocationRequired,
    OrderDispatchForbidden,
    OrderDispatchIdempotencyConflict,
    OrderDispatchMethodUnsupported,
    OrderDispatchNotFound,
    OrderDispatchPaymentRequired,
    OrderDispatchStale,
    OrderDispatchStateConflict,
    OrderDispatchUnavailable,
    UnsupportedOrderDispatch,
    dispatch_prepared_order,
    get_order_dispatch_eligibility,
)
from app.modules.m7_conversion.order_delivery import (
    OrderDeliveryDispatchEvidenceRequired,
    OrderDeliveryEvidenceAmbiguous,
    OrderDeliveryEvidenceInvalid,
    OrderDeliveryEvidenceMissing,
    OrderDeliveryEvidenceReused,
    OrderDeliveryForbidden,
    OrderDeliveryIdempotencyConflict,
    OrderDeliveryMethodUnsupported,
    OrderDeliveryNotFound,
    OrderDeliveryStale,
    OrderDeliveryStateConflict,
    OrderDeliveryUnavailable,
    UnsupportedOrderDelivery,
    confirm_delivered_order,
)
from app.request_ids import normalize_or_generate_request_id
from app.schemas.common import OrderStatus

router = APIRouter(prefix="/business/orders", tags=["business-orders"])
_require_reader = require_capability("order.read")


class StrictOrderRead(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderListQuery(StrictOrderRead):
    status: OrderStatus | None = None
    limit: int = Field(default=20, ge=1, le=50)
    offset: int = Field(default=0, ge=0, le=10000)


class OrderLineSnapshot(BaseModel):
    # Historical Order JSON can predate the current draft snapshot shape.
    model_config = ConfigDict(extra="ignore", protected_namespaces=())

    product_id: str = Field(max_length=50)
    sellable_item_id: UUID | None = None
    product_name: str | None = Field(default=None, max_length=200)
    model_label: str | None = Field(default=None, max_length=100)
    quantity: int = Field(ge=1)
    price_id: UUID | None = None
    unit_price_usd: Decimal | None = None
    exchange_rate_id: UUID | None = None
    usd_to_cdf_rate: Decimal | None = None
    unit_price_cdf: Decimal = Field(gt=0)


class OrderQueueItem(StrictOrderRead):
    order_id: UUID
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
    total_cdf: Decimal
    currency: str
    item_count: int
    first_product_name: str | None
    conversation_id: UUID | None


class OrderQueueResponse(StrictOrderRead):
    items: list[OrderQueueItem]
    has_more: bool


class OrderPreparationEligibilityResponse(StrictOrderRead):
    eligible: bool
    order_state: str
    payment_state: str
    allocation_state: str
    required_quantity: int | None
    allocated_quantity: int | None


class OrderDispatchEligibilityResponse(StrictOrderRead):
    eligible: bool
    order_state: str
    payment_state: str
    allocation_state: str
    inventory_state: str
    delivery_method_state: str
    required_quantity: int | None


class OrderDetail(OrderQueueItem):
    items: list[OrderLineSnapshot]
    delivery_zone: str
    preparation: OrderPreparationEligibilityResponse
    dispatch: OrderDispatchEligibilityResponse


class OrderCancellationReason(str, Enum):
    customer_requested = "customer_requested"
    duplicate_order = "duplicate_order"
    commercial_terms_unavailable = "commercial_terms_unavailable"
    other_operational = "other_operational"


class OrderCancellationRequest(StrictOrderRead):
    reason_code: OrderCancellationReason
    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class OrderCancellationResponse(StrictOrderRead):
    order_id: UUID
    status: OrderStatus
    updated_at: datetime
    replayed: bool


class OrderPreparationRequest(StrictOrderRead):
    expected_updated_at: datetime

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class OrderPreparationResponse(StrictOrderRead):
    order_id: UUID
    status: OrderStatus
    updated_at: datetime
    replayed: bool


class OrderDispatchRequest(StrictOrderRead):
    expected_updated_at: datetime
    handoff_reference: str = Field(min_length=1, max_length=100)
    attested: Literal[True]

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value

    @field_validator("handoff_reference")
    @classmethod
    def normalize_reference(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise ValueError("handoff_reference is invalid")
        return normalized


class OrderDispatchResponse(StrictOrderRead):
    order_id: UUID
    status: OrderStatus
    updated_at: datetime
    replayed: bool


class OrderDeliveryConfirmationRequest(StrictOrderRead):
    expected_updated_at: datetime
    evidence_message_id: UUID4
    attested: Literal[True]

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class OrderDeliveryConfirmationResponse(StrictOrderRead):
    order_id: UUID
    status: Literal[OrderStatus.delivered]
    delivered_at: datetime
    updated_at: datetime
    replayed: bool


def _unavailable() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=503,
        code="SERVICE_UNAVAILABLE",
        message="Orders are temporarily unavailable.",
    )


async def _verified_conversations(
    db: AsyncSession,
    order_ids: list[UUID],
) -> dict[UUID, UUID]:
    if not order_ids:
        return {}
    rows = await db.execute(
        select(OrderDraft.order_id, OrderDraft.conversation_id)
        .join(Order, Order.order_id == OrderDraft.order_id)
        .join(Conversation, Conversation.conversation_id == OrderDraft.conversation_id)
        .join(Lead, Lead.lead_id == Order.lead_id)
        .where(
            OrderDraft.order_id.in_(order_ids),
            OrderDraft.status == "confirmed",
            Lead.conversation_id == OrderDraft.conversation_id,
            Lead.customer_id == Order.customer_id,
            Conversation.customer_id == Order.customer_id,
        )
    )
    return {order_id: conversation_id for order_id, conversation_id in rows.all()}


def _lines(order: Order) -> list[OrderLineSnapshot]:
    return [OrderLineSnapshot.model_validate(item) for item in (order.items or [])]


def _summary(
    order: Order,
    lines: list[OrderLineSnapshot],
    conversation_id: UUID | None,
) -> OrderQueueItem:
    return OrderQueueItem(
        order_id=order.order_id,
        status=order.status,
        created_at=order.created_at,
        updated_at=order.updated_at,
        total_cdf=order.total_amount,
        currency=order.currency,
        item_count=len(lines),
        first_product_name=lines[0].product_name if lines else None,
        conversation_id=conversation_id,
    )


@router.get("", response_model=OrderQueueResponse)
async def list_orders(
    params: Annotated[OrderListQuery, Query()],
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderQueueResponse:
    try:
        statement = select(Order).order_by(
            Order.created_at.desc(), Order.order_id.desc()
        )
        if params.status is not None:
            statement = statement.where(Order.status == params.status.value)
        orders = list(
            (
                await db.scalars(
                    statement.offset(params.offset).limit(params.limit + 1)
                )
            ).all()
        )
        shown = orders[: params.limit]
        links = await _verified_conversations(db, [order.order_id for order in shown])
        items = [
            _summary(order, _lines(order), links.get(order.order_id)) for order in shown
        ]
    except (SQLAlchemyError, OSError, ValidationError, TypeError, ValueError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return OrderQueueResponse(items=items, has_more=len(orders) > params.limit)


@router.get("/{order_id}", response_model=OrderDetail)
async def get_order(
    order_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderDetail:
    try:
        order = await db.get(Order, order_id)
        if order is None:
            raise BrowserAuthError(
                status_code=404,
                code="ORDER_NOT_FOUND",
                message="Order not found.",
            )
        links = await _verified_conversations(db, [order_id])
        lines = _lines(order)
        summary = _summary(order, lines, links.get(order_id))
        eligibility = await get_order_preparation_eligibility(db, order=order)
        dispatch = await get_order_dispatch_eligibility(db, order=order)
        detail = OrderDetail(
            **summary.model_dump(),
            items=lines,
            delivery_zone=order.delivery_zone,
            preparation=OrderPreparationEligibilityResponse(**eligibility.__dict__),
            dispatch=OrderDispatchEligibilityResponse(**dispatch.__dict__),
        )
    except (SQLAlchemyError, OSError, ValidationError, TypeError, ValueError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return detail


@router.post("/{order_id}/cancel", response_model=OrderCancellationResponse)
async def cancel_order(
    order_id: UUID,
    body: OrderCancellationRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability("order.cancel"))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderCancellationResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await cancel_pending_order(
            db,
            order_id=order_id,
            expected_updated_at=body.expected_updated_at,
            reason_code=body.reason_code.value,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            idempotency_key=idempotency_key,
            idempotency_secret=settings.browser_idempotency_hmac_secret,
            request_id=request_id,
            source_network_fingerprint=(principal.session.record.ip_prefix_fingerprint),
            user_agent_fingerprint=(principal.session.record.user_agent_fingerprint),
        )
    except OrderNotFound as exc:
        raise BrowserAuthError(
            status_code=404, code="ORDER_NOT_FOUND", message="Order not found."
        ) from exc
    except OrderCancellationForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except OrderCancellationIdempotencyConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another request.",
        ) from exc
    except OrderCancellationStale as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STALE",
            message="The Order changed since it was reviewed.",
        ) from exc
    except OrderCancellationStateConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STATE_CONFLICT",
            message="Only a pending Order can be cancelled.",
        ) from exc
    except OrderCancellationPaymentConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_PAYMENT_CONFLICT",
            message="This Order has Payment state and cannot be cancelled here.",
        ) from exc
    except OrderCancellationUnavailable as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return OrderCancellationResponse(
        order_id=result.order.order_id,
        status=OrderStatus(result.order.status),
        updated_at=result.order.updated_at,
        replayed=result.replayed,
    )


@router.post("/{order_id}/prepare", response_model=OrderPreparationResponse)
async def prepare_order(
    order_id: UUID,
    body: OrderPreparationRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability("order.prepare"))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderPreparationResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await prepare_confirmed_order(
            db,
            order_id=order_id,
            expected_updated_at=body.expected_updated_at,
            idempotency_key=idempotency_key,
            actor_kind="human",
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
    except OrderPreparationNotFound as exc:
        raise BrowserAuthError(status_code=404, code="ORDER_NOT_FOUND", message="Order not found.") from exc
    except OrderPreparationForbidden as exc:
        raise BrowserAuthError(status_code=403, code="FORBIDDEN", message="Administrator permission is required.") from exc
    except OrderPreparationIdempotencyConflict as exc:
        raise BrowserAuthError(status_code=409, code="IDEMPOTENCY_CONFLICT", message="The idempotency key was already used for another request.") from exc
    except OrderPreparationStale as exc:
        raise BrowserAuthError(status_code=409, code="ORDER_STALE", message="The Order changed since it was reviewed.") from exc
    except OrderPreparationStateConflict as exc:
        raise BrowserAuthError(status_code=409, code="ORDER_STATE_CONFLICT", message="Only a confirmed Order can begin preparation.") from exc
    except OrderPreparationPaymentRequired as exc:
        raise BrowserAuthError(status_code=409, code="ORDER_PAYMENT_REQUIRED", message="A matching verified completed Payment is required.") from exc
    except OrderPreparationAllocationRequired as exc:
        raise BrowserAuthError(status_code=409, code="ORDER_ALLOCATION_REQUIRED", message="A full durable Stock allocation is required.") from exc
    except UnsupportedOrderPreparation as exc:
        raise BrowserAuthError(status_code=409, code="ORDER_PREPARATION_UNSUPPORTED", message="This Order cannot be prepared in this workflow.") from exc
    except OrderPreparationUnavailable as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return OrderPreparationResponse(
        order_id=result.order.order_id,
        status=OrderStatus(result.order.status),
        updated_at=result.order.updated_at,
        replayed=result.replayed,
    )


@router.post("/{order_id}/dispatch", response_model=OrderDispatchResponse)
async def dispatch_order(
    order_id: UUID,
    body: OrderDispatchRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability("order.dispatch"))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderDispatchResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await dispatch_prepared_order(
            db,
            order_id=order_id,
            expected_updated_at=body.expected_updated_at,
            idempotency_key=idempotency_key,
            handoff_reference=body.handoff_reference,
            attested=body.attested,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
    except OrderDispatchNotFound as exc:
        raise BrowserAuthError(
            status_code=404, code="ORDER_NOT_FOUND", message="Order not found."
        ) from exc
    except OrderDispatchForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except OrderDispatchIdempotencyConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another dispatch.",
        ) from exc
    except OrderDispatchStale as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STALE",
            message="The Order changed since it was reviewed.",
        ) from exc
    except OrderDispatchStateConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STATE_CONFLICT",
            message="Only a prepared Order can be dispatched.",
        ) from exc
    except OrderDispatchPaymentRequired as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_PAYMENT_REQUIRED",
            message="A matching verified completed Payment is required.",
        ) from exc
    except OrderDispatchAllocationRequired as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_ALLOCATION_REQUIRED",
            message="Full unconsumed allocated Stock is required.",
        ) from exc
    except OrderDispatchMethodUnsupported as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DISPATCH_METHOD_UNSUPPORTED",
            message="Only moto taxi Orders can be dispatched here.",
        ) from exc
    except UnsupportedOrderDispatch as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DISPATCH_UNSUPPORTED",
            message="This Order cannot be dispatched in this workflow.",
        ) from exc
    except OrderDispatchUnavailable as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return OrderDispatchResponse(
        order_id=result.order.order_id,
        status=OrderStatus(result.order.status),
        updated_at=result.order.updated_at,
        replayed=result.replayed,
    )


@router.post(
    "/{order_id}/confirm-delivery",
    response_model=OrderDeliveryConfirmationResponse,
)
async def confirm_order_delivery(
    order_id: UUID,
    body: OrderDeliveryConfirmationRequest,
    request: Request,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("order.delivery.confirm"))
    ],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderDeliveryConfirmationResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await confirm_delivered_order(
            db,
            order_id=order_id,
            expected_updated_at=body.expected_updated_at,
            evidence_message_id=body.evidence_message_id,
            attested=body.attested,
            idempotency_key=idempotency_key,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
    except OrderDeliveryNotFound as exc:
        raise BrowserAuthError(
            status_code=404, code="ORDER_NOT_FOUND", message="Order not found."
        ) from exc
    except OrderDeliveryForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except OrderDeliveryIdempotencyConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used for another confirmation.",
        ) from exc
    except OrderDeliveryStale as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STALE",
            message="The Order changed since it was reviewed.",
        ) from exc
    except OrderDeliveryStateConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STATE_CONFLICT",
            message="Only a delivering Order can be confirmed delivered.",
        ) from exc
    except OrderDeliveryMethodUnsupported as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_METHOD_UNSUPPORTED",
            message="Only moto taxi Orders can be confirmed here.",
        ) from exc
    except OrderDeliveryDispatchEvidenceRequired as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DISPATCH_EVIDENCE_REQUIRED",
            message="Matching dispatch evidence is required.",
        ) from exc
    except OrderDeliveryEvidenceMissing as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_EVIDENCE_MISSING",
            message="Customer receipt evidence was not found.",
        ) from exc
    except OrderDeliveryEvidenceInvalid as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_EVIDENCE_INVALID",
            message="An authentic post-dispatch inbound customer text is required.",
        ) from exc
    except OrderDeliveryEvidenceAmbiguous as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_EVIDENCE_AMBIGUOUS",
            message="The customer message is ambiguous between delivering Orders.",
        ) from exc
    except OrderDeliveryEvidenceReused as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_EVIDENCE_REUSED",
            message="The customer message was already used for another Order.",
        ) from exc
    except UnsupportedOrderDelivery as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DELIVERY_UNSUPPORTED",
            message="This Order cannot be confirmed in this workflow.",
        ) from exc
    except OrderDeliveryUnavailable as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return OrderDeliveryConfirmationResponse(
        order_id=result.order.order_id,
        status=OrderStatus.delivered,
        delivered_at=result.order.delivered_at,
        updated_at=result.order.updated_at,
        replayed=result.replayed,
    )
