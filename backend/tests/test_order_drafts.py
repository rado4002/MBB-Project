from __future__ import annotations

import uuid
import inspect
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    PrepareOrderDraftInput,
)
from app.modules.m7_conversion.order_drafts import (
    parse_order_draft_reply,
    render_order_draft_confirmation,
)
from app.schemas.product_offer import (
    DerivedCdfQuoteResponse,
    ProductOfferResponse,
)


def _offer() -> ProductOfferResponse:
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    return ProductOfferResponse(
        product_id=uuid.uuid4(),
        sellable_item_id=uuid.uuid4(),
        sku="AI6B-AF-6L",
        product_name="MBB Test Air Fryer",
        category_code="air_fryer",
        description="Synthetic test offer.",
        model_label="6L",
        attributes={"capacity_l": 6},
        primary_media=None,
        price_id=uuid.uuid4(),
        current_usd_price=Decimal("55.00"),
        price_effective_at=now,
        cdf_quote_status="available",
        cdf_quote_unavailable_reason=None,
        derived_cdf_quote=DerivedCdfQuoteResponse(
            cdf_amount=Decimal("154000.00"),
            exchange_rate_id=uuid.uuid4(),
            usd_to_cdf_rate=Decimal("2800.000000"),
            exchange_rate_effective_at=now,
        ),
        inventory_status="available",
        inventory_configured=True,
        inventory_updated_at=now,
        offer_status="sellable_now",
        is_sellable_now=True,
        reason_code="sellable_now",
        read_at=now,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("OUI A1B2C3D4", ("confirm", "A1B2C3D4")),
        ("iyo a1b2c3d4", ("confirm", "A1B2C3D4")),
        ("NDIYO A1B2C3D4.", ("confirm", "A1B2C3D4")),
        ("NON A1B2C3D4", ("cancel", "A1B2C3D4")),
        ("TE A1B2C3D4", ("cancel", "A1B2C3D4")),
        ("HAPANA A1B2C3D4", ("cancel", "A1B2C3D4")),
        ("oui", None),
        ("oui A1B2C3D4 autre chose", None),
        ("ok A1B2C3D4", None),
        ("oui ZZZZZZZZ", None),
    ],
)
def test_reply_parser_requires_an_exact_action_and_version_code(text, expected):
    assert parse_order_draft_reply(text) == expected


def test_confirmation_message_uses_only_offer_values_and_declares_no_order():
    text = render_order_draft_confirmation(
        offer=_offer(),
        quantity=2,
        total_cdf=Decimal("308000.00"),
        confirmation_code="A1B2C3D4",
        language="french",
    )
    assert "MBB Test Air Fryer 6L" in text
    assert "308 000 CDF" in text
    assert "55.00 USD" in text
    assert "OUI A1B2C3D4" in text
    assert "Aucune commande ni aucun paiement n'est encore créé" in text


def test_prepare_capability_has_no_model_price_field():
    definition = AI_CAPABILITY_REGISTRY.resolve("prepare_order_draft")
    assert definition is not None and definition.terminal_on_success is True
    assert set(definition.input_model.model_fields) == {
        "selected_sellable_item_id",
        "quantity",
    }
    with pytest.raises(ValidationError):
        PrepareOrderDraftInput.model_validate(
            {
                "selected_sellable_item_id": str(uuid.uuid4()),
                "quantity": 1,
                "unit_price_cdf": "1.00",
            },
            strict=True,
        )


def test_order_draft_domain_has_no_consequential_m7_or_external_call():
    import app.modules.m7_conversion.order_drafts as module

    source = inspect.getsource(module)
    for forbidden in (
        "create_order(",
        "Payment(",
        "initiate_payment(",
        "sync_order_to_crm(",
        "set_inventory_status(",
    ):
        assert forbidden not in source
