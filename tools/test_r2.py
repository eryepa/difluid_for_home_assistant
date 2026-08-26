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

from homeassistant.core import HomeAssistant
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
