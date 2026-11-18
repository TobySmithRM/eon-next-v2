"""Tests for building historic prices from tariff agreements."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.eon_next.eonnext import (
    STANDING_CHARGES,
    UNIT_RATES,
    EonNextApiError,
    EonNextAuthError,
    EonNextNotFoundError,
)

from custom_components.eon_next.unit_prices import (
    PriceHistory,
    PricePeriod,
    Prices,
    build_prices,
    price_at,
    tariff_product_code,
)

# The functions under test take ``now`` as an argument, and the cache tests
# use rates with no end date, so these fixed dates never go stale.
_NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)
_TARIFF = "E-1R-SAINSBURYS_STANDARD-C"


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _rate(start: str, end: str | None, inc_vat: float, method: str | None) -> dict:
    return {
        "valid_from": start,
        "valid_to": end,
        "value_inc_vat": inc_vat,
        "payment_method": method,
    }


_RATES = [
    _rate("2026-09-30T23:00:00Z", None, 28.089, "NON_DIRECT_DEBIT"),
    _rate("2026-09-30T23:00:00Z", None, 26.595, "DIRECT_DEBIT"),
    _rate("2026-06-30T23:00:00Z", "2026-09-30T23:00:00Z", 27.811, "NON_DIRECT_DEBIT"),
    _rate("2026-06-30T23:00:00Z", "2026-09-30T23:00:00Z", 26.347, "DIRECT_DEBIT"),
]

_STANDING = [
    _rate("2026-09-30T23:00:00Z", None, 50.509, "NON_DIRECT_DEBIT"),
    _rate("2026-09-30T23:00:00Z", None, 42.915, "DIRECT_DEBIT"),
]


def _unit_periods(agreements: list[dict], rates: list[dict]) -> list[PricePeriod]:
    return build_prices(agreements, {_TARIFF: rates}, {}, _NOW).unit


def _agreement(start: str, end: str | None, code: str = _TARIFF, rate=26.595) -> dict:
    return {
        "validFrom": start,
        "validTo": end,
        "tariff": {"tariffCode": code, "unitRate": rate},
    }


@pytest.mark.parametrize(
    ("code", "product"),
    [
        ("E-1R-SAINSBURYS_STANDARD-C", "SAINSBURYS_STANDARD"),
        ("G-1R-VAR-22-11-01-C", "VAR-22-11-01"),
        ("E-TOU-NEXT_DRIVE_FIXED_V24-H", "NEXT_DRIVE_FIXED_V24"),
        ("E-2R-SAINSBURYS_STANDARD-C", None),
        ("nonsense", None),
    ],
)
def test_product_code_recognised_for_single_rate_and_time_of_use(code, product) -> None:
    assert tariff_product_code(code) == product


@pytest.mark.parametrize(
    ("account_rate", "prices"),
    [(26.595, [0.26347, 0.26595]), (28.089, [0.27811, 0.28089])],
    ids=["direct-debit", "pay-on-bill"],
)
def test_prices_follow_the_payment_method_the_account_is_charged(
    account_rate: float, prices: list[float]
) -> None:
    periods = _unit_periods(
        [_agreement("2023-03-31T23:00:00+00:00", None, rate=account_rate)], _RATES
    )

    assert periods[0].start == _utc(2026, 6, 30, 23)
    assert [p.price for p in periods] == prices


def test_prices_are_limited_to_each_agreement() -> None:
    periods = _unit_periods([_agreement("2026-08-01T00:00:00+01:00", None)], _RATES)

    assert periods[0].start == _utc(2026, 7, 31, 23)


def test_rates_without_a_payment_method_apply_to_everyone() -> None:
    rates = [_rate("2026-01-01T00:00:00Z", None, 25.0, None)]

    periods = _unit_periods([_agreement("2026-01-01T00:00:00Z", None, rate=25.0)], rates)

    assert [p.price for p in periods] == [0.25]


def test_standing_charges_use_the_same_payment_method() -> None:
    prices = build_prices(
        [_agreement("2023-03-31T23:00:00+00:00", None)],
        {_TARIFF: _RATES},
        {_TARIFF: _STANDING},
        _NOW,
    )

    assert prices.standing == [PricePeriod(_utc(2026, 9, 30, 23), None, 0.42915)]


# Single-register time-of-use tariffs (e.g. NEXT Drive) return dated
# peak/off-peak bands with no payment-method discrimination.
_TOU_BANDS = [
    _rate("2026-11-15T00:00:00Z", "2026-11-15T06:00:00Z", 8.571, None),
    _rate("2026-11-15T06:00:00Z", "2026-11-16T00:00:00Z", 27.851, None),
]


@pytest.mark.parametrize(
    ("agreement", "rates"),
    [
        (_agreement("2023-03-31T23:00:00Z", None, rate=27.3), _RATES),
        (_agreement("2023-01-01T00:00:00Z", "2024-01-01T00:00:00Z"), _RATES),
    ],
    ids=["unrecognised-account-rate", "no-active-agreement"],
)
def test_nothing_is_priced_when_the_rate_is_uncertain(agreement, rates) -> None:
    prices = build_prices([agreement], {_TARIFF: rates}, {_TARIFF: _STANDING}, _NOW)

    assert prices == Prices([], [])


def test_time_of_use_rates_are_priced_per_band() -> None:
    # The two daily bands are both kept; price_at resolves the one in force,
    # so overnight usage is priced at off-peak and daytime at peak.
    prices = build_prices(
        [_agreement("2026-01-01T00:00:00Z", None, rate=8.571)],
        {_TARIFF: _TOU_BANDS},
        {_TARIFF: _STANDING},
        _NOW,
    )

    assert len(prices.unit) == 2
    assert price_at(prices.unit, _utc(2026, 11, 15, 3)) == 0.08571  # off-peak
    assert price_at(prices.unit, _utc(2026, 11, 15, 9)) == 0.27851  # peak


def test_price_at_finds_the_period_covering_an_hour() -> None:
    periods = [
        PricePeriod(_utc(2026, 6, 30, 23), _utc(2026, 9, 30, 23), 0.26),
        PricePeriod(_utc(2026, 9, 30, 23), None, 0.27),
    ]

    assert price_at(periods, _utc(2026, 9, 30, 22)) == 0.26
    assert price_at(periods, _utc(2026, 9, 30, 23)) == 0.27
    assert price_at(periods, _utc(2026, 6, 30, 22)) is None


def _meter() -> SimpleNamespace:
    return SimpleNamespace(
        account=SimpleNamespace(account_number="A-1"), supply_point_id="MPAN"
    )


def _api(**overrides) -> SimpleNamespace:
    async def tariff_rates(code: str, kind: str) -> list[dict]:
        return _RATES if kind == UNIT_RATES else _STANDING

    api = SimpleNamespace(
        async_get_tariff_agreements=AsyncMock(
            return_value={"MPAN": [_agreement("2023-03-31T23:00:00Z", None)]}
        ),
        async_get_tariff_rates=AsyncMock(side_effect=tariff_rates),
    )
    for name, value in overrides.items():
        setattr(api, name, value)
    return api


@pytest.mark.asyncio
async def test_history_is_fetched_once_and_reused() -> None:
    api = _api()
    history = PriceHistory(api)

    first = await history.async_prices(_meter())
    second = await history.async_prices(_meter())

    assert first == second
    assert (len(first.unit), len(first.standing)) == (2, 1)
    api.async_get_tariff_agreements.assert_awaited_once_with("A-1")
    assert [call.args for call in api.async_get_tariff_rates.await_args_list] == [
        (_TARIFF, UNIT_RATES),
        (_TARIFF, STANDING_CHARGES),
    ]


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_last_prices() -> None:
    api = _api()
    history = PriceHistory(api, refresh_after=timedelta(0))
    first = await history.async_prices(_meter())

    api.async_get_tariff_agreements.side_effect = RuntimeError("down")

    assert await history.async_prices(_meter()) == first


@pytest.mark.asyncio
async def test_auth_failures_are_not_hidden() -> None:
    api = _api(
        async_get_tariff_agreements=AsyncMock(side_effect=EonNextAuthError("expired"))
    )

    with pytest.raises(EonNextAuthError):
        await PriceHistory(api).async_prices(_meter())


@pytest.mark.asyncio
async def test_a_missing_old_tariff_does_not_stop_pricing() -> None:
    old_tariff = "E-1R-RETIRED-C"

    async def tariff_rates(code: str, kind: str) -> list[dict]:
        if code != _TARIFF:
            raise EonNextNotFoundError("404")
        return _RATES if kind == UNIT_RATES else _STANDING

    api = _api(
        async_get_tariff_agreements=AsyncMock(
            return_value={
                "MPAN": [
                    _agreement("2020-01-01T00:00:00Z", "2023-03-31T23:00:00Z", old_tariff),
                    _agreement("2023-03-31T23:00:00Z", None),
                ]
            }
        ),
        async_get_tariff_rates=AsyncMock(side_effect=tariff_rates),
    )

    prices = await PriceHistory(api).async_prices(_meter())

    assert (len(prices.unit), len(prices.standing)) == (2, 1)


@pytest.mark.asyncio
async def test_an_outage_never_gives_a_partial_price_list() -> None:
    async def tariff_rates(code: str, kind: str) -> list[dict]:
        if kind == STANDING_CHARGES:
            raise EonNextApiError("503")
        return _RATES

    api = _api(async_get_tariff_rates=AsyncMock(side_effect=tariff_rates))

    assert await PriceHistory(api).async_prices(_meter()) == Prices([], [])
