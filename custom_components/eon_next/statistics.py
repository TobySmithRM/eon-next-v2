"""External statistics import for the Eon Next integration.

Imports half-hourly (or daily) consumption data as external statistics
with correct timestamps so the Energy Dashboard attributes consumption
to the right period - even when data arrives late.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LIVE_WINDOW_DAYS
from .eonnext import METER_TYPE_ELECTRIC, METER_TYPE_GAS

_LOGGER = logging.getLogger(__name__)

_VALID_ID_CHAR = re.compile(r"[^a-z0-9]")


class StatisticsLookupError(Exception):
    """Raised when the last-statistic lookup fails for a recorder reason.

    Distinct from a genuine "no prior statistic exists" result: a lookup
    failure (busy DB, migration, recorder unavailable) must abort the import
    cycle rather than fall back to a zero sum base, which would overwrite
    existing rows with regressed cumulative sums.
    """


def _sanitize_id(value: str) -> str:
    """Convert a string to a valid statistic ID component."""
    sanitized = _VALID_ID_CHAR.sub("_", value.lower())
    sanitized = re.sub(r"_+", "_", sanitized)
    return sanitized.strip("_")


def _hour_start(dt: datetime) -> datetime:
    """Round a datetime down to the start of its hour (UTC)."""
    return dt.replace(minute=0, second=0, microsecond=0)


def live_window_start() -> datetime:
    """UTC start of the live window: local midnight ``LIVE_WINDOW_DAYS`` ago.

    Aligning to local midnight matters: a daily bucket written by an older
    version or by the historical backfill sits on its day's local midnight, and
    the live splice overwrites exactly that row with the day's real hours.
    """
    first_day = dt_util.now().date() - timedelta(days=LIVE_WINDOW_DAYS)
    return dt_util.as_utc(dt_util.start_of_local_day(first_day))


def statistic_id_for_meter(meter_serial: str, meter_type: str) -> str | None:
    """Build statistic_id for a supported meter."""
    sanitized_serial = _sanitize_id(meter_serial)
    if meter_type == METER_TYPE_GAS:
        fuel = "gas"
    elif meter_type == METER_TYPE_ELECTRIC:
        fuel = "electricity"
    else:
        return None
    return f"{DOMAIN}:{fuel}_{sanitized_serial}_consumption"


def _group_consumption_by_hour(
    entries: list[dict[str, Any]],
) -> dict[datetime, float]:
    """Aggregate consumption entries into hourly UTC buckets."""
    hourly: dict[datetime, float] = defaultdict(float)

    for entry in entries:
        interval_start = entry.get("interval_start")
        consumption = entry.get("consumption")
        if not interval_start or consumption is None:
            continue

        try:
            val = float(consumption)
        except (TypeError, ValueError):
            continue

        parsed = dt_util.parse_datetime(str(interval_start))
        if parsed is None:
            continue

        if parsed.tzinfo is None:
            _LOGGER.debug(
                "Naive interval_start '%s' received; assuming UTC",
                interval_start,
            )
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = dt_util.as_utc(parsed)

        hourly[_hour_start(parsed)] += val

    return dict(hourly)


async def _get_last_stat(
    hass: HomeAssistant,
    statistic_id: str,
    before: datetime,
) -> tuple[datetime | None, float]:
    """Retrieve latest timestamp and cumulative sum for a statistics ID.

    Returns ``(None, 0.0)`` only when no prior statistic genuinely exists.
    Raises :class:`StatisticsLookupError` on any recorder failure so the
    caller can skip the import cycle instead of restarting the cumulative
    sum from zero (which would regress existing rows).
    """
    try:
        from homeassistant.helpers.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            get_last_statistics,
            statistics_during_period,
        )

        # Prefer latest available sum regardless of age to preserve continuity
        # across long data gaps.
        result = await get_instance(hass).async_add_executor_job(
            get_last_statistics,
            hass,
            1,
            statistic_id,
            True,
            {"sum"},
        )
        if statistic_id in result and result[statistic_id]:
            latest = result[statistic_id][0]
            start_ts = latest.get("start")
            last_start = (
                dt_util.utc_from_timestamp(float(start_ts))
                if isinstance(start_ts, (int, float))
                else None
            )
            return last_start, float(latest.get("sum", 0.0) or 0.0)

        # Backward-compatible fallback for older recorder implementations.
        fallback = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            before - timedelta(days=7),
            before,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        if statistic_id in fallback and fallback[statistic_id]:
            latest = fallback[statistic_id][-1]
            start_ts = latest.get("start")
            last_start = (
                dt_util.utc_from_timestamp(float(start_ts))
                if isinstance(start_ts, (int, float))
                else None
            )
            return last_start, float(latest.get("sum", 0.0) or 0.0)
    except Exception as err:  # pylint: disable=broad-except
        # Do not fall back to a zero base: importing with last_sum=0.0 and no
        # skip guard would overwrite the existing rows with regressed sums.
        raise StatisticsLookupError(
            f"last-statistic lookup failed for {statistic_id}: {err}"
        ) from err

    # No prior statistic exists - a legitimate fresh start at zero.
    return None, 0.0


def _build_statistic_metadata(
    meter_serial: str, meter_type: str, statistic_id: str
) -> dict[str, Any]:
    """Build the StatisticMetaData kwargs for a meter's consumption stat."""
    fuel = "gas" if meter_type == METER_TYPE_GAS else "electricity"
    metadata_dict: dict[str, Any] = {
        "has_sum": True,
        "name": f"{meter_serial} {fuel.title()} Consumption",
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        "unit_class": "energy",
    }

    # Use mean_type (modern HA) with has_mean fallback (older HA).
    try:
        from homeassistant.components.recorder.models import StatisticMeanType

        metadata_dict["mean_type"] = StatisticMeanType.NONE
    except ImportError:
        metadata_dict["has_mean"] = False

    return metadata_dict


