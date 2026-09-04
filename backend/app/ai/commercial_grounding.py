"""Fail-closed grounding for provider-authored product price claims."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

COMMERCIAL_GROUNDING_VALIDATOR_VERSION = "mbb-commercial-grounding-validator-v1"
COMMERCIAL_GROUNDING_FAILURE_CODE = "commercial_grounding_failed"

_AMOUNT = r"(?:\d{1,3}(?:[ \u00a0\u202f,]\d{3})+|\d+(?:[.,]\d{1,2})?)"
_SUPPORTED_MONEY = re.compile(
    rf"(?:(?P<prefix>\$|usd|dollars?|cdf|fc)\s*"
    rf"(?P<prefix_amount>{_AMOUNT})|(?P<suffix_amount>{_AMOUNT})\s*"
    rf"(?P<suffix>\$|usd|dollars?|cdf|fc)(?:\s*us)?)",
    re.IGNORECASE,
)
_UNSUPPORTED_MONEY = re.compile(
    rf"(?:(?:€|eur|euros?|£|gbp)\s*{_AMOUNT}|" rf"{_AMOUNT}\s*(?:€|eur|euros?|£|gbp))",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\n]+|\b(?:mais|but|lakini|kasi|tandis\s+que|alors\s+que)\b",
    re.IGNORECASE,
)
_BUDGET_MARKER = re.compile(
    r"\b(?:budget|bajeti|maximum|max|plafond|moins\s+de|jusqu(?:'|’|e)?a|"
    r"under|up\s+to)\b",
    re.IGNORECASE,
)
_PRICE_MARKER = re.compile(
    r"\b(?:prix|price|cost|coute|coûte|cout|coût|bei|ntalo)\b",
    re.IGNORECASE,
)


class CommercialGroundingError(RuntimeError):
    """A provider-authored price claim is not grounded in one current offer."""

    safe_code = COMMERCIAL_GROUNDING_FAILURE_CODE
    validator_version = COMMERCIAL_GROUNDING_VALIDATOR_VERSION

    def __init__(self) -> None:
        super().__init__(COMMERCIAL_GROUNDING_FAILURE_CODE)


@dataclass(frozen=True)
class AuthoritativeCommercialOffer:
    """The commercial fields allowed to authorize a provider price claim."""

    product_id: uuid.UUID
    sellable_item_id: uuid.UUID
    name: str
    model_label: str | None
    current_usd_price: Decimal | None
    derived_cdf_price: Decimal | None
    sku: str | None = None
    availability: str | None = None
    is_sellable_now: bool | None = None


@dataclass(frozen=True)
class _IdentityMention:
    start: int
    end: int
    item_ids: frozenset[uuid.UUID]


@dataclass(frozen=True)
class _MoneyClaim:
    start: int
    end: int
    currency: str
    amount: Decimal


def offers_from_capability_output(
    capability_name: str,
    output: BaseModel | Mapping[str, Any],
) -> tuple[AuthoritativeCommercialOffer, ...]:
    """Extract only successful Product Offer projections from an allowed capability."""
    if capability_name not in {"search_products", "get_product_details"}:
        return ()
    value = (
        output.model_dump(mode="python") if isinstance(output, BaseModel) else output
    )
    if not isinstance(value, Mapping):
        return ()
    raw_items: object
    if capability_name == "search_products":
        raw_items = value.get("items")
        if not isinstance(raw_items, list):
            return ()
        items = raw_items
    else:
        product = value.get("product")
        if not isinstance(product, Mapping):
            return ()
        items = [product]

    offers: list[AuthoritativeCommercialOffer] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            product_id = uuid.UUID(str(item["product_id"]))
            sellable_item_id = uuid.UUID(str(item["sellable_item_id"]))
            name = item["name"]
            model_label = item.get("model_label")
            sku = item.get("sku")
            availability = item.get("availability")
            is_sellable_now = item.get("is_sellable_now")
            usd_price = _optional_decimal(item.get("current_usd_price"))
            quote = item.get("derived_cdf_quote")
            cdf_price = (
                _optional_decimal(quote.get("amount"))
                if isinstance(quote, Mapping)
                else None
            )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        if model_label is not None and not isinstance(model_label, str):
            continue
        if sku is not None and not isinstance(sku, str):
            continue
        if availability is not None and not isinstance(availability, str):
            continue
        if is_sellable_now is not None and not isinstance(is_sellable_now, bool):
            continue
        offers.append(
            AuthoritativeCommercialOffer(
                product_id=product_id,
                sellable_item_id=sellable_item_id,
                name=name.strip(),
                model_label=model_label.strip() if model_label else None,
                current_usd_price=usd_price,
                derived_cdf_price=cdf_price,
                sku=sku.strip() if sku else None,
                availability=availability,
                is_sellable_now=is_sellable_now,
            )
        )
    return tuple(offers)


def merge_authoritative_offers(
    current: Mapping[uuid.UUID, AuthoritativeCommercialOffer],
    additions: Iterable[AuthoritativeCommercialOffer],
) -> dict[uuid.UUID, AuthoritativeCommercialOffer]:
    """Return a latest-result-wins current-turn offer map."""
    merged = dict(current)
    for offer in additions:
        merged[offer.sellable_item_id] = offer
    return merged


def validate_commercial_grounding(
    text: str,
    offers: Iterable[AuthoritativeCommercialOffer],
) -> None:
    """Reject any explicit non-budget price that cannot bind to one matching item."""
    offer_list = tuple(offers)
    claims = _money_claims(text)
    mentions = _identity_mentions(text, offer_list)

    unsupported = tuple(_UNSUPPORTED_MONEY.finditer(text))
    if any(
        not _is_budget_amount(text, match.start(), match.end(), mentions)
        for match in unsupported
    ):
        raise CommercialGroundingError

    for claim in claims:
        if _is_budget_amount(text, claim.start, claim.end, mentions):
            continue
        offer = _resolve_offer(text, claim, mentions, offer_list)
        if offer is None or not _claim_matches(offer, claim):
            raise CommercialGroundingError


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("commercial prices must be positive finite decimals")
    return parsed


def _money_claims(text: str) -> tuple[_MoneyClaim, ...]:
    claims = []
    for match in _SUPPORTED_MONEY.finditer(text):
        currency = (match.group("prefix") or match.group("suffix")).casefold()
        raw_amount = match.group("prefix_amount") or match.group("suffix_amount")
        claims.append(
            _MoneyClaim(
                start=match.start(),
                end=match.end(),
                currency=(
                    "USD" if currency in {"$", "usd", "dollar", "dollars"} else "CDF"
                ),
                amount=_parse_amount(raw_amount),
            )
        )
    return tuple(claims)


def _parse_amount(raw: str) -> Decimal:
    compact = re.sub(r"[ \u00a0\u202f]", "", raw)
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", compact):
        compact = compact.replace(",", "")
    elif "," in compact and "." not in compact:
        compact = compact.replace(",", ".")
    return Decimal(compact)


def _identity_mentions(
    text: str,
    offers: tuple[AuthoritativeCommercialOffer, ...],
) -> tuple[_IdentityMention, ...]:
    aliases: dict[str, set[uuid.UUID]] = {}
    for offer in offers:
        for alias in (
            offer.name,
            offer.model_label,
            offer.sku,
            str(offer.sellable_item_id),
        ):
            if alias:
                aliases.setdefault(alias.casefold(), set()).add(offer.sellable_item_id)

    mentions: list[_IdentityMention] = []
    for alias, item_ids in aliases.items():
        if re.fullmatch(r"\d+\s*[a-zA-Z]", alias):
            digits = re.match(r"\d+", alias)
            suffix = re.search(r"[a-zA-Z]+", alias)
            assert digits is not None and suffix is not None
            pattern = rf"(?<!\w){re.escape(digits.group())}\s*{re.escape(suffix.group())}(?!\w)"
        else:
            pattern = re.escape(alias).replace(r"\ ", r"\s+")
        for match in re.finditer(pattern, text, re.IGNORECASE):
            mentions.append(
                _IdentityMention(
                    start=match.start(),
                    end=match.end(),
                    item_ids=frozenset(item_ids),
                )
            )
    return tuple(sorted(mentions, key=lambda item: (item.start, item.end)))


def _clause_bounds(text: str, position: int) -> tuple[int, int]:
    start = 0
    end = len(text)
    for match in _CLAUSE_BOUNDARY.finditer(text):
        if match.end() <= position:
            start = match.end()
        elif match.start() >= position:
            end = match.start()
            break
    return start, end


def _is_budget_amount(
    text: str,
    start: int,
    end: int,
    mentions: tuple[_IdentityMention, ...],
) -> bool:
    clause_start, clause_end = _clause_bounds(text, start)
    before = text[clause_start:start]
    budget_before = tuple(_BUDGET_MARKER.finditer(before))
    if budget_before:
        marker = budget_before[-1]
        absolute_marker_end = clause_start + marker.end()
        identity_after_marker = any(
            mention.start >= absolute_marker_end and mention.end <= start
            for mention in mentions
        )
        price_word_after_marker = _PRICE_MARKER.search(text[absolute_marker_end:start])
        if not identity_after_marker and price_word_after_marker is None:
            return True
    after = text[end : min(clause_end, end + 32)]
    return _BUDGET_MARKER.search(after) is not None


def _resolve_offer(
    text: str,
    claim: _MoneyClaim,
    mentions: tuple[_IdentityMention, ...],
    offers: tuple[AuthoritativeCommercialOffer, ...],
) -> AuthoritativeCommercialOffer | None:
    by_id = {offer.sellable_item_id: offer for offer in offers}
    clause_start, clause_end = _clause_bounds(text, claim.start)
    local_specific = [
        mention
        for mention in mentions
        if clause_start <= mention.start < clause_end and len(mention.item_ids) == 1
    ]
    if local_specific:
        claim_midpoint = (claim.start + claim.end) / 2
        distances = [
            (abs(((mention.start + mention.end) / 2) - claim_midpoint), mention)
            for mention in local_specific
        ]
        nearest_distance = min(distance for distance, _ in distances)
        nearest_ids = {
            next(iter(mention.item_ids))
            for distance, mention in distances
            if distance == nearest_distance
        }
        if len(nearest_ids) != 1:
            return None
        return by_id.get(next(iter(nearest_ids)))

    local_mentions = [
        mention for mention in mentions if clause_start <= mention.start < clause_end
    ]
    if local_mentions:
        candidates = set(local_mentions[0].item_ids)
        for mention in local_mentions[1:]:
            candidates.intersection_update(mention.item_ids)
        if len(candidates) == 1:
            return by_id.get(next(iter(candidates)))

    amount_matches = [offer for offer in offers if _claim_matches(offer, claim)]
    return amount_matches[0] if len(amount_matches) == 1 else None


def _claim_matches(
    offer: AuthoritativeCommercialOffer,
    claim: _MoneyClaim,
) -> bool:
    expected = (
        offer.current_usd_price if claim.currency == "USD" else offer.derived_cdf_price
    )
    return expected is not None and claim.amount == expected
