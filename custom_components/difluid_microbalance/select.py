from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE, CONF_IS_TI, DEVICE_TYPE_R2, DOMAIN
from .coordinator import DifluidMicrobalanceCoordinator

_LOGGER = logging.getLogger(__name__)

# (auto_detect_timing, auto_stop_timing)
_MODE_MAP: dict[str, tuple[bool, bool]] = {
    "Manual":    (False, False),
    "Espresso":  (True, False),
    "Pour Over": (True, True),
}

def _build_cmd(func: int, cmd: int, data: bytes = b"") -> bytes:
    header = bytes([0xDF, 0xDF])
    frame = bytes([func, cmd, len(data)]) + data
    full = header + frame
    return full + bytes([sum(full) & 0xFF])


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        return
    coordinator: DifluidMicrobalanceCoordinator = hass.data[DOMAIN][entry.entry_id]
    is_ti = entry.data.get(CONF_IS_TI, False)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if is_ti else "Microbalance",
    )
    async_add_entities([DifluidModeSelect(coordinator, entry, device_info)])


class DifluidModeSelect(CoordinatorEntity[DifluidMicrobalanceCoordinator], SelectEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Mode"
    _attr_icon = "mdi:coffee-maker"
    _attr_options = list(_MODE_MAP.keys())

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_mode"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected

    @property
    def current_option(self) -> str | None:
        """The named mode the scale is in, or None when it is in none of them.

        None rather than the "Manual" this used to fall back on.  _MODE_MAP covers
        three of the four combinations, and the fourth — auto-detect off with auto-stop
        still armed — is reachable: async_select_option sends two commands in
        succession, and a link that drops between them leaves the scale exactly there.
        Reporting that as Manual made the UI agree with what was asked for while the
        scale went on auto-stopping the timer, and re-selecting Manual was then a no-op
        from the frontend's side, so the one obvious way to fix it did nothing.
        Unknown is the honest answer and it leaves the option selectable.
        """
        data = self.coordinator.data
        if data is None:
            return None
        detect = data.auto_detect_timing
        stop = data.auto_stop_timing
        for name, (d, s) in _MODE_MAP.items():
            if d == detect and s == stop:
                return name
        _LOGGER.debug(
            "Scale is in an unnamed mode (auto_detect=%s, auto_stop=%s); reporting "
            "unknown rather than guessing",
            detect, stop,
        )
        return None

    async def async_select_option(self, option: str) -> None:
        detect, stop = _MODE_MAP.get(option, (False, False))
        cmd_detect = _build_cmd(0x01, 0x01, bytes([0x01 if detect else 0x00]))
        cmd_stop   = _build_cmd(0x01, 0x02, bytes([0x01 if stop else 0x00]))
        await self.coordinator.async_send_command(cmd_detect)
        await self.coordinator.async_send_command(cmd_stop)
