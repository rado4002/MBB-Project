"""Authoritative, non-consequential order-draft creation and resolution."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.commercial_state import read_commercial_state
from app.i18n.messages import t
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order_draft import OrderDraft
from app.modules.m1_gateway.service import persist_outbound
from app.modules.product_offer.service import get_product_offer
from app.schemas.order_drafts import OrderDraftReplyResult
from app.schemas.product_offer import ProductOfferResponse

_REPLY_PATTERN = re.compile(r"^\s*([A-Za-zÀ-ÿ]+)\s+([A-Fa-f0-9]{8})[.!]?\s*$")
_CONFIRM_WORDS = frozenset({"oui", "confirme", "iyo", "nandimi", "ndiyo", "nakubali"})
_CANCEL_WORDS = frozenset({"non", "annule", "te", "boya", "hapana", "ghairi"})


class OrderDraftError(Exception):
    """Base class for safe draft failures."""


class StaleOrderDraftAuthority(OrderDraftError):
    pass


class OrderDraftOfferUnavailable(OrderDraftError):
    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


@dataclass(frozen=True)
class PreparedOrderDraft:
    draft_id: uuid.UUID
    draft_version: int
    confirmation_text: str
    outbound_message_id: uuid.UUID
    commercial_state_revision: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _cdf_text(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{int(quantized):,}".replace(",", " ")
    return f"{quantized:,.2f}".replace(",", " ")


def _offer_fingerprint(offer: ProductOfferResponse) -> str:
    quote = offer.derived_cdf_quote
    payload = {
        "product_id": str(offer.product_id),
        "sellable_item_id": str(offer.sellable_item_id),
        "product_name": offer.product_name,
        "model_label": offer.model_label,
        "price_id": None if offer.price_id is None else str(offer.price_id),
        "current_usd_price": (
            None
            if offer.current_usd_price is None
            else _decimal_text(offer.current_usd_price)
        ),
        "price_effective_at": (
            None
            if offer.price_effective_at is None
            else offer.price_effective_at.isoformat()
        ),
        "cdf_amount": None if quote is None else _decimal_text(quote.cdf_amount),
        "exchange_rate_id": (None if quote is None else str(quote.exchange_rate_id)),
        "exchange_rate_effective_at": (
            None if quote is None else quote.exchange_rate_effective_at.isoformat()
        ),
        "inventory_status": offer.inventory_status,
        "inventory_updated_at": (
            None
            if offer.inventory_updated_at is None
            else offer.inventory_updated_at.isoformat()
        ),
        "offer_status": offer.offer_status,
        "is_sellable_now": offer.is_sellable_now,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_confirmable_offer(
    offer: ProductOfferResponse | None,
) -> ProductOfferResponse:
    if offer is None:
        raise OrderDraftOfferUnavailable("sellable_item_not_found")
    if not offer.is_sellable_now:
        raise OrderDraftOfferUnavailable(offer.offer_status)
    if (
        offer.price_id is None
        or offer.current_usd_price is None
        or offer.price_effective_at is None
    ):
        raise OrderDraftOfferUnavailable("price_unavailable")
    if offer.derived_cdf_quote is None:
        raise OrderDraftOfferUnavailable("cdf_quote_unavailable")
    return offer


def _product_display(offer: ProductOfferResponse) -> str:
    if offer.model_label:
        return f"{offer.product_name} {offer.model_label}"
    return offer.product_name


def render_order_draft_confirmation(
    *,
    offer: ProductOfferResponse,
    quantity: int,
    total_cdf: Decimal,
    confirmation_code: str,
    language: str,
    refreshed: bool = False,
) -> str:
    key = "order_draft_refreshed" if refreshed else "order_draft_confirmation"
    return t(key, language).format(
        product=_product_display(offer),
        quantity=quantity,
        unit_usd=_decimal_text(offer.current_usd_price or Decimal("0")),
        total_cdf=_cdf_text(total_cdf),
        code=confirmation_code,
    )


def parse_order_draft_reply(text: str) -> tuple[str, str] | None:
    """Return a bounded action and version code; unqualified replies are ignored."""
    match = _REPLY_PATTERN.fullmatch(text)
    if match is None:
        return None
    word = match.group(1).casefold()
    code = match.group(2).upper()
    if word in _CONFIRM_WORDS:
        return "confirm", code
    if word in _CANCEL_WORDS:
        return "cancel", code
    return None


async def _latest_message_id(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    direction: str,
) -> uuid.UUID | None:
    return await session.scalar(
        select(Message.message_id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == direction,
        )
        .order_by(
            Message.created_at.desc(),
            Message.timestamp.desc(),
            Message.message_id.desc(),
        )
        .limit(1)
    )


async def _persist_application_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    content: str,
    language: str,
) -> uuid.UUID:
    return await persist_outbound(
        session=session,
        conversation_id=conversation_id,
        content=content,
        language=language,
        processing_time_ms=0,
    )


def _new_confirmation_code() -> str:
    return uuid.uuid4().hex[:8].upper()


def _snapshot_values(
    offer: ProductOfferResponse,
    *,
    quantity: int,
) -> dict[str, object]:
    confirmed = _require_confirmable_offer(offer)
    assert confirmed.price_id is not None
    assert confirmed.current_usd_price is not None
    assert confirmed.price_effective_at is not None
    assert confirmed.derived_cdf_quote is not None
    unit_cdf = confirmed.derived_cdf_quote.cdf_amount
    return {
        "product_id": confirmed.product_id,
        "sellable_item_id": confirmed.sellable_item_id,
        "product_name": confirmed.product_name,
        "model_label": confirmed.model_label,
        "quantity": quantity,
        "price_id": confirmed.price_id,
        "unit_price_usd": confirmed.current_usd_price,
        "price_effective_at": confirmed.price_effective_at,
        "exchange_rate_id": confirmed.derived_cdf_quote.exchange_rate_id,
        "unit_price_cdf": unit_cdf,
        "total_cdf": unit_cdf * quantity,
        "exchange_rate_effective_at": (
            confirmed.derived_cdf_quote.exchange_rate_effective_at
        ),
        "inventory_status": confirmed.inventory_status,
        "inventory_updated_at": confirmed.inventory_updated_at,
        "offer_read_at": confirmed.read_at,
        "offer_fingerprint": _offer_fingerprint(confirmed),
    }


async def prepare_order_draft(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
    turn_id: uuid.UUID,
    expected_ownership_version: int,
    expected_commercial_state_revision: int,
    sellable_item_id: uuid.UUID,
    quantity: int,
) -> PreparedOrderDraft:
    """Persist one authoritative snapshot and its application-owned prompt."""
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.owner_type != "ai"
        or conversation.ai_execution_state != "eligible"
        or conversation.ownership_version != expected_ownership_version
    ):
        raise StaleOrderDraftAuthority
    if (
        await _latest_message_id(session, conversation_id, "inbound")
        != source_message_id
    ):
        raise StaleOrderDraftAuthority
    state = await read_commercial_state(session, conversation_id)
    state_revision = 0 if state is None else state.revision
    if state_revision != expected_commercial_state_revision:
        raise StaleOrderDraftAuthority

    replay = await session.scalar(
        select(OrderDraft).where(OrderDraft.source_message_id == source_message_id)
    )
    if replay is not None:
        text_value = await session.scalar(
            select(Message.content).where(
                Message.message_id == replay.presented_outbound_message_id
            )
        )
        if text_value is None:
            raise OrderDraftError("draft_prompt_missing")
        return PreparedOrderDraft(
            draft_id=replay.draft_id,
            draft_version=replay.draft_version,
            confirmation_text=text_value,
            outbound_message_id=replay.presented_outbound_message_id,
            commercial_state_revision=replay.commercial_state_revision,
        )

    offer = _require_confirmable_offer(
        await get_product_offer(session, sellable_item_id)
    )
    snapshot = _snapshot_values(offer, quantity=quantity)
    confirmation_code = _new_confirmation_code()
    confirmation_text = render_order_draft_confirmation(
        offer=offer,
        quantity=quantity,
        total_cdf=snapshot["total_cdf"],  # type: ignore[arg-type]
        confirmation_code=confirmation_code,
        language=conversation.language_detected,
    )
    outbound_message_id = await _persist_application_message(
        session,
        conversation_id=conversation_id,
        content=confirmation_text,
        language=conversation.language_detected,
    )

    active = await session.scalar(
        select(OrderDraft)
        .where(
            OrderDraft.conversation_id == conversation_id,
            OrderDraft.status == "awaiting_confirmation",
        )
        .with_for_update()
    )
    if active is not None:
        active.status = "invalidated"
        active.resolution_code = "superseded"
        active.resolved_by_message_id = source_message_id
        active.resolution_outbound_message_id = outbound_message_id
        active.resolved_at = _utcnow()
        await session.flush()

    draft_id = uuid.uuid4()
    draft = OrderDraft(
        draft_id=draft_id,
        draft_version=1,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        presented_outbound_message_id=outbound_message_id,
        created_by_turn_id=turn_id,
        ownership_version=expected_ownership_version,
        commercial_state_revision=state_revision,
        confirmation_code=confirmation_code,
        status="awaiting_confirmation",
        **snapshot,
    )
    session.add(draft)
    await session.flush()
    return PreparedOrderDraft(
        draft_id=draft_id,
        draft_version=1,
        confirmation_text=confirmation_text,
        outbound_message_id=outbound_message_id,
        commercial_state_revision=state_revision,
    )


async def _resolve_with_message(
    session: AsyncSession,
    *,
    draft: OrderDraft,
    source_message_id: uuid.UUID,
    status: str,
    resolution_code: str,
    text_value: str,
    language: str,
) -> uuid.UUID:
    outbound_id = await _persist_application_message(
        session,
        conversation_id=draft.conversation_id,
        content=text_value,
        language=language,
    )
    draft.status = status
    draft.resolution_code = resolution_code
    draft.resolved_by_message_id = source_message_id
    draft.resolution_outbound_message_id = outbound_id
    draft.resolved_at = _utcnow()
    await session.flush()
    return outbound_id


async def _repeat_reply(
    session: AsyncSession,
    *,
    draft: OrderDraft,
    state: str,
    text_key: str,
    language: str,
) -> OrderDraftReplyResult:
    text_value = t(text_key, language)
    outbound_id = await _persist_application_message(
        session,
        conversation_id=draft.conversation_id,
        content=text_value,
        language=language,
    )
    return OrderDraftReplyResult(
        state=state,
        draft_id=draft.draft_id,
        draft_version=draft.draft_version,
        customer_text=text_value,
        outbound_message_id=outbound_id,
    )


async def handle_order_draft_reply(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
    expected_ownership_version: int,
    customer_text: str,
) -> OrderDraftReplyResult | None:
    """Resolve an exact code-bound reply before any provider inference."""
    parsed = parse_order_draft_reply(customer_text)
    if parsed is None:
        return None
    action, confirmation_code = parsed
    draft = await session.scalar(
        select(OrderDraft)
        .where(OrderDraft.confirmation_code == confirmation_code)
        .with_for_update()
    )
    if draft is None or draft.conversation_id != conversation_id:
        return None

    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .with_for_update()
    )
    if (
        conversation is None
        or conversation.owner_type != "ai"
        or conversation.ai_execution_state != "eligible"
        or conversation.ownership_version != expected_ownership_version
    ):
        raise StaleOrderDraftAuthority
    if (
        await _latest_message_id(session, conversation_id, "inbound")
        != source_message_id
    ):
        raise StaleOrderDraftAuthority

    language = conversation.language_detected
    if draft.status == "confirmed" and action == "confirm":
        return await _repeat_reply(
            session,
            draft=draft,
            state="already_confirmed",
            text_key="order_draft_already_confirmed",
            language=language,
        )
    if draft.status == "confirmed" and action == "cancel":
        text_value = t("order_draft_cancelled", language)
        outbound_id = await _persist_application_message(
            session,
            conversation_id=draft.conversation_id,
            content=text_value,
            language=language,
        )
        draft.status = "cancelled"
        draft.resolution_code = "customer_cancelled_after_confirmation"
        draft.resolved_by_message_id = source_message_id
        draft.resolution_outbound_message_id = outbound_id
        draft.resolved_at = _utcnow()
        await session.flush()
        return OrderDraftReplyResult(
            state="cancelled",
            draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            customer_text=text_value,
            outbound_message_id=outbound_id,
        )
    if draft.status == "cancelled" and action == "cancel":
        return await _repeat_reply(
            session,
            draft=draft,
            state="already_cancelled",
            text_key="order_draft_already_cancelled",
            language=language,
        )
    if draft.status != "awaiting_confirmation":
        return await _repeat_reply(
            session,
            draft=draft,
            state="invalidated",
            text_key="order_draft_expired",
            language=language,
        )
    if await _latest_message_id(session, conversation_id, "outbound") != (
        draft.presented_outbound_message_id
    ):
        text_value = t("order_draft_expired", language)
        outbound_id = await _resolve_with_message(
            session,
            draft=draft,
            source_message_id=source_message_id,
            status="invalidated",
            resolution_code="newer_outbound_message",
            text_value=text_value,
            language=language,
        )
        return OrderDraftReplyResult(
            state="invalidated",
            draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            customer_text=text_value,
            outbound_message_id=outbound_id,
        )
    if action == "cancel":
        text_value = t("order_draft_cancelled", language)
        outbound_id = await _resolve_with_message(
            session,
            draft=draft,
            source_message_id=source_message_id,
            status="cancelled",
            resolution_code="customer_cancelled",
            text_value=text_value,
            language=language,
        )
        return OrderDraftReplyResult(
            state="cancelled",
            draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            customer_text=text_value,
            outbound_message_id=outbound_id,
        )

    state = await read_commercial_state(session, conversation_id)
    state_revision = 0 if state is None else state.revision
    if state_revision != draft.commercial_state_revision:
        text_value = t("order_draft_expired", language)
        outbound_id = await _resolve_with_message(
            session,
            draft=draft,
            source_message_id=source_message_id,
            status="invalidated",
            resolution_code="commercial_state_changed",
            text_value=text_value,
            language=language,
        )
        return OrderDraftReplyResult(
            state="invalidated",
            draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            customer_text=text_value,
            outbound_message_id=outbound_id,
        )

    try:
        offer = _require_confirmable_offer(
            await get_product_offer(session, draft.sellable_item_id)
        )
    except OrderDraftOfferUnavailable:
        text_value = t("order_draft_unavailable", language)
        outbound_id = await _resolve_with_message(
            session,
            draft=draft,
            source_message_id=source_message_id,
            status="invalidated",
            resolution_code="offer_unavailable",
            text_value=text_value,
            language=language,
        )
        return OrderDraftReplyResult(
            state="invalidated",
            draft_id=draft.draft_id,
            draft_version=draft.draft_version,
            customer_text=text_value,
            outbound_message_id=outbound_id,
        )

    refreshed_snapshot = _snapshot_values(offer, quantity=draft.quantity)
    if refreshed_snapshot["offer_fingerprint"] != draft.offer_fingerprint:
        confirmation_code = _new_confirmation_code()
        confirmation_text = render_order_draft_confirmation(
            offer=offer,
            quantity=draft.quantity,
            total_cdf=refreshed_snapshot["total_cdf"],  # type: ignore[arg-type]
            confirmation_code=confirmation_code,
            language=language,
            refreshed=True,
        )
        outbound_id = await _resolve_with_message(
            session,
            draft=draft,
            source_message_id=source_message_id,
            status="invalidated",
            resolution_code="commercial_terms_changed",
            text_value=confirmation_text,
            language=language,
        )
        next_version = draft.draft_version + 1
        session.add(
            OrderDraft(
                draft_id=draft.draft_id,
                draft_version=next_version,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                presented_outbound_message_id=outbound_id,
                created_by_turn_id=draft.created_by_turn_id,
                ownership_version=expected_ownership_version,
                commercial_state_revision=state_revision,
                confirmation_code=confirmation_code,
                status="awaiting_confirmation",
                **refreshed_snapshot,
            )
        )
        await session.flush()
        return OrderDraftReplyResult(
            state="refreshed",
            draft_id=draft.draft_id,
            draft_version=next_version,
            customer_text=confirmation_text,
            outbound_message_id=outbound_id,
        )

    text_value = t("order_draft_confirmed", language)
    outbound_id = await _resolve_with_message(
        session,
        draft=draft,
        source_message_id=source_message_id,
        status="confirmed",
        resolution_code="customer_confirmed",
        text_value=text_value,
        language=language,
    )
    return OrderDraftReplyResult(
        state="confirmed",
        draft_id=draft.draft_id,
        draft_version=draft.draft_version,
        customer_text=text_value,
        outbound_message_id=outbound_id,
    )
