"""Browser-session, read-only Orders queue and accepted-snapshot detail."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import BrowserPrincipal, require_capability
from app.api.browser_auth_errors import BrowserAuthError
from app.database import get_db
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.order import Order
from app.models.order_draft import OrderDraft
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
    total_cdf: Decimal
    currency: str
    item_count: int
    first_product_name: str | None
    conversation_id: UUID | None


class OrderQueueResponse(StrictOrderRead):
    items: list[OrderQueueItem]
    has_more: bool


class OrderDetail(OrderQueueItem):
    items: list[OrderLineSnapshot]
    delivery_zone: str


def _unavailable() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=503, code="SERVICE_UNAVAILABLE",
        message="Orders are temporarily unavailable.",
    )


async def _verified_conversations(
    db: AsyncSession, order_ids: list[UUID],
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
    order: Order, lines: list[OrderLineSnapshot], conversation_id: UUID | None,
) -> OrderQueueItem:
    return OrderQueueItem(
        order_id=order.order_id,
        status=order.status,
        created_at=order.created_at,
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
        statement = select(Order).order_by(Order.created_at.desc(), Order.order_id.desc())
        if params.status is not None:
            statement = statement.where(Order.status == params.status.value)
        orders = list((await db.scalars(statement.offset(params.offset).limit(params.limit + 1))).all())
        shown = orders[:params.limit]
        links = await _verified_conversations(db, [order.order_id for order in shown])
        items = [_summary(order, _lines(order), links.get(order.order_id)) for order in shown]
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
                status_code=404, code="ORDER_NOT_FOUND", message="Order not found.",
            )
        links = await _verified_conversations(db, [order_id])
        lines = _lines(order)
        summary = _summary(order, lines, links.get(order_id))
        detail = OrderDetail(
            **summary.model_dump(), items=lines, delivery_zone=order.delivery_zone,
        )
    except (SQLAlchemyError, OSError, ValidationError, TypeError, ValueError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return detail
