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
