"""Unit tests for historical backfill manager."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.eon_next import backfill as backfill_module
from custom_components.eon_next.backfill import EonNextBackfillManager
from custom_components.eon_next.const import (
    CONF_BACKFILL_CHUNK_DAYS,
    CONF_BACKFILL_DELAY_SECONDS,
    CONF_BACKFILL_ENABLED,
    CONF_BACKFILL_INCLUDE_ELECTRICITY,
    CONF_BACKFILL_INCLUDE_GAS,
    CONF_BACKFILL_LOOKBACK_DAYS,
    CONF_BACKFILL_REBUILD_STATISTICS,
    CONF_BACKFILL_REQUESTS_PER_RUN,
)

# Dynamic reference dates - keep tests valid regardless of when they run.
_REF_DT = datetime.now(tz=timezone.utc).replace(
    hour=0, minute=0, second=0, microsecond=0,
)
_REF_DATE = _REF_DT.date()
_REF_DATE_ISO = _REF_DATE.isoformat()
_REF_PREV_ISO = (_REF_DATE - timedelta(days=1)).isoformat()
_REF_NEXT_ISO = (_REF_DATE + timedelta(days=1)).isoformat()
_REF_PLUS2_ISO = (_REF_DATE + timedelta(days=2)).isoformat()
_LOOKBACK_START_ISO = (_REF_DATE - timedelta(days=9)).isoformat()  # 10-day lookback


def _meter(serial: str, meter_type: str = "electricity") -> SimpleNamespace:
    return SimpleNamespace(
        serial=serial,
        type=meter_type,
        supply_point_id=f"sp-{serial}",
    )


def _manager(
    options: dict,
    meters: list[SimpleNamespace],
) -> EonNextBackfillManager:
    hass = SimpleNamespace(
        data={},
        config=SimpleNamespace(config_dir="/tmp"),
    )
    entry = SimpleNamespace(
        entry_id="entry123",
        options=options,
        async_create_background_task=Mock(),
    )
    api = SimpleNamespace(accounts=[SimpleNamespace(meters=meters)])
    coordinator = SimpleNamespace(set_statistics_import_enabled=Mock())
    return EonNextBackfillManager(hass, entry, api, coordinator)


def test_get_status_disabled_by_default() -> None:
    """Backfill status should be disabled when option is off."""
    manager = _manager({}, [_meter("m1"), _meter("m2")])
    manager._state = {
        "initialized": False,
        "rebuild_done": False,
        "lookback_days": 0,
        "meters": {},
    }

    status = manager.get_status()
    assert status["state"] == "disabled"
    assert status["total_meters"] == 2
    assert status["pending_meters"] == 2


def test_get_status_running_and_completed() -> None:
    """Status transitions from running to completed when all cursors are done."""
    manager = _manager({CONF_BACKFILL_ENABLED: True}, [_meter("m1"), _meter("m2")])
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {
            "m1": {"next_start": _REF_NEXT_ISO, "done": False},
            "m2": {"next_start": _REF_DATE_ISO, "done": True},
        },
    }

    status = manager.get_status()
    assert status["state"] == "running"
    assert status["completed_meters"] == 1
    assert status["pending_meters"] == 1
    assert status["next_start_date"] == _REF_NEXT_ISO

    manager._state["meters"]["m1"]["done"] = True
    manager._state["meters"]["m1"]["next_start"] = _REF_PLUS2_ISO
    status = manager.get_status()
    assert status["state"] == "completed"
    assert status["pending_meters"] == 0


def test_get_status_tolerates_incomplete_meter_state() -> None:
    """A persisted meter entry missing done/next_start must not crash status.

    _ensure_state_loaded copies the stored dict verbatim, so a partial write
    or schema drift can leave an entry without these keys; get_status is read
    by the diagnostic sensor and the dashboard status panel, so it must not
    raise KeyError.
    """
    manager = _manager({CONF_BACKFILL_ENABLED: True}, [_meter("m1"), _meter("m2")])
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {
            # Entry present but missing both "done" and "next_start".
            "m1": {},
            "m2": {"next_start": _REF_DATE_ISO, "done": False},
        },
    }

    status = manager.get_status()
    # m1 (no next_start) is skipped for the earliest-pending calculation
    # rather than raising, so m2's date wins.
    assert status["next_start_date"] == _REF_DATE_ISO


@pytest.mark.asyncio
async def test_initialize_or_reset_progress_uses_lookback(monkeypatch) -> None:
    """Initialization should seed one cursor per meter using lookback setting."""
    manager = _manager(
        {
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_LOOKBACK_DAYS: 10,
        },
        [_meter("m1"), _meter("m2", "gas")],
    )
    manager._state = {
        "initialized": False,
        "rebuild_done": False,
        "lookback_days": 0,
        "meters": {},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT,
    )

    await manager._initialize_or_reset_progress(manager._eligible_meters())

    assert manager._state["initialized"] is True
    assert manager._state["lookback_days"] == 10
    assert manager._state["meters"]["m1"]["next_start"] == _LOOKBACK_START_ISO
    assert manager._state["meters"]["m2"]["next_start"] == _LOOKBACK_START_ISO


@pytest.mark.asyncio
async def test_clear_existing_statistics_without_rebuild_marks_done() -> None:
    """Without rebuild enabled, manager should skip clearing and mark rebuild done."""
    manager = _manager(
        {
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_REBUILD_STATISTICS: False,
        },
        [_meter("m1")],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": False,
        "lookback_days": 3650,
        "meters": {"m1": {"next_start": _REF_DATE_ISO, "done": False}},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]

    await manager._clear_existing_statistics(manager._eligible_meters())

    assert manager._state["rebuild_done"] is True
    manager._save_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_backfill_cycle_advances_cursor_and_imports(monkeypatch) -> None:
    """A cycle should import one chunk and advance meter cursor."""
    meter = _meter("m1", "electricity")
    manager = _manager(
        {
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_CHUNK_DAYS: 1,
            CONF_BACKFILL_REQUESTS_PER_RUN: 1,
            CONF_BACKFILL_DELAY_SECONDS: 0,
        },
        [meter],
    )
    # Cursor starts at yesterday: backfill only imports complete days, so
    # yesterday is the newest chunk it will process (today is coordinator-owned).
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {"m1": {"next_start": _REF_PREV_ISO, "done": False}},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    manager.api.async_get_consumption = AsyncMock(  # type: ignore[attr-defined]
        return_value={"results": [{"interval_start": _REF_PREV_ISO, "consumption": 1.5}]}
    )
    import_mock = AsyncMock()
    monkeypatch.setattr(
        backfill_module,
        "async_import_historical_statistics",
        import_mock,
    )
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT.replace(hour=12),
    )

    await manager._run_backfill_cycle()

    manager.api.async_get_consumption.assert_awaited_once()
    import_mock.assert_awaited_once()
    # Advanced from yesterday to today; done because it reached yesterday.
    assert manager._state["meters"]["m1"]["next_start"] == _REF_DATE_ISO
    assert manager._state["meters"]["m1"]["done"] is True


@pytest.mark.asyncio
async def test_run_backfill_cycle_leaves_cursor_on_api_error(monkeypatch) -> None:
    """A transport error must not advance the cursor (spec 02, 2.2).

    Advancing past a failed chunk leaves a permanent, never-retried hole while
    the state machine falsely reports progress.
    """
    meter = _meter("m1", "electricity")
    manager = _manager(
        {
            CONF_BACKFILL_ENABLED: True,
            CONF_BACKFILL_CHUNK_DAYS: 1,
            CONF_BACKFILL_REQUESTS_PER_RUN: 1,
            CONF_BACKFILL_DELAY_SECONDS: 0,
        },
        [meter],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 3650,
        "meters": {"m1": {"next_start": _REF_PREV_ISO, "done": False}},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]
    manager.api.async_get_consumption = AsyncMock(  # type: ignore[attr-defined]
        side_effect=backfill_module.EonNextApiError("boom")
    )
    import_mock = AsyncMock()
    monkeypatch.setattr(
        backfill_module,
        "async_import_historical_statistics",
        import_mock,
    )
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT.replace(hour=12),
    )

    await manager._run_backfill_cycle()

    manager.api.async_get_consumption.assert_awaited_once()
    import_mock.assert_not_awaited()
    # Cursor untouched and not marked done: the chunk is retried next cycle.
    assert manager._state["meters"]["m1"]["next_start"] == _REF_PREV_ISO
    assert manager._state["meters"]["m1"]["done"] is False


# --- meters_progress attribute tests ---


def test_meters_progress_all_done(monkeypatch) -> None:
    """When all meters are done, days_completed should equal lookback_days."""
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT,
    )
    manager = _manager(
        {CONF_BACKFILL_ENABLED: True, CONF_BACKFILL_LOOKBACK_DAYS: 10},
        [_meter("m1")],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {
            "m1": {"next_start": _REF_NEXT_ISO, "done": True},
        },
    }

    status = manager.get_status()
    prog = status["meters_progress"]
    assert "m1" in prog
    assert prog["m1"]["done"] is True
    assert prog["m1"]["days_completed"] == 10
    assert prog["m1"]["days_remaining"] == 0


def test_meters_progress_partial(monkeypatch) -> None:
    """A meter partway through backfill reports correct split."""
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT,
    )
    # 10-day lookback: start is _REF_DATE - 9 days.
    # next_start halfway: _REF_DATE - 4 days (5 days completed).
    halfway = (_REF_DATE - timedelta(days=4)).isoformat()
    manager = _manager(
        {CONF_BACKFILL_ENABLED: True, CONF_BACKFILL_LOOKBACK_DAYS: 10},
        [_meter("m1")],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {
            "m1": {"next_start": halfway, "done": False},
        },
    }

    status = manager.get_status()
    prog = status["meters_progress"]["m1"]
    assert prog["done"] is False
    assert prog["days_completed"] == 5
    assert prog["days_remaining"] == 5
    assert prog["next_start"] == halfway


def test_meters_progress_missing_meter_state(monkeypatch) -> None:
    """A meter not yet in persisted state gets zero progress."""
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT,
    )
    manager = _manager(
        {CONF_BACKFILL_ENABLED: True, CONF_BACKFILL_LOOKBACK_DAYS: 10},
        [_meter("m1"), _meter("m2")],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {
            "m1": {"next_start": _REF_DATE_ISO, "done": True},
            # m2 intentionally missing from state
        },
    }

    status = manager.get_status()
    assert status["meters_progress"]["m2"]["done"] is False
    assert status["meters_progress"]["m2"]["days_completed"] == 0
    assert status["meters_progress"]["m2"]["days_remaining"] == 10


def test_meters_progress_invalid_next_start(monkeypatch) -> None:
    """An invalid next_start should fall back to backfill_start (0 days)."""
    monkeypatch.setattr(
        backfill_module.dt_util,
        "now",
        lambda: _REF_DT,
    )
    manager = _manager(
        {CONF_BACKFILL_ENABLED: True, CONF_BACKFILL_LOOKBACK_DAYS: 10},
        [_meter("m1")],
    )
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {
            "m1": {"next_start": "not-a-date", "done": False},
        },
    }

    status = manager.get_status()
    prog = status["meters_progress"]["m1"]
    assert prog["days_completed"] == 0
    assert prog["days_remaining"] == 10


# --- Per-fuel backfill switches -------------------------------------------


def test_eligible_meters_respects_fuel_switches() -> None:
    meters = [_meter("e1", "electricity"), _meter("g1", "gas")]

    def serials(options: dict) -> set[str]:
        return {m.serial for m in _manager(options, meters)._eligible_meters()}

    assert serials({}) == {"e1", "g1"}  # default: both
    assert serials({CONF_BACKFILL_INCLUDE_GAS: False}) == {"e1"}
    assert serials({CONF_BACKFILL_INCLUDE_ELECTRICITY: False}) == {"g1"}
    assert (
        serials(
            {
                CONF_BACKFILL_INCLUDE_ELECTRICITY: False,
                CONF_BACKFILL_INCLUDE_GAS: False,
            }
        )
        == set()
    )


@pytest.mark.asyncio
async def test_initialize_marks_meters_not_cleared() -> None:
    meters = [_meter("e1", "electricity"), _meter("g1", "gas")]
    manager = _manager({CONF_BACKFILL_LOOKBACK_DAYS: 10}, meters)
    manager._state = {
        "initialized": False,
        "rebuild_done": False,
        "lookback_days": 0,
        "meters": {},
        "include": {},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]

    await manager._initialize_or_reset_progress(manager._eligible_meters())

    assert manager._state["meters"]["e1"]["cleared"] is False
    assert manager._state["meters"]["g1"]["cleared"] is False
    assert manager._state["include"] == {"electricity": True, "gas": True}


@pytest.mark.asyncio
async def test_reinclusion_resets_only_that_fuel() -> None:
    meters = [_meter("e1", "electricity"), _meter("g1", "gas")]
    manager = _manager(
        {CONF_BACKFILL_LOOKBACK_DAYS: 10, CONF_BACKFILL_INCLUDE_GAS: True}, meters
    )
    # Both previously finished; gas was last switched OFF.
    manager._state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {
            "e1": {"next_start": _REF_NEXT_ISO, "done": True, "cleared": True},
            "g1": {"next_start": _REF_NEXT_ISO, "done": True, "cleared": True},
        },
        "include": {"electricity": True, "gas": False},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]

    await manager._initialize_or_reset_progress(manager._eligible_meters())

    # Gas re-included -> reset for a fresh re-run (and re-clear).
    assert manager._state["meters"]["g1"]["done"] is False
    assert manager._state["meters"]["g1"]["cleared"] is False
    assert manager._state["meters"]["g1"]["next_start"] == _LOOKBACK_START_ISO
    # Electricity left exactly as it was.
    assert manager._state["meters"]["e1"]["done"] is True
    assert manager._state["meters"]["e1"]["cleared"] is True
    assert manager._state["meters"]["e1"]["next_start"] == _REF_NEXT_ISO
    assert manager._state["include"] == {"electricity": True, "gas": True}


@pytest.mark.asyncio
async def test_clear_statistics_only_clears_uncleared_meters(monkeypatch) -> None:
    meters = [_meter("e1", "electricity"), _meter("g1", "gas")]
    manager = _manager({CONF_BACKFILL_REBUILD_STATISTICS: True}, meters)
    manager._state = {
        "initialized": True,
        "rebuild_done": False,
        "lookback_days": 10,
        "meters": {
            "e1": {"next_start": _REF_DATE_ISO, "done": False, "cleared": True},
            "g1": {"next_start": _REF_DATE_ISO, "done": False, "cleared": False},
        },
        "include": {"electricity": True, "gas": True},
    }
    manager._save_state = AsyncMock()  # type: ignore[method-assign]

    cleared_ids: list[str] = []

    def fake_clear(ids, on_done):
        cleared_ids.extend(ids)
        on_done()

    monkeypatch.setattr(
        backfill_module,
        "get_instance",
        lambda hass: SimpleNamespace(async_clear_statistics=fake_clear),
    )
    manager.hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())

    await manager._clear_existing_statistics(manager._eligible_meters())

    # Only the gas meter (cleared=False) had its statistics cleared.
    assert len(cleared_ids) == 1
    assert "g1" in cleared_ids[0]
    assert manager._state["meters"]["g1"]["cleared"] is True
    assert manager._state["meters"]["e1"]["cleared"] is True  # unchanged


@pytest.mark.asyncio
async def test_ensure_state_loaded_migrates_cleared_from_rebuild_done() -> None:
    manager = _manager({}, [_meter("e1", "electricity")])
    old_state = {
        "initialized": True,
        "rebuild_done": True,
        "lookback_days": 10,
        "meters": {"e1": {"next_start": _REF_DATE_ISO, "done": True}},
    }
    manager._store = SimpleNamespace(  # type: ignore[assignment]
        async_load=AsyncMock(return_value=old_state),
        async_save=AsyncMock(),
    )

    await manager._ensure_state_loaded()

    # Missing per-meter "cleared" inherits the old global rebuild_done (True),
    # so an established series is never re-wiped on upgrade.
    assert manager._state["meters"]["e1"]["cleared"] is True
    assert manager._state["include"] == {}


@pytest.mark.asyncio
async def test_ensure_state_loaded_migrates_uncleared_when_rebuild_not_done() -> None:
    manager = _manager({}, [_meter("e1", "electricity")])
    old_state = {
        "initialized": True,
        "rebuild_done": False,
        "lookback_days": 10,
        "meters": {"e1": {"next_start": _REF_DATE_ISO, "done": False}},
    }
    manager._store = SimpleNamespace(  # type: ignore[assignment]
        async_load=AsyncMock(return_value=old_state),
        async_save=AsyncMock(),
    )

    await manager._ensure_state_loaded()

    assert manager._state["meters"]["e1"]["cleared"] is False
