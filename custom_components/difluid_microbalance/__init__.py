from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .brew_detect import config_from_options
from .brew_session import async_get_session, async_remove_session
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_LICENSE_KEY,
    CONF_MODEL,
    CONF_RECORD_DATASET,
    DEFAULT_MODEL_MICROBALANCE,
    DEFAULT_MODEL_MICROBALANCE_TI,
    DEVICE_TYPE_R2,
    DOMAIN,
)
from .coordinator import DifluidMicrobalanceCoordinator
from .coordinator_r2 import DifluidR2Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON, Platform.NUMBER, Platform.SELECT]

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
        brew = await async_get_session(
            hass,
            config_from_options(dict(entry.options)),
            bool(entry.options.get(CONF_RECORD_DATASET, False)),
        )
        coordinator = DifluidMicrobalanceCoordinator(
            hass,
            address=address,
            is_ti=is_ti,
            license_key=entry.data.get(CONF_LICENSE_KEY, ""),
            model=entry.data.get(CONF_MODEL) or default_model,
            brew=brew,
        )
        # Changing a threshold in the options flow reloads the entry, which rebuilds
        # the detector with the new config.
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    # Register coordinator before forwarding platforms so entity setup can access it.
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # async_start never raises — if device is off it silently waits for BLE advertisement.
    await coordinator.async_start()
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down one entry's coordinator.

    Deliberately leaves the shared brew session alone.  Home Assistant calls this for
    a reload as well as for a removal, and every options change reloads the entry
    (see _async_reload_entry), so anything cleaned up here is cleaned up several
    times a day.  The session is what carries last_pair, brew_count and an unpaired
    dose across those reloads — dropping it here would mean tightening a threshold
    between weighing the beans and pulling the shot lost the dose.  Removal is
    handled in async_remove_entry below, which only runs when the entry is deleted.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the brew session when the scale's entry is deleted.

    Home Assistant calls this only on deletion, after async_unload_entry has run —
    that is the distinction async_unload_entry cannot make for itself.

    Without it, `hass.data[DOMAIN][BREW_KEY]` survived removal (unload pops only the
    entry_id key) and the persisted Store survived along with it, so deleting the
    integration and adding it again brought back the previous last_pair, brew_count
    and detector config.  Reinstalling is what someone does precisely when they want
    that state gone.
    """
    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        # The R2 never creates a session; it only reads the scale's.
        return

    # The session is one per Home Assistant rather than one per entry, so a second
    # scale would still be using it.  The entry being removed is still present in the
    # registry while this runs, so it has to be excluded by id.
    others = [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
        and other.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_R2
    ]
    if others:
        _LOGGER.debug(
            "Keeping the brew session: %d other scale entry/entries still use it",
            len(others),
        )
        return

    await async_remove_session(hass)
