from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .brew_detect import TUNABLE_FIELDS, config_from_options
from .brew_session import DEFAULT_STORE_KEY, async_create_session
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_LICENSE_KEY,
    CONF_MODEL,
    CONF_R2_ENTRY,
    CONF_RECORD_DATASET,
    CONF_SCALE_ENTRY,
    CONF_STORE_KEY,
    CONF_UID_PREFIX,
    DEFAULT_MODEL_MICROBALANCE,
    DEFAULT_MODEL_MICROBALANCE_TI,
    DEVICE_TYPE_DETECTOR,
    DEVICE_TYPE_R2,
    DOMAIN,
)
from .coordinator import DifluidMicrobalanceCoordinator
from .coordinator_r2 import DifluidR2Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT]
#: The detector has no BLE link, so no number or select of its own.
DETECTOR_PLATFORMS = [Platform.SENSOR, Platform.BUTTON]


def _scale_ready_signal(scale_entry_id: str) -> str:
    """Dispatcher signal fired once a scale's coordinator is in hass.data."""
    return f"{DOMAIN}_scale_ready_{scale_entry_id}"

# Lovelace card bundled with the integration.  We serve the whole www/
# directory (a directory static path is more reliable than a single-file one)
# and auto-load the module so the card shows up in the "Add card" picker.
_WWW_DIR = Path(__file__).parent / "www"
_CARD_URL_BASE = f"/{DOMAIN}"
_CARD_FILE_URL = f"{_CARD_URL_BASE}/difluid-card.js"
_FRONTEND_KEY = f"{DOMAIN}_frontend_registered"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the custom card and auto-load it so it shows in the card picker."""
    if hass.data.get(_FRONTEND_KEY):
        return
    hass.data[_FRONTEND_KEY] = True

    # Serve the www/ directory statically.  cache_headers=True lets the browser
    # cache the module (busted by the ?v={version} query on version bump) — with
    # no caching every load re-downloads and can lose the render race.
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(_CARD_URL_BASE, str(_WWW_DIR), True)]
        )
    except ImportError:  # older HA
        hass.http.register_static_path(_CARD_URL_BASE, str(_WWW_DIR), True)
    except Exception as err:  # noqa: BLE001 - already registered / path issue
        _LOGGER.warning("Could not register DiFluid card static path: %s", err)

    # Version string is used only for cache-busting the module URL.
    version = ""
    try:
        from homeassistant.loader import async_get_integration

        integration = await async_get_integration(hass, DOMAIN)
        version = integration.version or ""
    except Exception:  # noqa: BLE001
        pass

    url = f"{_CARD_FILE_URL}?v={version}"

    # Primary: register as a Lovelace resource.  Resources are delivered to every
    # client over the websocket on each dashboard load, so the module loads
    # reliably everywhere — including cached/PWA frontends on phones, where
    # add_extra_js_url() often never reaches the client.
    if await _async_register_lovelace_resource(hass, url):
        _LOGGER.info(
            "DiFluid dashboard card registered as Lovelace resource at %s", url
        )
        return

    # Fallback (e.g. YAML-mode Lovelace where resources are read-only).
    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, url)
        _LOGGER.info(
            "DiFluid dashboard card auto-loaded via extra_js_url at %s", url
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Could not auto-load DiFluid card: %s", err)


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Register the card as a storage-mode Lovelace resource.

    Returns True on success, False if resources are unavailable or read-only
    (YAML mode), in which case the caller falls back to add_extra_js_url.
    """
    try:
        lovelace = hass.data.get("lovelace")
        resources = getattr(lovelace, "resources", None)
        if resources is None and isinstance(lovelace, dict):
            resources = lovelace.get("resources")
        # YAML-mode collection has no async_create_item -> not writable.
        if resources is None or not hasattr(resources, "async_create_item"):
            return False

        if not getattr(resources, "loaded", True):
            await resources.async_load()
            resources.loaded = True

        base = url.split("?")[0]
        for item in resources.async_items():
            if item.get("url", "").split("?")[0] == base:
                if item.get("url") != url:
                    await resources.async_update_item(
                        item["id"], {"res_type": "module", "url": url}
                    )
                return True

        await resources.async_create_item({"res_type": "module", "url": url})
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Lovelace resource registration failed: %s", err)
        return False


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    await _async_register_card(hass)

    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR:
        return await _async_setup_detector(hass, entry)

    address = entry.data[CONF_ADDRESS]

    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        coordinator = DifluidR2Coordinator(
            hass,
            address=address,
            license_key=entry.data.get(CONF_LICENSE_KEY, ""),
        )
    else:
        is_ti = entry.data.get(CONF_IS_TI, False)
        default_model = (
            DEFAULT_MODEL_MICROBALANCE_TI if is_ti else DEFAULT_MODEL_MICROBALANCE
        )
        coordinator = DifluidMicrobalanceCoordinator(
            hass,
            address=address,
            is_ti=is_ti,
            license_key=entry.data.get(CONF_LICENSE_KEY, ""),
            model=entry.data.get(CONF_MODEL) or default_model,
        )

    # Register coordinator before forwarding platforms so entity setup can access it.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if isinstance(coordinator, DifluidMicrobalanceCoordinator):
        # Tell any detector waiting on this scale that its coordinator now exists.
        # Without this a detector that lost the load-order race would sit in
        # ConfigEntryNotReady backoff for up to a couple of minutes after a restart,
        # not detecting anything, for no reason other than the order HA happened to
        # set the two entries up in.
        async_dispatcher_send(hass, _scale_ready_signal(entry.entry_id))
        await _async_import_detector(hass, entry)

    # async_start never raises — if device is off it silently waits for BLE advertisement.
    await coordinator.async_start()
    return True


