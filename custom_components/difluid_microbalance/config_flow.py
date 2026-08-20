from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .brew_detect import TUNABLE_FIELDS, DetectorConfig
from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_LICENSE_KEY,
    CONF_MODEL,
    CONF_RECORD_DATASET,
    DEVICE_TYPE_MICROBALANCE,
    DEVICE_TYPE_R2,
    DOMAIN,
    SERVICE_UUID_MICROBALANCE,
    SERVICE_UUID_MICROBALANCE_TI,
    SERVICE_UUID_R2,
)


def _device_type(service_uuids: list[str]) -> str | None:
    lower = {u.lower() for u in service_uuids}
    if SERVICE_UUID_R2 in lower:
        return DEVICE_TYPE_R2
    if SERVICE_UUID_MICROBALANCE in lower or SERVICE_UUID_MICROBALANCE_TI in lower:
        return DEVICE_TYPE_MICROBALANCE
    return None


def _device_type_from_name(name: str) -> str | None:
    """Detect device type from BLE advertisement name.

    DiFluid devices never advertise service UUIDs, so name matching is the
    only reliable way to auto-discover them from the BLE scan list.
    """
    lower = (name or "").lower()
    # R2 advertises as "DiFluid R2 XXXXXX"
    if lower.startswith("difluid r2") or lower.startswith("r2"):
        return DEVICE_TYPE_R2
    # Microbalance advertises as "Microbalance XXXXXX" or "Microbalance Ti XXXXXX"
    if lower.startswith("microbalance"):
        return DEVICE_TYPE_MICROBALANCE
    return None


def _discover_type(info: BluetoothServiceInfoBleak) -> str | None:
    return _device_type(info.service_uuids) or _device_type_from_name(info.name or "")


class DifluidMicrobalanceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "DifluidOptionsFlow":
        return DifluidOptionsFlow()

    # ── triggered by HA Bluetooth integration (service UUID match) ────────────

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        dtype = _discover_type(discovery_info)
        if dtype is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery_info is not None
        info = self._discovery_info
        dtype = _discover_type(info) or DEVICE_TYPE_MICROBALANCE

        if user_input is not None:
            is_ti = SERVICE_UUID_MICROBALANCE_TI in {u.lower() for u in info.service_uuids}
            return self.async_create_entry(
                title=info.name or f"DiFluid ({info.address})",
                data={
                    CONF_ADDRESS: info.address,
                    CONF_DEVICE_TYPE: dtype,
                    CONF_IS_TI: is_ti,
                },
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema({}),
            description_placeholders={"name": info.name or info.address},
        )

    # ── user-initiated flow ────────────────────────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address not in current and _discover_type(info):
                self._discovered_devices[info.address] = info

        if not self._discovered_devices:
            # Nothing found in BLE scan — go straight to MAC entry form
            return await self.async_step_mac()

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            if address == "manual":
                return await self.async_step_mac()

            address = address.strip().upper()
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            info = self._discovered_devices.get(address)
            dtype = (_discover_type(info) if info else None) or DEVICE_TYPE_MICROBALANCE
            is_ti = (
                SERVICE_UUID_MICROBALANCE_TI in {u.lower() for u in info.service_uuids}
                if info else False
            )
            title = (info.name if info and info.name else None) or f"DiFluid ({address})"
            return self.async_create_entry(
                title=title,
                data={
                    CONF_ADDRESS: address,
                    CONF_DEVICE_TYPE: dtype,
                    CONF_IS_TI: is_ti,
                },
            )

        choices = {
            addr: f"{d.name or 'DiFluid'} ({addr})"
            for addr, d in self._discovered_devices.items()
        }
        choices["manual"] = "Enter MAC address manually…"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_ADDRESS, default="manual"): vol.In(choices)}
            ),
        )

    async def async_step_mac(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual MAC entry — shown when no devices are discovered or user selects 'manual'."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper()
            if len(address) != 17 or address.count(":") != 5:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                dtype = user_input[CONF_DEVICE_TYPE]
                return self.async_create_entry(
                    title=f"DiFluid {dtype.capitalize()} ({address})",
                    data={
                        CONF_ADDRESS: address,
                        CONF_DEVICE_TYPE: dtype,
                        CONF_IS_TI: False,
                    },
                )

        return self.async_show_form(
            step_id="mac",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    vol.Required(
                        CONF_DEVICE_TYPE, default=DEVICE_TYPE_MICROBALANCE
                    ): vol.In(
                        {
                            DEVICE_TYPE_MICROBALANCE: "Microbalance / Microbalance Ti",
                            DEVICE_TYPE_R2: "R2 Extract",
                        }
                    ),
                }
            ),
            errors=errors,
        )


# ── options: brew detector thresholds ─────────────────────────────────────────

#: Error surfaced when the entered values are individually in range but describe
#: a band that runs backwards (see _misordered_pairs).
ERROR_RANGE_ORDER = "invalid_range_order"

