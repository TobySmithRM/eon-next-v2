"""Unit tests for EonNextCoordinator fallback logic.

These tests exercise the coordinator's static helpers directly,
patching ``homeassistant.util.dt.now()`` to a fixed reference time
so that date-dependent logic is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.eon_next.coordinator import EonNextCoordinator
from custom_components.eon_next.eonnext import EonNextApiError

# Fixed reference time: 2025-06-15 14:00 UTC.  All tests derive
# "yesterday" / "today" from this constant so they never become flaky
# around midnight.
_REF_NOW = datetime(2025, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
_YESTERDAY = (_REF_NOW - timedelta(days=1)).date()  # 2025-06-14
_TODAY = _REF_NOW.date()  # 2025-06-15


def _make_entry(
    interval_start: str, consumption: float | None
) -> dict[str, Any]:
    return {"interval_start": interval_start, "consumption": consumption}


def _patch_now():
    """Patch ``homeassistant.util.dt.now`` to return *_REF_NOW*."""
    return patch("homeassistant.util.dt.now", return_value=_REF_NOW)


class TestAggregateYesterdayConsumption:
    """Tests for EonNextCoordinator._aggregate_yesterday_consumption."""

    def test_returns_none_for_empty_list(self) -> None:
        with _patch_now():
            assert (
                EonNextCoordinator._aggregate_yesterday_consumption([]) is None
            )

    def test_sums_yesterday_entries(self) -> None:
        entries = [
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", 1.5),
            _make_entry(f"{_YESTERDAY}T08:30:00+00:00", 2.5),
            _make_entry(f"{_YESTERDAY}T09:00:00+00:00", 1.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_yesterday_consumption(
                entries
            )
        assert result == pytest.approx(5.0)

    def test_ignores_today_entries(self) -> None:
        entries = [
            _make_entry(f"{_TODAY}T08:00:00+00:00", 10.0),
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", 3.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_yesterday_consumption(
                entries
            )
        assert result == pytest.approx(3.0)

    def test_ignores_older_entries(self) -> None:
        two_days_ago = (_REF_NOW - timedelta(days=2)).date()
        entries = [
            _make_entry(f"{two_days_ago}T08:00:00+00:00", 5.0),
        ]
        with _patch_now():
            assert (
                EonNextCoordinator._aggregate_yesterday_consumption(entries)
                is None
            )

    def test_skips_none_consumption(self) -> None:
        entries = [
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", None),
            _make_entry(f"{_YESTERDAY}T09:00:00+00:00", 2.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_yesterday_consumption(
                entries
            )
        assert result == pytest.approx(2.0)

    def test_returns_none_when_no_yesterday_data(self) -> None:
        entries = [
            _make_entry(f"{_TODAY}T08:00:00+00:00", 5.0),
        ]
        with _patch_now():
            assert (
                EonNextCoordinator._aggregate_yesterday_consumption(entries)
                is None
            )

    def test_min_entries_rejects_incomplete_data(self) -> None:
        """With min_entries=44, fewer entries should return None."""
        entries = [
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", 1.0),
            _make_entry(f"{_YESTERDAY}T08:30:00+00:00", 1.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_yesterday_consumption(
                entries, min_entries=44
            )
        assert result is None

    def test_min_entries_accepts_complete_data(self) -> None:
        """With min_entries=44, 48 entries should succeed."""
        entries = [
            _make_entry(f"{_YESTERDAY}T{h:02d}:{m:02d}:00+00:00", 0.5)
            for h in range(24)
            for m in (0, 30)
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_yesterday_consumption(
                entries, min_entries=44
            )
        assert result == pytest.approx(24.0)


class TestAggregateDailyConsumption:
    """Tests for EonNextCoordinator._aggregate_daily_consumption."""

    def test_sums_today_entries(self) -> None:
        entries = [
            _make_entry(f"{_TODAY}T08:00:00+00:00", 1.5),
            _make_entry(f"{_TODAY}T08:30:00+00:00", 2.5),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["total"] == pytest.approx(4.0)
        assert result["last_reset"] == f"{_TODAY}T08:00:00+00:00"

    def test_ignores_yesterday_entries(self) -> None:
        entries = [
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", 10.0),
            _make_entry(f"{_TODAY}T09:00:00+00:00", 3.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["total"] == pytest.approx(3.0)

    def test_returns_zero_when_no_today_entries(self) -> None:
        """When data exists but none matches today, return 0.0 with midnight."""
        entries = [
            _make_entry(f"{_YESTERDAY}T08:00:00+00:00", 5.0),
            _make_entry(f"{_YESTERDAY}T08:30:00+00:00", 3.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["total"] == 0.0
        # last_reset must be today's local midnight
        from homeassistant.util import dt as dt_util

        parsed = dt_util.parse_datetime(result["last_reset"])
        assert parsed is not None
        assert parsed.date() == _TODAY
        assert parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0

    def test_returns_none_when_today_entries_all_invalid(self) -> None:
        """Today entries exist but all have None consumption → unknown."""
        entries = [
            _make_entry(f"{_TODAY}T08:00:00+00:00", None),
            _make_entry(f"{_TODAY}T08:30:00+00:00", None),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["total"] is None
        assert result["last_reset"] is None

    def test_returns_none_for_empty_list(self) -> None:
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption([])
        assert result["total"] is None
        assert result["last_reset"] is None

    def test_skips_none_consumption(self) -> None:
        entries = [
            _make_entry(f"{_TODAY}T08:00:00+00:00", None),
            _make_entry(f"{_TODAY}T09:00:00+00:00", 2.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["total"] == pytest.approx(2.0)

    def test_last_reset_is_earliest_today_entry(self) -> None:
        entries = [
            _make_entry(f"{_TODAY}T10:00:00+00:00", 1.0),
            _make_entry(f"{_TODAY}T08:00:00+00:00", 1.0),
            _make_entry(f"{_TODAY}T09:00:00+00:00", 1.0),
        ]
        with _patch_now():
            result = EonNextCoordinator._aggregate_daily_consumption(entries)
        assert result["last_reset"] == f"{_TODAY}T08:00:00+00:00"


class TestCostFallbackToTariff:
    """Tests that unit_rate and standing_charge fall back to tariff values.

    These exercise the tariff-fallback guards that live in
    ``_async_update_data``.  The logic is simple conditional assignment
    so we replicate the guard here rather than spinning up a full
    coordinator update cycle.
    """

    @staticmethod
    def _apply_tariff_fallback(meter_data: dict[str, Any]) -> None:
        """Replicate the tariff-fallback guards from _async_update_data."""
        if (
            meter_data.get("unit_rate") is None
            and meter_data.get("tariff_unit_rate") is not None
        ):
            meter_data["unit_rate"] = meter_data["tariff_unit_rate"]
        if (
            meter_data.get("standing_charge") is None
            and meter_data.get("tariff_standing_charge") is not None
        ):
            meter_data["standing_charge"] = meter_data["tariff_standing_charge"]

    def test_unit_rate_falls_back_to_tariff(self) -> None:
        meter_data: dict[str, Any] = {
            "unit_rate": None,
            "standing_charge": None,
            "tariff_unit_rate": 0.2236,
            "tariff_standing_charge": 0.5335,
        }
        self._apply_tariff_fallback(meter_data)
        assert meter_data["unit_rate"] == pytest.approx(0.2236)
        assert meter_data["standing_charge"] == pytest.approx(0.5335)

    def test_cost_endpoint_values_not_overridden(self) -> None:
        meter_data: dict[str, Any] = {
            "unit_rate": 0.25,
            "standing_charge": 0.50,
            "tariff_unit_rate": 0.2236,
            "tariff_standing_charge": 0.5335,
        }
        self._apply_tariff_fallback(meter_data)
        assert meter_data["unit_rate"] == pytest.approx(0.25)
        assert meter_data["standing_charge"] == pytest.approx(0.50)

    def test_no_fallback_when_tariff_missing(self) -> None:
        meter_data: dict[str, Any] = {
            "unit_rate": None,
            "standing_charge": None,
            "tariff_unit_rate": None,
            "tariff_standing_charge": None,
        }
        self._apply_tariff_fallback(meter_data)
        assert meter_data["unit_rate"] is None
        assert meter_data["standing_charge"] is None


class TestPreviousDayCostComputation:
    """Tests for computed previous_day_cost from consumption + tariff."""

    def test_computes_cost_from_consumption_and_tariff(self) -> None:
        consumption = [
            _make_entry(f"{_YESTERDAY}T00:00:00+00:00", 10.0),
        ]
        unit_rate = 0.25  # GBP/kWh
        standing_charge = 0.50  # GBP/day

        with _patch_now():
            yesterday_kwh = (
                EonNextCoordinator._aggregate_yesterday_consumption(
                    consumption
                )
            )
        assert yesterday_kwh is not None

        cost = round(yesterday_kwh * unit_rate + standing_charge, 4)
        # 10.0 * 0.25 + 0.50 = 3.0
        assert cost == pytest.approx(3.0)

    def test_no_cost_when_no_yesterday_consumption(self) -> None:
        consumption = [
            _make_entry(f"{_TODAY}T00:00:00+00:00", 10.0),
        ]
        with _patch_now():
            yesterday_kwh = (
                EonNextCoordinator._aggregate_yesterday_consumption(
                    consumption
                )
            )
        assert yesterday_kwh is None

    def test_cost_with_multiple_half_hourly_slots(self) -> None:
        # 48 half-hourly slots at 0.5 kWh each = 24 kWh total
        consumption = [
            _make_entry(f"{_YESTERDAY}T{h:02d}:{m:02d}:00+00:00", 0.5)
            for h in range(24)
            for m in (0, 30)
        ]
        unit_rate = 0.22  # GBP/kWh
        standing_charge = 0.53  # GBP/day

        with _patch_now():
            yesterday_kwh = (
                EonNextCoordinator._aggregate_yesterday_consumption(
                    consumption
                )
            )
        assert yesterday_kwh == pytest.approx(24.0)

        cost = round(yesterday_kwh * unit_rate + standing_charge, 4)
        # 24.0 * 0.22 + 0.53 = 5.81
        assert cost == pytest.approx(5.81)


class TestYesterdayEntries:
    """_yesterday_entries returns the raw entries that fall on yesterday."""

    def test_filters_to_yesterday_local(self) -> None:
        entries = [
            _make_entry(f"{_YESTERDAY}T00:00:00+00:00", 1.0),
            _make_entry(f"{_YESTERDAY}T23:30:00+00:00", 2.0),
            _make_entry(f"{_TODAY}T00:00:00+00:00", 3.0),  # today, excluded
            _make_entry("not-a-date", 9.0),  # unparseable, excluded
        ]
        with _patch_now():
            result = EonNextCoordinator._yesterday_entries(entries)
        assert len(result) == 2
        assert all(e["interval_start"].startswith(str(_YESTERDAY)) for e in result)

    def test_empty_when_no_yesterday(self) -> None:
        entries = [_make_entry(f"{_TODAY}T12:00:00+00:00", 1.0)]
        with _patch_now():
            assert EonNextCoordinator._yesterday_entries(entries) == []


class TestFetchConsumptionGranularity:
    """_fetch_consumption reports the granularity that was actually used.

    The coordinator gates the external-statistics import on this so that
    daily-granularity fallback data (which covers *today* as one partial
    midnight bucket) is never imported and double-counted (spec 02, 2.3).
    """

    @staticmethod
    def _coordinator(api) -> EonNextCoordinator:
        # Bypass the HA DataUpdateCoordinator base init: _fetch_consumption
        # only depends on self.api.
        coord = EonNextCoordinator.__new__(EonNextCoordinator)
        coord.api = api
        # These tests are about granularity; the one-off diagnostic probe has
        # its own tests below, so mark the test meter as already probed.
        coord._consumption_probed = {"m1"}
        return coord

    @staticmethod
    def _meter() -> SimpleNamespace:
        return SimpleNamespace(
            type="electricity", supply_point_id="sp-1", serial="m1"
        )

    @pytest.mark.asyncio
    async def test_reports_half_hour_when_available(self) -> None:
        api = SimpleNamespace(
            async_get_consumption=AsyncMock(
                return_value={"results": [{"interval_start": "x", "consumption": 1}]}
            )
        )
        coord = self._coordinator(api)
        entries, granularity = await coord._fetch_consumption(self._meter())
        assert granularity == "half_hour"
        assert entries == [{"interval_start": "x", "consumption": 1}]
        api.async_get_consumption.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reports_day_on_daily_fallback(self) -> None:
        api = SimpleNamespace(
            async_get_consumption=AsyncMock(
                side_effect=[
                    {"results": []},  # half-hourly empty -> fall back
                    {"results": [{"interval_start": "x", "consumption": 2}]},
                ]
            )
        )
        coord = self._coordinator(api)
        entries, granularity = await coord._fetch_consumption(self._meter())
        assert granularity == "day"
        assert entries == [{"interval_start": "x", "consumption": 2}]
        assert api.async_get_consumption.await_count == 2

    @pytest.mark.asyncio
    async def test_reports_none_when_no_data(self) -> None:
        api = SimpleNamespace(
            async_get_consumption=AsyncMock(return_value={"results": []})
        )
        coord = self._coordinator(api)
        entries, granularity = await coord._fetch_consumption(self._meter())
        assert entries is None
        assert granularity is None


class TestHalfHourProbe:
    """The one-off probe logs how E.ON answers each consumption request shape."""

    @staticmethod
    def _coordinator(api) -> EonNextCoordinator:
        coord = EonNextCoordinator.__new__(EonNextCoordinator)
        coord.api = api
        coord._consumption_probed = set()
        return coord

    @staticmethod
    def _meter() -> SimpleNamespace:
        return SimpleNamespace(
            type="electricity", supply_point_id="sp-1", serial="m1"
        )

    @staticmethod
    def _api() -> SimpleNamespace:
        """E.ON rejects the live request shape; every other shape works."""

        async def _get(*_a, group_by="day", page_size=10, period_from=None, **_kw):
            if group_by is None and period_from is not None:
                raise EonNextApiError(
                    "REST consumption endpoint returned status 400: bad request"
                )
            return {
                "results": [
                    {
                        "interval_start": "2026-10-24T00:00:00+01:00",
                        "interval_end": "2026-10-24T00:30:00+01:00",
                        "consumption": 0.1,
                    }
                ]
            }

        return SimpleNamespace(async_get_consumption=AsyncMock(side_effect=_get))

    @pytest.mark.asyncio
    async def test_probe_logs_each_variant_once_and_keeps_daily_fallback(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        api = self._api()
        coord = self._coordinator(api)

        with caplog.at_level("WARNING", logger="custom_components.eon_next.coordinator"):
            entries, granularity = await coord._fetch_consumption(self._meter())
            first_call_count = api.async_get_consumption.await_count
            await coord._fetch_consumption(self._meter())

        # The probe never changes what the coordinator uses.
        assert granularity == "day"
        assert entries is not None

        probe_logs = [r for r in caplog.records if "Half-hourly consumption" in r.message]
        assert len(probe_logs) == 1
        text = probe_logs[0].getMessage()
        assert "m1" in text
        assert "(current request)" in text and "status 400: bad request" in text
        assert "interval 30 min" in text

        # 1 live request + 4 probe variants + 1 daily on the first pass; the
        # second pass must not probe again (1 live request + 1 daily).
        assert first_call_count == 6
        assert api.async_get_consumption.await_count == first_call_count + 2

    @pytest.mark.asyncio
    async def test_probe_failure_never_breaks_fetch(self) -> None:
        api = SimpleNamespace(
            async_get_consumption=AsyncMock(side_effect=EonNextApiError("down"))
        )
        coord = self._coordinator(api)
        entries, granularity = await coord._fetch_consumption(self._meter())
        assert (entries, granularity) == (None, None)


class TestLiveRequestShape:
    """The live request omits group_by and asks for the rolling window."""

    @staticmethod
    def _coordinator(api) -> EonNextCoordinator:
        coord = EonNextCoordinator.__new__(EonNextCoordinator)
        coord.api = api
        coord._consumption_probed = {"m1"}
        return coord

    @staticmethod
    def _meter() -> SimpleNamespace:
        return SimpleNamespace(type="electricity", supply_point_id="sp-1", serial="m1")

    @pytest.mark.asyncio
    async def test_requests_window_without_group_by(self) -> None:
        from custom_components.eon_next.const import LIVE_PAGE_SIZE
        from custom_components.eon_next.statistics import live_window_start

        api = SimpleNamespace(
            async_get_consumption=AsyncMock(
                return_value={"results": [{"interval_start": "x", "consumption": 1}]}
            )
        )
        coord = self._coordinator(api)

        await coord._fetch_consumption(self._meter())

        kwargs = api.async_get_consumption.await_args.kwargs
        assert kwargs["group_by"] is None
        assert kwargs["page_size"] == LIVE_PAGE_SIZE
        assert kwargs["period_from"] == live_window_start().strftime("%Y-%m-%dT%H:%M:%SZ")

    @pytest.mark.asyncio
    async def test_coarse_intervals_are_not_treated_as_half_hourly(self) -> None:
        """A response of daily-sized buckets must never reach the hourly splice."""
        daily = {
            "results": [
                {
                    "interval_start": "2026-10-24T00:00:00+01:00",
                    "interval_end": "2026-10-25T00:00:00+01:00",
                    "consumption": 30.0,
                }
            ]
        }
        api = SimpleNamespace(async_get_consumption=AsyncMock(return_value=daily))
        coord = self._coordinator(api)

        entries, granularity = await coord._fetch_consumption(self._meter())

        assert granularity == "day"
        assert entries == daily["results"]
