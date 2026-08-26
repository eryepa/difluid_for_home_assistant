from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .brew_session import BrewSession, detector_device_info
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_UID_PREFIX,
    DEVICE_TYPE_DETECTOR,
    DEVICE_TYPE_R2,
    DOMAIN,
)
from .coordinator import DifluidMicrobalanceCoordinator, MicrobalanceData
from .coordinator_r2 import DifluidR2Coordinator, R2Data


#: Measured brews published for the chart.  The session keeps more than this; a
#: control chart with fifty dots on it stops showing where you are and starts showing
#: where you have ever been.
CHART_POINTS = 20


@dataclass(frozen=True)
class DifluidSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
    # Optional dynamic-icon callback: (data) -> icon string, or None to fall
    # back to the device_class / static icon.
    icon_fn: Callable | None = None


def _battery_icon(d) -> str | None:
    """Show a lightning-bolt (charging) battery icon while charging.

    Returns None when not charging so HA uses the dynamic battery-level icon
    provided by device_class = battery.
    """
    if not d.charging:
        return None
    level = max(0, min(100, int(d.battery)))
    if level >= 95:
        return "mdi:battery-charging-100"
    if level < 15:
        return "mdi:battery-charging-outline"
    rounded = min(90, max(20, int(round(level / 10.0) * 10)))
    return f"mdi:battery-charging-{rounded}"


# ── Microbalance sensors ──────────────────────────────────────────────────────
# Order here is the intended display order (Weight → Flow → Timer → Status →
# Battery).  Charging is merged into the Battery icon (lightning bolt while
# charging), so there is no separate Charging sensor.

MICROBALANCE_SENSORS: tuple[DifluidSensorDescription, ...] = (
    DifluidSensorDescription(
        key="weight",
        name="Weight",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        value_fn=lambda d: d.weight,
    ),
    DifluidSensorDescription(
        key="flow_rate",
        name="Flow Rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/s",
        suggested_display_precision=1,
        icon="mdi:water-flow",
        value_fn=lambda d: d.flow_rate,
    ),
    DifluidSensorDescription(
        key="timer",
        name="Timer",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        icon="mdi:timer-outline",
        value_fn=lambda d: d.timer,
    ),
    DifluidSensorDescription(
        key="device_status",
        name="Device Status",
        icon="mdi:information-outline",
        value_fn=lambda d: d.device_status,
    ),
    DifluidSensorDescription(
        key="battery",
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: d.battery,
        icon_fn=_battery_icon,
    ),
)

# ── brew detector sensors ─────────────────────────────────────────────────────
# These read the shared BrewSession rather than the coordinator, because the last
# detected shot must stay readable after the scale auto-disconnects — which it does
# five minutes after the pour, well before anyone looks at the dashboard.


@dataclass(frozen=True)
class DifluidBrewSensorDescription(SensorEntityDescription):
    value_fn: Callable = lambda _: None
    attrs_fn: Callable | None = None
    #: Set on the two per-day figures.  Their numerator only changes when a brew is
    #: detected, but their *denominator* grows continuously, so unlike every other
    #: sensor here they go stale on their own and need a clock.  See
    #: DifluidBrewRateSensor.
    ticks: bool = False


def _weigh_attrs(event) -> dict:
    if event is None:
        return {}
    # rise_seconds is None whenever the load was already on the scale before the
    # detector was watching — after a restart mid-session, a BLE reconnect, or a gap
    # in the stream.  It is published as None rather than coerced to 0.0, because 0.0
    # is not a stand-in for "unknown" here: it is the specific claim that the load
    # was set down in one go rather than ground on, which is what tells a portafilter
    # from a dose.  A template reading this must be able to tell the two apart, so
    # the attribute stays present and null rather than silently becoming a number or
    # silently disappearing.
    rise = event.rise_seconds
    return {
        "detected_at": dt_util.utc_from_timestamp(event.at).isoformat(),
        "plateau_seconds": event.hold_seconds,
        "rise_seconds": None if rise is None else round(float(rise), 1),
        # More than one entry means the weighing was topped up or was still
        # settling; it explains a value that looks off without a trip to the
        # recorder. A single entry is the ordinary case.
        "steps": event.steps,
    }


def _iso(timestamp: float) -> str | None:
    """A POSIX timestamp as an ISO string, or None if it was never set."""
    return dt_util.utc_from_timestamp(timestamp).isoformat() if timestamp else None