async def _async_setup_detector(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a detector entry: a session fed by a scale that it does not own."""
    _async_seed_detector_identity(hass, entry)

    scale_entry_id = entry.data.get(CONF_SCALE_ENTRY)
    coordinator = hass.data.setdefault(DOMAIN, {}).get(scale_entry_id)
    if not isinstance(coordinator, DifluidMicrobalanceCoordinator):
        # The scale is not set up yet — or at all.  Retry on backoff, and also wake
        # up the moment the scale announces itself, whichever comes first.
        entry.async_on_unload(
            async_dispatcher_connect(
                hass,
                _scale_ready_signal(scale_entry_id),
                lambda: hass.config_entries.async_schedule_reload(entry.entry_id),
            )
        )
        raise ConfigEntryNotReady(
            f"Waiting for the scale ({scale_entry_id}) this detector reads"
        )

    session = await async_create_session(
        hass,
        config_from_options(dict(entry.options)),
        bool(entry.options.get(CONF_RECORD_DATASET, False)),
        entry.data.get(CONF_STORE_KEY) or DEFAULT_STORE_KEY,
    )
    hass.data[DOMAIN][entry.entry_id] = session
    entry.async_on_unload(coordinator.add_brew_consumer(session))
    # Changing a threshold in the options flow reloads the entry, which rebuilds the
    # detector with the new config.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    await hass.config_entries.async_forward_entry_setups(entry, DETECTOR_PLATFORMS)
    return True


def _async_seed_detector_identity(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Fix this detector's unique_id prefix and storage key, once and for good.

    Both derive from the entry's own entry_id, which does not exist while the config
    flow is running, so a user-created detector arrives here without them.  An
    imported one arrives with both already set — to the *scale's* entry_id and to the
    old singleton's storage key — and must keep them: that is what makes the seven
    statistics entities the same entities they were before the move, history and all.

    Written only when absent.  Recomputing them would mean that re-pointing a
    detector at a different scale silently orphaned its entities and restarted the
    odometer at zero.
    """
    seeded = {}
    if not entry.data.get(CONF_UID_PREFIX):
        seeded[CONF_UID_PREFIX] = entry.entry_id
    if not entry.data.get(CONF_STORE_KEY):
        seeded[CONF_STORE_KEY] = f"{DEFAULT_STORE_KEY}.{entry.entry_id}"
    if seeded:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **seeded})


async def _async_import_detector(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Give an install that predates the detector entry one, without asking.

    Recognised by the scale still carrying detector thresholds in its options: those
    only ever got there through the old options flow, which was attached to the
    scale.  Nothing is moved by hand — the import flow creates the entry carrying the
    scale's entry_id as the unique_id prefix, and the entities re-register themselves
    against it on the next setup, keeping their entity_ids.
    """
    if not any(key in entry.options for key in TUNABLE_FIELDS):
        return
    if any(
        other.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR
        and other.data.get(CONF_SCALE_ENTRY) == entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        return

    r2 = next(
        (
            other.entry_id
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2
        ),
        None,
    )
    _LOGGER.info(
        "Moving the brew detector off the scale entry and onto one of its own; "
        "entity_ids and brew_count are preserved"
    )
    # The options are copied into the flow's data here, before they are cleared below.
    flow_data = {
        CONF_SCALE_ENTRY: entry.entry_id,
        CONF_R2_ENTRY: r2,
        "options": dict(entry.options),
    }
    # Not awaited: creating an entry sets it up, and that setup reaches back into
    # hass.data for this scale's coordinator.  Starting it as its own task keeps the
    # scale's setup from waiting on an entry that is waiting on the scale.
    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=flow_data
        )
    )
    # Drop the thresholds from the scale so the import runs once and only once.
    hass.config_entries.async_update_entry(entry, options={})


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down one entry.

    Home Assistant unloads an entry to reload it as well as to remove it, and every
    threshold change reloads the detector.  The session is dropped here and rebuilt
    from its Store on the next setup, so the flush below is not housekeeping: without
    it, up to _SAVE_DELAY seconds of state exists only in memory, and changing a
    threshold in the seconds after weighing the beans would lose the dose.  The old
    singleton avoided this by outliving the entry — see BrewSession.async_flush.
    """
    is_detector = entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR
    platforms = DETECTOR_PLATFORMS if is_detector else PLATFORMS
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        held = hass.data[DOMAIN].pop(entry.entry_id, None)
        if held is None:
            return unload_ok
        if is_detector:
            # The weight-stream subscription is already gone via async_on_unload.
            await held.async_flush()
        else:
            await held.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget a detector's stored state when its entry is deleted.

    Home Assistant calls this only on deletion, after async_unload_entry has run —
    that is the distinction async_unload_entry cannot make for itself, and the reason
    the two exist separately.

    Deleting the entry is what someone does precisely when they want the count and
    the last pair gone, so the Store goes with it.  This used to need a headcount of
    the remaining scale entries to decide whether anyone else still owned the shared
    session; a detector owns exactly its own, so there is nothing left to work out.
    """
    if entry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_DETECTOR:
        return

    session = await async_create_session(
        hass,
        config_from_options(dict(entry.options)),
        False,
        entry.data.get(CONF_STORE_KEY) or DEFAULT_STORE_KEY,
    )
    await session.async_remove()
    _LOGGER.info("Removed the brew detector's stored state")
