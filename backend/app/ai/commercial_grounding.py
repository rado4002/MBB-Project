"""Fail-closed grounding for provider-authored product price claims."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel

COMMERCIAL_GROUNDING_VALIDATOR_VERSION = "mbb-commercial-grounding-validator-v3"
COMMERCIAL_GROUNDING_FAILURE_CODE = "commercial_grounding_failed"

_AMOUNT = r"-?(?:\d{1,3}(?:[ \u00a0\u202f,]\d{3})+|\d+)(?:[.,]\d{1,2})?"
_CURRENCY = r"\$|€|£|(?<![a-z])(?:usd|dollars?|cdf|fc|euros?|eur|gbp)(?![a-z])"
_MONEY = re.compile(
    rf"(?:(?P<prefix>{_CURRENCY})\s*"
    rf"(?P<prefix_amount>{_AMOUNT})|(?P<suffix_amount>{_AMOUNT})\s*"
    rf"(?P<suffix>{_CURRENCY})(?:\s*us\b)?)",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;\n]+|\b(?:mais|but|lakini|kasi|tandis\s+que|alors\s+que)\b",
    re.IGNORECASE,
)
_BUDGET_MARKER = re.compile(
    r"\b(?:budget|bajeti|maximum|max|plafond|moins\s+de|jusqu(?:'|’|e)?[aà]|"
    r"under|up\s+to)\b",
    re.IGNORECASE,
)
_PRICE_MARKER = re.compile(
    r"\b(?:prix|price|costs?|coute|coûte|coutent|coûtent|cout|coût|bei|ntalo)\b",
    re.IGNORECASE,
)
_NON_PRODUCT_MONEY_MARKER = re.compile(
    r"\b(?:livraison|transport|expedition|expédition|delivery|shipping|"
    r"paiement|payment|acompte|deposit|versement|taxe|tax|frais|fees?)\b",
    re.IGNORECASE,
)
_IDENTITY_COORDINATOR = re.compile(
    r"\b(?:et|and|pamoja\s+na|na)\b|[&+,]", re.IGNORECASE
)
_IDENTITY_SCAFFOLD = re.compile(
    r"\b(?:le|la|les|l|un|une|des|du|de|d|the|ya|kwa|pour|for|"
    r"modele|modèle|modeles|modèles|model|models|produit|produits|product|"
    r"products|article|articles|item|items|option|options|version|versions|"
    r"est|sont|is|are|ezali|na|ni|a|à|au|en|de|du|actuel|actuelle|current|"
    r"disponible|indisponible|available|unavailable|vendable|rupture|stock|"
    r"maintenant|now)\b",
    re.IGNORECASE,
)
# These are complete local heads, not bags of words to erase around any amount.
# In particular, neither a catalog miss nor a marker somewhere in the sentence
# establishes a non-product role.
_MONEY_SUBJECT_PREFIX = (
    r"(?:(?:je\s+(?:garde|note|retiens)\s+)?"
    r"(?:mon|ton|votre|notre|le|la|les|un|une)\s+|na\s+)?"
)
_BUDGET_HEAD = re.compile(
    _MONEY_SUBJECT_PREFIX
    + _BUDGET_MARKER.pattern
    + r"(?:\s+(?:actuel|actuelle|est|de|ya|ni|ezali|is|na\s+ngai|yangu))*\s*[:=(]*\s*",
    re.IGNORECASE,
)
_NON_PRODUCT_HEAD = re.compile(
    _MONEY_SUBJECT_PREFIX
    + _NON_PRODUCT_MONEY_MARKER.pattern
    + r"(?:\s+(?:de|du|la|le)\s*"
    + _NON_PRODUCT_MONEY_MARKER.pattern
    + r")?"
    + r"(?:\s+(?:est|de|du|ya|ni|ezali|is|received|reçu|recu|prévu|prevu|"
    r"coûte|coute|costs?|à|a))*\s*[:=(]*\s*",
    re.IGNORECASE,
)
_BARE_PAIR_LINK = re.compile(
    r"\s*(?:,\s*)?(?:(?:et|and|na|pamoja\s+na|soit)\b|[/,(])\s*\(?\s*",
    re.IGNORECASE,
)
_ASSERTION_LEADER = re.compile(
    r"\s*[),]*\s*(?:(?:et|and|pamoja\s+na|na)\b\s*)?", re.IGNORECASE
)
_POSTFIX_BUDGET = re.compile(
    r"\s*(?:de\s+)?" + _BUDGET_MARKER.pattern + r"\s*", re.IGNORECASE
)
_PRODUCT_TAIL = re.compile(
    r"[\s),]*(?:(?:et|and)\s+)?"
    r"(?:(?:(?:est|is|ezali|ni)\s+)?"
    r"(?:disponible|indisponible|vendable|available|unavailable|en\s+stock|"
    r"en\s+rupture(?:\s+de\s+stock)?)"
    r"(?:\s+(?:et|and)\s+(?:disponible|vendable|available))*"
    r"(?:\s+(?:maintenant|now))?)?\s*",
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


@dataclass(frozen=True)
class _MoneyAssertion:
    """One detected amount's local association; never an authorization for fees."""

    claim: _MoneyClaim
    assertion_start: int
    role: Literal["product_price", "non_product", "unresolved"]
    offers: tuple[AuthoritativeCommercialOffer, ...] = ()
    currencies: frozenset[str] = frozenset()


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
    """Validate bounded local monetary assertions before delivery.

    Supported product claims identify one item through adjacent authoritative aliases,
    or identify several items through explicit conjunctive coordination. A parent name
    may be narrowed by an adjacent model/SKU alias. Identity-free, unfamiliar,
    disjunctive, or cross-clause associations fail closed. Clearly marked budgets and
    delivery/payment/fee heads govern only their local amounts. Only an adjacent
    bare USD/CDF pair can continue an association without another explicit head.
    Non-product exemption does not validate the truth of those amounts.
    """
    offer_list = tuple(offers)
    claims = _money_claims(text)
    for assertion in _money_assertions(text, claims, offer_list):
        if assertion.role == "non_product":
            continue
        if assertion.role == "unresolved" or any(
            not _claim_matches(offer, assertion.claim) for offer in assertion.offers
        ):
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
    for match in _MONEY.finditer(text):
        currency = (match.group("prefix") or match.group("suffix")).casefold()
        raw_amount = match.group("prefix_amount") or match.group("suffix_amount")
        try:
            amount = _parse_amount(raw_amount)
        except InvalidOperation:
            raise CommercialGroundingError from None
        claims.append(
            _MoneyClaim(
                start=match.start(),
                end=match.end(),
                currency=(
                    "USD"
                    if currency in {"$", "usd", "dollar", "dollars"}
                    else "CDF"
                    if currency in {"cdf", "fc"}
                    else "unsupported"
                ),
                amount=amount,
            )
        )
    return tuple(claims)