#: Accepted range for every tunable, as (min, max) inclusive.
#:
#: Without this the form took any float at all, and some of them are not merely
#: badly tuned but arithmetically impossible.  ``stable_seconds = 0`` is the worst
#: of them: brew_detect divides the held time by it on every single sample, so a
#: zero there raises ZeroDivisionError roughly five times a second for as long as
#: the scale stays connected, and BrewSession.feed swallows each one — a dead
#: detector, a flooded log, and nothing anywhere in the UI to say so.
#:
#: The bounds are deliberately much wider than any sensible brew setup.  This is a
#: guard against the nonsensical, not an opinion about how anyone should tune their
#: detector: masses cannot be negative, seconds and divisors must be positive, and a
#: fraction of a window is by definition between 0 and 1.  Mass ceilings are kept at
#: DetectorConfig.max_mass (500 g), above which the detector treats the reading as a
#: cup being lifted rather than as something being weighed, so a threshold beyond it
#: could never match anything.
_OPTION_RANGES: dict[str, tuple[float, float]] = {
    # Grams around the window median.  Must be > 0 or nothing is ever "within
    # tolerance" and no reading can stabilise; the Hampel prefilter also uses it as
    # its noise floor, where zero would restore the bug that split a bowl of oats.
    "stable_tol": (0.01, 50.0),
    # Divisor in `held / cfg.stable_seconds` — see the note above.
    "stable_seconds": (0.5, 60.0),
    # A share of the stability window, so 0-1 by construction.  The floor is not
    # zero because a fraction of zero makes every reading "stable" instantly,
    # including the middle of a pour.
    "stable_time_fraction": (0.05, 1.0),
    # A hold time, so any non-negative value is arithmetically fine; zero just means
    # a value seen for a single sample before the lift is accepted.
    "settle_min_seconds": (0.0, 60.0),
    "min_mass": (0.0, 500.0),
    "dose_min": (0.0, 500.0),
    "dose_max": (0.0, 500.0),
    "dose_min_hold_seconds": (0.0, 3600.0),
    "dose_min_rise_seconds": (0.0, 3600.0),
    "yield_min": (0.0, 500.0),
    "yield_max": (0.0, 500.0),
    # Nothing forces an upper bound here beyond keeping a typo from pairing a dose
    # with a pour from a different day; 24 h is the generous end of that.
    "pair_window_seconds": (1.0, 86400.0),
    # Ratios are yield/dose, so strictly positive.  1:50 is far past anything
    # drinkable and still leaves room for someone metering a very long brew.
    "ratio_min": (0.01, 50.0),
    "ratio_max": (0.01, 50.0),
    # How long a hole in the stream has to be before the detector discards state.
    # Must be > 0: at zero every sample looks like a discontinuity and the detector
    # resets itself before it can ever accumulate a window.
    "gap_reset_seconds": (0.5, 3600.0),
}

#: Bounds used for a tunable that brew_detect exposes but this table does not know
#: about yet.  Every field in DetectorConfig is a mass, a duration or a ratio, and
#: none of those is ever negative, so a non-negative floor is a safe default that
#: still keeps a new field from being validated as "anything at all" the moment it
#: is added to TUNABLE_FIELDS.
_FALLBACK_RANGE = (0.0, 86400.0)

#: Pairs the detector reads as a band, low first.  brew_detect compares against them
#: with `low <= v <= high` (classify) and `ratio_min <= ratio <= ratio_max`
#: (BrewPairer.offer), so a band entered backwards is not a bad tuning — it is a
#: band that nothing can ever fall into, and the detector goes quiet with no
#: complaint from anywhere.  Individual vol.Range checks cannot catch this because
#: each value on its own is perfectly legal.
_ORDERED_PAIRS: tuple[tuple[str, str], ...] = (
    ("dose_min", "dose_max"),
    ("yield_min", "yield_max"),
    ("ratio_min", "ratio_max"),
)


def _field_validator(key: str):
    """voluptuous validator for one tunable: coerce to float, then bounds-check."""
    low, high = _OPTION_RANGES.get(key, _FALLBACK_RANGE)
    return vol.All(vol.Coerce(float), vol.Range(min=low, max=high))


def _misordered_pairs(values: dict[str, Any]) -> list[tuple[str, str]]:
    """Return the bands in `values` whose low bound is above its high bound."""
    bad: list[tuple[str, str]] = []
    for low_key, high_key in _ORDERED_PAIRS:
        if low_key not in values or high_key not in values:
            continue
        try:
            if float(values[low_key]) > float(values[high_key]):
                bad.append((low_key, high_key))
        except (TypeError, ValueError):
            # Coercion already failed for this field; vol reports that itself.
            continue
    return bad


class DifluidOptionsFlow(OptionsFlow):
    """Tune the dose/pour detector without editing code or redeploying.

    Only exposed for the scale — the R2 entry has nothing to tune here.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if self.config_entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_R2:
            return self.async_abort(reason="no_options")

        errors: dict[str, str] = {}
        # What the form should show.  Normally the stored options; after a rejected
        # submission the rejected input instead, so the offending value is still on
        # screen to be corrected rather than silently reverting to the old one.
        current: dict[str, Any] = dict(self.config_entry.options)

        if user_input is not None:
            bad = _misordered_pairs(user_input)
            if not bad:
                return self.async_create_entry(title="", data=user_input)
            errors["base"] = ERROR_RANGE_ORDER
            # Also flag the individual fields, so the pair at fault is obvious on a
            # form with fourteen numbers on it.
            for low_key, high_key in bad:
                errors[low_key] = ERROR_RANGE_ORDER
                errors[high_key] = ERROR_RANGE_ORDER
            current = dict(user_input)

        defaults = DetectorConfig()
        schema: dict[Any, Any] = {}
        for key in TUNABLE_FIELDS:
            default = getattr(defaults, key, None)
            if default is None:
                # TUNABLE_FIELDS names something DetectorConfig does not have.  The
                # two are kept side by side precisely so they cannot drift, but a
                # half-applied edit there must not take the whole options form down
                # with an AttributeError — the form is the only way to undo a bad
                # threshold.
                continue
            schema[
                vol.Optional(key, default=current.get(key, default))
            ] = _field_validator(key)
        schema[
            vol.Optional(
                CONF_RECORD_DATASET, default=current.get(CONF_RECORD_DATASET, False)
            )
        ] = bool

        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )
