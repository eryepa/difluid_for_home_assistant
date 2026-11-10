"""What the refractometer says, and when the detector is allowed to believe it.

    pytest tools/test_r2.py

Two defects found on 2026-08-26, both from the same morning's log, both invisible from
inside the integration:

1. A test started on the R2 itself showed nothing in Home Assistant.  Not a BLE
   problem — the packets arrived and were logged.  The parser accepted Device Action
   commands 0x00-0x02 and the device had answered on 0x03, so every packet of a loop
   test was dropped between `_LOGGER.info("R2 notification: ...")` and the entities.

2. The first shot this ever measured was recorded at 11.03 % when the R2 had read
   11.04 %.  The status packet arrives before the result packet — 216 ms apart that
   day — and the detector read TDS the instant the status said "Test Finished", so it
   recorded whatever the previous test had left in the entity.

Both fixtures below are the real bytes and the real timings, copied out of the log.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.difluid_microbalance import (  # noqa: E402
    _TDS_SETTLE_SECONDS,
    _async_watch_refractometer,
)
from custom_components.difluid_microbalance.const import (  # noqa: E402
    CONF_R2_ENTRY,
    DOMAIN,
)
from custom_components.difluid_microbalance.coordinator_r2 import (  # noqa: E402
    DifluidR2Coordinator,
)


# ── 1. the packets a device-side test sends ──────────────────────────────────────
# Captured 2026-08-26 08:47:25-32.  Command 0x03 throughout: the R2 re-tests in a loop
# until the prism reaches the sample's temperature, and only then reports a result.
# Pressing Start Test in Home Assistant on an already-warm prism takes command 0x00 and
# answers in one shot, which is why that path worked and this one did not.
_LOOP_STATUS = bytes.fromhex("dfdf030303000800cf")
_LOOP_TEMPS = bytes.fromhex("dfdf03030601010d011400ee")
_LOOP_RESULT = bytes.fromhex("dfdf03030702044f0002106092")

# The same three from the single test at 08:48:51, command 0x00, which always worked.
_SINGLE_FINISHED = bytes.fromhex("dfdf030003000000c4")


def _coordinator(hass: HomeAssistant) -> DifluidR2Coordinator:
    return DifluidR2Coordinator(hass, "F4:12:FA:CE:8F:2A", "")


async def test_a_loop_test_started_on_the_device_reaches_the_entities(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)

    coordinator._on_data_notification(None, bytearray(_LOOP_STATUS))
    coordinator._on_data_notification(None, bytearray(_LOOP_TEMPS))
    coordinator._on_data_notification(None, bytearray(_LOOP_RESULT))

    assert coordinator.data.test_status == "Loop Test Ongoing"
    assert coordinator.data.prism_temperature == 26.9
    assert coordinator.data.sample_temperature == 27.6
    assert coordinator.data.temperature_unit == "°C"
    assert coordinator.data.concentration == 11.03
    assert coordinator.data.refractive_index == 1.35264


async def test_the_single_test_path_still_reports_finished(
    hass: HomeAssistant,
) -> None:
    """The command that always worked has to go on working.

    The fix widens the set of accepted commands, and a set is exactly the shape of
    change that can widen in one direction while dropping something in the other.
    """
    coordinator = _coordinator(hass)
    coordinator._on_data_notification(None, bytearray(_SINGLE_FINISHED))
    assert coordinator.data.test_status == "Test Finished"


async def test_an_error_packet_is_not_read_as_a_reading(hass: HomeAssistant) -> None:
    """Func 3 cmd 254 is an error code, and 0 % is not what the sample measured."""
    coordinator = _coordinator(hass)
    coordinator._on_data_notification(None, bytearray(_LOOP_RESULT))
    before = coordinator.data.concentration

    error = bytes([0xDF, 0xDF, 0x03, 0xFE, 0x03, 0x02, 0x03, 0x00])
    coordinator._on_data_notification(None, bytearray(error + bytes([sum(error) & 0xFF])))

    assert coordinator.data.concentration == before


# ── 2. which reading belongs to the test that just finished ──────────────────────

class _Session:
    """Stands in for the BrewSession: all the watcher asks of it is this one call."""

    def __init__(self) -> None:
        self.recorded: list[float] = []

    def record_measurement(self, tds: float) -> None:
        self.recorded.append(tds)


async def _watch(hass: HomeAssistant) -> tuple[_Session, str, str]:
    """Wire the watcher onto a registered R2 the way __init__ does at setup."""
    r2 = MockConfigEntry(domain=DOMAIN, title="DiFluid R2", entry_id="r2entry")
    r2.add_to_hass(hass)
    detector = MockConfigEntry(
        domain=DOMAIN, title="Brew Detector", data={CONF_R2_ENTRY: r2.entry_id}
    )
    detector.add_to_hass(hass)

    registry = er.async_get(hass)
    status = registry.async_get_or_create(
        "sensor", DOMAIN, f"{r2.entry_id}_test_status",
        config_entry=r2, suggested_object_id="r2_test_status",
    )
    tds = registry.async_get_or_create(
        "sensor", DOMAIN, f"{r2.entry_id}_concentration",
        config_entry=r2, suggested_object_id="r2_concentration",
    )

    session = _Session()
    _async_watch_refractometer(hass, detector, session)
    return session, status.entity_id, tds.entity_id


async def test_the_reading_recorded_is_the_one_the_finished_test_produced(
    hass: HomeAssistant,
) -> None:
    """The 11.03/11.04 defect, at the timings it actually happened at."""
    session, status_id, tds_id = await _watch(hass)

    # The previous shot's reading, still sitting in the entity.
    hass.states.async_set(tds_id, "11.03")
    hass.states.async_set(status_id, "Test Start")
    await hass.async_block_till_done()

    hass.states.async_set(status_id, "Test Finished")
    await hass.async_block_till_done()
    assert session.recorded == [], "recorded before this test's own result arrived"

    # 216 ms later, the result packet.
    hass.states.async_set(tds_id, "11.04")
    await hass.async_block_till_done()
    assert session.recorded == [11.04]


async def test_a_repeat_of_the_same_reading_still_records(
    hass: HomeAssistant,
) -> None:
    """Two samples that read the same write no state change at all.

    Which is why the wait has to end on a timer as well as on a value: with no timeout
    a shot measured at exactly the TDS of the one before it would never be recorded,
    and that failure would look like the refractometer having missed it.
    """
    session, status_id, tds_id = await _watch(hass)

    hass.states.async_set(tds_id, "11.03")
    hass.states.async_set(status_id, "Test Start")
    await hass.async_block_till_done()

    hass.states.async_set(status_id, "Test Finished")
    await hass.async_block_till_done()
    assert session.recorded == []

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=_TDS_SETTLE_SECONDS + 1)
    )
    await hass.async_block_till_done()
    assert session.recorded == [11.03]


async def test_a_loop_test_still_hunting_records_nothing(
    hass: HomeAssistant,
) -> None:
    """Live values belong on the card, not in the record.

    A loop test reports every attempt while the prism comes up to temperature — 11.11,
    11.14, 11.03, 11.03 in one capture.  Each of those writes the concentration entity,
    and every one of them would be recorded as a separate verdict on the same shot if
    the reading alone were enough to trigger a record.
    """
    session, status_id, tds_id = await _watch(hass)

    hass.states.async_set(status_id, "Loop Test Ongoing")
    for value in ("11.11", "11.14", "11.03"):
        hass.states.async_set(tds_id, value)
    await hass.async_block_till_done()
    assert session.recorded == []

    hass.states.async_set(status_id, "Loop Test Finished")
    hass.states.async_set(tds_id, "11.05")
    await hass.async_block_till_done()
    assert session.recorded == [11.05]


async def test_switching_the_r2_back_on_does_not_record_the_last_test_again(
    hass: HomeAssistant,
) -> None:
    """Found in the live data on 2026-08-26, at 10:02:45.

    The R2 is a handheld and spends its life switched off; its sensors go unavailable
    with it and the coordinator keeps the last reading in memory.  So a reconnect
    republishes "Test Finished" together with whatever TDS was measured last — and
    "every reading belongs to the most recent brew" then pins that stale number onto
    whichever cup happens to be the latest.  That day it landed back on the same brew
    and only corrected a rounding; the next unmeasured shot would have been given a
    reading it never had.
    """
    session, status_id, tds_id = await _watch(hass)

    hass.states.async_set(tds_id, "11.04")
    hass.states.async_set(status_id, "Test Finished")
    await hass.async_block_till_done()
    session.recorded.clear()

    # Switched off, then on again — the retained value comes back unchanged.
    hass.states.async_set(status_id, "unavailable")
    hass.states.async_set(tds_id, "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set(status_id, "Test Finished")
    hass.states.async_set(tds_id, "11.04")
    await hass.async_block_till_done()

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=_TDS_SETTLE_SECONDS + 1)
    )
    await hass.async_block_till_done()
    assert session.recorded == []

    # A real test run right afterwards still records, which is what keeps the guard
    # from being "ignore the first measurement after every reconnect".
    hass.states.async_set(status_id, "Test Start")
    await hass.async_block_till_done()
    hass.states.async_set(status_id, "Test Finished")
    hass.states.async_set(tds_id, "10.20")
    await hass.async_block_till_done()
    assert session.recorded == [10.20]


async def test_a_calibration_is_not_a_brew(hass: HomeAssistant) -> None:
    """Distilled water on the prism finishes like any other test and reads ~0 %."""
    session, status_id, tds_id = await _watch(hass)

    hass.states.async_set(tds_id, "11.03")
    hass.states.async_set(status_id, "Calibration Start")
    await hass.async_block_till_done()

    hass.states.async_set(status_id, "Calibration Finished")
    hass.states.async_set(tds_id, "0.0")
    await hass.async_block_till_done()

    assert session.recorded == []


# ── the device's own units, and the packets that are not readings ────────────────
# Both fixtures below are the protocol document's worked examples, checksums included,
# rather than anything invented here: docs/protocolR2.md:162 for the Fahrenheit
# temperatures and :142 for what a calibration answers with.

#: protocolR2.md:162 — `DF DF 03 00 06 01 03 17 03 14 01 FA`, i.e. prism 79.1 and
#: sample 78.8 with Data5 = 0x01 = Fahrenheit.
_F_TEMPS = bytes.fromhex("dfdf030006010317031401fa")

#: A calibration result: same shape as a single test, cmd 0x02, concentration 0.
_CALIBRATION_RESULT = bytes.fromhex("dfdf0302070200000002093d14")


async def test_a_fahrenheit_device_is_published_in_celsius(hass: HomeAssistant) -> None:
    """The R2 reports in whatever its own display is set to; the sensors say Celsius.

    Nothing converted, so a prism at 26.2 C reached Home Assistant as 79.1 tagged
    Celsius — a 53-degree error written straight into long-term statistics with nothing
    logged at any level.  The scale's coordinator has always done this correctly for
    ounces; the R2 tracked the unit and then ignored it.
    """
    coordinator = _coordinator(hass)

    coordinator._on_data_notification(None, bytearray(_F_TEMPS))

    assert coordinator.data.prism_temperature == 26.2      # 79.1 F
    assert coordinator.data.sample_temperature == 26.0     # 78.8 F
    assert coordinator.data.temperature_unit == "°F", (
        "the unit the device reported in is still worth recording, it is just not "
        "the unit of the value we store"
    )


async def test_a_celsius_device_is_left_exactly_as_it_reported(
    hass: HomeAssistant,
) -> None:
    """The control case: conversion must not touch the readings that were already right."""
    coordinator = _coordinator(hass)

    coordinator._on_data_notification(None, bytearray(_LOOP_TEMPS))

    assert coordinator.data.prism_temperature == 26.9
    assert coordinator.data.sample_temperature == 27.6
    assert coordinator.data.temperature_unit == "°C"


async def test_a_calibration_result_does_not_overwrite_the_last_reading(
    hass: HomeAssistant,
) -> None:
    """protocolR2.md:142 — a successful calibration answers with concentration 0.

    Taken as a reading, that replaces the last real TDS with 0.00 in the entity and in
    recorder history, for a packet whose whole meaning is "the prism was just zeroed on
    distilled water".  The status package is still read, so the calibration itself stays
    visible — it is only its result that must not be mistaken for coffee.
    """
    coordinator = _coordinator(hass)
    coordinator._on_data_notification(None, bytearray(_LOOP_RESULT))
    assert coordinator.data.concentration == 11.03

    coordinator._on_data_notification(None, bytearray(_CALIBRATION_RESULT))

    assert coordinator.data.concentration == 11.03, (
        "a calibration zeroed the last measured shot"
    )
    assert coordinator.data.refractive_index == 1.35264, (
        "and took the refractive index with it"
    )


# A package-2 header that claims seven payload bytes and carries three: the frame
# announces more than arrived, which is what a dropped notification looks like.
_TRUNCATED_RESULT = bytes.fromhex("dfdf0303070204 4f".replace(" ", ""))


def test_a_short_frame_is_rejected_rather_than_parsed_short() -> None:
    """`data_len` comes off the wire, so it cannot be trusted to bound the slice.

    Without the length check the parser slices `payload[3:7]` off a payload of three
    bytes, gets an empty slice, and publishes a refractive index of 0.0 — a number
    shaped like a reading, from a packet that did not contain one.
    """
    from custom_components.difluid_microbalance.coordinator_r2 import _decode_frame

    assert _decode_frame(_TRUNCATED_RESULT) is None
    assert _decode_frame(b"\xdf\xdf\x03") is None
    assert _decode_frame(b"\xda\xda\x00\x00\x00") is None
    assert _decode_frame(_LOOP_RESULT) is not None


async def test_a_truncated_result_leaves_the_last_reading_alone(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._on_data_notification(None, bytearray(_LOOP_RESULT))
    before = coordinator.data.refractive_index

    coordinator._on_data_notification(None, bytearray(_TRUNCATED_RESULT))

    assert coordinator.data.refractive_index == before
    assert coordinator.data.concentration == 11.03


# ── the predicate, on its own ────────────────────────────────────────────────────
# It used to be four ifs inside a closure that only a real state change could reach,
# which meant three of its four branches could be deleted with the suite green.

def _status_event(old_state, new_state):
    from types import SimpleNamespace

    def state(value):
        return None if value is None else State("sensor.r2_test_status", value)

    return SimpleNamespace(
        data={"old_state": state(old_state), "new_state": state(new_state)}
    )


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        # The ordinary case: a test started and has now finished.
        ("Test Start", "Test Finished", True),
        ("Loop Test Ongoing", "Loop Test Finished", True),
        # Finished again with the same state republished: no new test.
        ("Test Finished", "Test Finished", False),
        # Coming back from the dead is not a test finishing — the coordinator still
        # holds the previous reading and republishes it with this status.
        ("unavailable", "Test Finished", False),
        ("unknown", "Test Finished", False),
        (None, "Test Finished", False),
        # Nothing finished at all.
        ("Test Start", "Test Start", False),
        ("Test Start", "Loop Test Ongoing", False),
        ("Calibration Start", "Calibration Finished", False),
        # The entity is gone rather than changed: removing it fires a state change
        # with no new state at all.
        ("Test Finished", None, False),
    ],
)
def test_only_a_real_finish_counts_as_one(old, new, expected) -> None:
    from custom_components.difluid_microbalance import _is_finished_test

    assert _is_finished_test(_status_event(old, new)) is expected


# A Device Settings frame that sets the unit to Fahrenheit: func 0x01, cmd 0x00.
# Taken from protocolR2.md's own example, checksum included.
_SETTINGS_FAHRENHEIT = bytes.fromhex("dfdf01000101c1")

# The same command with no payload — a *request* rather than a setting.  It must not
# overwrite the unit, and it is the only frame that reaches the settings parser
# without a payload, so it is what the length guard is really for.
_SETTINGS_EMPTY = bytes.fromhex("dfdf010000bf")


async def test_the_unit_arrives_from_the_settings_frame_too(hass: HomeAssistant) -> None:
    """A fourth branch of the old parser: func 0x01, cmd 0x00.

    It was the one branch with no test at all, and deleting it from the dispatch table
    left the whole suite green — so a device that reports its unit this way would have
    silently kept reporting the previous one.
    """
    coordinator = _coordinator(hass)

    coordinator._on_data_notification(None, bytearray(_LOOP_TEMPS))
    assert coordinator.data.temperature_unit == "°C"

    coordinator._on_data_notification(None, bytearray(_SETTINGS_FAHRENHEIT))

    assert coordinator.data.temperature_unit == "°F"


async def test_an_empty_settings_frame_leaves_the_unit_alone(
    hass: HomeAssistant,
) -> None:
    coordinator = _coordinator(hass)
    coordinator._on_data_notification(None, bytearray(_LOOP_TEMPS))
    before = coordinator.data.temperature_unit

    coordinator._on_data_notification(None, bytearray(_SETTINGS_EMPTY))

    assert coordinator.data.temperature_unit == before


async def test_a_misspelled_field_fails_loudly(hass: HomeAssistant) -> None:
    """The parser table returns field names as strings, so a typo would be silent.

    `setattr(self.data, "temperature_units", …)` creates a stray attribute instead of
    raising, and the entity that should have updated simply stops — no error, no log
    line.  The guard turns that into an exception at the first packet.
    """
    from custom_components.difluid_microbalance import coordinator_r2

    coordinator = _coordinator(hass)
    original = coordinator_r2._R2_PACKAGE_PARSERS[coordinator_r2._R2_PKG_STATUS]
    coordinator_r2._R2_PACKAGE_PARSERS[coordinator_r2._R2_PKG_STATUS] = (
        lambda payload, cmd: {"temperature_units": "°F"}
    )
    try:
        with pytest.raises(KeyError, match="temperature_units"):
            coordinator._on_data_notification(None, bytearray(_LOOP_STATUS))
    finally:
        coordinator_r2._R2_PACKAGE_PARSERS[coordinator_r2._R2_PKG_STATUS] = original


# ── what happens when the R2 is simply not there ─────────────────────────────────
# The R2 sat unavailable for a week with not a single line in the log: its start-up
# attempt failed, nothing armed a retry, and the only path back was an advertisement
# callback that never fired.  The scale had been taught this lesson already; the R2
# had not.

def test_the_retry_pace_follows_presence_not_failure_count() -> None:
    """Absent means poll slowly; in range means back off from five seconds.

    Pacing by failure count is what the scale's loop used to do, and it is why a brew
    went missing on 2026-08-14: after a few failures the delay pegs at two minutes,
    which is longer than the device stays awake.
    """
    from custom_components.difluid_microbalance.coordinator_r2 import _next_retry_delay

    assert _next_retry_delay(present=False, delay=5.0) == 15.0
    assert _next_retry_delay(present=False, delay=120.0) == 15.0
    assert _next_retry_delay(present=True, delay=5.0) == 10.0
    assert _next_retry_delay(present=True, delay=80.0) == 120.0
    assert _next_retry_delay(present=True, delay=120.0) == 120.0


async def test_a_startup_failure_arms_the_reconnect_loop(hass: HomeAssistant) -> None:
    """The gap that left the R2 silent for a week.

    `async_start` logged "will connect when seen in BLE scan" and armed nothing, so
    with no advertisement arriving there was no retry at all — no timer, no log line,
    no evidence anything was wrong.
    """
    from unittest.mock import AsyncMock, patch

    from custom_components.difluid_microbalance.coordinator_r2 import DifluidR2Coordinator

    coordinator = DifluidR2Coordinator(hass, "F4:12:FA:CE:8F:2A", "")
    with (
        patch(
            "custom_components.difluid_microbalance.coordinator_r2"
            ".bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch.object(
            DifluidR2Coordinator, "_do_connect", AsyncMock(side_effect=OSError("gone"))
        ),
    ):
        await coordinator.async_start()

    assert coordinator._reconnect_task is not None, (
        "a failed start-up left no way back except an advertisement that may never come"
    )
    assert not coordinator._reconnect_task.done()

    await coordinator.async_stop()


async def test_the_loop_is_never_started_twice(hass: HomeAssistant) -> None:
    from custom_components.difluid_microbalance.coordinator_r2 import DifluidR2Coordinator

    coordinator = DifluidR2Coordinator(hass, "F4:12:FA:CE:8F:2A", "")
    coordinator._ensure_reconnect_loop()
    first = coordinator._reconnect_task
    coordinator._ensure_reconnect_loop()

    assert coordinator._reconnect_task is first

    await coordinator.async_stop()


async def test_two_attempts_at_once_collapse_into_one(hass: HomeAssistant) -> None:
    """Same guard as the scale's, and it was missing here too."""
    import asyncio

    coordinator = _coordinator(hass)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def slow_connect() -> None:
        calls.append("attempt")
        started.set()
        await release.wait()

    coordinator._do_connect = slow_connect

    first = asyncio.create_task(coordinator._connect_guarded())
    await started.wait()
    await asyncio.wait_for(coordinator._connect_guarded(), timeout=0.5)  # must return
    # at once, not queue a second attempt that would block until `release`
    release.set()
    await first

    assert calls == ["attempt"]
    assert coordinator._connecting is False


async def test_the_guard_is_released_after_a_failure(hass: HomeAssistant) -> None:
    coordinator = _coordinator(hass)
    coordinator._do_connect = AsyncMock(side_effect=OSError("nothing there"))

    with pytest.raises(OSError):
        await coordinator._connect_guarded()

    assert coordinator._connecting is False


async def test_an_advertisement_during_a_connect_starts_nothing(
    hass: HomeAssistant,
) -> None:
    from unittest.mock import MagicMock

    coordinator = _coordinator(hass)
    coordinator._connecting = True

    coordinator._on_bt_advertisement(MagicMock(), MagicMock())

    assert coordinator._reconnect_task is None
