from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.adapters.fx.exchange_rate_api import (
    EXCHANGE_RATE_API_BASE_URL,
    ExchangeRateAPIAdapter,
    ExchangeRateAPIError,
)


def _success_payload(**changes):
    payload = {
        "result": "success",
        "time_last_update_unix": 1789156800,
        "base_code": "USD",
        "conversion_rates": {"USD": 1, "CDF": 2262.5},
    }
    payload.update(changes)
    return payload


@pytest.mark.asyncio
async def test_keyed_standard_response_is_normalized_without_key_in_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_success_payload())

    adapter = ExchangeRateAPIAdapter(
        api_key="test-secret-key",
        http_transport=httpx.MockTransport(handler),
    )
    observation = await adapter.fetch_usd_cdf()

    assert observation.base_currency == "USD"
    assert observation.quote_currency == "CDF"
    assert observation.rate == Decimal("2262.5")
    assert observation.published_at == datetime.fromtimestamp(
        1789156800, tz=timezone.utc
    )
    assert str(seen[0].url) == f"{EXCHANGE_RATE_API_BASE_URL}/latest/USD"
    assert seen[0].headers["Authorization"] == "Bearer test-secret-key"
    assert "test-secret-key" not in str(seen[0].url)


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"base_code": "EUR"}, "provider_base_mismatch"),
        ({"conversion_rates": {"USD": 1}}, "provider_cdf_missing"),
        ({"conversion_rates": {"CDF": 0}}, "provider_rate_invalid"),
        ({"conversion_rates": {"CDF": "not-a-rate"}}, "provider_rate_invalid"),
        ({"time_last_update_unix": None}, "provider_timestamp_invalid"),
        (
            {"result": "error", "error-type": "quota-reached"},
            "provider_rejected_request",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_provider_payloads_fail_closed(changes, code) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_payload(**changes))

    adapter = ExchangeRateAPIAdapter(
        api_key="test-key",
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ExchangeRateAPIError) as exc_info:
        await adapter.fetch_usd_cdf()
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_malformed_json_and_http_failure_fail_closed() -> None:
    malformed = ExchangeRateAPIAdapter(
        api_key="test-key",
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"{truncated")
        ),
    )
    with pytest.raises(ExchangeRateAPIError) as malformed_error:
        await malformed.fetch_usd_cdf()
    assert malformed_error.value.code == "provider_malformed_json"

    unavailable = ExchangeRateAPIAdapter(
        api_key="test-key",
        http_transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"result": "error"})
        ),
    )
    with pytest.raises(ExchangeRateAPIError) as unavailable_error:
        await unavailable.fetch_usd_cdf()
    assert unavailable_error.value.code == "provider_http_error"


@pytest.mark.asyncio
async def test_missing_key_stops_before_network_dispatch() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=json.dumps(_success_payload()).encode())

    adapter = ExchangeRateAPIAdapter(
        api_key="",
        http_transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ExchangeRateAPIError) as exc_info:
        await adapter.fetch_usd_cdf()
    assert exc_info.value.code == "provider_not_configured"
    assert calls == 0
