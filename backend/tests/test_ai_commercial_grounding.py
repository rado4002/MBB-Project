import uuid
from dataclasses import replace
from decimal import Decimal
from itertools import permutations

import pytest

from app.ai.commercial_grounding import (
    COMMERCIAL_GROUNDING_FAILURE_CODE,
    COMMERCIAL_GROUNDING_VALIDATOR_VERSION,
    AuthoritativeCommercialOffer,
    CommercialGroundingError,
    offers_from_capability_output,
    validate_commercial_grounding,
)

PRODUCT_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")
P6_ID = uuid.UUID("60000000-0000-4000-8000-000000000006")
P8_ID = uuid.UUID("80000000-0000-4000-8000-000000000008")


@pytest.mark.parametrize(
    "response",
    (
        "6L coûte 55 USD et 8L coûte 55 USD.",
        "8L coûte 70 USD et 6L coûte 70 USD.",
        "6L coûte 55 USD et Blender X coûte 55 USD.",
        "6L coûte 55 USD et 154 000 FC et 8L coûte 55 USD et 154 000 FC.",
        "Budget 45 USD, Blender X à 55 USD.",
        "La livraison est de 12 USD, Blender X coûte 55 USD.",
        "Paiement 12 USD, Blender X coûte 55 USD.",
        "Frais 12 USD, Blender X coûte 55 USD.",
        "Taxe 12 USD, Blender X coûte 55 USD.",
        "Acompte 12 USD, Blender X coûte 55 USD.",
        "Acompte prévu, Blender X coûte 55 EUR.",
    ),
)
def test_claim_boundary_review_rejects_demonstrated_bypasses(response):
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "6L coûte 55 USD et 8L coûte 70 USD.",
        "6L coûte 55 USD et 154 000 FC et 8L coûte 70 USD et 196 000 FC.",
        "Mon budget est 45 USD, le modèle 6L coûte 55 USD.",
        "La livraison est de 12 USD, le 6L coûte 55 USD.",
    ),
)
def test_claim_boundary_review_accepts_valid_mixed_assertions(response):
    validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "Le MBB Test Air Fryer 6L est disponible.",
        "Le MBB Test Air Fryer 6L est disponible à $55.",
        "Le MBB Test Air Fryer 6L est disponible à 55 USD.",
        "Le MBB Test Air Fryer 6L est disponible à 55 dollars.",
        "Le modèle 6L est disponible, au prix de 55 USD.",
        "Le 6L est disponible et coûte 55 USD.",
        "MBB Test Air Fryer 6L : disponible, 55 USD.",
        "Le MBB Test Air Fryer 6L coûte 55 USD / 154 000 FC et est disponible.",
    ),
)
def test_c01_authoritative_natural_french_forms_remain_accepted(response):
    validate_commercial_grounding(response, (_offers()[0],))


@pytest.mark.parametrize(
    "response",
    (
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 55 USD "
        "(environ 154 000 CDF). Tu veux que je te dise ce qu'il y a comme "
        "autres modèles dans le même budget ?",
        "Oui, il est bien dispo 🔥 Le MBB Test Air Fryer 6L est à 55 USD "
        "(environ 154 000 CDF). Tu veux que je te dise ce qu'il y a comme "
        "autres modèles dans le même budget ?",
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 55 USD.",
    ),
)
def test_accepts_live_c01_explicit_product_price_forms(response):
    validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 70 USD "
        "(environ 154 000 CDF).",
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 55 USD "
        "(environ 196 000 CDF).",
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 70 USD "
        "(environ 196 000 CDF).",
        "Non, il n'est pas dispo  Le MBB Test Air Fryer 6L est à 55 USD "
        "(environ 154 000 CDF).",
        "Oui, il est bien dispo  Le MBB Test Air Fryer 8L est à 70 USD "
        "(environ 196 000 CDF).",
        "Oui, il est bien dispo  Le MBB Test Air Fryer est à 55 USD "
        "(environ 154 000 CDF).",
        "Oui, il est bien dispo  Le Blender X est à 55 USD (environ 154 000 CDF).",
        "Oui, il est bien dispo  Le MBB Test Air Fryer 6L est à 55 USD "
        "(environ 154 000 CDF). Cela représente aussi 12 USD.",
    ),
)
def test_live_c01_correction_remains_fail_closed(response):
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "Oui, le MBB Test Air Fryer 6L est disponible. Son prix est de 55 USD.",
        "Le MBB Test Air Fryer 6L est disponible. Il coûte 55 dollars.",
        "Le modèle 6L est disponible! Son prix actuel est $55.",
        "Le 6L est en stock. Son prix est de 55 USD / 154 000 FC.",
        "Le modèle 8L est en rupture de stock. Il coûte 70 USD.",
    ),
)
def test_accepts_unambiguous_immediate_local_product_price_reference(response):
    validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "Le modèle 6L est disponible. Il coûte 70 USD.",
        "Les modèles 6L et 8L sont disponibles. Il coûte 55 USD.",
        "Le MBB Test Air Fryer est disponible. Il coûte 55 USD.",
        "Le modèle 6L est disponible. Je peux vous aider. Il coûte 55 USD.",
        "Le Blender X est disponible. Son prix est de 55 USD.",
        "Le modèle 6L est disponible à 55 USD. Il coûte 55 USD.",
        "Le modèle 6L est indisponible. Son prix est de 55 USD.",
        "Le modèle 6L n'est pas disponible. Son prix est de 55 USD.",
        "Le modèle 8L est disponible. Il coûte 70 USD.",
    ),
)
def test_rejects_unsafe_or_nonlocal_product_price_reference(response):
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())


