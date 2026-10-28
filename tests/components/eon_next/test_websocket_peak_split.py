"""Tests for the peak/off-peak overlay on consumption history responses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from custom_components.eon_next.schemas import ConsumptionHistoryEntry
from custom_components.eon_next.websocket import _apply_peak_split

_REF_DATE = datetime.now(tz=timezone.utc).replace(
    hour=3, minute=0, second=0, microsecond=0
)
_TODAY_ISO = _REF_DATE.date().isoformat()
_OLD_ISO = (_REF_DATE - timedelta(days=30)).date().isoformat()


def _ts(hour: int) -> str:
    return f"{_TODAY_ISO}T{hour:02d}:00:00+00:00"


def _hh(hour: int, kwh: float) -> dict[str, Any]:
    return {
        "interval_start": _ts(hour),
        "interval_end": f"{_TODAY_ISO}T{hour:02d}:30:00+00:00",
        "consumption": kwh,
    }


def _tou_meter_data() -> dict[str, Any]:
    return {
        "tariff_unit_rate": 0.10,
        "tariff_is_tou": True,
        "tariff_code": "E-1R-NEXT-DRIVE-01",
        "tariff_rates_schedule": [
            {"value": 7.0, "validFrom": _ts(2), "validTo": _ts(5)},
            {"value": 25.0, "validFrom": _ts(5), "validTo": _ts(8)},
        ],
        "consumption": [_hh(3, 1.0), _hh(6, 2.0)],
    }


def test_overlays_split_onto_matching_day() -> None:
    data = _tou_meter_data()
    entries = [
        ConsumptionHistoryEntry(date=_OLD_ISO, consumption=10.0),
        ConsumptionHistoryEntry(date=_TODAY_ISO, consumption=3.0),
    ]
    _apply_peak_split(data, entries)

    today = next(e for e in entries if e.date == _TODAY_ISO)
    assert today.split_available is True
    assert today.offpeak_kwh == 1.0
    assert today.peak_kwh == 2.0
    assert today.offpeak_cost == 0.07
    assert today.peak_cost == 0.5

    # The older day has no half-hourly data, so it is left untouched.
    old = next(e for e in entries if e.date == _OLD_ISO)
    assert old.split_available is False
    assert old.peak_cost is None


def test_flat_rate_is_noop() -> None:
    data = {"tariff_is_tou": False, "consumption": [_hh(3, 1.0)]}
    entries = [ConsumptionHistoryEntry(date=_TODAY_ISO, consumption=1.0)]
    _apply_peak_split(data, entries)
    assert entries[0].split_available is False


def test_none_meter_data_is_noop() -> None:
    entries = [ConsumptionHistoryEntry(date=_TODAY_ISO, consumption=1.0)]
    _apply_peak_split(None, entries)
    assert entries[0].split_available is False
