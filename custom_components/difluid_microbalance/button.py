from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .brew_session import BREW_KEY, BrewSession
from .const import CONF_DEVICE_TYPE, CONF_IS_TI, DEVICE_TYPE_R2, DOMAIN
from .coordinator import DifluidMicrobalanceCoordinator
from .coordinator_r2 import DifluidR2Coordinator

# ── Microbalance commands ──────────────────────────────────────────────────────
# DF DF 03 02 01 01  CS=C5  Power Button single click → Tare
_CMD_TARE         = bytes.fromhex("dfdf03020101c5")
# DF DF 03 02 01 00  CS=C4  DLink Button single click → Timer start / resume
_CMD_TIMER_START  = bytes.fromhex("dfdf03020100c4")
# DF DF 03 01 01 00  CS=C3  DLink Button press → Timer stop / pause
_CMD_TIMER_STOP   = bytes.fromhex("dfdf03010100c3")

# ── R2 commands ────────────────────────────────────────────────────────────────
# DF DF 03 00 00  CS=C1  Single test
_CMD_R2_TEST      = bytes.fromhex("dfdf030000c1")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    is_r2 = entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2
    is_ti = entry.data.get(CONF_IS_TI, False)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    if is_r2:
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="R2 Extract",
        )
        async_add_entities([R2TestButton(coordinator, entry, device_info)])
    else:
        device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="Microbalance Ti" if is_ti else "Microbalance",
        )
        buttons: list[ButtonEntity] = [
            TareButton(coordinator, entry, device_info),
            TimerStartStopButton(coordinator, entry, device_info),
        ]
        session = hass.data.get(DOMAIN, {}).get(BREW_KEY)
        if session is not None:
            buttons.append(ResetPeriodButton(session, entry, device_info))
        async_add_entities(buttons)


# ── base classes ───────────────────────────────────────────────────────────────

class _MicrobalanceButton(CoordinatorEntity[DifluidMicrobalanceCoordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, device_info, key, name, icon):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected


class _R2Button(CoordinatorEntity[DifluidR2Coordinator], ButtonEntity):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, device_info, key, name, icon):
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected


# ── Microbalance buttons ───────────────────────────────────────────────────────

class TareButton(_MicrobalanceButton):
    def __init__(self, coordinator, entry, device_info):
        super().__init__(coordinator, entry, device_info, "tare", "Tare", "mdi:scale-balance")

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(_CMD_TARE)


class TimerStartStopButton(_MicrobalanceButton):
    def __init__(self, coordinator, entry, device_info):
        super().__init__(coordinator, entry, device_info, "timer_start", "Start/Stop", "mdi:timer-play-outline")

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(_CMD_TIMER_START)


class ResetPeriodButton(ButtonEntity):
    """Start a new statistics period — the trip-meter reset.

    Deliberately not a _MicrobalanceButton.  The other two send a BLE command, so it is
    right that they grey out when the scale is off; this one only moves a snapshot in
    the session, and the scale is off almost all the time — it auto-disconnects five
    minutes after a pour.  Tying availability to the link would make the button
    reachable for about five minutes a day, none of them the moment somebody decides to
    start a new month.  Same reasoning as DifluidBrewSensor.available.

    No entity_category: the other two are CONFIG because they configure the scale, and
    this configures nothing — it belongs with the statistics it resets, which carry no
    category either.
    """

    _attr_has_entity_name = True
    _attr_name = "Reset Period"
    _attr_icon = "mdi:backup-restore"
    _attr_should_poll = False

    def __init__(self, session: BrewSession, entry: ConfigEntry, device_info: DeviceInfo):
        self._session = session
        self._attr_unique_id = f"{entry.entry_id}_reset_period"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return True

    async def async_press(self) -> None:
        self._session.reset_period()


# ── R2 buttons ─────────────────────────────────────────────────────────────────

class R2TestButton(_R2Button):
    def __init__(self, coordinator, entry, device_info):
        super().__init__(coordinator, entry, device_info, "test", "Start Test", "mdi:test-tube")

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(_CMD_R2_TEST)
