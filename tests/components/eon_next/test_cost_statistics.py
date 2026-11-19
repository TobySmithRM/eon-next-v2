"""Recorder-backed tests for the cost statistics that follow consumption."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
import logging
from unittest.mock import patch

import pytest

from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.helpers.recorder import get_instance
from homeassistant.setup import async_setup_component

from custom_components.eon_next.statistics import (
    _hours_in_local_day,
    _local_day_start,
    async_import_cost_statistics,
    async_import_historical_statistics,
    cost_statistic_id_for_meter,
)
from custom_components.eon_next.unit_prices import PricePeriod, Prices

# Pin the hour to midday UTC so the now-relative test dates never coincide with
# local midnight.  At local midnight a single daily-total usage row lands exactly
# on the day start, which triggers the standing-charge "whole charge on one row"
# collapse (statistics._standing_charge_shares) and makes the per-hour count tests
# flaky for whichever hour maps to 00:00 local.
_NOW = datetime.now(tz=timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
_DAY_A = _NOW - timedelta(days=3)
_DAY_B = _NOW - timedelta(days=5)
# The price goes up from 20p to 30p between day B and day A.
_PERIODS = [
    PricePeriod(_DAY_B - timedelta(days=30), _DAY_A - timedelta(hours=1), 0.2),
    PricePeriod(_DAY_A - timedelta(hours=1), None, 0.3),
]
_PRICES = Prices(_PERIODS, [])
# Plus a standing charge of £2.40 a day.
_WITH_STANDING = Prices(_PERIODS, [PricePeriod(_PERIODS[0].start, None, 2.4)])


@pytest.fixture(autouse=True)
def _quiet_sqlalchemy_engine_logs() -> Generator[None, None, None]:
    logger = logging.getLogger("sqlalchemy.engine")
    previous_level = logger.level
    logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        logger.setLevel(previous_level)


async def _ensure_recorder(hass: HomeAssistant) -> None:
    if "recorder" in hass.config.components:
        return
    recorder_helper.async_initialize_recorder(hass)
    with patch("homeassistant.components.recorder.ALLOW_IN_MEMORY_DB", True):
        assert await async_setup_component(
            hass,
            "recorder",
            {"recorder": {"db_url": "sqlite://", "commit_interval": 0}},
        )
    await hass.async_block_till_done()
    await hass.data[recorder_helper.DATA_RECORDER].db_connected


def _entry(when: datetime, kwh: float) -> dict:
    return {"interval_start": when.isoformat(), "consumption": kwh}


def _hours_so_far(start: datetime) -> list[datetime]:
    """Every hour from ``start`` to the current one, as the import counts them."""
    this_hour = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    count = int((this_hour - start) / timedelta(hours=1)) + 1
    return [start + timedelta(hours=h) for h in range(count)]


def _standing_share(hour: datetime) -> float:
    return 2.4 / _hours_in_local_day(hour)


async def _cost_sums(hass: HomeAssistant, meter: str, expect: int) -> list[float]:
    stat_id = cost_statistic_id_for_meter(meter, "electricity")
    rows: list = []
    for _ in range(50):
        await hass.async_block_till_done()
        await get_instance(hass).async_block_till_done()
        data = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            _DAY_B - timedelta(days=1),
            _NOW + timedelta(days=1),
            {stat_id},
            "hour",
            None,
            {"sum"},
        )
        rows = sorted(data.get(stat_id, []), key=lambda r: r["start"])
        if len(rows) >= expect:
            break
    return [round(float(r["sum"]), 4) for r in rows]


@pytest.mark.asyncio
async def test_first_import_prices_all_consumption_at_the_rate_of_the_day(
    hass: HomeAssistant,
) -> None:
    await _ensure_recorder(hass)
    meter = "COST-FIRST"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_B, 2.0), _entry(_DAY_A, 1.0)]
    )

    await async_import_cost_statistics(hass, meter, "electricity", _PRICES, _NOW)

    # 2 kWh at 20p, then 1 kWh at 30p.
    assert await _cost_sums(hass, meter, expect=2) == [0.4, 0.7]
    cost_id = cost_statistic_id_for_meter(meter, "electricity")
    metadata = await get_instance(hass).async_add_executor_job(
        lambda: get_metadata(hass, statistic_ids={cost_id})
    )
    assert next(iter(metadata.values()))[1]["unit_of_measurement"] == "GBP"


@pytest.mark.asyncio
async def test_backfilled_hours_are_costed_and_later_sums_move_up(
    hass: HomeAssistant,
) -> None:
    await _ensure_recorder(hass)
    meter = "COST-SPLICE"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_A, 1.0)]
    )
    await async_import_cost_statistics(hass, meter, "electricity", _PRICES, _DAY_A)
    assert await _cost_sums(hass, meter, expect=1) == [0.3]

    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_B, 2.0)]
    )
    await async_import_cost_statistics(hass, meter, "electricity", _PRICES, _DAY_B)

    assert await _cost_sums(hass, meter, expect=2) == [0.4, 0.7]


@pytest.mark.asyncio
async def test_hours_that_lose_their_price_cost_nothing(hass: HomeAssistant) -> None:
    await _ensure_recorder(hass)
    meter = "COST-UNPRICED"
    hours = [_DAY_A + timedelta(hours=h) for h in range(3)]
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(hour, 1.0) for hour in hours]
    )
    await async_import_cost_statistics(hass, meter, "electricity", _PRICES, hours[0])
    assert await _cost_sums(hass, meter, expect=3) == [0.3, 0.6, 0.9]

    # Prices now only start at the last hour, e.g. after an agreement drops out.
    later = [PricePeriod(hours[2], None, 0.3)]
    await async_import_cost_statistics(
        hass, meter, "electricity", Prices(later, []), hours[0]
    )

    assert await _cost_sums(hass, meter, expect=3) == [0.0, 0.0, 0.3]


@pytest.mark.asyncio
async def test_nothing_is_written_without_prices(hass: HomeAssistant) -> None:
    await _ensure_recorder(hass)
    meter = "COST-NONE"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_A, 1.0)]
    )

    await async_import_cost_statistics(
        hass, meter, "electricity", Prices([], []), _NOW
    )

    assert await _cost_sums(hass, meter, expect=0) == []


@pytest.mark.asyncio
async def test_every_hour_up_to_now_carries_its_share_of_the_standing_charge(
    hass: HomeAssistant,
) -> None:
    await _ensure_recorder(hass)
    meter = "COST-STANDING"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_B, 2.0), _entry(_DAY_A, 1.0)]
    )

    assert await async_import_cost_statistics(
        hass, meter, "electricity", _WITH_STANDING, None, include_standing_charge=True
    )

    # Every hour from the start of the first usage day to now, including the
    # days in between with no usage data, carries its share.
    hours = _hours_so_far(_local_day_start(_DAY_B))
    sums = await _cost_sums(hass, meter, expect=len(hours))
    assert len(sums) == len(hours)
    standing = sum(_standing_share(hour) for hour in hours)
    assert sums[-1] == pytest.approx(0.4 + 0.3 + standing, abs=0.0002)


@pytest.mark.asyncio
async def test_a_whole_day_adds_exactly_its_standing_charge(hass: HomeAssistant) -> None:
    await _ensure_recorder(hass)
    meter = "COST-STANDING-DAY"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_B, 2.0)]
    )

    await async_import_cost_statistics(
        hass, meter, "electricity", _WITH_STANDING, None, include_standing_charge=True
    )

    hours = _hours_so_far(_local_day_start(_DAY_B))
    sums = await _cost_sums(hass, meter, expect=len(hours))
    day_two = _local_day_start(_DAY_B + timedelta(days=1))
    day_three = _local_day_start(_DAY_B + timedelta(days=2))
    assert sums[hours.index(day_three) - 1] - sums[hours.index(day_two) - 1] == (
        pytest.approx(2.4, abs=0.0001)
    )


@pytest.mark.asyncio
async def test_a_day_stored_as_one_total_takes_its_charge_in_one_row(
    hass: HomeAssistant,
) -> None:
    await _ensure_recorder(hass)
    meter = "COST-STANDING-BUCKET"
    day = _local_day_start(_DAY_B)
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(day, 10.0)], daily_granularity=True
    )

    await async_import_cost_statistics(
        hass, meter, "electricity", _WITH_STANDING, None, include_standing_charge=True
    )

    hours = [day] + _hours_so_far(_local_day_start(day + timedelta(hours=25)))
    sums = await _cost_sums(hass, meter, expect=len(hours))
    # 10 kWh at 20p plus the whole day's £2.40, then the next day's hours.
    assert sums[0] == pytest.approx(4.4)
    assert len(sums) == len(hours)


@pytest.mark.asyncio
async def test_switching_the_standing_charge_off_removes_it_everywhere(
    hass: HomeAssistant,
) -> None:
    await _ensure_recorder(hass)
    meter = "COST-STANDING-OFF"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_B, 2.0), _entry(_DAY_A, 1.0)]
    )
    await async_import_cost_statistics(
        hass, meter, "electricity", _WITH_STANDING, None, include_standing_charge=True
    )
    hours = _hours_so_far(_local_day_start(_DAY_B))
    await _cost_sums(hass, meter, expect=len(hours))

    await async_import_cost_statistics(hass, meter, "electricity", _WITH_STANDING, None)

    sums = await _cost_sums(hass, meter, expect=len(hours))
    assert sums[-1] == pytest.approx(0.7)
    assert all(later >= earlier for earlier, later in zip(sums, sums[1:]))


@pytest.mark.asyncio
async def test_half_hour_time_zones_still_write_whole_hours(hass: HomeAssistant) -> None:
    await _ensure_recorder(hass)
    await hass.config.async_set_time_zone("Asia/Kolkata")
    meter = "COST-KOLKATA"
    await async_import_historical_statistics(
        hass, meter, "electricity", [_entry(_DAY_A, 1.0)]
    )

    assert await async_import_cost_statistics(
        hass, meter, "electricity", _WITH_STANDING, None, include_standing_charge=True
    )

    assert await _cost_sums(hass, meter, expect=24)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("when", "hours"),
    [
        # Fixed dates: the UK clock changes in 2026, which never go stale.
        (datetime(2026, 3, 29, 12, tzinfo=timezone.utc), 23),
        (datetime(2026, 10, 25, 12, tzinfo=timezone.utc), 25),
        (datetime(2026, 11, 16, 12, tzinfo=timezone.utc), 24),
    ],
)
async def test_clock_change_days_split_the_charge_into_23_or_25(
    hass: HomeAssistant, when: datetime, hours: int
) -> None:
    await hass.config.async_set_time_zone("Europe/London")

    assert _hours_in_local_day(when) == hours