def test_rejection_diagnostic_distinguishes_association_from_price_mismatch():
    detached = (
        "Oui, le MBB Test Air Fryer 6L est disponible. "
        "Je peux vous aider. Son prix est de 55 USD."
    )
    with pytest.raises(CommercialGroundingError) as unresolved:
        validate_commercial_grounding(detached, (_offers()[0],))
    unresolved_evidence = unresolved.value.diagnostic.evidence()
    assert unresolved_evidence == {
        "validator_version": COMMERCIAL_GROUNDING_VALIDATOR_VERSION,
        "failure_category": "product_association_unresolved",
        "assertion_role": "unresolved",
        "claim_currency": "USD",
        "claim_amount": "55",
        "assertion_start": detached.index("Son prix"),
        "claim_start": detached.index("55 USD"),
        "claim_end": detached.index("55 USD") + len("55 USD"),
        "resolved_offers": [],
    }
    assert str(unresolved.value) == COMMERCIAL_GROUNDING_FAILURE_CODE
    assert detached not in str(unresolved.value)

    with pytest.raises(CommercialGroundingError) as mismatched:
        validate_commercial_grounding(
            "Le modèle 6L est disponible. Il coûte 70 USD.",
            (_offers()[0],),
        )
    mismatch_evidence = mismatched.value.diagnostic.evidence()
    assert mismatch_evidence["failure_category"] == "authoritative_price_mismatch"
    assert mismatch_evidence["claim_amount"] == "70"
    assert mismatch_evidence["resolved_offers"] == [
        {"sellable_item_id": str(P6_ID), "expected_amount": "55.00"}
    ]


def _offers() -> tuple[AuthoritativeCommercialOffer, ...]:
    return (
        AuthoritativeCommercialOffer(
            product_id=PRODUCT_ID,
            sellable_item_id=P6_ID,
            name="MBB Test Air Fryer",
            model_label="6L",
            sku="MBB-AF-6L",
            current_usd_price=Decimal("55.00"),
            derived_cdf_price=Decimal("154000.00"),
            availability="available",
            is_sellable_now=True,
        ),
        AuthoritativeCommercialOffer(
            product_id=PRODUCT_ID,
            sellable_item_id=P8_ID,
            name="MBB Test Air Fryer",
            model_label="8L",
            sku="MBB-AF-8L",
            current_usd_price=Decimal("70.00"),
            derived_cdf_price=Decimal("196000.00"),
            availability="out_of_stock",
            is_sellable_now=False,
        ),
    )


def test_corrected_validator_version_is_explicit():
    assert (
        COMMERCIAL_GROUNDING_VALIDATOR_VERSION
        == "mbb-commercial-grounding-validator-v3"
    )


@pytest.mark.parametrize(
    "response",
    (
        "Le modèle 6L coûte $55 et 154 000 FC.",
        "Le modèle 6L coûte 55 $ et CDF 154000.",
        "Le modèle 6L coûte 55 USD et 154,000 CDF.",
        "Le modèle 6L coûte 55 $ US et 154 000 FC.",
        "Le modèle 8L coûte 70 USD et 196 000 FC.",
    ),
)
def test_accepts_supported_dual_currency_formats(response):
    validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "Le 6L ezali na 55 USD.",
        "Bei ya 8L ni 196 000 FC.",
        "Pour le 6L, ni 154000 CDF.",
        "8L ni $70.",
    ),
)
def test_accepts_single_currency_claims_across_supported_languages(response):
    validate_commercial_grounding(response, _offers())


