"""Historical statistics backfill manager for Eon Next."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, timedelta
import logging
from typing import Any, TypedDict

from homeassistant.core import callback
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    BACKFILL_HH_MAX_CHUNK_DAYS,
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_INCLUDE_ELECTRICITY,
    CONF_BACKFILL_INCLUDE_GAS,
    CONF_BACKFILL_LOOKBACK_DAYS,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
    CONF_BACKFILL_RUN_INTERVAL_MINUTES,
    DEFAULT_BACKFILL_CHUNK_DAYS,
    DEFAULT_BACKFILL_DELAY_SECONDS,
    DEFAULT_BACKFILL_ENABLED,
    DEFAULT_BACKFILL_INCLUDE_ELECTRICITY,
    DEFAULT_BACKFILL_INCLUDE_GAS,
    DEFAULT_BACKFILL_LOOKBACK_DAYS,
    DEFAULT_BACKFILL_REBUILD_STATISTICS,
    DEFAULT_BACKFILL_REQUESTS_PER_RUN,
    DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
    DOMAIN,
)
from .eonnext import (
    EonNextApiError,
    EonNextAuthError,
    METER_TYPE_ELECTRIC,
    METER_TYPE_GAS,
)
from .statistics import (
    async_import_historical_statistics,
    cost_statistic_id_for_meter,
    statistic_id_for_meter,
)

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1


class MeterBackfillState(TypedDict):
    """Backfill state for one meter."""

    next_start: str
    done: bool
    # Whether this meter's statistics have been cleared for the current backfill
    # run (per-meter so one fuel can be re-run without disturbing the other).
    # Read with ``.get("cleared", True)`` so pre-upgrade state is treated as
    # already cleared and never surprise-wiped.
    cleared: bool


class BackfillState(TypedDict):
    """Persisted backfill state."""

    initialized: bool
    rebuild_done: bool
    lookback_days: int
    meters: dict[str, MeterBackfillState]
    # Last-applied per-fuel include flags, so a fuel toggled off then on again
    # is detected as a re-run request for just that fuel.
    include: dict[str, bool]


class BackfillStatus(TypedDict):
    """Runtime status snapshot for historical backfill."""

    state: str
    enabled: bool
    initialized: bool
    rebuild_done: bool
    lookback_days: int
    total_meters: int
    completed_meters: int
    pending_meters: int
    next_start_date: str | None
    meters_progress: dict[str, dict[str, Any]]


class EonNextBackfillManager:
    """Manage slow, resumable historical statistics backfill."""

    def __init__(self, hass, entry, api, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self.coordinator = coordinator
        self._store: Store[BackfillState] = Store(
            hass, _STORE_VERSION, f"{DOMAIN}_{entry.entry_id}_backfill"
        )
        self._state: BackfillState | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []

    async def async_prime(self) -> None:
        """Load persisted backfill state before the first refresh."""
        await self._ensure_state_loaded()

    async def async_start(self) -> None:
        """Start background backfill loop."""
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._async_run(),
            "eon_next_historical_backfill",
        )

    async def async_stop(self) -> None:
        """Stop background backfill loop."""
        if not self._task:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    @callback
    def async_add_listener(self, update_callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback for status updates."""
        self._listeners.append(update_callback)

        @callback
        def _remove_listener() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove_listener

    @callback
    def _notify_listeners(self) -> None:
        """Notify registered listeners that status changed."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("Backfill status listener failed", exc_info=True)

    def _backfill_enabled(self) -> bool:
        return bool(
            self.entry.options.get(CONF_BACKFILL_ENABLED, DEFAULT_BACKFILL_ENABLED)
        )

    def _backfill_lookback_days(self) -> int:
        value = int(
            self.entry.options.get(
                CONF_BACKFILL_LOOKBACK_DAYS, DEFAULT_BACKFILL_LOOKBACK_DAYS
            )
        )
        return max(1, value)

    def _backfill_chunk_days(self) -> int:
        value = int(
            self.entry.options.get(CONF_BACKFILL_CHUNK_DAYS, DEFAULT_BACKFILL_CHUNK_DAYS)
        )
        return max(1, value)

    def _backfill_requests_per_run(self) -> int:
        value = int(
            self.entry.options.get(
                CONF_BACKFILL_REQUESTS_PER_RUN, DEFAULT_BACKFILL_REQUESTS_PER_RUN
            )
        )
        return max(1, value)

    def _backfill_run_interval_minutes(self) -> int:
        value = int(
            self.entry.options.get(
                CONF_BACKFILL_RUN_INTERVAL_MINUTES,
                DEFAULT_BACKFILL_RUN_INTERVAL_MINUTES,
            )
        )
        return max(1, value)

    def _backfill_delay_seconds(self) -> int:
        value = int(
            self.entry.options.get(
                CONF_BACKFILL_DELAY_SECONDS, DEFAULT_BACKFILL_DELAY_SECONDS
            )
        )
        return max(0, value)

    def _backfill_rebuild_statistics(self) -> bool:
        return bool(
            self.entry.options.get(
                CONF_BACKFILL_REBUILD_STATISTICS, DEFAULT_BACKFILL_REBUILD_STATISTICS
            )
        )

    def _backfill_include_electricity(self) -> bool:
        return bool(
            self.entry.options.get(
                CONF_BACKFILL_INCLUDE_ELECTRICITY, DEFAULT_BACKFILL_INCLUDE_ELECTRICITY
            )
        )

    def _backfill_include_gas(self) -> bool:
        return bool(
            self.entry.options.get(
                CONF_BACKFILL_INCLUDE_GAS, DEFAULT_BACKFILL_INCLUDE_GAS
            )
        )

    def _include_flags(self) -> dict[str, bool]:
        """Current per-fuel include flags, keyed by meter type."""
        return {
            METER_TYPE_ELECTRIC: self._backfill_include_electricity(),
            METER_TYPE_GAS: self._backfill_include_gas(),
        }

    async def _ensure_state_loaded(self) -> None:
        if self._state is not None:
            return

        loaded = await self._store.async_load()
        self._state = {
            "initialized": bool(loaded.get("initialized", False)) if loaded else False,
            "rebuild_done": bool(loaded.get("rebuild_done", False)) if loaded else False,
            "lookback_days": int(loaded.get("lookback_days", 0)) if loaded else 0,
            "meters": dict(loaded.get("meters", {})) if loaded else {},
            "include": dict(loaded.get("include", {})) if loaded else {},
        }
        # Migrate pre-per-fuel state: a meter saved without a per-meter
        # ``cleared`` flag inherits the old global ``rebuild_done`` value, so
        # upgrading preserves whether its statistics were already cleared and
        # never re-wipes an established series.
        rebuild_done = self._state["rebuild_done"]
        for meter_state in self._state["meters"].values():
            meter_state.setdefault("cleared", rebuild_done)

    async def _save_state(self) -> None:
        if self._state is None:
            return
        await self._store.async_save(self._state)
        self._notify_listeners()

    def _eligible_meters(self) -> list[Any]:
        include = self._include_flags()
        meters: list[Any] = []
        for account in self.api.accounts:
            for meter in account.meters:
                if meter.type not in (METER_TYPE_GAS, METER_TYPE_ELECTRIC):
                    continue
                # Per-fuel include switches: an excluded fuel's meters are never
                # initialised, cleared, processed or shown, so the other fuel's
                # statistics are left completely untouched.
                if not include.get(meter.type, True):
                    continue
                if not meter.serial or not meter.supply_point_id:
                    continue
                meters.append(meter)
        return meters

    @staticmethod
    def _utc_boundary_iso(day: date) -> str:
        """Local midnight of *day* as a UTC ISO 8601 timestamp.

        Chunk boundaries must be true local-day edges: formatting a local date
        with a literal ``Z`` labels a local midnight as UTC, shifting the
        window by the local offset (an hour off during BST for the whole UK).
        """
        return (
            dt_util.as_utc(dt_util.start_of_local_day(day))
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def _meter_is_tou(self, meter: Any) -> bool:
        """Whether this meter is on a time-of-use tariff.

        Read from the coordinator's latest snapshot (keyed by meter serial).
        Time-of-use meters are backfilled at half-hourly resolution so historic
        days can be split into peak/off-peak; flat-rate meters stay on cheaper
        daily buckets.  Defaults to False when the snapshot is missing, so a
        meter never upgrades to the heavier fetch on incomplete data.
        """
        data = getattr(self.coordinator, "data", None) or {}
        meter_data = data.get(meter.serial) or {}
        return bool(meter_data.get("tariff_is_tou", False))

    @staticmethod
    def _entries_are_half_hourly(entries: list[dict[str, Any]]) -> bool:
        """False when entries carry intervals longer than one half-hour.

        E.ON only holds half-hourly data back to the smart meter's install, so a
        half-hourly request for older history returns daily (or empty) buckets;
        this lets the backfill fall back to a daily import for those chunks.
        Entries without parseable bounds are given the benefit of the doubt.
        """
        for entry in entries[:3]:
            start = dt_util.parse_datetime(str(entry.get("interval_start") or ""))
            end = dt_util.parse_datetime(str(entry.get("interval_end") or ""))
            if start and end and (end - start) > timedelta(minutes=31):
                return False
        return True

    async def _fetch_chunk(
        self,
        meter: Any,
        start_date: date,
        end_date: date,
        *,
        half_hourly: bool,
    ) -> list[dict[str, Any]]:
        """Fetch one consumption chunk, half-hourly or daily.

        Half-hourly asks for the window with ``group_by`` omitted (E.ON rejects
        ``group_by=half_hour``); daily uses ``group_by=day``.  Returns the raw
        results list (possibly empty).  Raises :class:`EonNextApiError` on
        transport failure so the caller can leave the cursor for a retry.
        """
        period_from = self._utc_boundary_iso(start_date)
        period_to = self._utc_boundary_iso(end_date + timedelta(days=1))
        day_count = (end_date - start_date).days + 1
        if half_hourly:
            # 48 slots a day, plus headroom for a 25-hour DST day.
            page_size = day_count * 48 + 48
            group_by = None
        else:
            page_size = day_count
            group_by = "day"
        result = await self.api.async_get_consumption(
            meter.type,
            meter.supply_point_id,
            meter.serial,
            group_by=group_by,
            page_size=page_size,
            period_from=period_from,
            period_to=period_to,
        )
        return (result or {}).get("results") or []

    async def _wait_or_stop(self, seconds: int) -> bool:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=max(1, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _all_done_for_meters(self, meters: list[Any]) -> bool:
        if self._state is None:
            return False
        if not meters:
            return True
        meter_state = self._state["meters"]
        return all(meter_state.get(meter.serial, {}).get("done", False) for meter in meters)

    @callback
    def get_status(self) -> BackfillStatus:
        """Return a status snapshot for diagnostics."""
        enabled = self._backfill_enabled()
        meters = self._eligible_meters()
        total_meters = len(meters)

        initialized = bool(self._state and self._state["initialized"])
        rebuild_done = bool(self._state and self._state["rebuild_done"])
        lookback_days = (
            int(self._state["lookback_days"])
            if self._state and self._state["lookback_days"] > 0
            else self._backfill_lookback_days()
        )

        completed_meters = 0
        pending_meters = total_meters
        next_start_date: str | None = None
        meters_progress: dict[str, dict[str, Any]] = {}

        today = dt_util.now().date()

        if self._state is not None:
            meter_state = self._state["meters"]
            completed_meters = sum(
                1 for meter in meters if meter_state.get(meter.serial, {}).get("done", False)
            )
            pending_meters = max(total_meters - completed_meters, 0)
            pending_dates = [
                str(meter_state[meter.serial]["next_start"])
                for meter in meters
                if meter.serial in meter_state
                and not meter_state[meter.serial].get("done", False)
                and meter_state[meter.serial].get("next_start")
            ]
            if pending_dates:
                next_start_date = min(pending_dates)

            # Build per-meter progress details.
            backfill_start = today - timedelta(days=lookback_days - 1)
            for meter in meters:
                ms = meter_state.get(meter.serial)
                if ms is None:
                    meters_progress[meter.serial] = {
                        "done": False,
                        "next_start": None,
                        "days_completed": 0,
                        "days_remaining": lookback_days,
                    }
                    continue
                is_done = ms.get("done", False)
                try:
                    ns = date.fromisoformat(ms["next_start"])
                except (ValueError, KeyError):
                    ns = backfill_start
                days_completed = max((ns - backfill_start).days, 0)
                days_remaining = max(lookback_days - days_completed, 0)
                meters_progress[meter.serial] = {
                    "done": is_done,
                    "next_start": ms.get("next_start"),
                    "days_completed": days_completed,
                    "days_remaining": days_remaining,
                }

        if not enabled:
            state = "disabled"
        elif initialized and pending_meters == 0:
            state = "completed"
        elif initialized:
            state = "running"
        else:
            state = "initializing"

        return {
            "state": state,
            "enabled": enabled,
            "initialized": initialized,
            "rebuild_done": rebuild_done,
            "lookback_days": lookback_days,
            "total_meters": total_meters,
            "completed_meters": completed_meters,
            "pending_meters": pending_meters,
            "next_start_date": next_start_date,
            "meters_progress": meters_progress,
        }

    async def _initialize_or_reset_progress(self, meters: list[Any]) -> None:
        if self._state is None:
            return

        lookback_days = self._backfill_lookback_days()
        today = dt_util.now().date()
        start = today - timedelta(days=lookback_days - 1)
        stored_lookback = int(self._state["lookback_days"])
        include_now = self._include_flags()

        def fresh() -> MeterBackfillState:
            return {"next_start": start.isoformat(), "done": False, "cleared": False}

        if not self._state["initialized"]:
            self._state["initialized"] = True
            self._state["rebuild_done"] = False
            self._state["lookback_days"] = lookback_days
            self._state["include"] = include_now
            self._state["meters"] = {meter.serial: fresh() for meter in meters}
            await self._save_state()
            _LOGGER.debug(
                "Initialized historical backfill progress from %s (%d days)",
                start,
                lookback_days,
            )
            return

        if lookback_days > stored_lookback:
            # Window extended further back: move each meter's cursor to the new
            # (earlier) start so the newly-included older days are fetched, and
            # re-run the rebuild step.  A *shrink* deliberately does NOT wipe
            # progress or clear statistics outside the new window - that would
            # destroy already-imported history the user asked to keep.
            self._state["lookback_days"] = lookback_days
            self._state["rebuild_done"] = False
            self._state["include"] = include_now
            for meter in meters:
                self._state["meters"][meter.serial] = fresh()
            await self._save_state()
            _LOGGER.debug(
                "Extended historical backfill window to %d days (from %s)",
                lookback_days,
                start,
            )
            return

        changed = False
        if lookback_days < stored_lookback:
            # Record the smaller window (for status/UX) but keep progress and
            # existing statistics intact.
            self._state["lookback_days"] = lookback_days
            changed = True

        # Re-inclusion: a fuel toggled off and then back on is a request to
        # re-run just that fuel - reset its meters (so its stats are re-cleared
        # and re-imported) while the other fuel is left exactly as it was.  A
        # fuel that was already on, or that has no prior snapshot (pre-upgrade
        # state), is never reset, so upgrades don't surprise-wipe anyone.
        include_prev = self._state.get("include") or {}
        reinstated = {
            fuel
            for fuel, on in include_now.items()
            if on and include_prev.get(fuel, True) is False
        }
        if include_prev != include_now:
            self._state["include"] = include_now
            changed = True
        for meter in meters:
            if meter.type in reinstated:
                self._state["meters"][meter.serial] = fresh()
                self._state["rebuild_done"] = False
                changed = True

        for meter in meters:
            if meter.serial in self._state["meters"]:
                continue
            self._state["meters"][meter.serial] = fresh()
            changed = True
        if changed:
            await self._save_state()

    async def _clear_existing_statistics(self, meters: list[Any]) -> None:
        if self._state is None:
            return

        meter_states = self._state["meters"]

        if not self._backfill_rebuild_statistics():
            # Not clearing: mark eligible meters cleared so they are not retried,
            # and note the run won't wipe pre-existing statistics.
            changed = False
            for meter in meters:
                state = meter_states.get(meter.serial)
                if state is not None and not state.get("cleared", True):
                    state["cleared"] = True
                    changed = True
            if not self._state["rebuild_done"]:
                self._state["rebuild_done"] = True
                changed = True
                _LOGGER.info(
                    "Historical backfill is running without clearing existing "
                    "statistics; results may be partial if prior statistics "
                    "already exist"
                )
            if changed:
                await self._save_state()
            return

        # Clear only the meters not yet cleared for this run, so re-running a
        # single fuel never touches the other fuel's statistics.
        to_clear = [
            meter
            for meter in meters
            if not meter_states.get(meter.serial, {}).get("cleared", True)
        ]
        if not to_clear:
            if not self._state["rebuild_done"]:
                self._state["rebuild_done"] = True
                await self._save_state()
            return

        statistic_ids = [
            statistic_id
            for meter in to_clear
            for build_id in (statistic_id_for_meter, cost_statistic_id_for_meter)
            if (statistic_id := build_id(meter.serial, meter.type)) is not None
        ]
        if statistic_ids:
            done = asyncio.Event()
            # The recorder calls ``on_done`` from the recorder thread;
            # ``Event.set`` is not thread-safe, so hop back onto the event loop
            # to wake the waiter promptly instead of appearing to time out.
            get_instance(self.hass).async_clear_statistics(
                statistic_ids,
                on_done=lambda: self.hass.loop.call_soon_threadsafe(done.set),
            )
            try:
                await asyncio.wait_for(done.wait(), timeout=120)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Timed out waiting for recorder statistics clear to complete"
                )
            _LOGGER.info(
                "Cleared %d statistics IDs before historical backfill rebuild",
                len(statistic_ids),
            )

        for meter in to_clear:
            meter_states[meter.serial]["cleared"] = True
        self._state["rebuild_done"] = True
        await self._save_state()

    async def _run_backfill_cycle(self) -> None:
        if self._state is None or not self._backfill_enabled():
            return

        meters = self._eligible_meters()
        if not meters:
            return

        await self._initialize_or_reset_progress(meters)
        await self._clear_existing_statistics(meters)

        if self._all_done_for_meters(meters):
            return

        requests_remaining = self._backfill_requests_per_run()
        chunk_days = self._backfill_chunk_days()
        delay_seconds = self._backfill_delay_seconds()
        made_progress = False

        # Spend the per-run request budget across meters, allowing multiple
        # chunks per meter per cycle.  Live imports are never suspended now -
        # each chunk is spliced in and later sums recomputed - so there is no
        # reason to limit a cycle to one chunk per meter.
        while requests_remaining > 0 and not self._all_done_for_meters(meters):
            progressed_this_pass = False

            for meter in meters:
                if requests_remaining <= 0:
                    break

                today = dt_util.now().date()
                # Backfill only complete days: today is owned exclusively by the
                # coordinator's half-hourly import; importing today's partial
                # daily bucket would be double-counted once half-hours arrive.
                yesterday = today - timedelta(days=1)

                # In practice init seeds every eligible meter, so the default
                # is a type-complete fallback that is never actually used.
                meter_state = self._state["meters"].setdefault(
                    meter.serial,
                    {"next_start": today.isoformat(), "done": False, "cleared": True},
                )
                if meter_state["done"]:
                    continue

                try:
                    start_date = date.fromisoformat(meter_state["next_start"])
                except ValueError:
                    start_date = today

                if start_date > yesterday:
                    meter_state["done"] = True
                    await self._save_state()
                    continue

                # Time-of-use meters are fetched half-hourly so historic days
                # can be split into peak/off-peak; the chunk span is capped to
                # keep the half-hourly page_size under E.ON's limit.  Flat-rate
                # meters stay on cheaper daily buckets.
                is_tou = self._meter_is_tou(meter)
                span = (
                    min(chunk_days, BACKFILL_HH_MAX_CHUNK_DAYS) if is_tou else chunk_days
                )
                end_date = min(start_date + timedelta(days=span - 1), yesterday)
                try:
                    consumption = await self._fetch_chunk(
                        meter, start_date, end_date, half_hourly=is_tou
                    )
                    half_hourly = (
                        is_tou
                        and bool(consumption)
                        and self._entries_are_half_hourly(consumption)
                    )
                    if is_tou and not half_hourly:
                        # E.ON holds no half-hourly data this far back (or none
                        # at all): fall back to a daily import for this chunk so
                        # TOU meters still backfill fully, just without a split.
                        consumption = await self._fetch_chunk(
                            meter, start_date, end_date, half_hourly=False
                        )
                except EonNextApiError as err:
                    # Transport/server error: leave the cursor untouched so this
                    # chunk is retried next cycle instead of leaving a permanent
                    # hole in history.
                    _LOGGER.debug(
                        "Backfill chunk %s→%s failed for meter %s; will retry: %s",
                        start_date,
                        end_date,
                        meter.serial,
                        err,
                    )
                    requests_remaining -= 1
                    continue

                if consumption:
                    # Half-hourly buckets are imported as hourly rows (splittable
                    # by time-of-use); supersede any lone daily bucket a prior
                    # flat-rate backfill left on the same day.  Daily buckets are
                    # flagged so a day the coordinator already imported at
                    # half-hourly resolution is not double-counted.
                    await async_import_historical_statistics(
                        self.hass,
                        meter.serial,
                        meter.type,
                        consumption,
                        daily_granularity=not half_hourly,
                        supersede_daily_buckets=half_hourly,
                    )
                    await self.coordinator.async_import_costs(
                        meter, dt_util.as_utc(dt_util.start_of_local_day(start_date))
                    )

                meter_state["next_start"] = (end_date + timedelta(days=1)).isoformat()
                meter_state["done"] = end_date >= yesterday
                await self._save_state()
                made_progress = True
                progressed_this_pass = True
                requests_remaining -= 1

                if requests_remaining > 0 and delay_seconds > 0:
                    if await self._wait_or_stop(delay_seconds):
                        return

            if not progressed_this_pass:
                # No meter advanced this pass (all done, or all erroring with
                # budget exhausted); stop to avoid spinning.
                break

        if made_progress and self._all_done_for_meters(meters):
            _LOGGER.info("Historical backfill completed")

    async def _async_run(self) -> None:
        await self._ensure_state_loaded()
        while not self._stop_event.is_set():
            try:
                if self._backfill_enabled():
                    await self._run_backfill_cycle()
            except EonNextAuthError as err:
                # A bad/expired token during backfill must start HA re-auth,
                # not be swallowed as a warning - otherwise the loop hammers
                # the API with a dead token every cycle forever.
                _LOGGER.warning(
                    "Historical backfill stopping to trigger re-auth: %s", err
                )
                self.entry.async_start_reauth(self.hass)
                break
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Historical backfill cycle failed: %s", err)

            if await self._wait_or_stop(self._backfill_run_interval_minutes() * 60):
                break
