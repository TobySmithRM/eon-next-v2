"""Historic prices, used to cost imported consumption.

The account's tariff agreements say which tariff applied when.  E.ON's public
products API lists every unit rate and standing charge each tariff has had,
separately for each payment method.  Together they give the price of any past
hour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .eonnext import (
    STANDING_CHARGES,
    UNIT_RATES,
    EonNextAuthError,
    EonNextNotFoundError,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_REFRESH_AFTER = timedelta(hours=6)
# Pay-by-bill rates are about 5% above Direct Debit ones, so a match has to be
# much closer than that to tell them apart.
PAYMENT_METHOD_TOLERANCE = 0.02


@dataclass(frozen=True, slots=True)
class PricePeriod:
    """A price in pounds, including VAT, from ``start`` until ``end``."""

    start: datetime
    end: datetime | None
    price: float


@dataclass(frozen=True, slots=True)
class Prices:
    """A meter point's unit rates (GBP/kWh) and standing charges (GBP/day)."""

    unit: list[PricePeriod]
    standing: list[PricePeriod]


def tariff_product_code(tariff_code: str) -> str | None:
    """Return the product code of a priceable tariff code, else None.

    Tariff codes look like ``E-1R-<product>-<region>`` (single rate) or
    ``E-TOU-<product>-<region>`` (single-register time-of-use, e.g. NEXT
    Drive).  Both publish a full rate history - including, for time-of-use,
    the per-band peak/off-peak rates - so both are priced.  Two-rate (``2R``)
    Economy-7 tariffs have separate day/night registers and register-split
    consumption we cannot reconstruct, so they are not priced here.
    """
    parts = tariff_code.split("-")
    if len(parts) < 4 or parts[0] not in ("E", "G") or parts[1] not in ("1R", "TOU"):
        return None
    return "-".join(parts[2:-1])


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(str(value))
    return dt_util.as_utc(parsed) if parsed else None


def _covers(start: datetime | None, end: datetime | None, when: datetime) -> bool:
    return start is not None and start <= when and (end is None or when < end)


def _tariff_code(agreement: dict[str, Any]) -> str:
    return str((agreement.get("tariff") or {}).get("tariffCode") or "")


def _payment_method(
    rates: list[dict[str, Any]], unit_rate: Any, now: datetime
) -> str | None:
    """Pick the payment method whose current rate matches the account's.

    The account's own tariff reports today's unit rate but not how the account
    pays, and pay-by-bill rates are higher than Direct Debit ones.
    """
    try:
        target = float(unit_rate)
    except (TypeError, ValueError):
        return None
    current = [
        rate
        for rate in rates
        if rate.get("payment_method")
        and _covers(_parse(rate.get("valid_from")), _parse(rate.get("valid_to")), now)
    ]
    if not current:
        return None
    closest = min(current, key=lambda rate: abs(float(rate["value_inc_vat"]) - target))
    if abs(float(closest["value_inc_vat"]) - target) > target * PAYMENT_METHOD_TOLERANCE:
        _LOGGER.debug(
            "No published rate matches the account's unit rate %s; not costing", target
        )
        return None
    return closest["payment_method"]


def build_prices(
    agreements: list[dict[str, Any]],
    unit_rates_by_tariff: dict[str, list[dict[str, Any]]],
    standing_charges_by_tariff: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> Prices:
    """Combine a meter point's agreements with its tariffs' price history."""
    active = next(
        (
            agreement
            for agreement in agreements
            if _covers(
                _parse(agreement.get("validFrom")), _parse(agreement.get("validTo")), now
            )
        ),
        None,
    )
    if active is None:
        return Prices([], [])
    method = _payment_method(
        unit_rates_by_tariff.get(_tariff_code(active), []),
        (active.get("tariff") or {}).get("unitRate"),
        now,
    )
    unit = _periods(agreements, unit_rates_by_tariff, method)
    if not unit:
        return Prices([], [])
    return Prices(unit, _periods(agreements, standing_charges_by_tariff, method))


def _periods(
    agreements: list[dict[str, Any]],
    rates_by_tariff: dict[str, list[dict[str, Any]]],
    method: str | None,
) -> list[PricePeriod]:
    """Clip each tariff's rates to its agreements, in pounds including VAT.

    Time-of-use tariffs come back as many short dated bands (e.g. an off-peak
    00:00-06:00 and a peak 06:00-24:00 each day); they are kept as-is, and
    :func:`price_at` resolves the band in force at any instant.
    """
    periods: list[PricePeriod] = []
    for agreement in agreements:
        rates = rates_by_tariff.get(_tariff_code(agreement))
        agreed_from = _parse(agreement.get("validFrom"))
        if not rates or agreed_from is None:
            continue
        agreed_to = _parse(agreement.get("validTo"))
        for rate in rates:
            if rate.get("payment_method") not in (method, None):
                continue
            start = _parse(rate.get("valid_from"))
            if start is None:
                continue
            end = _parse(rate.get("valid_to"))
            start = max(start, agreed_from)
            if agreed_to is not None:
                end = agreed_to if end is None else min(end, agreed_to)
            if end is not None and end <= start:
                continue
            pounds = round(float(rate["value_inc_vat"]) / 100, 7)
            periods.append(PricePeriod(start, end, pounds))
    return sorted(periods, key=lambda period: period.start)


def price_at(periods: list[PricePeriod], when: datetime) -> float | None:
    """The price in force at ``when``, or None if no period covers it."""
    for period in periods:
        if _covers(period.start, period.end, when):
            return period.price
    return None


class PriceHistory:
    """Fetches and caches each meter point's prices."""

    def __init__(self, api: Any, refresh_after: timedelta = DEFAULT_REFRESH_AFTER):
        self._api = api
        self._refresh_after = refresh_after
        self._cache: dict[str, tuple[datetime, Prices]] = {}

    async def async_prices(self, meter: Any) -> Prices:
        """Return the meter's prices, refreshing them when stale."""
        key = meter.supply_point_id
        now = dt_util.utcnow()
        cached = self._cache.get(key)
        if cached and now - cached[0] < self._refresh_after:
            return cached[1]
        try:
            prices = await self._async_fetch(meter, now)
        except EonNextAuthError:
            raise
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Price history unavailable for %s: %s", key, err)
            return cached[1] if cached else Prices([], [])
        self._cache[key] = (now, prices)
        return prices

    async def _async_fetch(self, meter: Any, now: datetime) -> Prices:
        by_point = await self._api.async_get_tariff_agreements(
            meter.account.account_number
        )
        agreements = (by_point or {}).get(meter.supply_point_id, [])
        unit_rates: dict[str, list[dict[str, Any]]] = {}
        standing_charges: dict[str, list[dict[str, Any]]] = {}
        for agreement in agreements:
            code = _tariff_code(agreement)
            if not code or code in unit_rates or not tariff_product_code(code):
                continue
            unit_rates[code] = await self._async_rates(code, UNIT_RATES)
            standing_charges[code] = await self._async_rates(code, STANDING_CHARGES)
        return build_prices(agreements, unit_rates, standing_charges, now)

    async def _async_rates(self, code: str, kind: str) -> list[dict[str, Any]]:
        try:
            return await self._api.async_get_tariff_rates(code, kind)
        except EonNextNotFoundError as err:
            # A retired product can be gone from the API; price the rest.  Any
            # other error fails the whole fetch, so half a price list is never
            # used to reprice history.
            _LOGGER.debug("No %s for tariff %s: %s", kind, code, err)
            return []