def _parse_amount(raw: str) -> Decimal:
    compact = re.sub(r"[ \u00a0\u202f]", "", raw)
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?", compact):
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


def _money_assertions(
    text: str,
    claims: tuple[_MoneyClaim, ...],
    offers: tuple[AuthoritativeCommercialOffer, ...],
) -> tuple[_MoneyAssertion, ...]:
    assertions: list[_MoneyAssertion] = []
    previous_end = 0
    for index, claim in enumerate(claims):
        gap = text[previous_end : claim.start]
        boundaries = tuple(_CLAUSE_BOUNDARY.finditer(gap))
        if (
            assertions
            and not boundaries
            and _BARE_PAIR_LINK.fullmatch(gap)
            and assertions[-1].currencies | {claim.currency} == {"USD", "CDF"}
            and claim.currency not in assertions[-1].currencies
        ):
            previous = assertions[-1]
            assertion = _MoneyAssertion(
                claim,
                previous.assertion_start,
                previous.role,
                previous.offers,
                previous.currencies | {claim.currency},
            )
        else:
            # Inspect only the gap after the previous amount. Decimal punctuation
            # inside a money token can never become a sentence boundary here.
            start = previous_end + (boundaries[-1].end() if boundaries else 0)
            leader = _ASSERTION_LEADER.match(text, start, claim.start)
            assert leader is not None
            start = leader.end()
            head = text[start : claim.start]
            resolved = _resolve_offers(head, offers)
            if resolved is not None and claim.currency in {"USD", "CDF"}:
                role = "product_price"
            elif not _identity_mentions(head, offers) and (
                _BUDGET_HEAD.fullmatch(head) or _NON_PRODUCT_HEAD.fullmatch(head)
            ):
                role = "non_product"
            else:
                role = "unresolved"
                # Retain the narrow identity-free "45 USD de budget" form only
                # when the complete suffix ends at a hard boundary/end of text.
                next_start = (
                    claims[index + 1].start if index + 1 < len(claims) else len(text)
                )
                suffix = text[claim.end : next_start]
                boundary = _CLAUSE_BOUNDARY.search(suffix)
                if boundary:
                    suffix = suffix[: boundary.start()]
                if (
                    not head.strip()
                    and (boundary is not None or index + 1 == len(claims))
                    and _POSTFIX_BUDGET.fullmatch(suffix)
                ):
                    role = "non_product"
            assertion = _MoneyAssertion(
                claim, start, role, resolved or (), frozenset({claim.currency})
            )
        assertions.append(assertion)
        previous_end = claim.end
    for index, assertion in enumerate(assertions):
        next_assertion = assertions[index + 1] if index + 1 < len(assertions) else None
        if (
            next_assertion
            and next_assertion.assertion_start == assertion.assertion_start
        ):
            continue
        end = next_assertion.claim.start if next_assertion else len(text)
        tail = text[assertion.claim.end : end]
        boundary = _CLAUSE_BOUNDARY.search(tail)
        if boundary:
            tail = tail[: boundary.start()]
        elif next_assertion:
            # This entire gap was checked as the next explicit assertion head.
            continue
        if assertion.role == "product_price" and not _PRODUCT_TAIL.fullmatch(tail):
            assertions[index] = replace(assertion, role="unresolved")
        elif assertion.role == "non_product" and _PRICE_MARKER.search(tail):
            # A following predicate cannot turn a fee into a product's price.
            assertions[index] = replace(assertion, role="unresolved")
    return tuple(assertions)