def _period_attrs(session) -> dict:
    """When the current period began and how long it has been running.

    Shared by every period sensor so that a reset is legible from any of them: without
    it, "3 brews" and "1.5 cups/d" are two numbers with no visible relationship to each
    other or to when the button was last pressed.

    elapsed_days is the raw age, deliberately *not* the floored value the averages
    divide by — on the first day those two differ, and the honest way to explain a
    daily average that looks low is to show the real age next to it rather than the
    one the formula used.  See BrewTotals.elapsed_days for why the floor exists.
    """
    started = session.totals.period_started
    if not started:
        return {}
    now = dt_util.utcnow().timestamp()
    return {
        "period_started": _iso(started),
        "elapsed_days": round(max(0.0, now - started) / 86400.0, 2),
    }


# state_class = MEASUREMENT on all three: it is what makes Home Assistant keep
# long-term statistics for them (permanent, unlike the recorder's ~10 days) and it
# gives the Prometheus exporter a numeric series to publish.
#
# entity_category = DIAGNOSTIC on the five "last shot" sensors moves them into the
# Diagnostic panel of the detector's device page and out of the way of the statistics
# below, which is what they are: the working parts of one brew, useful when a result
# looks wrong and noise the rest of the time.  It changes *only* their grouping —
# entity_id is assigned at first registration and does not depend on the category, so
# the Prometheus rules and the notification email keep resolving, and both recorder
# history and long-term statistics carry on uninterrupted.
BREW_SENSORS: tuple[DifluidBrewSensorDescription, ...] = (
    DifluidBrewSensorDescription(
        key="last_dose",
        name="Last Dose",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:coffee-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_dose.value if s.last_dose else None,
        attrs_fn=lambda s: _weigh_attrs(s.last_dose),
    ),
    DifluidBrewSensorDescription(
        key="last_yield",
        name="Last Yield",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:cup-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_yield.value if s.last_yield else None,
        attrs_fn=lambda s: _weigh_attrs(s.last_yield),
    ),
    # The two members of the last pair, as opposed to the last weighing of each kind
    # above.  They exist because those two are not the same thing and reporting them
    # as if they were produced two wrong emails: on 2026-08-15 the ratio was computed
    # from a 42.8 g pour while Last Yield had already moved on to 59.8 g, and on
    # 2026-08-17 Last Dose read 19.3 g — the holder set back on the scale — while the
    # pair had correctly used the 18.0 g that was ground.
    #
    # Anything reporting a brew must read these three together; they change at one
    # moment and always describe the same cup.  Last Dose and Last Yield stay as they
    # are, for seeing weighings that never paired — which is how the last three
    # defects were found.
    DifluidBrewSensorDescription(
        key="brew_dose",
        name="Brew Dose",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:coffee",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.last_pair.dose, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {"paired_at": dt_util.utc_from_timestamp(s.last_pair.yield_at).isoformat()}
            if s.last_pair
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_yield",
        name="Brew Yield",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=1,
        icon="mdi:cup",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.last_pair.yield_g, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {"paired_at": dt_util.utc_from_timestamp(s.last_pair.yield_at).isoformat()}
            if s.last_pair
            else {}
        ),
    ),
    DifluidBrewSensorDescription(
        key="brew_ratio",
        name="Brew Ratio",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:scale-unbalanced",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: round(s.last_pair.ratio, 2) if s.last_pair else None,
        attrs_fn=lambda s: (
            {
                "dose": round(s.last_pair.dose, 2),
                "yield": round(s.last_pair.yield_g, 2),
                "paired_at": dt_util.utc_from_timestamp(
                    s.last_pair.yield_at
                ).isoformat(),
            }
            if s.last_pair
            else {}
        ),
    ),
    # ── the refractometer's verdict on the last measured brew ─────────────────
    # DIAGNOSTIC alongside the rest of the last shot's working parts.  Both read the
    # stored measurement rather than the R2's own sensors, which are unavailable
    # whenever it is switched off — which is nearly always, it being a handheld.
    DifluidBrewSensorDescription(
        key="last_tds",
        name="Last TDS",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:water-percent",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_measurement.tds if s.last_measurement else None,
        attrs_fn=lambda s: (
            {
                "measured_at": dt_util.utc_from_timestamp(
                    s.last_measurement.measured_at
                ).isoformat(),
                "brew_at": dt_util.utc_from_timestamp(s.last_measurement.at).isoformat(),
            }
            if s.last_measurement
            else {}
        ),
    ),
    # Extraction yield: the share of the ground coffee that ended up dissolved in the
    # cup.  TDS x yield / dose, which is what the DiFluid app's ratio diagonals are
    # drawn for — on that chart a 1:2 line is TDS = EXT / 2.
    DifluidBrewSensorDescription(
        key="last_extraction",
        name="Extraction",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:coffee-to-go-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.last_measurement.ext if s.last_measurement else None,
        attrs_fn=lambda s: (
            {
                "dose": s.last_measurement.dose,
                "yield": s.last_measurement.yield_g,
                "tds": s.last_measurement.tds,
                "ratio": round(s.last_measurement.ratio, 2),
                # Null, not 0, when the pour's start was never observed.  See
                # BrewPair.pour_seconds.
                "seconds": s.last_measurement.seconds,
                "brew_at": dt_util.utc_from_timestamp(
                    s.last_measurement.at
                ).isoformat(),
                "measured_at": dt_util.utc_from_timestamp(
                    s.last_measurement.measured_at
                ).isoformat(),
                "measured_brews": len(s.measurements),
                # The series the control chart plots, oldest first, as
                # [brew time, EXT %, TDS %, ratio, dose g, yield g, pour s, read at].
                # Positional rather than named to keep it small: this rides along on
                # every state change, and a list of dicts would be several times the
                # size for no more information.
                #
                # Everything after the ratio is what the chart shows *about* a point
                # rather than where it sits, and it is per-point rather than read off
                # the top-level attributes because the card has to be able to walk back
                # through the history and describe an older brew, not only this one.
                #
                # The two times are both here because they are not the same time and
                # the distance between them is the point.  Every reading attaches to
                # the most recent brew with no window at all — deliberately, since
                # measuring is something you get round to — so a reading taken on a
                # morning when the scale was off the air lands on yesterday's cup and
                # is arithmetically correct about the wrong coffee.  That happened on
                # 2026-08-26: a shot read at 10:12 was divided by an 08:44 brew.
                # Nothing in the numbers shows it; the clock does.
                #
                # An attribute rather than a service or a websocket command because it
                # is small, it is already flowing to every dashboard, and it needs no
                # round trip when the card first renders.
                "points": [
                    [
                        round(m.at, 1), m.ext, m.tds, round(m.ratio, 2),
                        m.dose, m.yield_g, m.seconds, round(m.measured_at, 1),
                    ]
                    for m in s.measurements[-CHART_POINTS:]
                ],
            }
            if s.last_measurement
            else {}
        ),
    ),
    # ── statistics ────────────────────────────────────────────────────────────
    # An odometer, a trip meter and a daily rate for each of two quantities: cups
    # drunk and coffee ground.
    #
    # No entity_category on any of the six, deliberately.  1.4.0-beta.13 marked them
    # DIAGNOSTIC to keep them out of the Sensors card, which on the scale's device page
    # was the only way to stop them crowding out the weight and the flow rate — that
    # page renders exactly four cards and picks one from domain plus entity_category,
    # with no "statistics" card to ask for.  They are not on the scale's page any more.
    # On the detector's own page nothing competes with them, so they belong in Sensors
    # for the plain reason that they are what it reads.
    #
    # Brew Count is the cups odometer and is not duplicated: "cups, all time" is
    # already this number, and a second sensor reporting it would be a second answer
    # to a question that must have one.  It stays exactly as it was, including its
    # entity_id — the Prometheus rules and the email read it.
    DifluidBrewSensorDescription(
        key="brew_count",
        name="Brew Count",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda s: s.brew_count,
        attrs_fn=lambda s: {
            # The average dose over the whole odometer, which is only meaningful once
            # both counters have seen the same brews — see Coffee Ground below for why
            # they start out of step.
            "average_dose": (
                round(s.totals.total_dose_g / s.brew_count, 2)
                if s.brew_count and s.totals.total_dose_g
                else None
            ),
        },
    ),
    DifluidBrewSensorDescription(
        key="brew_count_period",
        name="Brew Count (Period)",
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:counter",
        value_fn=lambda s: s.totals.period_brews(s.brew_count),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    DifluidBrewSensorDescription(
        key="brews_per_day",
        name="Brews per Day",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="cups/d",
        suggested_display_precision=2,
        icon="mdi:chart-line",
        ticks=True,
        value_fn=lambda s: s.totals.per_day(
            s.totals.period_brews(s.brew_count), dt_util.utcnow().timestamp()
        ),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    # Grams rather than kilograms as the native unit, because that is what the scale
    # reports and what every other weight here is in.  Home Assistant offers kg as a
    # display unit on a WEIGHT sensor, so the choice stays with whoever is reading it
    # once the numbers get big.
    DifluidBrewSensorDescription(
        key="coffee_total",
        name="Coffee Ground",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        icon="mdi:coffee-maker",
        value_fn=lambda s: s.totals.total_dose_g,
        # counting_since exists because this odometer and Brew Count do not start
        # level: the count has been running since the counter was added, while nobody
        # was summing doses until this version, so the first reading here is 0 against
        # a count of 29.  The alternative — seeding it with 29 x the current dose —
        # would put a number nobody weighed into an odometer whose only job is to hold
        # numbers that were weighed.  The attribute says when the count began, so the
        # gap can be read rather than guessed at.
        attrs_fn=lambda s: {
            "counting_since": _iso(s.totals.period_started)
            if not s.totals.total_dose_g
            else None,
        },
    ),
    DifluidBrewSensorDescription(
        key="coffee_period",
        name="Coffee Ground (Period)",
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        icon="mdi:coffee-maker-outline",
        value_fn=lambda s: s.totals.period_dose_g(),
        attrs_fn=lambda s: _period_attrs(s),
    ),
    DifluidBrewSensorDescription(
        key="coffee_per_day",
        name="Coffee per Day",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="g/d",
        suggested_display_precision=1,
        icon="mdi:chart-line",
        ticks=True,
        value_fn=lambda s: s.totals.per_day(
            s.totals.period_dose_g(), dt_util.utcnow().timestamp()
        ),
        attrs_fn=lambda s: _period_attrs(s),
    ),
)


# ── R2 sensors ────────────────────────────────────────────────────────────────

R2_SENSORS: tuple[DifluidSensorDescription, ...] = (
    DifluidSensorDescription(
        key="concentration",
        name="Concentration (TDS)",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=2,
        icon="mdi:water-percent",
        value_fn=lambda d: d.concentration,
    ),
    DifluidSensorDescription(
        key="refractive_index",
        name="Refractive Index",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=5,
        icon="mdi:eye-outline",
        value_fn=lambda d: d.refractive_index,
    ),
    DifluidSensorDescription(
        key="prism_temperature",
        name="Prism Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.prism_temperature,
    ),
    DifluidSensorDescription(
        key="sample_temperature",
        name="Sample Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        value_fn=lambda d: d.sample_temperature,
    ),
    DifluidSensorDescription(
        key="test_status",
        name="Test Status",
        icon="mdi:flask-outline",
        value_fn=lambda d: d.test_status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    held = hass.data[DOMAIN][entry.entry_id]
    device_type = entry.data.get(CONF_DEVICE_TYPE)

    if device_type == DEVICE_TYPE_DETECTOR:
        session: BrewSession = held
        prefix = entry.data.get(CONF_UID_PREFIX) or entry.entry_id
        device_info = detector_device_info(entry)
        async_add_entities(
            (DifluidBrewRateSensor if desc.ticks else DifluidBrewSensor)(
                session, desc, prefix, device_info
            )
            for desc in BREW_SENSORS
        )
        return

    if device_type == DEVICE_TYPE_R2:
        async_add_entities(
            DifluidR2Sensor(held, desc, entry) for desc in R2_SENSORS
        )
        return

    entities: list = [
        DifluidMicrobalanceSensor(held, desc, entry)
        for desc in MICROBALANCE_SENSORS
    ]

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Difluid",
        model="Microbalance Ti" if entry.data.get(CONF_IS_TI) else "Microbalance",
    )
    entities.append(await DifluidVersionSensor.async_create(hass, entry, device_info))
    async_add_entities(entities)


# ── Microbalance entity ───────────────────────────────────────────────────────

class DifluidMicrobalanceSensor(
    CoordinatorEntity[DifluidMicrobalanceCoordinator], SensorEntity
):
    entity_description: DifluidSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DifluidMicrobalanceCoordinator,
        description: DifluidSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="Microbalance Ti" if entry.data.get(CONF_IS_TI) else "Microbalance",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def icon(self):
        icon_fn = self.entity_description.icon_fn
        if icon_fn is not None and self.coordinator.data is not None:
            dynamic = icon_fn(self.coordinator.data)
            if dynamic is not None:
                return dynamic
            return None  # fall back to device_class default (battery level icon)
        return self.entity_description.icon

    @property
    def available(self) -> bool:
        client = self.coordinator._client
        return client is not None and client.is_connected


# ── brew detector entities ────────────────────────────────────────────────────

class DifluidBrewSensor(SensorEntity):
    """A detected dose / pour / ratio.

    Deliberately not a CoordinatorEntity: it must not go unavailable when the BLE
    link drops.  State is restored from BrewSession's Store, which loads before
    platform setup, so RestoreEntity would be redundant.
    """

    entity_description: DifluidBrewSensorDescription
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        session: BrewSession,
        description: DifluidBrewSensorDescription,
        uid_prefix: str,
        device_info: DeviceInfo,
    ) -> None:
        self._session = session
        self.entity_description = description
        # The prefix is the detector entry's CONF_UID_PREFIX, not its entry_id — see
        # const.py.  On an install that predates the detector entry it is the scale's
        # entry_id, which is what lets these entities keep the registry rows, and the
        # entity_ids and history, they had when the scale owned them.
        self._attr_unique_id = f"{uid_prefix}_{description.key}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        self._session.add_listener(self._handle_update)

    async def async_will_remove_from_hass(self) -> None:
        self._session.remove_listener(self._handle_update)

    def _handle_update(self) -> None:
        # Always invoked from the event loop (BLE notification -> BrewSession).
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        # The last shot stays meaningful whether or not the scale is switched on.
        return True

    @property
    def native_value(self):
        return self.entity_description.value_fn(self._session)

    @property
    def extra_state_attributes(self) -> dict:
        attrs_fn = self.entity_description.attrs_fn
        return attrs_fn(self._session) if attrs_fn else {}


class DifluidBrewRateSensor(DifluidBrewSensor):
    """A per-day average, which goes stale without anyone touching the scale.

    Every other sensor here changes only when the session does, so a listener is all
    they need.  These two divide by the age of the period, and that age grows whether
    or not coffee is being made — left to the session's own updates, the figure shown
    on a quiet Sunday would be the one computed at the last brew on Friday.

    An hour is the coarsest tick that never looks wrong: the denominator is in days, so
    an hour moves a daily average by at most ~4% of itself, and it costs 24 recorder
    rows a day instead of the 8,640 a five-minute tick would.  The timer is registered
    through async_on_remove, so it is torn down with the entity — which matters because
    BrewSession deliberately survives entry reloads and would otherwise accumulate one
    live timer per reload, every options change adding another.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._tick, timedelta(hours=1))
        )

    @callback
    def _tick(self, _now) -> None:
        self.async_write_ha_state()


class DifluidVersionSensor(SensorEntity):
    """Which build of the integration is actually loaded.

    Exists to answer that question directly while iterating on pre-release builds,
    instead of guessing from behaviour after a HACS redownload.
    """

    _attr_has_entity_name = True
    _attr_name = "Integration Version"
    _attr_icon = "mdi:package-variant"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, version: str, entry: ConfigEntry, device_info: DeviceInfo):
        self._attr_native_value = version
        self._attr_unique_id = f"{entry.entry_id}_integration_version"
        self._attr_device_info = device_info

    @classmethod
    async def async_create(
        cls, hass: HomeAssistant, entry: ConfigEntry, device_info: DeviceInfo
    ) -> "DifluidVersionSensor":
        version = "unknown"
        try:
            from homeassistant.loader import async_get_integration

            integration = await async_get_integration(hass, DOMAIN)
            version = str(integration.version or "unknown")
        except Exception:  # noqa: BLE001 - diagnostic only, never block setup
            pass
        return cls(version, entry, device_info)

    @property
    def available(self) -> bool:
        return True


# ── R2 entity ─────────────────────────────────────────────────────────────────

class DifluidR2Sensor(CoordinatorEntity[DifluidR2Coordinator], SensorEntity):
    entity_description: DifluidSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DifluidR2Coordinator,
        description: DifluidSensorDescription,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Difluid",
            model="R2 Extract",
        )

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None and self.coordinator.data.authenticated
