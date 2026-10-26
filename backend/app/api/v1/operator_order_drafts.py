"""Browser-session preparation of customer-confirmable Order drafts."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response, status
from pydantic import UUID4
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.modules.m7_conversion.order_drafts import (
    OperatorOrderDraftActiveConflict,
    OperatorOrderDraftForbidden,
    OperatorOrderDraftIdempotencyConflict,
    OperatorOrderDraftInsufficientStock,
    OperatorOrderDraftNotEligible,
    OperatorOrderDraftStaleOffer,
    OperatorOrderDraftUnavailable,
    OrderDraftOrderUnavailable,
    StaleOrderDraftAuthority,
    prepare_operator_order_draft,
)
from app.request_ids import normalize_or_generate_request_id
from app.schemas.order_drafts import (
    OperatorOrderDraftPrepareRequest,
    OperatorOrderDraftResponse,
)

router = APIRouter(
    prefix="/operator/conversations", tags=["operator-order-drafts"]
)


def _error(status_code: int, code: str, message: str) -> BrowserAuthError:
    return BrowserAuthError(status_code=status_code, code=code, message=message)


def _response(result) -> OperatorOrderDraftResponse:
    draft = result.draft
    return OperatorOrderDraftResponse(
        draft_id=draft.draft_id,
        draft_version=draft.draft_version,
        status=draft.status,
        product_id=draft.product_id,
        sellable_item_id=draft.sellable_item_id,
        product_name=draft.product_name,
        model_label=draft.model_label,
        quantity=draft.quantity,
        unit_price_usd=draft.unit_price_usd,
        unit_price_cdf=draft.unit_price_cdf,
        total_cdf=draft.total_cdf,
        confirmation_code=draft.confirmation_code,
        required_customer_reply=(
            f"OUI {draft.confirmation_code}"
            if draft.status == "awaiting_confirmation" else None
        ),
        customer_confirmation_text=result.customer_confirmation_text,
        presented_to_customer=False,
        replayed=result.replayed,
    )


@router.post(
    "/{conversation_id}/order-drafts",
    response_model=OperatorOrderDraftResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_operator_order_draft(
    conversation_id: UUID,
    body: OperatorOrderDraftPrepareRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _draft_principal: Annotated[
        BrowserPrincipal, Depends(require_capability("order_draft.prepare"))
    ],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[
        UUID4, Header(alias="Idempotency-Key", include_in_schema=True)
    ],
) -> OperatorOrderDraftResponse:
    validate_state_changing_request(request, settings)
    request_id = normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )
    try:
        result = await prepare_operator_order_draft(
            db,
            conversation_id=conversation_id,
            expected_ownership_version=body.expected_ownership_version,
            product_id=body.product_id,
            sellable_item_id=body.sellable_item_id,
            price_id=body.price_id,
            exchange_rate_id=body.exchange_rate_id,
            inventory_updated_at=body.inventory_updated_at,
            quantity=body.quantity,
            idempotency_key=idempotency_key,
            actor_account_id=principal.account.account_id,
            actor_display_name=principal.account.display_name,
            actor_role=principal.account.role,
            request_id=request_id,
            source_network_fingerprint=(
                principal.session.record.ip_prefix_fingerprint
            ),
            user_agent_fingerprint=(
                principal.session.record.user_agent_fingerprint
            ),
        )
    except OperatorOrderDraftForbidden as exc:
        raise _error(
            409, "OWNERSHIP_CONFLICT",
            "Take over this conversation before preparing an Order draft.",
        ) from exc
    except StaleOrderDraftAuthority as exc:
        raise _error(
            409, "OWNERSHIP_VERSION_CONFLICT",
            "Conversation ownership changed. Refresh before preparing the draft.",
        ) from exc
    except OperatorOrderDraftStaleOffer as exc:
        raise _error(
            409, "PRODUCT_OFFER_STALE",
            "Product, price, exchange rate, or Stock changed. Refresh the Offer.",
        ) from exc
    except OperatorOrderDraftInsufficientStock as exc:
        raise _error(
            409, "INSUFFICIENT_STOCK",
            "The requested quantity is no longer available.",
        ) from exc
    except OperatorOrderDraftActiveConflict as exc:
        raise _error(
            409, "ORDER_DRAFT_ACTIVE",
            "This conversation already has a draft awaiting customer confirmation.",
        ) from exc
    except OperatorOrderDraftIdempotencyConflict as exc:
        raise _error(
            409, "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used for another draft request.",
        ) from exc
    except OperatorOrderDraftNotEligible as exc:
        raise _error(
            409, "ORDER_DRAFT_NOT_ELIGIBLE",
            "The conversation or Customer is not eligible for an Order draft.",
        ) from exc
    except OrderDraftOrderUnavailable as exc:
        raise _error(
            409, "ORDER_DRAFT_UNAVAILABLE",
            "The authoritative Order draft could not be prepared.",
        ) from exc
    except OperatorOrderDraftUnavailable as exc:
        raise _error(
            503, "ORDER_DRAFT_UNAVAILABLE",
            "Order draft preparation is temporarily unavailable.",
        ) from exc
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _error(
            503, "ORDER_DRAFT_UNAVAILABLE",
            "Order draft preparation is temporarily unavailable.",
        ) from exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["Idempotent-Replayed"] = (
        "true" if result.replayed else "false"
    )
    return _response(result)
