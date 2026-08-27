"""Does moving an entity to another config entry keep its entity_id?

    pip install pytest-homeassistant-custom-component
    pytest tools/test_migration.py

1.5.0 moved the brew statistics off the scale's config entry and onto a detector entry
of their own.  The entire plan for doing that without losing history rests on one claim
about Home Assistant that is nowhere in this repository's code:

    the entity registry is keyed by (domain, platform, unique_id), and config_entry_id
    and device_id are ordinary fields that re-registration overwrites in place

If that is true, a detector entry registering `f"{scale_entry_id}_brew_count"` — the
unique_id the scale gave it — claims the existing row, and entity_id, recorder history,
long-term statistics and the Prometheus series all carry on pointing at the same thing.
If it is false, the upgrade silently creates `sensor.…_brew_count_2`, the odometer
restarts at zero, and the notification email stops resolving.

There is no way to find out from our side of the API, and exactly one chance to be
wrong about it on the live install.  So this asks Home Assistant directly, using the
same version it runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.difluid_microbalance.const import (  # noqa: E402
    CONF_DETECTOR_IMPORTED as CONF_DETECTOR_IMPORTED_KEY,
    CONF_DEVICE_TYPE,
    CONF_SCALE_ENTRY,
    CONF_UID_PREFIX,
    DEVICE_TYPE_DETECTOR,
    DEVICE_TYPE_MICROBALANCE,
    DOMAIN,
)
from custom_components.difluid_microbalance.sensor import (  # noqa: E402
    BREW_SENSORS,
    DifluidBrewSensor,
)

#: The seven that move.  Written out rather than derived so that adding a statistic
#: without thinking about the move is a test failure and not a silent omission.
MOVED_KEYS = (
    "brew_count",
    "brew_count_period",
    "brews_per_day",
    "coffee_total",
    "coffee_period",
    "coffee_per_day",
)


@pytest.fixture
def entries(hass: HomeAssistant):
    """A scale entry as it exists today, and the detector entry that will claim it."""
    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
    )
    scale.add_to_hass(hass)
    detector = MockConfigEntry(
        domain=DOMAIN,
        title="Brew Detector",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_DETECTOR,
            CONF_SCALE_ENTRY: scale.entry_id,
            # The import flow seeds this with the scale's entry_id.  That is the move.
            CONF_UID_PREFIX: scale.entry_id,
        },
    )
    detector.add_to_hass(hass)
    return scale, detector


async def test_moving_an_entity_between_entries_keeps_its_entity_id(
    hass: HomeAssistant, entries
) -> None:
    scale, detector = entries
    devices = dr.async_get(hass)
    entities = er.async_get(hass)

    scale_device = devices.async_get_or_create(
        config_entry_id=scale.entry_id,
        identifiers={(DOMAIN, scale.entry_id)},
        name="Kitchen Microbalance 304268",
    )
    # The registry as it stands on the live install: seven rows owned by the scale.
    before = {}
    for key in MOVED_KEYS:
        entry = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{scale.entry_id}_{key}",
            config_entry=scale,
            device_id=scale_device.id,
            suggested_object_id=f"kitchen_microbalance_304268_{key}",
        )
        before[key] = entry.entity_id

    detector_device = devices.async_get_or_create(
        config_entry_id=detector.entry_id,
        identifiers={(DOMAIN, detector.entry_id)},
        name="Brew Detector",
        entry_type=dr.DeviceEntryType.SERVICE,
        via_device=(DOMAIN, scale.entry_id),
    )
    assert detector_device.id != scale_device.id

    # The upgrade: the same unique_ids, registered by the detector entry this time.
    for key in MOVED_KEYS:
        after = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{scale.entry_id}_{key}",
            config_entry=detector,
            device_id=detector_device.id,
        )
        # The claim, in three parts.
        assert after.entity_id == before[key], (
            f"{key} was renamed: {before[key]} -> {after.entity_id}. History, the "
            f"Prometheus rules and the email all break at this point."
        )
        assert after.config_entry_id == detector.entry_id
        assert after.device_id == detector_device.id

    # And nothing was duplicated along the way — a "_2" would have shown up here.
    assert len(entities.entities) == len(MOVED_KEYS)


async def test_the_detector_builds_the_unique_ids_the_scale_left_behind(
    hass: HomeAssistant, entries
) -> None:
    """The prefix reaches the entity, rather than the entry_id being used directly.

    Separate from the registry test on purpose: that one proves Home Assistant honours
    a re-registration, this one proves we hand it the right string to honour.  Both
    have to hold for the move to work, and they fail for different reasons.
    """
    scale, detector = entries
    by_key = {desc.key: desc for desc in BREW_SENSORS}
    for key in MOVED_KEYS:
        sensor = DifluidBrewSensor(
            session=None,
            description=by_key[key],
            uid_prefix=detector.data[CONF_UID_PREFIX],
            device_info=None,
        )
        assert sensor.unique_id == f"{scale.entry_id}_{key}"
        assert detector.entry_id not in sensor.unique_id


async def test_a_detector_created_from_scratch_does_not_borrow_a_scale_id(
    hass: HomeAssistant, entries
) -> None:
    """A fresh install namespaces its entities under its own entry.

    Only an imported detector inherits the scale's prefix.  Were a new one to take it
    too, adding a detector to a scale that already had one — or re-adding one after a
    removal — would land on the previous detector's registry rows and inherit its
    odometer.
    """
    scale, _ = entries
    fresh = MockConfigEntry(
        domain=DOMAIN,
        title="Brew Detector",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_DETECTOR, CONF_SCALE_ENTRY: scale.entry_id},
    )
    fresh.add_to_hass(hass)

    from custom_components.difluid_microbalance import _async_seed_detector_identity

    _async_seed_detector_identity(hass, fresh)
    assert fresh.data[CONF_UID_PREFIX] == fresh.entry_id
    assert fresh.data[CONF_UID_PREFIX] != scale.entry_id


async def test_seeding_never_overwrites_an_imported_prefix(
    hass: HomeAssistant, entries
) -> None:
    """The imported prefix survives every later setup, not just the first.

    Seeding runs on every setup of a detector entry, and the guard that makes it write
    only into empty fields is the whole of what stops it.  Losing that guard is
    invisible on a fresh install — it would rewrite the entry_id with the entry_id —
    and quietly fatal on an imported one: the scale's prefix would be replaced by the
    detector's own, and the seven entities would come back as duplicates with the
    odometer at zero.

    The timing is what makes it worth a test of its own.  The upgrade itself would look
    perfect; the damage would land on the *next* restart, by which time nothing would
    obviously connect the two.
    """
    scale, imported = entries
    assert imported.data[CONF_UID_PREFIX] == scale.entry_id

    from custom_components.difluid_microbalance import _async_seed_detector_identity

    for _ in range(3):
        _async_seed_detector_identity(hass, imported)
        assert imported.data[CONF_UID_PREFIX] == scale.entry_id, (
            "the imported prefix was overwritten; on the live install this loses "
            "brew_count and every statistic's history on the second restart"
        )


async def test_a_scale_on_default_thresholds_is_migrated_too(
    hass: HomeAssistant,
) -> None:
    """The failure 1.5.0-beta.1 shipped, as a test.

    That version decided an install needed migrating by looking for detector
    thresholds in the scale's options.  Options are only written when somebody opens
    the options form and changes a value, so a scale running on the defaults has none
    — `entry.options == {}` — and the check said "already migrated, nothing to do".

    It said that on an install where the scale had, in the same release, stopped
    creating the statistics entities.  Seven sensors went unavailable, registry rows
    orphaned, and the log said nothing at all because the function returned before its
    first line of logging.

    Empty options is the *ordinary* case, which is what makes it worth pinning: the
    version that broke it passed every other test in this file.
    """
    from custom_components.difluid_microbalance import _async_import_detector
    from custom_components.difluid_microbalance.const import CONF_DETECTOR_IMPORTED

    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
        options={},  # never touched the options form — the state this install was in
    )
    scale.add_to_hass(hass)

    await _async_import_detector(hass, scale)
    await hass.async_block_till_done()

    detectors = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR
    ]
    assert len(detectors) == 1, (
        "a scale left on default thresholds got no detector, so its seven statistics "
        "entities have no owner and read unavailable"
    )
    assert detectors[0].data[CONF_UID_PREFIX] == scale.entry_id
    assert scale.data.get(CONF_DETECTOR_IMPORTED) is True

    # Runs on every restart, so it must not keep creating them.
    await _async_import_detector(hass, scale)
    await hass.async_block_till_done()
    assert (
        len(
            [
                e
                for e in hass.config_entries.async_entries(DOMAIN)
                if e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR
            ]
        )
        == 1
    )

    await _unload_all(hass)


async def test_a_deliberately_deleted_detector_does_not_come_back(
    hass: HomeAssistant,
) -> None:
    """The flag is the only thing separating "migrate once" from "recreate forever".

    Dropping the options check made the condition "a scale with no detector pointing
    at it", which is true again the moment somebody deletes theirs on purpose.
    """
    from custom_components.difluid_microbalance import _async_import_detector
    from custom_components.difluid_microbalance.const import CONF_DETECTOR_IMPORTED

    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Microbalance 304268",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE,
            "address": "AA:BB:CC:DD:EE:FF",
            CONF_DETECTOR_IMPORTED: True,  # migrated at some point, detector since removed
        },
    )
    scale.add_to_hass(hass)

    await _async_import_detector(hass, scale)
    await hass.async_block_till_done()
    assert not [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR
    ]


async def _unload_all(hass: HomeAssistant) -> None:
    """Put every entry this test created back down.

    Not housekeeping.  A detector that cannot find its scale raises
    ConfigEntryNotReady, and Home Assistant answers that with a retry timer — so a
    test that walks away leaves one running, and the harness reports it as a leak.
    Unloading is both the fix and a free check that the teardown path works.
    """
    for entry in hass.config_entries.async_entries(DOMAIN):
        await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_a_detector_set_up_before_its_scale_recovers_when_the_scale_arrives(
    hass: HomeAssistant,
) -> None:
    """The load-order path, end to end — which nothing checked until it broke.

    Home Assistant sets entries up concurrently, so which of the two goes first is not
    ours to decide, and on this install the detector went first after a restart: it
    raised ConfigEntryNotReady and stayed in setup_retry with all thirteen entities
    missing.  The wake-up meant to prevent that had been written on the detector's
    side, subscribing to a signal just before raising — and Home Assistant runs an
    entry's async_on_unload callbacks when its setup raises, so the subscription was
    gone before the signal was sent.  It failed silently and looked like a broken
    upgrade.

    The version that shipped that bug passed every other test in this file, because
    every one of them either set the scale up first or never set it up at all.
    """
    from homeassistant.config_entries import ConfigEntryState

    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
    )
    scale.add_to_hass(hass)
    detector = MockConfigEntry(
        domain=DOMAIN,
        title="Brew Detector",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_DETECTOR,
            CONF_SCALE_ENTRY: scale.entry_id,
            CONF_UID_PREFIX: scale.entry_id,
            CONF_DETECTOR_IMPORTED_KEY: True,
        },
    )
    detector.add_to_hass(hass)

    # The order cannot be forced by setting one entry up first: doing that sets the
    # component up, and the component sets up every entry of its domain.  So both are
    # brought up, then the race is reproduced from the other end — the scale goes away
    # and the detector is reloaded without it, which is the state a restart left this
    # install in.
    await hass.config_entries.async_setup(detector.entry_id)
    await hass.async_block_till_done()
    assert scale.state is ConfigEntryState.LOADED

    await hass.config_entries.async_unload(scale.entry_id)
    await hass.async_block_till_done()
    await hass.config_entries.async_reload(detector.entry_id)
    await hass.async_block_till_done()
    assert detector.state is ConfigEntryState.SETUP_RETRY, (
        "a detector with no scale should wait, not claim to be working"
    )

    # The scale turns up.
    await hass.config_entries.async_setup(scale.entry_id)
    await hass.async_block_till_done()

    assert detector.state is ConfigEntryState.LOADED, (
        "the detector never recovered from losing the load-order race; on a real "
        "install that is every brew statistic missing until something reloads it"
    )

    await _unload_all(hass)


async def test_reloading_the_scale_does_not_deafen_a_loaded_detector(
    hass: HomeAssistant,
) -> None:
    """Reload the scale and the detector must still be fed by it.

    The failure this pins looks completely healthy from the outside, which is why it
    survived three releases.  `add_brew_consumer` binds the session to the coordinator
    *object* that existed when the detector set up, and it is called exactly once.
    Reloading the scale — the obvious thing to do on this install after a BLE drop —
    replaces that object with one whose consumer list is empty.  The scale then
    reconnects, weight and flow stream normally, the detector still reads LOADED with
    every entity present, and `BrewSession.feed` is never called again: no dose, no
    pour, no brew_count, and nothing logged at any level.

    Asserted against the objects that are live *now*, and with the coordinator identity
    checked first, so the test cannot pass by the scale having quietly not reloaded.
    """
    from homeassistant.config_entries import ConfigEntryState

    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
    )
    scale.add_to_hass(hass)
    detector = MockConfigEntry(
        domain=DOMAIN,
        title="Brew Detector",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_DETECTOR,
            CONF_SCALE_ENTRY: scale.entry_id,
            CONF_UID_PREFIX: scale.entry_id,
            CONF_DETECTOR_IMPORTED_KEY: True,
        },
    )
    detector.add_to_hass(hass)

    await hass.config_entries.async_setup(detector.entry_id)
    await hass.async_block_till_done()
    assert scale.state is ConfigEntryState.LOADED
    assert detector.state is ConfigEntryState.LOADED

    before = hass.data[DOMAIN][scale.entry_id]
    assert hass.data[DOMAIN][detector.entry_id] in before._brew_consumers, (
        "the detector was never subscribed in the first place"
    )

    await hass.config_entries.async_reload(scale.entry_id)
    await hass.async_block_till_done()

    after = hass.data[DOMAIN][scale.entry_id]
    assert after is not before, (
        "the scale did not actually rebuild its coordinator, so this test proves "
        "nothing about what happens when it does"
    )
    assert detector.state is ConfigEntryState.LOADED

    session = hass.data[DOMAIN][detector.entry_id]
    assert session in after._brew_consumers, (
        "the detector is loaded but subscribed to a coordinator that no longer "
        "exists — every brew from here on is silently undetected"
    )

    # And the stream really does arrive, rather than merely being wired up: the
    # subscription is only worth anything if feed() runs.
    seen: list[float] = []
    session.feed = lambda weight, flow: seen.append(weight)
    for consumer in after._brew_consumers:
        consumer.feed(18.0, 0.0)
    assert seen == [18.0]

    await _unload_all(hass)


async def test_the_detector_offers_a_measured_yield_control(
    hass: HomeAssistant,
) -> None:
    """Measured Yield is on a platform the detector did not forward until 1.7.0.

    Worth its own test because a platform missing from DETECTOR_PLATFORMS fails the
    way the load-order bug did: nothing raises, nothing is logged above debug, the
    entity simply is not there.  Setting it here also exercises the whole path —
    entity to session to stored measurement — rather than the session in isolation.
    """
    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
    )
    scale.add_to_hass(hass)
    detector = MockConfigEntry(
        domain=DOMAIN,
        title="Brew Detector",
        data={
            CONF_DEVICE_TYPE: DEVICE_TYPE_DETECTOR,
            CONF_SCALE_ENTRY: scale.entry_id,
            CONF_UID_PREFIX: scale.entry_id,
            CONF_DETECTOR_IMPORTED_KEY: True,
        },
    )
    detector.add_to_hass(hass)
    await hass.config_entries.async_setup(detector.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    yield_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{scale.entry_id}_measured_yield"
    )
    assert yield_id is not None, "the detector has no Measured Yield control"
    dose_id = registry.async_get_entity_id(
        "number", DOMAIN, f"{scale.entry_id}_measured_dose"
    )
    assert dose_id is not None, "the detector has no Measured Dose control"
    assert dose_id != yield_id, (
        "both controls resolved to one entity — the two share a base class, and a "
        "subclass that inherited the other's _key would silently be a second copy of it"
    )

    session = hass.data[DOMAIN][detector.entry_id]
    session._save = lambda: None
    session.last_dose = _dose(18.1, 2000.0)
    session.record_measurement(10.13)
    assert session.last_measurement.ext is None

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": yield_id, "value": 37.0},
        blocking=True,
    )

    assert session.last_measurement.yield_g == 37.0
    assert session.last_measurement.ext == 20.71
    assert hass.states.get(yield_id).state == "37.0"

    # And the dose the same way round: the entity reads the stored measurement rather
    # than a value of its own, so a correction has to come back out of the session.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": dose_id, "value": 18.0},
        blocking=True,
    )

    assert session.last_measurement.dose == 18.0
    assert session.last_measurement.ext == 20.82      # 10.13 * 37.0 / 18.0
    assert hass.states.get(dose_id).state == "18.0"
    assert hass.states.get(yield_id).state == "37.0", (
        "correcting the dose moved the yield — the two controls are writing over "
        "each other's field"
    )

    await _unload_all(hass)


def _session(tmp_key="difluid_microbalance.test"):
    """A session with no Home Assistant behind it — Store is never touched here."""
    from unittest.mock import MagicMock

    from custom_components.difluid_microbalance.brew_session import BrewSession

    session = BrewSession(MagicMock(), None, tmp_key)
    session._store = MagicMock()
    session._save = lambda: None
    return session


def _pair(dose, yield_g, at, pour_seconds=None, dose_at=None):
    from custom_components.difluid_microbalance.brew_detect import BrewPair

    return BrewPair(
        dose=dose,
        dose_at=at - 200 if dose_at is None else dose_at,
        yield_g=yield_g,
        yield_at=at,
        pour_seconds=pour_seconds,
    )


def test_a_reading_attaches_to_the_last_brew_and_computes_extraction() -> None:
    """EXT = TDS x yield / dose — the relation the chart's ratio diagonals are drawn
    from, so a disagreement here puts every dot off its own line."""
    session = _session()
    session.last_pair = _pair(17.8, 37.4, 1000.0)

    point = session.record_measurement(10.67)

    assert point is not None
    assert point.dose == 17.8 and point.yield_g == 37.4 and point.tds == 10.67
    assert point.ext == 22.42          # 10.67 * 37.4 / 17.8
    assert point.at == 1000.0          # identified by the brew, not by the reading
    assert session.measurements == [point]


def _dose(value, at, rise=2.0):
    from custom_components.difluid_microbalance.brew_session import WeighEvent

    return WeighEvent(value=value, at=at, hold_seconds=30.0, rise_seconds=rise)


def test_a_dose_weighed_after_the_last_pair_is_the_brew_being_measured() -> None:
    """The 2026-08-26 defect, at its real numbers.

    18.1 g was ground at 10:06, the BLE link died 25 s later and stayed down through
    the shot, and the reading taken at 10:12 was divided by the dose and yield of an
    08:44 brew.  Arithmetically perfect, about the wrong coffee.
    """
    session = _session()
    session.last_pair = _pair(18.0, 37.4, 1000.0)      # the 08:44 cup
    session.last_dose = _dose(18.1, 2000.0)            # ground at 10:06, never paired

    point = session.record_measurement(10.13)

    assert point.dose == 18.1, "the reading was attributed to the previous brew"
    assert point.at == 2000.0
    assert point.yield_g is None
    assert point.ext is None, "extraction is not computable without a yield"


def test_a_dose_older_than_the_last_pair_does_not_hijack_the_reading() -> None:
    """The ordinary case: grind, pull, measure.  last_dose belongs to that same pair
    and is older than its pour, so the pair — which knows the yield — must win."""
    session = _session()
    session.last_dose = _dose(18.0, 900.0)
    session.last_pair = _pair(18.0, 37.4, 1000.0)

    point = session.record_measurement(10.67)

    assert point.yield_g == 37.4
    assert point.ext == 22.17                          # 10.67 * 37.4 / 18.0


def test_typing_the_yield_completes_the_extraction() -> None:
    session = _session()
    session.last_dose = _dose(18.1, 2000.0)
    session.record_measurement(10.13)

    updated = session.set_measured_yield(37.0)

    assert updated.yield_g == 37.0
    assert updated.ext == 20.71                       # 10.13 * 37.0 / 18.1
    assert len(session.measurements) == 1, "it edited the brew rather than adding one"
    assert session.last_measurement.ext == 20.71


def test_typing_a_yield_with_nothing_measured_does_nothing() -> None:
    """Deliberately not remembered for the next reading: a figure typed an hour ago
    silently overriding a pour the scale did see is worse than doing nothing."""
    session = _session()
    assert session.set_measured_yield(37.0) is None
    assert session.measurements == []


def test_typing_the_dose_corrects_the_extraction() -> None:
    """The 2026-08-17 defect, after the fact.

    current_brew anchors on last_dose, which is the last thing weighed in the dose
    range and not the pairer's considered choice — so the portafilter set back on the
    scale at 19.3 g became the dose for a shot ground at 18.0 g.  Every figure derived
    from it was then arithmetically perfect about the wrong quantity of beans.
    """
    session = _session()
    session.last_pair = _pair(19.3, 37.4, 1000.0)
    session.record_measurement(11.03)
    assert session.last_measurement.ext == 21.37       # 11.03 * 37.4 / 19.3

    updated = session.set_measured_dose(18.0)

    assert updated.dose == 18.0
    assert updated.ext == 22.92                        # 11.03 * 37.4 / 18.0
    assert updated.yield_g == 37.4, "correcting the dose moved the yield"
    assert round(updated.ratio, 2) == 2.08, "the ratio still divides the old dose"
    assert len(session.measurements) == 1, "it edited the brew rather than adding one"
    assert session.last_measurement.ext == 22.92


def test_correcting_the_dose_of_a_brew_with_no_pour_is_not_an_extraction() -> None:
    """The two failures can land on the same cup: the dose was wrong *and* the link
    dropped before the pour.  Fixing one of them must not manufacture the other —
    and the log line has to survive formatting a yield that is still None."""
    session = _session()
    session.last_dose = _dose(19.3, 2000.0)
    session.record_measurement(11.03)

    updated = session.set_measured_dose(18.0)

    assert updated.dose == 18.0
    assert updated.yield_g is None
    assert updated.ext is None, "extraction is not computable without a yield"
    assert updated.ratio is None


def test_a_dose_of_zero_says_unknown_rather_than_nothing() -> None:
    """0 is the only value in the control's range that cannot be a real dose, so it is
    how you say you do not know — and it must blank the derived figures rather than
    divide by itself."""
    session = _session()
    session.last_pair = _pair(19.3, 37.4, 1000.0)
    session.record_measurement(11.03)

    updated = session.set_measured_dose(0)

    assert updated.dose == 0
    assert updated.ext is None
    assert updated.ratio is None


def test_typing_a_dose_with_nothing_measured_does_nothing() -> None:
    session = _session()
    assert session.set_measured_dose(18.0) is None
    assert session.measurements == []


def test_a_late_pour_leaves_the_measurements_in_order() -> None:
    """Completing a brew moves its `at` forward by minutes; the list must follow.

    Everything downstream reads this list positionally, so an unsorted list does not
    degrade — it reports a different cup.  The sequence below is two documented
    incidents back to back: the beans are ground and the link drops (2026-08-26), and
    while it is down the portafilter goes back on the scale and is taken for a dose
    (2026-08-17), so there are two yieldless points when the real pour finally lands.
    """
    session = _session()
    session.last_dose = _dose(18.1, 1000.0)          # the beans
    session.record_measurement(10.13)
    session.last_dose = _dose(19.3, 1070.0)          # the portafilter, read again
    session.record_measurement(10.20)
    assert [m.at for m in session.measurements] == [1000.0, 1070.0]

    # The cup is weighed at last and pairs with the beans.
    session._complete_measurement(
        _pair(18.0, 37.0, 1930.0, pour_seconds=17.0, dose_at=1000.4)
    )

    ats = [m.at for m in session.measurements]
    assert ats == sorted(ats), "the completed brew was left out of order"
    assert session.last_measurement.at == 1930.0, (
        "last_measurement returned a different cup than the one just completed"
    )


def test_a_completed_brew_agrees_with_its_own_numbers() -> None:
    """EXT is stored, not derived, so it has to come from the dose in the same record.

    It was computed from the pair's dose while the record kept the dose it had been
    anchored on, so a stored brew could report an extraction its own dose and yield do
    not produce — and the chart's ratio diagonals are drawn from exactly that relation.
    """
    session = _session()
    session.last_dose = _dose(18.1, 1000.0)
    session.record_measurement(10.13)

    session._complete_measurement(
        _pair(18.0, 37.0, 1930.0, pour_seconds=17.0, dose_at=1000.4)
    )

    point = session.last_measurement
    assert point.dose == 18.0, "the pairer's considered dose was not adopted"
    assert point.yield_g == 37.0
    assert point.seconds == 17.0
    assert point.ext == round(point.tds * point.yield_g / point.dose, 2)
    assert point.ext == 20.82                        # 10.13 * 37.0 / 18.0


def test_typing_the_yield_does_not_block_the_real_pour_from_arriving() -> None:
    """The remedy must not disable the recovery.

    set_measured_yield exists for a shot whose pour the scale missed — the same point
    _complete_measurement is written to complete.  Requiring the yield to still be
    missing meant using the documented remedy made the point permanently unmatchable:
    its `at` stayed on the dose, so the card called the cup in your hand stale and a
    second reading of it started a twin.
    """
    session = _session()
    session.last_dose = _dose(18.1, 1000.0)
    session.record_measurement(10.13)
    session.set_measured_yield(36.0)                 # a guess, while the link is down
    assert session.last_measurement.at == 1000.0

    session._complete_measurement(
        _pair(18.0, 37.0, 1930.0, pour_seconds=17.0, dose_at=1000.4)
    )

    assert len(session.measurements) == 1, "the late pour started a second point"
    point = session.last_measurement
    assert point.at == 1930.0, "the point never moved onto the pour it belongs to"
    assert point.yield_g == 37.0, (
        "the typed stand-in outlived the pour the scale actually weighed"
    )
    assert point.ext == round(point.tds * point.yield_g / point.dose, 2)


def test_a_late_pour_completes_the_brew_that_was_already_measured() -> None:
    """The scale comes back and reports the pour after the cup was measured.

    Without this the reading would sit extraction-less forever *and* a second reading
    would start a second point, because a dose-anchored point is stamped with the
    dose's time and a paired one with the pour's.
    """
    session = _session()
    session.last_dose = _dose(18.1, 2000.0)
    session.record_measurement(10.13)
    assert session.measurements[0].ext is None

    # dose_at 0.3 s off the dose's own timestamp: the two are independent conversions
    # of the same monotonic instant, made minutes apart with whatever clock offset was
    # measured at the time, so they are never bit-identical in production.  An equality
    # match here would pass on a fixture and never fire on the real thing.
    pair = _pair(18.1, 37.0, 2100.0, pour_seconds=24.0, dose_at=2000.3)
    session._complete_measurement(pair)

    assert len(session.measurements) == 1
    point = session.measurements[0]
    assert point.yield_g == 37.0
    assert point.ext == 20.71
    assert point.seconds == 24.0
    assert point.at == 2100.0, "the point moved onto the pour's time"

    # And a second reading of that cup now replaces the point instead of twinning it.
    session.last_pair = pair
    session.record_measurement(10.20)
    assert len(session.measurements) == 1


def test_a_later_brew_does_not_steal_an_earlier_cups_missing_yield() -> None:
    """Measure cup A with its pour lost, then grind and pull cup B.

    B's pour must not be written onto A.  This is why the match is on the dose's time
    rather than "the newest measurement that has no yield", which is simpler and
    wrong.
    """
    session = _session()
    session.last_dose = _dose(18.1, 2000.0)
    session.record_measurement(10.13)

    # Two minutes after A's dose, which is a back-to-back pair of shots and not an
    # unusual one.  The gap is deliberately small: a tolerance loose enough to cover
    # it would steal A's identity, and the first version of this test used a
    # three-quarter-hour gap that a 30-minute tolerance sailed through.
    session._complete_measurement(_pair(18.0, 37.4, 2200.0, dose_at=2120.0))

    assert session.measurements[0].yield_g is None
    assert session.measurements[0].ext is None


def test_the_extraction_sensor_says_when_the_last_brew_was() -> None:
    """What the chart puts its highlight out from.

    Its own test because the card cannot check it: emptying this attribute leaves every
    JavaScript assertion passing and every dot lit forever, which is the state the
    highlight exists to prevent.
    """
    session = _session()
    session.last_dose = _dose(18.0, 900.0)
    session.last_pair = _pair(18.0, 37.2, 1000.0)
    session.record_measurement(11.01)

    desc = next(d for d in BREW_SENSORS if d.key == "last_extraction")
    assert desc.attrs_fn(session)["last_brew_at"] == 1000.0

    # Another shot pulled and not measured.  The attribute moves ahead of the newest
    # point, which is exactly the gap the card reads as "your cup is not this one".
    session.last_pair = _pair(18.0, 37.0, 5000.0)
    attrs = desc.attrs_fn(session)
    assert attrs["last_brew_at"] == 5000.0
    assert attrs["points"][-1][0] == 1000.0


def test_the_point_carries_the_pour_it_came_from() -> None:
    """The chart's legend shows what the scale saw, and it has to be *this* brew's.

    Reading the duration off the session's last_yield instead would be the same
    mistake that reported a 42.8 g shot's ratio against a 59.8 g weighing on
    2026-08-15: last_yield moves on to whatever is weighed next, and a point plotted
    a month from now must still describe the cup it was.
    """
    session = _session()
    session.last_pair = _pair(17.8, 37.4, 1000.0, pour_seconds=20.4)
    point = session.record_measurement(10.67)
    assert point.seconds == 20.4

    # Something else goes on the scale, and the measured brew is unaffected.
    session.last_pair = _pair(18.0, 40.0, 2000.0, pour_seconds=31.0)
    assert session.measurements[0].seconds == 20.4


def test_a_pour_nobody_watched_start_has_no_duration() -> None:
    """None, not 0.0 — the distinction Plateau.rise_seconds exists for.

    A restart or a BLE gap mid-shot leaves the detector with no idea how the coffee
    got into the cup, and "0 s" would state that it arrived instantly.
    """
    session = _session()
    session.last_pair = _pair(17.8, 37.4, 1000.0)
    assert session.record_measurement(10.67).seconds is None


def test_re_measuring_the_same_brew_replaces_its_point() -> None:
    """Stir and measure again and you have corrected one reading, not drunk twice.

    Worth pinning because the rule that makes it possible — every reading belongs to
    the most recent brew — is also what would produce the twin.
    """
    session = _session()
    session.last_pair = _pair(17.8, 37.4, 1000.0)

    session.record_measurement(9.0)
    session.record_measurement(10.67)

    assert len(session.measurements) == 1
    assert session.measurements[0].tds == 10.67


def test_a_reading_with_no_brew_behind_it_is_dropped() -> None:
    """Measuring something on a fresh install has nothing to attach to."""
    session = _session()
    assert session.last_pair is None
    assert session.record_measurement(10.67) is None
    assert session.measurements == []


def test_measurements_survive_a_restart_and_a_record_from_the_future() -> None:
    """Restored per-record, like the totals and for the same reason: this is the only
    copy.  A reading cannot be reconstructed from anywhere — the R2 keeps its own log,
    but nothing can re-associate an entry in it with the brew it belonged to."""
    from custom_components.difluid_microbalance.brew_session import BrewSession

    session = _session()
    session.last_pair = _pair(17.8, 37.4, 1000.0)
    session.record_measurement(10.67)
    stored = session._snapshot()

    # One record written by a later version, carrying a field this one never heard of.
    stored["measurements"].append(
        {**stored["measurements"][0], "at": 2000.0, "grinder_setting": 4.5}
    )

    restored = BrewSession._restore_measurements(stored)
    assert [m.at for m in restored] == [1000.0], (
        "an unreadable record took the readable ones with it"
    )


async def test_the_import_flow_carries_the_prefix_store_key_and_thresholds(
    hass: HomeAssistant,
) -> None:
    """The upgrade's one moving part, exercised.

    On the live install the scale's setup starts this flow, and if it produces the
    wrong entry — or none — the statistics entities have no owner at all: the scale
    stopped creating them in this version.  Failing here is not a degraded upgrade, it
    is seven entities going unavailable.
    """
    from homeassistant.config_entries import SOURCE_IMPORT

    from custom_components.difluid_microbalance.brew_session import DEFAULT_STORE_KEY
    from custom_components.difluid_microbalance.const import CONF_STORE_KEY

    scale = MockConfigEntry(
        domain=DOMAIN,
        title="Kitchen Microbalance 304268",
        data={CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE, "address": "AA:BB:CC:DD:EE:FF"},
    )
    scale.add_to_hass(hass)

    thresholds = {"dose_min": 12.0, "dose_max": 25.0, "ratio_max": 3.5}
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={
            CONF_SCALE_ENTRY: scale.entry_id,
            "r2_entry": None,
            "options": thresholds,
        },
    )
    assert result["type"] == "create_entry", result

    created = result["result"]
    assert created.data[CONF_DEVICE_TYPE] == DEVICE_TYPE_DETECTOR
    assert created.data[CONF_SCALE_ENTRY] == scale.entry_id
    # The two that carry the history across.
    assert created.data[CONF_UID_PREFIX] == scale.entry_id
    assert created.data[CONF_STORE_KEY] == DEFAULT_STORE_KEY
    # Thresholds tuned over months of brewing, not silently reset to defaults.
    assert dict(created.options) == thresholds

    # Run it twice: the scale's setup runs on every restart, and a second detector
    # would fight the first for the same weight stream and the same stored count.
    again = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={CONF_SCALE_ENTRY: scale.entry_id, "r2_entry": None, "options": {}},
    )
    assert again["type"] == "abort"
    assert again["reason"] == "already_configured"

    await _unload_all(hass)