def test_accepts_correct_multi_product_comparison():
    validate_commercial_grounding(
        "6L: 55 USD / 154 000 FC; 8L: 70 USD / 196 000 FC.",
        _offers(),
    )


@pytest.mark.parametrize(
    "response",
    (
        "Les modèles 8L et 6L coûtent 55 USD.",
        "Les modèles 6L et 8L coûtent 55 USD.",
        "Blender X coûte 55 USD.",
        "Blender X et 6L coûtent 55 USD.",
        "MBB Test Air Fryer coûte 55 USD.",
        "AF Mini coûte 55 USD.",
        "Le modèle 8L est populaire. Il coûte 55 USD.",
    ),
)
def test_rejects_unresolved_ambiguous_or_detached_identity_price_claims(response):
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())


def test_accepts_shared_price_only_when_every_explicit_identity_matches():
    shared_price = (
        _offers()[0],
        AuthoritativeCommercialOffer(
            product_id=PRODUCT_ID,
            sellable_item_id=P8_ID,
            name="MBB Test Air Fryer",
            model_label="8L",
            sku="MBB-AF-8L",
            current_usd_price=Decimal("55.00"),
            derived_cdf_price=Decimal("154000.00"),
        ),
    )

    validate_commercial_grounding(
        "Les modèles 8L et 6L coûtent 55 USD et 154 000 FC.",
        shared_price,
    )
    validate_commercial_grounding(
        "Les modèles 6L et 8L coûtent 55 USD et 154 000 FC.",
        shared_price,
    )


@pytest.mark.parametrize(
    "response",
    (
        "Le modèle 6L coûte 55 USD et 196 000 FC.",
        "6L: 55 USD / 196 000 FC; 8L: 70 USD / 154 000 FC.",
        "MBB Test Air Fryer coûte 60 USD.",
        "Le 6L coûte 55 EUR.",
    ),
)
def test_rejects_crossed_ambiguous_or_unsupported_claims(response):
    with pytest.raises(CommercialGroundingError) as captured:
        validate_commercial_grounding(response, _offers())
    assert captured.value.safe_code == COMMERCIAL_GROUNDING_FAILURE_CODE
    assert captured.value.validator_version == COMMERCIAL_GROUNDING_VALIDATOR_VERSION


def test_unidentified_price_never_resolves_from_amount_alone():
    duplicated_price = (
        _offers()[0],
        AuthoritativeCommercialOffer(
            product_id=PRODUCT_ID,
            sellable_item_id=P8_ID,
            name="MBB Test Air Fryer",
            model_label="8L",
            current_usd_price=Decimal("55.00"),
            derived_cdf_price=Decimal("196000.00"),
        ),
    )
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding("Le prix actuel est 154 000 FC.", _offers())
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding("Le prix actuel est 55 USD.", duplicated_price)


def test_non_price_numbers_and_customer_budget_are_not_product_price_claims():
    validate_commercial_grounding(
        "Pour 2 personnes, le 6L convient. Na budget ya 45 dollars, "
        "option moins chère ezali te.",
        _offers(),
    )


@pytest.mark.parametrize(
    "response",
    (
        "La livraison coûte 55 USD.",
        "La livraison coûte 12 USD.",
        "Payment received: 55 USD.",
        "Payment received: 12 USD.",
    ),
)
def test_non_product_money_is_exempt_regardless_of_catalog_price(response):
    validate_commercial_grounding(response, _offers())


def test_response_without_explicit_product_price_claim_is_unchanged():
    response = "Le modèle 8L est en rupture; je peux montrer le modèle 6L."
    validate_commercial_grounding(response, _offers())


def test_extracts_only_nested_authoritative_product_offer_records():
    output = {
        "items": [
            {
                "product_id": str(PRODUCT_ID),
                "sellable_item_id": str(P6_ID),
                "name": "MBB Test Air Fryer",
                "model_label": "6L",
                "current_usd_price": "55.00",
                "derived_cdf_quote": {"currency": "CDF", "amount": "154000.00"},
            }
        ]
    }

    extracted = offers_from_capability_output("search_products", output)
    assert len(extracted) == 1
    assert extracted[0].sellable_item_id == P6_ID
    assert extracted[0].current_usd_price == Decimal("55.00")
    assert extracted[0].derived_cdf_price == Decimal("154000.00")
    assert extracted[0].sku is None
    assert offers_from_capability_output("request_human_handoff", output) == ()


