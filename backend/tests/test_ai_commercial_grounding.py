import uuid
from decimal import Decimal

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
        ),
        AuthoritativeCommercialOffer(
            product_id=PRODUCT_ID,
            sellable_item_id=P8_ID,
            name="MBB Test Air Fryer",
            model_label="8L",
            sku="MBB-AF-8L",
            current_usd_price=Decimal("70.00"),
            derived_cdf_price=Decimal("196000.00"),
        ),
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


def test_unidentified_price_is_accepted_only_when_one_offer_matches():
    validate_commercial_grounding("Le prix actuel est 154 000 FC.", _offers())

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
        validate_commercial_grounding("Le prix actuel est 55 USD.", duplicated_price)


def test_non_price_numbers_and_customer_budget_are_not_product_price_claims():
    validate_commercial_grounding(
        "Pour 2 personnes, le 6L convient. Na budget ya 45 dollars, "
        "option moins chère ezali te.",
        _offers(),
    )


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