async def async_import_consumption_statistics(
    hass: HomeAssistant,
    meter_serial: str,
    meter_type: str,
    consumption_entries: list[dict[str, Any]],
) -> None:
    """Import recent half-hourly consumption as external statistics.

    The live path owns a rolling window (see :func:`live_window_start`).  Each
    call splices the window's hours into the stored series, overwriting hours
    that already exist and recomputing the cumulative sums, so hours that
    arrived late, partially or out of order are corrected on the next poll.
    Entries older than the window are ignored: older history belongs to the
    historical backfill, and keeping the two ranges apart is what stops them
    double-counting each other.  A poll that changes nothing writes nothing.
    """
    from homeassistant.helpers.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    window_start = live_window_start()
    hourly = {
        hour: kwh
        for hour, kwh in _group_consumption_by_hour(consumption_entries).items()
        if hour >= window_start
    }
    if not hourly:
        return

    statistic_id = statistic_id_for_meter(meter_serial, meter_type)
    if statistic_id is None:
        _LOGGER.warning(
            "Unknown meter type '%s' for serial %s; skipping statistics import",
            meter_type,
            meter_serial,
        )
        return

    # Splice from the window start (not the first hour that happens to have
    # data) so a daily bucket on the window's first day is always in view.
    chunk_min = window_start
    end = _hour_start(dt_util.utcnow()) + timedelta(hours=1)
    try:
        baseline_sum, existing = await _fetch_baseline_and_existing(
            hass, statistic_id, chunk_min, end
        )
    except StatisticsLookupError as err:
        # Skip this cycle entirely rather than import with a guessed base.
        _LOGGER.warning("Skipping statistics import for %s: %s", statistic_id, err)
        return

    series = _merge_and_recompute_series(
        baseline_sum, existing, hourly, supersede_daily_buckets=True
    )
    if _series_unchanged(existing, series):
        return

    statistics = [
        StatisticData(start=hour, state=cumulative, sum=cumulative)
        for hour, cumulative in series
    ]
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            **_build_statistic_metadata(meter_serial, meter_type, statistic_id)
        ),
        statistics,
    )
    # Make the write durable so a concurrent backfill chunk (which reads rows
    # back to rebase sums) never sees a stale series.
    await get_instance(hass).async_block_till_done()
    _LOGGER.debug(
        "Spliced %d hourly statistics for %s from %s",
        len(statistics),
        statistic_id,
        chunk_min.isoformat(),
    )


def _series_unchanged(
    existing: list[tuple[datetime, float]],
    series: list[tuple[datetime, float]],
) -> bool:
    """True when ``series`` matches the stored rows, so no write is needed."""
    if len(existing) != len(series):
        return False
    return all(
        old_hour == new_hour and abs(old_sum - new_sum) < 0.0005
        for (old_hour, old_sum), (new_hour, new_sum) in zip(existing, series)
    )