def _resolve_offers(
    text: str,
    offers: tuple[AuthoritativeCommercialOffer, ...],
) -> tuple[AuthoritativeCommercialOffer, ...] | None:
    by_id = {offer.sellable_item_id: offer for offer in offers}
    local_mentions = _identity_mentions(text, offers)
    if not local_mentions:
        return None

    boundaries = [0]
    boundaries.extend(
        boundary
        for match in _IDENTITY_COORDINATOR.finditer(text)
        if not any(
            mention.start <= match.start() and match.end() <= mention.end
            for mention in local_mentions
        )
        for boundary in (match.start(), match.end())
    )
    boundaries.append(len(text))

    resolved_ids: list[uuid.UUID] = []
    for index in range(0, len(boundaries) - 1, 2):
        relative_start = boundaries[index]
        relative_end = boundaries[index + 1]
        segment_start = relative_start
        segment_end = relative_end
        segment_mentions = [
            mention
            for mention in local_mentions
            if segment_start <= mention.start and mention.end <= segment_end
        ]
        if _has_unresolved_identity_text(
            text,
            segment_start,
            segment_end,
            segment_mentions,
        ):
            return None
        if not segment_mentions:
            continue
        candidates = set(segment_mentions[0].item_ids)
        for mention in segment_mentions[1:]:
            candidates.intersection_update(mention.item_ids)
        if len(candidates) != 1:
            return None
        resolved_ids.append(next(iter(candidates)))

    if not resolved_ids:
        return None
    unique_ids = tuple(dict.fromkeys(resolved_ids))
    if len(unique_ids) != len(resolved_ids):
        return None
    return tuple(by_id[item_id] for item_id in unique_ids)


def _has_unresolved_identity_text(
    text: str,
    start: int,
    end: int,
    mentions: Iterable[_IdentityMention],
) -> bool:
    residual = list(text[start:end])
    for mention in mentions:
        for index in range(max(start, mention.start), min(end, mention.end)):
            residual[index - start] = " "
    remaining = "".join(residual)
    remaining = _PRICE_MARKER.sub(" ", remaining)
    remaining = _IDENTITY_SCAFFOLD.sub(" ", remaining)
    return re.search(r"\w", remaining, re.UNICODE) is not None


def _claim_matches(
    offer: AuthoritativeCommercialOffer,
    claim: _MoneyClaim,
) -> bool:
    expected = (
        offer.current_usd_price
        if claim.currency == "USD"
        else offer.derived_cdf_price
        if claim.currency == "CDF"
        else None
    )
    return expected is not None and claim.amount == expected
