from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .brew_session import BrewSession, detector_device_info
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_UID_PREFIX,
    DEVICE_TYPE_DETECTOR,
    DEVICE_TYPE_R2,
    DOMAIN,
)
from .coordinator import DifluidMicrobalanceCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
        return  # R2 has its own hardware auto-off, no need for this entity

    if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_DETECTOR:
        session: BrewSession = hass.data[DOMAIN][entry.entry_id]
        async_add_entities(
            [
                MeasuredYieldNumber(
                    session,
                    entry.data.get(CONF_UID_PREFIX) or entry.entry_id,
                    detector_device_info(entry),
                )
            ]
        )
        return

    coordinator: DifluidMicrobalanceCoordinator = hass.data[DOMAIN][entry.entry_id]
    is_ti = entry.data.get(CONF_IS_TI, False)
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if is_ti else "Microbalance",
    )
    async_add_entities([AutoShutdownNumber(coordinator, entry, device_info)])


class MeasuredYieldNumber(NumberEntity):
    """The yield of the brew that was measured last, typed in when the scale missed it.

    Reads the stored measurement rather than holding a value of its own, so it shows
    what the chart is actually plotting and cannot drift out of step with it.  Not a
    RestoreEntity for the same reason: there is nothing here to restore that the
    session does not already keep.

    Deliberately not EntityCategory.CONFIG.  It is not a setting — it is a reading, and
    it belongs in Controls next to Reset Period, which is where somebody looking at a
    measurement with no extraction will go.

    Always available, like the rest of the detector's entities: the scale being off the
    air is precisely when this gets used.
    """

    _attr_has_entity_name = True
    _attr_name = "Measured Yield"
    _attr_icon = "mdi:cup-outline"
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    # The detector's own plausible-pour range, widened a little at both ends: this is
    # for the cases the detector did not judge, so its thresholds are guidance here
    # rather than law.
    _attr_native_min_value = 0
    _attr_native_max_value = 200
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, session: BrewSession, uid_prefix: str, device_info: DeviceInfo):
        self._session = session
        # Same prefix rule as the brew sensors — see DifluidBrewSensor.__init__.  The
        # `sensor.…_brew_yield` that already exists is a different domain, so this
        # unique_id cannot collide with it.
        self._attr_unique_id = f"{uid_prefix}_measured_yield"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        # add_listener returns nothing, so the pairing is explicit rather than through
        # async_on_remove — same as DifluidBrewSensor.
        self._session.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._session.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float | None:
        point = self._session.last_measurement
        return None if point is None else point.yield_g

    async def async_set_native_value(self, value: float) -> None:
        self._session.set_measured_yield(value)


class AutoShutdownNumber(
    CoordinatorEntity[DifluidMicrobalanceCoordinator], NumberEntity, RestoreEntity
):
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    # Name it after what it actually does: disconnect BLE after N idle minutes,
    # which lets the scale power off via its own hardware auto-off timer.
    _attr_name = "Auto-disconnect Bluetooth"
    _attr_icon = "mdi:bluetooth-off"
    _attr_native_min_value = 0
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        entry: ConfigEntry,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_auto_shutdown"
        self._attr_device_info = device_info
        self._current_value: float = 0

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state and state.state not in (None, "unavailable", "unknown"):
            try:
                self._current_value = float(state.state)
            except ValueError:
                self._current_value = 0
        self.coordinator.set_auto_shutdown_minutes(int(self._current_value))

    @property
    def available(self) -> bool:
        # Always available — user can configure it even when device is off
        return True

    @property
    def native_value(self) -> float:
        return self._current_value

    async def async_set_native_value(self, value: float) -> None:
        self._current_value = value
        self.coordinator.set_auto_shutdown_minutes(int(value))
        self.async_write_ha_state()
