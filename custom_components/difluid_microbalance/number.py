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
        uid_prefix = entry.data.get(CONF_UID_PREFIX) or entry.entry_id
        device_info = detector_device_info(entry)
        async_add_entities(
            [
                MeasuredDoseNumber(session, uid_prefix, device_info),
                MeasuredYieldNumber(session, uid_prefix, device_info),
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


class _MeasuredNumber(NumberEntity):
    """One half of the last measured brew, typed in by hand.

    Reads the stored measurement rather than holding a value of its own, so it shows
    what the chart is actually plotting and cannot drift out of step with it.  Not a
    RestoreEntity for the same reason: there is nothing here to restore that the
    session does not already keep.

    Deliberately not EntityCategory.CONFIG.  Neither is a setting — each is a reading,
    and they belong in Controls, which is where somebody looking at a measurement whose
    numbers are wrong will go.

    Always available, like the rest of the detector's entities: the scale being off the
    air, or having weighed the wrong thing, is precisely when these get used.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_native_min_value = 0
    _attr_native_step = 0.1
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    #: entity_id suffix and the field of BrewMeasurement this one shows.  Subclasses
    #: set both; nothing here has a default, because a subclass that forgot one would
    #: otherwise silently become a second copy of the other control.
    _key: str
    _field: str

    def __init__(self, session: BrewSession, uid_prefix: str, device_info: DeviceInfo):
        self._session = session
        # Same prefix rule as the brew sensors — see DifluidBrewSensor.__init__.  The
        # `sensor.…_brew_yield` and `sensor.…_last_dose` that already exist are a
        # different domain, so these unique_ids cannot collide with them.
        self._attr_unique_id = f"{uid_prefix}_{self._key}"
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
        return None if point is None else getattr(point, self._field)


class MeasuredDoseNumber(_MeasuredNumber):
    """What went in, corrected by hand when the detector weighed the wrong thing.

    The case it answers is not a missing dose — a reading with no dose at all is never
    recorded, because current_brew has nothing to anchor it to — but a dose that is
    confidently wrong: a portafilter set back on the scale outranking the beans, which
    is what happened on 2026-08-17.  See BrewSession.set_measured_dose.
    """

    _key = "measured_dose"
    _field = "dose"
    _attr_name = "Measured Dose"
    _attr_icon = "mdi:coffee-outline"
    # A dose, not a pour: the detector's own dose range with a lot of room either side,
    # since this is for the cases it judged wrongly and its thresholds are guidance
    # here rather than law.  100 g is already several portafilters.
    _attr_native_max_value = 100

    async def async_set_native_value(self, value: float) -> None:
        self._session.set_measured_dose(value)


class MeasuredYieldNumber(_MeasuredNumber):
    """The yield of the brew that was measured last, typed in when the scale missed it.

    See BrewSession.set_measured_yield.
    """

    _key = "measured_yield"
    _field = "yield_g"
    _attr_name = "Measured Yield"
    _attr_icon = "mdi:cup-outline"
    # The detector's own plausible-pour range, widened a little at both ends, for the
    # same reason the dose's is.
    _attr_native_max_value = 200

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
