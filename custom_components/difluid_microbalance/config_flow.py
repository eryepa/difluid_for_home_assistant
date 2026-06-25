from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import (
    CONF_DEVICE_TYPE,
    CONF_IS_TI,
    CONF_LICENSE_KEY,
    CONF_MODEL,
    DEVICE_TYPE_MICROBALANCE,
    DEVICE_TYPE_R2,
    DOMAIN,
    SERVICE_UUID_MICROBALANCE,
    SERVICE_UUID_MICROBALANCE_TI,
    SERVICE_UUID_R2,
)

_ALL_SERVICE_UUIDS = {
    SERVICE_UUID_MICROBALANCE,
    SERVICE_UUID_MICROBALANCE_TI,
    SERVICE_UUID_R2,
}


def _device_type(service_uuids: list[str]) -> str | None:
    lower = {u.lower() for u in service_uuids}
    if SERVICE_UUID_R2 in lower:
        return DEVICE_TYPE_R2
    if SERVICE_UUID_MICROBALANCE in lower or SERVICE_UUID_MICROBALANCE_TI in lower:
        return DEVICE_TYPE_MICROBALANCE
    return None


class DifluidMicrobalanceConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        dtype = _device_type(discovery_info.service_uuids)
        if dtype is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        if dtype == DEVICE_TYPE_R2:
            return await self.async_step_r2_license()
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery_info is not None
        info = self._discovery_info

        if user_input is not None:
            lower = {u.lower() for u in info.service_uuids}
            is_ti = SERVICE_UUID_MICROBALANCE_TI in lower
            return self.async_create_entry(
                title=info.name or f"Difluid Microbalance ({info.address})",
                data={
                    CONF_ADDRESS: info.address,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE,
                    CONF_IS_TI: is_ti,
                    CONF_LICENSE_KEY: user_input.get(CONF_LICENSE_KEY, "").strip(),
                    CONF_MODEL: user_input.get(CONF_MODEL, "").strip(),
                },
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_LICENSE_KEY, default=""): str,
                    vol.Optional(CONF_MODEL, default=""): str,
                }
            ),
            description_placeholders={"name": info.name or info.address},
        )

    async def async_step_r2_license(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery_info is not None
        info = self._discovery_info
        errors: dict[str, str] = {}

        if user_input is not None:
            return self.async_create_entry(
                title=info.name or f"Difluid R2 ({info.address})",
                data={
                    CONF_ADDRESS: info.address,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_R2,
                    CONF_LICENSE_KEY: user_input.get(CONF_LICENSE_KEY, "").strip(),
                },
            )

        return self.async_show_form(
            step_id="r2_license",
            data_schema=vol.Schema({vol.Optional(CONF_LICENSE_KEY, default=""): str}),
            description_placeholders={"name": info.name or info.address},
            errors=errors,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            raw_addr = user_input[CONF_ADDRESS]
            # "manual" is the sentinel value shown when discovered devices are listed
            # but the user chooses to type their own MAC instead.
            if raw_addr == "manual":
                return self.async_show_form(
                    step_id="user",
                    data_schema=vol.Schema(
                        {
                            vol.Required(CONF_ADDRESS): str,
                            vol.Required(CONF_DEVICE_TYPE, default=DEVICE_TYPE_MICROBALANCE): vol.In(
                                {
                                    DEVICE_TYPE_MICROBALANCE: "Microbalance / Microbalance Ti",
                                    DEVICE_TYPE_R2: "R2 Extract",
                                }
                            ),
                            vol.Optional(CONF_LICENSE_KEY, default=""): str,
                            vol.Optional(CONF_MODEL, default=""): str,
                        }
                    ),
                    errors={},
                )
            address = raw_addr.strip().upper()
            device_type_override = user_input.get(CONF_DEVICE_TYPE, DEVICE_TYPE_MICROBALANCE)
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()

            info = self._discovered_devices.get(address)
            if info is not None:
                dtype = _device_type(info.service_uuids) or device_type_override
                self._discovery_info = info
            else:
                # Manual MAC entry — trust the user-selected device type
                dtype = device_type_override

            if dtype == DEVICE_TYPE_R2:
                # Create a minimal discovery_info placeholder for r2_license step
                self._discovery_info = self._discovered_devices.get(address)
                if self._discovery_info is None:
                    # Build a minimal stand-in so r2_license step can read .address
                    class _FakeInfo:
                        def __init__(self, addr: str) -> None:
                            self.address = addr
                            self.name = f"Difluid R2 ({addr})"
                            self.service_uuids: list[str] = []
                    self._discovery_info = _FakeInfo(address)  # type: ignore[assignment]
                return await self.async_step_r2_license()

            is_ti = (
                SERVICE_UUID_MICROBALANCE_TI in {u.lower() for u in info.service_uuids}
                if info
                else False
            )
            return self.async_create_entry(
                title=f"Difluid Microbalance ({address})",
                data={
                    CONF_ADDRESS: address,
                    CONF_DEVICE_TYPE: DEVICE_TYPE_MICROBALANCE,
                    CONF_IS_TI: is_ti,
                    CONF_LICENSE_KEY: user_input.get(CONF_LICENSE_KEY, "").strip(),
                    CONF_MODEL: user_input.get(CONF_MODEL, "").strip(),
                },
            )

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address not in current and _device_type(info.service_uuids):
                self._discovered_devices[info.address] = info

        # DiFluid devices do not advertise Service UUIDs in their BLE packets, so
        # auto-discovery rarely works.  Always show the manual-MAC form; if any
        # devices happened to be found, offer them as a dropdown choice too.
        device_type_selector = vol.Required(
            CONF_DEVICE_TYPE, default=DEVICE_TYPE_MICROBALANCE
        )
        device_type_choices = {
            DEVICE_TYPE_MICROBALANCE: "Microbalance / Microbalance Ti",
            DEVICE_TYPE_R2: "R2 Extract",
        }
        if self._discovered_devices:
            choices = {
                addr: f"{d.name or 'Difluid Device'} ({addr})"
                for addr, d in self._discovered_devices.items()
            }
            # Add a "manual entry" sentinel so the user can still type a MAC
            choices["manual"] = "Enter MAC address manually…"
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS, default="manual"): vol.In(choices),
                    device_type_selector: vol.In(device_type_choices),
                    vol.Optional(CONF_LICENSE_KEY, default=""): str,
                    vol.Optional(CONF_MODEL, default=""): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): str,
                    device_type_selector: vol.In(device_type_choices),
                    vol.Optional(CONF_LICENSE_KEY, default=""): str,
                    vol.Optional(CONF_MODEL, default=""): str,
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