# Expectations come from these fixture records and the explicit subjects/amounts
# used to render each input, never from the guard's parsing or matching helpers.
_ASSERTION_FORMS = (
    "Le modèle {model} coûte {money}",
    "Ntalo ya {model} ezali {money}",
    "Bei ya {model} ni {money}",
    "Pour le {model}, bei ni {money}",
)
_SEPARATORS = (" et ", ", ", "; ", "\n")
_EXEMPTION_MARKERS = (
    "budget",
    "bajeti",
    "maximum",
    "max",
    "plafond",
    "moins de",
    "jusqu'a",
    "jusqu’à",
    "under",
    "up to",
    "livraison",
    "transport",
    "expedition",
    "expédition",
    "delivery",
    "shipping",
    "paiement",
    "payment",
    "acompte",
    "deposit",
    "versement",
    "taxe",
    "tax",
    "frais",
    "fee",
    "fees",
)


def _assert_grounding_outcome(response, offers, valid):
    if valid:
        validate_commercial_grounding(response, offers)
    else:
        with pytest.raises(CommercialGroundingError):
            validate_commercial_grounding(response, offers)


@pytest.mark.parametrize("form", _ASSERTION_FORMS)
@pytest.mark.parametrize("separator", _SEPARATORS)
@pytest.mark.parametrize("reverse", (False, True))
@pytest.mark.parametrize("currencies", (("USD",), ("CDF",), ("USD", "CDF")))
@pytest.mark.parametrize(
    "variation",
    (
        "correct",
        "wrong_first",
        "wrong_last",
        "unknown_first",
        "unknown_middle",
        "unknown_last",
    ),
)
def test_independent_assertion_matrix(form, separator, reverse, currencies, variation):
    offers = _offers()
    ordered = list(reversed(offers)) if reverse else list(offers)
    if variation == "unknown_middle":
        ordered.insert(1, ordered[0])
    assertions = []
    valid = True
    for index, offer in enumerate(ordered):
        model = offer.model_label
        amounts = {"USD": offer.current_usd_price, "CDF": offer.derived_cdf_price}
        target = (
            (variation.endswith("first") and index == 0)
            or (variation.endswith("last") and index == len(ordered) - 1)
            or (variation.endswith("middle") and index == 1)
        )
        if target:
            if variation.startswith("unknown"):
                model = "Blender X"
            else:
                other = next(item for item in offers if item != offer)
                amounts = {
                    "USD": other.current_usd_price,
                    "CDF": other.derived_cdf_price,
                }
        authoritative = next(
            (item for item in offers if item.model_label == model), None
        )
        valid = (
            valid
            and authoritative is not None
            and all(
                amounts[currency]
                == (
                    authoritative.current_usd_price
                    if currency == "USD"
                    else authoritative.derived_cdf_price
                )
                for currency in currencies
            )
        )
        money = " et ".join(
            f"{amounts[currency]} {currency}" for currency in currencies
        )
        assertions.append(form.format(model=model, money=money))
    _assert_grounding_outcome(separator.join(assertions), offers, valid)


@pytest.mark.parametrize("marker", _EXEMPTION_MARKERS)
@pytest.mark.parametrize("amount", ("12", "55"))
@pytest.mark.parametrize("separator", _SEPARATORS)
@pytest.mark.parametrize("exemption_first", (False, True))
@pytest.mark.parametrize(
    ("product", "valid"),
    (
        ("6L coûte 55 USD", True),
        ("8L coûte 55 USD", False),
        ("Blender X coûte 55 USD", False),
    ),
)
def test_amount_specific_exemption_matrix(
    marker, amount, separator, exemption_first, product, valid
):
    exemption = f"{marker} {amount} USD"
    parts = (exemption, product) if exemption_first else (product, exemption)
    _assert_grounding_outcome(separator.join(parts), _offers(), valid)


@pytest.mark.parametrize("marker", _EXEMPTION_MARKERS)
@pytest.mark.parametrize("subject", ("6L", "8L", "Blender X"))
@pytest.mark.parametrize("currency", ("USD", "EUR"))
def test_unconsumed_marker_cannot_govern_a_later_product(marker, subject, currency):
    _assert_grounding_outcome(
        f"{marker} prévu, {subject} coûte 55 {currency}", _offers(), False
    )


