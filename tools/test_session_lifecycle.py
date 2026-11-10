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
import time
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.difluid_microbalance.brew_detect import (  # noqa: E402
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
    before = time.time()
    await session.async_load()
    after = time.time()

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


async def test_removal_delegates_to_the_store(hass: HomeAssistant) -> None:
    """`async_remove` must hand the deletion to the Store, and clear memory first.

    What is asserted is the delegation, not the deletion, and that is a limit of the
    harness rather than of the code: pytest-homeassistant-custom-component replaces
    both `Store._async_write_data` and `Store.async_remove` with in-memory mocks, so
    nothing is ever written to a file and the mock's `async_remove` does not cancel
    the pending delayed save.  The cancellation lives in Home Assistant's own
    `Store.async_remove` (`_async_cleanup_delay_listener`), which the harness never
    reaches — read its source if you want to see it.

    So the contract tested here is the one this repository owns: the session clears
    itself and asks the store to delete.  A session that cleared itself and forgot to
    ask would pass an in-memory assertion and leave the file behind on a real install.
    """
    session = _new_session(hass)
    await session.async_load()
    session.last_dose = _dose()
    session.brew_count = 3

    removed = []

    class _RecordingStore:
        path = "/nonexistent"

        async def async_remove(self) -> None:
            removed.append(True)

    session._store = _RecordingStore()  # type: ignore[assignment]
    session.add_listener(lambda: None)

    await session.async_remove()

    assert removed == [True], "the session never asked the store to delete its state"
    assert session.brew_count == 0
    assert session.last_dose is None
    assert session.measurements == []
    assert session._listeners == [], (
        "a removed session kept its listeners — the entities would keep repainting "
        "from a detector that no longer exists"
    )
