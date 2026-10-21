"""Offline approval gate for Administrator automatic FX operations."""

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.adapters.fx.exchange_rate_api import ExchangeRateAPIError, ExchangeRateAPIObservation
from app.api.browser_auth_errors import BrowserAuthError
from app.api.v1.commerce_admin import _require_automatic_fx_configuration
from app.config import Settings
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.pricing import service as pricing_service


@pytest.mark.parametrize(
    ("enabled", "key"),
    [(False, "offline-test-only"), (True, ""), (True, "   ")],
)
def test_automatic_fx_requires_explicit_gate_and_key(enabled: bool, key: str) -> None:
    settings = Settings(automatic_fx_refresh_enabled=enabled, exchange_rate_api_key=key)
    with pytest.raises(BrowserAuthError) as exc:
        _require_automatic_fx_configuration(settings)
    assert exc.value.status_code == 409


def test_automatic_fx_configuration_only_authorizes_offline_adapter_use() -> None:
    settings = Settings(automatic_fx_refresh_enabled=True, exchange_rate_api_key="offline-test-only")
    _require_automatic_fx_configuration(settings)


@pytest.mark.asyncio
async def test_stale_or_failed_provider_cannot_write_fx(monkeypatch: pytest.MonkeyPatch) -> None:
    session = AsyncMock()
    monkeypatch.setattr(pricing_service, "require_commerce_administrator", AsyncMock(return_value=object()))
    admin = CommerceAdminContext(uuid.uuid4(), "offline-fx-test")
    now = datetime.now(timezone.utc)
    stale = AsyncMock()
    stale.fetch_usd_cdf.return_value = ExchangeRateAPIObservation(
        base_currency="USD", quote_currency="CDF", rate=Decimal("2800.000000"),
        published_at=now - timedelta(hours=37),
    )
    with pytest.raises(pricing_service.AutomaticExchangeRateRejected, match="provider_data_stale"):
        await pricing_service.refresh_automatic_exchange_rate(
            session, provider=stale, administrator=admin, now=now,
        )
    failed = AsyncMock()
    failed.fetch_usd_cdf.side_effect = ExchangeRateAPIError("provider_unavailable")
    with pytest.raises(ExchangeRateAPIError):
        await pricing_service.refresh_automatic_exchange_rate(
            session, provider=failed, administrator=admin, now=now,
        )
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()