def _neutralise_superseded_daily_buckets(
    per_hour: dict[datetime, float], new_hourly: dict[datetime, float]
) -> None:
    """Zero out lone midnight rows that real hourly data is about to replace.

    The row is rewritten with no consumption rather than omitted: statistics
    are upserted by hour, so leaving it out of the written series would leave
    the stale bucket (and its large cumulative sum) in the recorder.
    """
    days: dict[Any, list[datetime]] = defaultdict(list)
    for hour in new_hourly:
        days[dt_util.as_local(hour).date()].append(hour)

    for day, new_hours in days.items():
        day_start = dt_util.as_utc(dt_util.start_of_local_day(day))
        day_end = dt_util.as_utc(
            dt_util.start_of_local_day(day + timedelta(days=1))
        )
        in_day = [h for h in per_hour if day_start <= h < day_end]
        if (
            len(new_hours) >= 2
            and in_day == [day_start]
            and day_start not in new_hourly
        ):
            per_hour[day_start] = 0.0


def _merge_and_recompute_series(
    baseline_sum: float,
    existing: list[tuple[datetime, float]],
    new_hourly: dict[datetime, float],
    *,
    daily_granularity: bool = False,
    supersede_daily_buckets: bool = False,
) -> list[tuple[datetime, float]]:
    """Splice new hours into an existing series and recompute cumulative sums.

    ``existing`` is the list of ``(hour, cumulative_sum)`` rows already stored
    from the first affected hour onward, sorted ascending; ``baseline_sum`` is
    the cumulative sum of the row immediately *before* them (0.0 if none).

    Per-hour consumption is reconstructed from the differences between
    consecutive cumulative sums, new hours overwrite existing ones, and the
    whole range is re-accumulated from ``baseline_sum`` so the series stays
    monotonic.  Returns ``(hour, cumulative_sum)`` for every hour in the range.

    When ``daily_granularity`` is set, each entry in ``new_hourly`` represents a
    whole local day collapsed into one hour bucket.  A daily bucket that landed
    on a day the coordinator already populated with finer half-hourly rows would
    only overwrite one of that day's ~24 hours and leave the rest - inflating
    the day by ~2x.  So a daily bucket is skipped for any day already covered by
    more than one existing row; the finer rows are more accurate and preserved.

    ``supersede_daily_buckets`` is the converse, for hourly data arriving over a
    day that currently holds a daily bucket (left by an older version or by the
    backfill).  If a local day's only existing row is a lone entry at the day's
    midnight that the new data does not itself cover, and the new data has real
    hours for that day, the lone row is the daily bucket and is zeroed - the
    day's hours replace it rather than adding on top of it.
    """
    per_hour: dict[datetime, float] = {}

    # Reconstruct each existing hour's own consumption from the sum deltas.
    prev_sum = baseline_sum
    for hour, cumulative in existing:
        per_hour[hour] = round(cumulative - prev_sum, 3)
        prev_sum = cumulative

    if supersede_daily_buckets and not daily_granularity:
        _neutralise_superseded_daily_buckets(per_hour, new_hourly)

    effective_new = dict(new_hourly)
    if daily_granularity:
        # A daily bucket's own hour is the day's local midnight in UTC, so the
        # local day spans exactly [day_hour, day_hour + 24h) - tz-robust without
        # any local-time conversion here.
        for day_hour in list(effective_new):
            day_end = day_hour + timedelta(hours=24)
            finer_existing = sum(1 for h in per_hour if day_hour <= h < day_end)
            if finer_existing > 1:
                del effective_new[day_hour]

    # New (backfilled) values are authoritative for their hour.
    for hour, kwh in effective_new.items():
        per_hour[hour] = round(kwh, 3)

    # Re-accumulate from the baseline across the merged, ordered hours.
    result: list[tuple[datetime, float]] = []
    running = baseline_sum
    for hour in sorted(per_hour):
        running = round(running + per_hour[hour], 3)
        result.append((hour, running))
    return result


