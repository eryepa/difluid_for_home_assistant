from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_LICENSE_KEY,
    CONF_MODEL,
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

    # async_start never raises — if device is off it silently waits for BLE advertisement.
    await coordinator.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_stop()
    return unload_ok
