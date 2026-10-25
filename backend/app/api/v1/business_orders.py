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
    model_validator,
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
from app.models.order import Order
from app.models.order_delivery_issue import OrderDeliveryIssue
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
from app.modules.m7_conversion.order_reads import verified_order_conversations
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
    OrderDeliveryIssueOpen,
    OrderDeliveryMethodUnsupported,
    OrderDeliveryNotFound,
    OrderDeliveryStale,
    OrderDeliveryStateConflict,
    OrderDeliveryUnavailable,
    UnsupportedOrderDelivery,
    confirm_delivered_order,
    get_order_delivery_eligibility,
)
from app.modules.m7_conversion.order_delivery_issues import (
    OrderDeliveryIssueAlreadyOpen,
    OrderDeliveryIssueDispatchRequired,
    OrderDeliveryIssueEvidenceInvalid,
    OrderDeliveryIssueEvidenceMissing,
    OrderDeliveryIssueForbidden,
    OrderDeliveryIssueIdempotencyConflict,
    OrderDeliveryIssueNotFound,
    OrderDeliveryIssueStale,
    OrderDeliveryIssueStateConflict,
    OrderDeliveryIssueUnavailable,
    close_order_delivery_issue,
    list_order_delivery_issues,
    report_order_delivery_issue,
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


class OrderDeliveryEvidenceMessageResponse(StrictOrderRead):
    message_id: UUID
    content_excerpt: str = Field(max_length=500)
    content_truncated: bool
    received_at: datetime
    inbound_receipt_order: int = Field(gt=0)
    provenance: Literal["inbound_whatsapp_customer_message"] = (
        "inbound_whatsapp_customer_message"
    )


class OrderDeliveryEligibilityResponse(StrictOrderRead):
    eligible: bool
    order_state: str
    delivery_method_state: str
    line_state: str
    dispatch_state: str
    association_state: str
    evidence_state: str
    issue_state: Literal["none", "open"]
    dispatch_occurred_at: datetime | None
    evidence_messages: list[OrderDeliveryEvidenceMessageResponse] = Field(max_length=20)
    evidence_has_more: bool


class OrderDetail(OrderQueueItem):
    items: list[OrderLineSnapshot]
    delivery_zone: str
    delivered_at: datetime | None
    preparation: OrderPreparationEligibilityResponse
    dispatch: OrderDispatchEligibilityResponse
    delivery: OrderDeliveryEligibilityResponse
    delivery_issues: list["OrderDeliveryIssueResponse"] = Field(max_length=20)


class OrderDeliveryIssueType(str, Enum):
    failed = "failed"
    disputed = "disputed"


class OrderDeliveryIssueReason(str, Enum):
    recipient_unavailable = "recipient_unavailable"
    recipient_refused = "recipient_refused"
    address_problem = "address_problem"
    courier_problem = "courier_problem"
    receipt_disputed = "receipt_disputed"
    condition_disputed = "condition_disputed"
    item_disputed = "item_disputed"
    other = "other"


class OrderDeliveryIssueSource(str, Enum):
    customer_message = "customer_message"
    customer_call = "customer_call"
    courier_report = "courier_report"
    staff_observation = "staff_observation"
    other = "other"


class OrderDeliveryIssueResolution(str, Enum):
    false_alarm = "false_alarm"
    delivery_continuing = "delivery_continuing"


class OrderDeliveryIssueReportRequest(StrictOrderRead):
    expected_order_updated_at: datetime
    issue_type: OrderDeliveryIssueType
    reason_code: OrderDeliveryIssueReason
    source: OrderDeliveryIssueSource
    summary: str = Field(min_length=10, max_length=500)
    evidence_message_id: UUID4 | None = None

    @field_validator("expected_order_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expected_order_updated_at must include a timezone")
        return value

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 10:
            raise ValueError("summary must contain at least 10 characters")
        return normalized

    @model_validator(mode="after")
    def reason_matches_type(self):
        failed = {
            OrderDeliveryIssueReason.recipient_unavailable,
            OrderDeliveryIssueReason.recipient_refused,
            OrderDeliveryIssueReason.address_problem,
            OrderDeliveryIssueReason.courier_problem,
            OrderDeliveryIssueReason.other,
        }
        disputed = {
            OrderDeliveryIssueReason.receipt_disputed,
            OrderDeliveryIssueReason.condition_disputed,
            OrderDeliveryIssueReason.item_disputed,
            OrderDeliveryIssueReason.other,
        }
        allowed = failed if self.issue_type == OrderDeliveryIssueType.failed else disputed
        if self.reason_code not in allowed:
            raise ValueError("reason_code does not match issue_type")
        return self


class OrderDeliveryIssueCloseRequest(StrictOrderRead):
    resolution_code: OrderDeliveryIssueResolution
    resolution_reason: str = Field(min_length=10, max_length=500)

    @field_validator("resolution_reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 10:
            raise ValueError("resolution_reason must contain at least 10 characters")
        return normalized


class OrderDeliveryIssueResponse(StrictOrderRead):
    issue_id: UUID
    order_id: UUID
    issue_type: OrderDeliveryIssueType
    reason_code: OrderDeliveryIssueReason
    source: OrderDeliveryIssueSource
    summary: str
    evidence_message_id: UUID | None
    status: Literal["open", "closed"]
    reported_by_account_id: UUID
    reported_by_display_name: str
    reported_at: datetime
    resolution_code: OrderDeliveryIssueResolution | None
    resolution_reason: str | None
    closed_by_account_id: UUID | None
    closed_by_display_name: str | None
    closed_at: datetime | None
    replayed: bool = False


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


def _delivery_issue_response(
    issue: OrderDeliveryIssue, *, replayed: bool = False
) -> OrderDeliveryIssueResponse:
    return OrderDeliveryIssueResponse(
        issue_id=issue.issue_id,
        order_id=issue.order_id,
        issue_type=issue.issue_type,
        reason_code=issue.reason_code,
        source=issue.source,
        summary=issue.summary,
        evidence_message_id=issue.evidence_message_id,
        status=issue.status,
        reported_by_account_id=issue.reported_by_account_id,
        reported_by_display_name=issue.reporter_display_name,
        reported_at=issue.reported_at,
        resolution_code=issue.resolution_code,
        resolution_reason=issue.resolution_reason,
        closed_by_account_id=issue.closed_by_account_id,
        closed_by_display_name=issue.closed_by_display_name,
        closed_at=issue.closed_at,
        replayed=replayed,
    )


async def _delivery_issue_responses(
    db: AsyncSession, *, order_id: UUID
) -> list[OrderDeliveryIssueResponse]:
    return [
        _delivery_issue_response(issue)
        for issue in await list_order_delivery_issues(db, order_id=order_id)
    ]


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
        links = await verified_order_conversations(db, [order.order_id for order in shown])
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
        links = await verified_order_conversations(db, [order_id])
        lines = _lines(order)
        summary = _summary(order, lines, links.get(order_id))
        eligibility = await get_order_preparation_eligibility(db, order=order)
        dispatch = await get_order_dispatch_eligibility(db, order=order)
        delivery = await get_order_delivery_eligibility(db, order=order)
        delivery_issues = await _delivery_issue_responses(db, order_id=order_id)
        detail = OrderDetail(
            **summary.model_dump(),
            items=lines,
            delivery_zone=order.delivery_zone,
            delivered_at=order.delivered_at,
            preparation=OrderPreparationEligibilityResponse(**eligibility.__dict__),
            dispatch=OrderDispatchEligibilityResponse(**dispatch.__dict__),
            delivery=OrderDeliveryEligibilityResponse(
                **{
                    **delivery.__dict__,
                    "evidence_messages": [
                        OrderDeliveryEvidenceMessageResponse(**message.__dict__)
                        for message in delivery.evidence_messages
                    ],
                }
            ),
            delivery_issues=delivery_issues,
        )
    except (SQLAlchemyError, OSError, ValidationError, TypeError, ValueError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return detail


@router.post(
    "/{order_id}/delivery-issues",
    response_model=OrderDeliveryIssueResponse,
    status_code=201,
)
async def report_delivery_issue(
    order_id: UUID,
    body: OrderDeliveryIssueReportRequest,
    request: Request,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("order.delivery_issue.create"))
    ],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderDeliveryIssueResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await report_order_delivery_issue(
            db,
            order_id=order_id,
            expected_order_updated_at=body.expected_order_updated_at,
            issue_type=body.issue_type.value,
            reason_code=body.reason_code.value,
            source=body.source.value,
            summary=body.summary,
            evidence_message_id=body.evidence_message_id,
            idempotency_key=idempotency_key,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
        item = _delivery_issue_response(result.issue, replayed=result.replayed)
    except OrderDeliveryIssueNotFound as exc:
        raise BrowserAuthError(
            status_code=404, code="ORDER_NOT_FOUND", message="Order not found."
        ) from exc
    except OrderDeliveryIssueForbidden as exc:
        raise BrowserAuthError(
            status_code=403, code="FORBIDDEN", message="Permission is required."
        ) from exc
    except OrderDeliveryIssueIdempotencyConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used.",
        ) from exc
    except OrderDeliveryIssueStale as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_STALE",
            message="The Order changed since it was reviewed.",
        ) from exc
    except OrderDeliveryIssueAlreadyOpen as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_ALREADY_OPEN",
            message="An unresolved delivery issue already exists for this Order.",
        ) from exc
    except OrderDeliveryIssueStateConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_STATE_CONFLICT",
            message="This Order is not eligible for that delivery issue.",
        ) from exc
    except OrderDeliveryIssueDispatchRequired as exc:
        raise BrowserAuthError(
            status_code=409,
            code="ORDER_DISPATCH_EVIDENCE_REQUIRED",
            message="Matching dispatch evidence is required.",
        ) from exc
    except OrderDeliveryIssueEvidenceMissing as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_EVIDENCE_MISSING",
            message="The selected customer evidence was not found.",
        ) from exc
    except OrderDeliveryIssueEvidenceInvalid as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_EVIDENCE_INVALID",
            message="Evidence must be an authentic post-dispatch customer text.",
        ) from exc
    except OrderDeliveryIssueUnavailable as exc:
        raise _unavailable() from exc
    response.status_code = 200 if result.replayed else 201
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return item