async def _fetch_baseline_and_existing(
    hass: HomeAssistant,
    statistic_id: str,
    chunk_min: datetime,
    end: datetime,
) -> tuple[float, list[tuple[datetime, float]]]:
    """Return the baseline sum before *chunk_min* and existing rows in range.

    Raises :class:`StatisticsLookupError` on any recorder failure so the caller
    skips the import rather than corrupting the series.
    """
    try:
        from homeassistant.helpers.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        def _rows(start: datetime, stop: datetime) -> list[Any]:
            data = statistics_during_period(
                hass, start, stop, {statistic_id}, "hour", None, {"sum"}
            )
            return list(data.get(statistic_id, []))

        instance = get_instance(hass)

        # Baseline: the newest stored row strictly before the chunk.  The live
        # path calls this every poll, so look back a short way first and only
        # scan the whole history when that finds nothing.
        baseline_sum = 0.0
        for lookback_days in (14, 3650):
            before_rows = await instance.async_add_executor_job(
                _rows, chunk_min - timedelta(days=lookback_days), chunk_min
            )
            found = False
            for row in before_rows:
                start_dt = _row_start(row)
                if start_dt is not None and start_dt < chunk_min:
                    baseline_sum = float(row.get("sum", 0.0) or 0.0)
                    found = True
            if found:
                break

        # Existing rows from the chunk start onward (these get rewritten).
        after_rows = await instance.async_add_executor_job(_rows, chunk_min, end)
        existing: list[tuple[datetime, float]] = []
        for row in after_rows:
            start_dt = _row_start(row)
            if start_dt is None or start_dt < chunk_min:
                continue
            existing.append((start_dt, float(row.get("sum", 0.0) or 0.0)))
        existing.sort(key=lambda item: item[0])
        return baseline_sum, existing
    except Exception as err:  # pylint: disable=broad-except
        raise StatisticsLookupError(
            f"statistics lookup failed for {statistic_id}: {err}"
        ) from err


def _row_start(row: dict[str, Any]) -> datetime | None:
    start_ts = row.get("start")
    if isinstance(start_ts, (int, float)):
        return dt_util.utc_from_timestamp(float(start_ts))
    return None


async def async_import_historical_statistics(
    hass: HomeAssistant,
    meter_serial: str,
    meter_type: str,
    consumption_entries: list[dict[str, Any]],
    *,
    daily_granularity: bool = False,
) -> None:
    """Import *historical* consumption without suspending live imports.

    Unlike the append-only live path, this splices earlier hours into the
    existing series and rewrites every subsequent cumulative sum so the series
    stays monotonic - letting the historical backfill run concurrently with
    live 30-minute imports.  Adding a constant to all later sums does not
    change the per-period deltas the Energy Dashboard displays.

    Set ``daily_granularity`` when ``consumption_entries`` are daily buckets so
    a day the coordinator already imported at half-hourly resolution is not
    double-counted (see :func:`_merge_and_recompute_series`).
    """
    from homeassistant.helpers.recorder import get_instance
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )

    hourly = _group_consumption_by_hour(consumption_entries)
    if not hourly:
        return

    statistic_id = statistic_id_for_meter(meter_serial, meter_type)
    if statistic_id is None:
        _LOGGER.warning(
            "Unknown meter type '%s' for serial %s; skipping statistics import",
            meter_type,
            meter_serial,
        )
        return

    chunk_min = min(hourly)
    end = _hour_start(dt_util.utcnow()) + timedelta(hours=1)

    try:
        baseline_sum, existing = await _fetch_baseline_and_existing(
            hass, statistic_id, chunk_min, end
        )
    except StatisticsLookupError as err:
        _LOGGER.warning(
            "Skipping historical statistics import for %s: %s", statistic_id, err
        )
        return

    series = _merge_and_recompute_series(
        baseline_sum, existing, hourly, daily_granularity=daily_granularity
    )
    if not series:
        return

    metadata_dict = _build_statistic_metadata(meter_serial, meter_type, statistic_id)
    statistics = [
        StatisticData(start=hour, state=cumulative, sum=cumulative)
        for hour, cumulative in series
    ]

    async_add_external_statistics(
        hass, StatisticMetaData(**metadata_dict), statistics
    )
    # Make this write durable before returning.  The recompute-forward design
    # reads existing rows back to rebase later sums, so a same-cycle consecutive
    # chunk (requests_per_run > 1 with delay_seconds = 0) must not read a stale
    # baseline and emit a regressive, non-monotonic sum.  ``async_add_external_
    # statistics`` only queues the write; block until the recorder has applied
    # it so the next read - and any reader - sees a committed series.
    await get_instance(hass).async_block_till_done()
    _LOGGER.debug(
        "Backfilled %d hourly statistics for %s (rewrote from %s)",
        len(statistics),
        statistic_id,
        chunk_min.isoformat(),
    )
