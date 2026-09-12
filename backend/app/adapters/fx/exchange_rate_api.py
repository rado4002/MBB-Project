"""Narrow ExchangeRate-API boundary for the USD to CDF observation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

EXCHANGE_RATE_API_SOURCE = "EXCHANGE_RATE_API"
EXCHANGE_RATE_API_BASE_URL = "https://v6.exchangerate-api.com/v6"
_MAX_RESPONSE_BYTES = 256_000


class ExchangeRateAPIError(Exception):
    """Safe provider-boundary failure with no response content attached."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExchangeRateAPIObservation:
    base_currency: str
    quote_currency: str
    rate: Decimal
    published_at: datetime
    source: str = EXCHANGE_RATE_API_SOURCE


class ExchangeRateAPIAdapter:
    """Fetch one standard USD response using documented bearer authentication."""

    def __init__(
        self,
        *,
        api_key: str,
        timeout_s: int = 10,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._timeout = httpx.Timeout(timeout_s)
        self._http_transport = http_transport

    async def fetch_usd_cdf(self) -> ExchangeRateAPIObservation:
        if not self._api_key:
            raise ExchangeRateAPIError("provider_not_configured")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._http_transport,
            ) as client:
                response = await client.get(
                    f"{EXCHANGE_RATE_API_BASE_URL}/latest/USD",
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._api_key}",
                    },
                )
                response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ExchangeRateAPIError("provider_timeout") from exc
        except httpx.HTTPStatusError as exc:
            raise ExchangeRateAPIError("provider_http_error") from exc
        except httpx.RequestError as exc:
            raise ExchangeRateAPIError("provider_unavailable") from exc

        if len(response.content) > _MAX_RESPONSE_BYTES:
            raise ExchangeRateAPIError("provider_response_too_large")
        try:
            payload = json.loads(
                response.content.decode("utf-8"),
                parse_float=Decimal,
                parse_int=int,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExchangeRateAPIError("provider_malformed_json") from exc
        return _parse_observation(payload)


def _parse_observation(payload: Any) -> ExchangeRateAPIObservation:
    if not isinstance(payload, dict):
        raise ExchangeRateAPIError("provider_invalid_shape")
    if payload.get("result") != "success":
        raise ExchangeRateAPIError("provider_rejected_request")
    if payload.get("base_code") != "USD":
        raise ExchangeRateAPIError("provider_base_mismatch")

    rates = payload.get("conversion_rates")
    if not isinstance(rates, dict) or "CDF" not in rates:
        raise ExchangeRateAPIError("provider_cdf_missing")
    rate = rates["CDF"]
    if isinstance(rate, bool) or not isinstance(rate, (Decimal, int, str)):
        raise ExchangeRateAPIError("provider_rate_invalid")
    try:
        normalized_rate = Decimal(rate)
    except Exception as exc:  # Decimal rejects malformed provider scalar types.
        raise ExchangeRateAPIError("provider_rate_invalid") from exc
    if not normalized_rate.is_finite() or normalized_rate <= 0:
        raise ExchangeRateAPIError("provider_rate_invalid")

    timestamp = payload.get("time_last_update_unix")
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ExchangeRateAPIError("provider_timestamp_invalid")
    try:
        published_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ExchangeRateAPIError("provider_timestamp_invalid") from exc
    return ExchangeRateAPIObservation(
        base_currency="USD",
        quote_currency="CDF",
        rate=normalized_rate,
        published_at=published_at,
    )