@router.post(
    "/{order_id}/delivery-issues/{issue_id}/close",
    response_model=OrderDeliveryIssueResponse,
)
async def close_delivery_issue(
    order_id: UUID,
    issue_id: UUID,
    body: OrderDeliveryIssueCloseRequest,
    request: Request,
    response: Response,
    principal: Annotated[
        BrowserPrincipal, Depends(require_capability("order.delivery_issue.close"))
    ],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OrderDeliveryIssueResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await close_order_delivery_issue(
            db,
            order_id=order_id,
            issue_id=issue_id,
            resolution_code=body.resolution_code.value,
            resolution_reason=body.resolution_reason,
            idempotency_key=idempotency_key,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
            user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
        )
        item = _delivery_issue_response(result.issue, replayed=result.replayed)
    except OrderDeliveryIssueNotFound as exc:
        raise BrowserAuthError(
            status_code=404,
            code="DELIVERY_ISSUE_NOT_FOUND",
            message="Delivery issue not found.",
        ) from exc
    except OrderDeliveryIssueForbidden as exc:
        raise BrowserAuthError(
            status_code=403,
            code="FORBIDDEN",
            message="Administrator permission is required.",
        ) from exc
    except OrderDeliveryIssueIdempotencyConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The idempotency key was already used.",
        ) from exc
    except OrderDeliveryIssueStateConflict as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_STATE_CONFLICT",
            message=str(exc),
        ) from exc
    except OrderDeliveryIssueUnavailable as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = str(result.replayed).lower()
    return item


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
    except OrderDeliveryIssueOpen as exc:
        raise BrowserAuthError(
            status_code=409,
            code="DELIVERY_ISSUE_OPEN",
            message="Resolve the open delivery issue before confirming receipt.",
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