@pytest.mark.parametrize("separator", _SEPARATORS)
def test_appending_false_assertion_and_permuting_independent_assertions(separator):
    valid = ("6L coûte 55 USD", "8L coûte 70 USD", "Budget 45 USD")
    for order in permutations(valid):
        validate_commercial_grounding(separator.join(order), _offers())
        for false in ("8L coûte 55 USD", "6L coûte 70 USD", "Blender X coûte 55 USD"):
            with pytest.raises(CommercialGroundingError):
                validate_commercial_grounding(
                    separator.join((*order, false)), _offers()
                )


@pytest.mark.parametrize("unrelated_usd", ("12", "55", "70", "100"))
def test_unrelated_offer_mutation_cannot_authorize_unknown_identity(unrelated_usd):
    offers = (
        _offers()[0],
        replace(_offers()[1], current_usd_price=Decimal(unrelated_usd)),
    )
    for response in (
        "Blender X coûte 55 USD",
        "6L coûte 55 USD et Blender X coûte 55 USD",
        "Budget 45 USD, Blender X à 55 USD",
        "Payment received: 12 USD, Blender X coûte 55 USD",
    ):
        with pytest.raises(CommercialGroundingError):
            validate_commercial_grounding(response, offers)


@pytest.mark.parametrize("separator", (" et ", ", ", " na ", " pamoja na "))
@pytest.mark.parametrize("reverse", (False, True))
def test_shared_list_checks_all_named_items_and_both_currencies(separator, reverse):
    offers = (
        _offers()[0],
        replace(
            _offers()[1],
            current_usd_price=Decimal("55"),
            derived_cdf_price=Decimal("154000"),
        ),
    )
    models = ("8L", "6L") if reverse else ("6L", "8L")
    response = f"Les modèles {separator.join(models)} coûtent 55 USD et 154 000 FC"
    validate_commercial_grounding(response, offers)
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(
            response.replace(models[0], "Blender X", 1), offers
        )


@pytest.mark.parametrize(
    "response",
    (
        "Le prix actuel est 55 USD",
        "AF Mini coûte 55 USD",
        "MBB Test Air Fryer coûte 55 USD",
        "6L ou 8L coûte 55 USD",
        "8L est disponible. Il coûte 70 USD",
        "6L coûte 55 USD; il coûte 154 000 FC",
        "6L coûte 55 USD et Blender X à 154000 FC",
        "6L coûte 55 USD et 196000 FC",
        "6L coûte -55 USD",
        "6L coûte 55 USD et 55 EUR",
        "Budget 45 USD et 55 USD",
        "55 USD budget 45 USD",
        "55 USD maximum 70 USD",
        "6L coûte 154,000,00 CDF",
        "Livraison 12 USD, prix 55 USD",
        "6L: 55 USD et 154000 CDF pour 8L.",
        "6L: 55 USD et Blender X aussi.",
        "6L: 55 USD pour Blender X; 8L: 70 USD.",
        "Livraison 55 USD est le prix du Blender X.",
        "6L: 55USD, 8L: 55USD.",
        "Shipping prévu, Blender X: EUR55.",
    ),
)
def test_ambiguous_or_false_continuations_stay_rejected(response):
    with pytest.raises(CommercialGroundingError):
        validate_commercial_grounding(response, _offers())


@pytest.mark.parametrize(
    "response",
    (
        "6L coûte 55.00 USD et 154,000.00 CDF, 8L coûte 70,00 USD et 196000 CDF",
        "Mon budget est 45 USD et Ntalo ya 6L ezali 55 USD",
        "Bajeti yangu ni 45 USD, Bei ya 8L ni 70 USD",
        "Na budget ya 45 USD, Pour le 6L, bei ni 55 USD",
        "Je garde ton budget de 50 USD",
        "45 USD de budget; le 6L coûte 55 USD",
        "La livraison coûte 55 USD",
        "Payment received: 12 USD",
        "6L: USD55; 8L: USD70.",
        "6 L: USD55, soit 154000CDF.",
        "6L: 55 USD (154000 CDF); 8L: 70 USD (196000 CDF).",
    ),
)
def test_bounded_natural_forms_remain_accepted(response):
    validate_commercial_grounding(response, _offers())
