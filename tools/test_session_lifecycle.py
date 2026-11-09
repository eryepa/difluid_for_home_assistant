"""What survives a reload, and what does not.

    pytest tools/test_session_lifecycle.py

Characteristic tests for the lifecycle methods nothing exercised before:
`async_flush`, `async_load`, `apply_config`, `async_remove`.  They are here because
the two worst incidents in this integration's history were both in this area and
neither had a test:

- a dose weighed and then lost, because the session is owned by a config entry now
  and a reload rebuilds it from disk while `Store` writes five seconds late;
- `brew_count` coming back as zero, because one unreadable stored field took every
  field after it down with it — and `TOTAL_INCREASING` turns that into an
  irreversible counter reset in long-term statistics.

Each test below pins one of those, so the structural work that follows has something
to break.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.difluid_microbalance.brew_detect import (  # noqa: E402
    BrewPair,
    DetectorConfig,
)
from custom_components.difluid_microbalance import brew_session  # noqa: E402
from custom_components.difluid_microbalance.brew_session import (  # noqa: E402
    BrewSession,
    WeighEvent,
)

_KEY = "difluid_microbalance.lifecycle"


def _new_session(hass: HomeAssistant) -> BrewSession:
    return BrewSession(hass, DetectorConfig(), _KEY)


def _pair(dose=18.0, yield_g=37.2, at=1000.0) -> BrewPair:
    return BrewPair(dose=dose, dose_at=at - 200, yield_g=yield_g, yield_at=at)


def _dose(value=18.0, at=900.0) -> WeighEvent:
    return WeighEvent(value=value, at=at, hold_seconds=30.0, rise_seconds=2.0)


async def test_a_flush_is_what_carries_an_unwritten_dose_across_a_reload(
    hass: HomeAssistant,
) -> None:
    """Without the flush, everything since the last delayed write is gone.

    `_save` schedules a write five seconds out, which is right while the scale
    streams — five samples a second must not be five disk writes a second.  But the
    session is rebuilt from disk on every reload, and a reload is exactly what
    changing a threshold does, so the flush on unload is the only thing standing
    between "weighed the beans" and "the dose is gone".
    """
    session = _new_session(hass)
    await session.async_load()
    session.last_dose = _dose()
    session.brew_count = 7
    session._save()  # delayed only — nothing on disk yet

    unsaved = _new_session(hass)
    await unsaved.async_load()
    assert unsaved.brew_count == 0, "the delayed write reached disk with no flush"
    assert unsaved.last_dose is None

    await session.async_flush()

    reloaded = _new_session(hass)
    await reloaded.async_load()
    assert reloaded.brew_count == 7
    assert reloaded.last_dose is not None
    assert reloaded.last_dose.value == 18.0

    await reloaded.async_remove()


async def test_one_unreadable_field_does_not_take_the_brew_count_with_it(
    hass: HomeAssistant,
) -> None:
    """The regression that made the counter restart at zero.

    The fields used to share one try block, so a stored dict that no longer matched
    its dataclass — one field added to BrewPair in a later version is enough — raised
    on `last_pair` and skipped `brew_count`, which came back as 0.  Home Assistant
    reads a drop to zero on a TOTAL_INCREASING sensor as a counter reset and writes
    that into long-term statistics, and nothing afterwards can undo it.
    """
    store: Store = Store(hass, 1, _KEY)
    await store.async_save(
        {
            "last_pair": {"dose": 18.0, "field_from_the_future": True},
            "brew_count": 42,
            "last_dose": {
                "value": 18.1,
                "at": 900.0,
                "hold_seconds": 30.0,
                "rise_seconds": 2.0,
                "steps": [18.1],
            },
            "totals": {},
            "measurements": [],
        }
    )

    session = _new_session(hass)
    await session.async_load()

    assert session.brew_count == 42, "the counter was lost to a neighbouring field"
    assert session.last_pair is None, "the unreadable field should be dropped alone"
    assert session.last_dose is not None and session.last_dose.value == 18.1

    await session.async_remove()


async def test_first_load_starts_the_period_now_rather_than_at_the_epoch(
    hass: HomeAssistant,
) -> None:
    """An unstarted period must not read as twenty thousand days old.

    `elapsed_days` divides by the age of the period, so a `period_started` left at
    0.0 publishes a daily average of zero indefinitely — which looks like a working
    sensor reporting no coffee.
    """
    session = _new_session(hass)
    before = dt_util.utcnow().timestamp()
    await session.async_load()
    after = dt_util.utcnow().timestamp()

    assert before <= session.totals.period_started <= after


async def test_applying_a_config_swaps_the_pairer_as_well_as_the_thresholds(
    hass: HomeAssistant,
) -> None:
    """The options flow calls this on a live session, so both helpers must be rebuilt.

    Swapping only `cfg` would leave the detector and the pairer running the old
    thresholds — the new values would show in the UI and change nothing.
    """
    session = _new_session(hass)
    await session.async_load()
    assert session.record_dataset is False

    old_detector, old_pairer = session._detector, session._pairer
    tighter = DetectorConfig(min_mass=9.0)

    session.apply_config(tighter, record_dataset=True)

    assert session.cfg is tighter
    assert session.record_dataset is True
    assert session._detector is not old_detector
    assert session._pairer is not old_pairer
    assert session._pairer.cfg is tighter


async def test_removing_a_session_leaves_nothing_behind(
    hass: HomeAssistant, monkeypatch
) -> None:
    """A deleted detector must not come back on the next install.

    The delayed save is the trap: `Store.async_remove` cancels it, so a session
    removed just after a brew does not rewrite its own state file seconds later.

    The delay is shortened to milliseconds here, and that is not a shortcut.
    `Store.async_delay_save` schedules on the real event loop rather than through
    Home Assistant's scheduler, so `async_fire_time_changed` does not reach it — a
    version of this test built on that passed whether or not the save was ever
    cancelled.  The delay is a parameter of the call, so the constant that feeds it
    is what gets patched.
    """
    monkeypatch.setattr(brew_session, "_SAVE_DELAY", 0.01)

    session = _new_session(hass)
    await session.async_load()

    session.last_dose = _dose()
    session.brew_count = 3
    session._save()
    await asyncio.sleep(0.05)

    with_state = _new_session(hass)
    await with_state.async_load()
    assert with_state.brew_count == 3, "the delayed write never reached disk at all"

    await with_state.async_remove()

    assert with_state.brew_count == 0
    assert with_state.last_dose is None
    assert with_state.measurements == []

    await asyncio.sleep(0.05)

    fresh = _new_session(hass)
    await fresh.async_load()
    assert fresh.brew_count == 0, "a cancelled save resurrected the deleted state"
    assert fresh.last_dose is None
