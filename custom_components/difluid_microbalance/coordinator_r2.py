from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import aiohttp
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from typing import Callable

from .const import DOMAIN, DEFAULT_MODEL_R2, R2_API_URL, R2_DEFAULT_LICENSE_KEY, R2_STATUS_MAP

_LOGGER = logging.getLogger(__name__)

_HEADER = bytes([0xDF, 0xDF])
_R2_SERVICE_UUID = "000000ff-0000-1000-8000-00805f9b34fb"
_DIRECT_PROBE_TIMEOUT = 3.0

#: How often to look for a device that is not in the air, and how fast to retry one
#: that is.
#:
#: The R2 is a handheld that spends most of its life switched off, so most of the time
#: "not connected" means "not there" and there is nothing to retry.  Pacing by presence
#: rather than by failure count keeps the radio quiet while it is off and still notices
#: within about five seconds when it is switched on.  Same shape as the scale's loop,
#: which learned this after a two-minute backoff hid a brew.
_ABSENT_POLL_SECONDS = 15.0
_PRESENT_RETRY_SECONDS = 5.0
_PRESENT_RETRY_MAX_SECONDS = 120.0


def _next_retry_delay(present: bool, delay: float) -> float:
    """Next wait, given whether the device is in range right now."""
    if not present:
        return _ABSENT_POLL_SECONDS
    return min(_PRESENT_RETRY_MAX_SECONDS, delay * 2)

def _build_cmd(func: int, cmd: int, data: bytes = b"") -> bytes:
    frame = bytes([func, cmd, len(data)]) + data
    full = _HEADER + frame
    return full + bytes([sum(full) & 0xFF])

_CMD_GET_FIRMWARE = _build_cmd(0x00, 0x02)

_R2_TEST_COMMANDS = frozenset({0x00, 0x01, 0x02, 0x03})

_R2_FUNC_FIRMWARE = 0x00
_R2_FUNC_SETTINGS = 0x01
_R2_FUNC_ACTION = 0x03
_R2_CMD_FIRMWARE = 0x02
_R2_CMD_CALIBRATE = 0x02
_R2_PKG_STATUS = 0x00
_R2_PKG_TEMPERATURES = 0x01
_R2_PKG_RESULT = 0x02

_Fields = dict


def _decode_frame(raw: bytes) -> Optional[tuple[int, int, bytes]]:
    """Split a DF DF frame into (func, cmd, payload), or None if it is not one.

    `data_len` comes off the wire, so the frame is only as trustworthy as that byte:
    the length check is what stops a truncated packet reaching the parsers at all.
    Each parser re-checks the payload it needs, so this is the outer of two guards
    rather than the only one.
    """
    if len(raw) < 6 or raw[0] != 0xDF or raw[1] != 0xDF:
        return None
    func, cmd, data_len = raw[2], raw[3], raw[4]
    if len(raw) < 5 + data_len + 1:
        return None
    return func, cmd, bytes(raw[5 : 5 + data_len])


def _parse_status(payload: bytes, _cmd: int) -> Optional[_Fields]:
    if len(payload) < 2:
        return None
    return {"test_status": R2_STATUS_MAP.get(payload[1], "Unknown")}


def _parse_temperatures(payload: bytes, _cmd: int) -> Optional[_Fields]:
    if len(payload) < 6:
        return None
    prism_raw = int.from_bytes(payload[1:3], "big", signed=True)
    sample_raw = int.from_bytes(payload[3:5], "big", signed=True)
    fahrenheit = payload[5] == 1
    return {
        "temperature_unit": "°F" if fahrenheit else "°C",
        "prism_temperature": _to_celsius(prism_raw / 10.0, fahrenheit),
        "sample_temperature": _to_celsius(sample_raw / 10.0, fahrenheit),
    }


def _parse_result(payload: bytes, cmd: int) -> Optional[_Fields]:
    if cmd == _R2_CMD_CALIBRATE or len(payload) < 7:
        return None
    conc_raw = int.from_bytes(payload[1:3], "big", signed=True)
    ri_raw = int.from_bytes(payload[3:7], "big", signed=True)
    return {
        "concentration": conc_raw / 100.0,
        "refractive_index": ri_raw / 100000.0,
    }


_CMD_GET_FIRMWARE = _build_cmd(_R2_FUNC_FIRMWARE, _R2_CMD_FIRMWARE)

_R2_PACKAGE_PARSERS = {
    _R2_PKG_STATUS: _parse_status,
    _R2_PKG_TEMPERATURES: _parse_temperatures,
    _R2_PKG_RESULT: _parse_result,
}


def _parse_action(cmd: int, payload: bytes) -> Optional[_Fields]:
    """Parse one Device Action response: action command, then package number.

    `_R2_TEST_COMMANDS` carries 0x03, which protocolR2.md does not document.  A test
    started on the device itself answers on it whenever the prism is not yet at the
    sample's temperature: the R2 re-tests in a loop until it settles, and reports
    statuses 7/8/9 over that command.  Without it, every packet of such a test was
    dropped and the reading only ever reached the device's own screen.

    Bounded rather than open-ended because 0xFE and 0xFF are error codes in the same
    function, and an error is not a reading.
    """
    if cmd not in _R2_TEST_COMMANDS or not payload:
        return None
    parser = _R2_PACKAGE_PARSERS.get(payload[0])
    return parser(payload, cmd) if parser else None


def _parse_settings(cmd: int, payload: bytes) -> Optional[_Fields]:
    if cmd != 0x00 or not payload:
        return None
    return {"temperature_unit": "°F" if payload[0] == 1 else "°C"}


_R2_FUNC_PARSERS = {
    _R2_FUNC_ACTION: _parse_action,
    _R2_FUNC_SETTINGS: _parse_settings,
}

def _to_celsius(value: float, fahrenheit: bool) -> float:
    """The R2's temperature in Celsius, whichever unit it reported it in.

    Rounded to a tenth because that is the resolution the device sends (the raw field
    is degrees x 10); without it a converted Fahrenheit reading arrives as
    26.166666666666668 and the sensor's own display precision is the only thing
    hiding it.
    """
    return round((value - 32.0) * 5.0 / 9.0, 1) if fahrenheit else value

_SECTION_FIELDS = frozenset(
    {"concentration", "refractive_index", "prism_temperature", "sample_temperature",
     "temperature_unit", "test_status"}
)


@dataclass
class R2Data:
    concentration: float = 0.0
    refractive_index: float = 0.0
    prism_temperature: float = 0.0
    sample_temperature: float = 0.0
    temperature_unit: str = "°C"
    test_status: str = "Unknown"
    authenticated: bool = False

class DifluidR2Coordinator(DataUpdateCoordinator[R2Data]):

    def __init__(
        self, hass: HomeAssistant, address: str, license_key: str
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_r2_{address}", update_interval=None)
        self.address = address
        self.license_key = license_key or R2_DEFAULT_LICENSE_KEY
        self._client: Optional[BleakClientWithServiceCache] = None
        self._uuid_encrypted: Optional[str] = None
        self._uuid_cleartext: Optional[str] = None
        self._auth_response: Optional[asyncio.Future] = None
        self._direct_probe: Optional[asyncio.Future] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._shutdown_task: Optional[asyncio.Task] = None
        self._bt_cancel: Optional[Callable] = None
        self._no_reconnect_until: float = 0.0
        self._auto_shutdown_minutes: int = 0
        self._last_activity_time: float = 0.0
        self._sn: str = ""
        self._mac: str = ""
        self.data = R2Data()

    async def async_start(self) -> None:
        self._bt_cancel = bluetooth.async_register_callback(
            self.hass,
            self._on_bt_advertisement,
            BluetoothCallbackMatcher(address=self.address),
            BluetoothScanningMode.PASSIVE,
        )
        try:
            await self._do_connect()
        except Exception as err:
            _LOGGER.info(
                "R2 %s not available at startup (%s) — will keep looking",
                self.address, err,
            )
            self._ensure_reconnect_loop()

    async def async_stop(self) -> None:
        if self._bt_cancel:
            self._bt_cancel()
            self._bt_cancel = None
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._shutdown_task and not self._shutdown_task.done():
            self._shutdown_task.cancel()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    def set_auto_shutdown_minutes(self, minutes: int) -> None:
        self._auto_shutdown_minutes = max(0, minutes)

    async def async_power_off(self) -> None:
        """Disconnect BLE and suppress reconnect for 60 s."""
        import time as _time
        self._no_reconnect_until = _time.monotonic() + 60.0
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._shutdown_task and not self._shutdown_task.done():
            self._shutdown_task.cancel()
        client = self._client
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                pass
        _LOGGER.info("R2 power-off: BLE disconnected, reconnect suppressed for 60 s")

    async def async_send_command(self, cmd: bytes) -> None:
        """Send a DF DF command to the device."""
        client = self._client
        if not client or not client.is_connected:
            raise RuntimeError("R2 not connected")
        char_uuid = self._uuid_cleartext or self._uuid_encrypted
        if char_uuid:
            await client.write_gatt_char(char_uuid, cmd, response=False)

    async def _auto_shutdown_loop(self) -> None:
        """Disconnect after _auto_shutdown_minutes of inactivity (no test results)."""
        import time as _time
        while True:
            await asyncio.sleep(30)
            if self._auto_shutdown_minutes <= 0:
                continue
            client = self._client
            if not client or not client.is_connected:
                return
            idle_seconds = _time.monotonic() - self._last_activity_time
            if idle_seconds >= self._auto_shutdown_minutes * 60:
                _LOGGER.info("R2 auto-shutdown: %.0f min idle, disconnecting", idle_seconds / 60)
                await self.async_power_off()
                return

    @callback
    def _on_bt_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        import time as _time
        if _time.monotonic() < self._no_reconnect_until:
            return
        if self._client and self._client.is_connected:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        _LOGGER.info("R2 %s detected in BLE scan, connecting…", self.address)
        self._reconnect_task = self.hass.async_create_background_task(
            self._connect_once(), name="difluid_r2_connect_once"
        )

    async def _connect_once(self) -> None:
        import time as _time
        if _time.monotonic() < self._no_reconnect_until:
            return
        try:
            await self._do_connect()
        except Exception as err:
            _LOGGER.debug(
                "BT-triggered R2 connection to %s failed: %s; resuming retry loop",
                self.address, err,
            )
            self._ensure_reconnect_loop()

    def _ensure_reconnect_loop(self) -> None:
        """Start the reconnect loop unless one is already running."""
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_loop(), name="difluid_r2_reconnect_loop"
        )

    async def _do_connect(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise RuntimeError(f"BLE device {self.address} not found")

        client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self.address,
            disconnected_callback=self._on_disconnect,
        )
        _LOGGER.info("Connected to Difluid R2 %s", self.address)

        for svc in client.services:
            _LOGGER.info("  Service: %s", svc.uuid)
            for char in svc.characteristics:
                _LOGGER.info("    Characteristic: %s  props=%s", char.uuid, char.properties)

        r2_chars = [
            c
            for svc in client.services
            for c in svc.characteristics
            if svc.uuid.lower() == _R2_SERVICE_UUID
        ]
        self._uuid_encrypted = next(
            (c.uuid.lower() for c in r2_chars if "ff01" in c.uuid.lower()), None
        )
        self._uuid_cleartext = next(
            (c.uuid.lower() for c in r2_chars if "aa01" in c.uuid.lower()), None
        )

        if not self._uuid_cleartext or not self._uuid_encrypted:
            await client.disconnect()
            raise RuntimeError(
                f"R2 BLE channels not found in service {_R2_SERVICE_UUID}. "
                f"Found: {[c.uuid for c in r2_chars]}"
            )

        _LOGGER.info(
            "R2 channels — encrypted: %s  cleartext: %s",
            self._uuid_encrypted, self._uuid_cleartext,
        )

        self._client = client

        await client.start_notify(self._uuid_cleartext, self._on_data_notification)

        loop = asyncio.get_event_loop()
        self._direct_probe = loop.create_future()
        await client.write_gatt_char(self._uuid_cleartext, _CMD_GET_FIRMWARE, response=False)
        _LOGGER.info("R2: probing cleartext channel (aa01) directly…")

        try:
            await asyncio.wait_for(asyncio.shield(self._direct_probe), timeout=_DIRECT_PROBE_TIMEOUT)
            _LOGGER.info("R2: cleartext channel works without handshake")
            self.data.authenticated = True
            self.async_set_updated_data(self.data)
            import time as _time
            self._last_activity_time = _time.monotonic()
            if self._shutdown_task and not self._shutdown_task.done():
                self._shutdown_task.cancel()
            self._shutdown_task = self.hass.async_create_background_task(
                self._auto_shutdown_loop(), name="difluid_r2_auto_shutdown"
            )
        except asyncio.TimeoutError:
            _LOGGER.info(
                "R2: no response on aa01 in %.0fs; trying cloud handshake", _DIRECT_PROBE_TIMEOUT
            )
            await self._authenticate(client)
        finally:
            self._direct_probe = None

    async def _authenticate(self, client: BleakClientWithServiceCache) -> None:
        """3-step cloud handshake to unlock the R2 cleartext channel."""
        headers = {"Content-Type": "application/json", "license": self.license_key}

        await client.start_notify(self._uuid_encrypted, self._on_auth_notification)

        try:
            async with aiohttp.ClientSession() as session:
                cmd1 = await self._srv_cmd_request(session, headers, "cmd1")
                resp1 = await self._write_and_wait(client, bytes.fromhex(cmd1))

                result1 = await self._srv_dev_respond(session, headers, resp1.hex())
                self._sn = result1.get("sn", "")
                self._mac = result1.get("mac", "")

                cmd2 = await self._srv_cmd_request(
                    session, headers, "cmd2", {"sn": self._sn, "mac": self._mac}
                )
                resp2 = await self._write_and_wait(client, bytes.fromhex(cmd2))

                result2 = await self._srv_dev_respond(
                    session, headers, resp2.hex(), sn=self._sn, mac=self._mac
                )
                cmd3 = result2.get("instructContent", "")
                resp3 = await self._write_and_wait(client, bytes.fromhex(cmd3))

                await self._srv_dev_respond(
                    session, headers, resp3.hex(), sn=self._sn, mac=self._mac
                )
                enable_cmd = await self._srv_cmd_request(
                    session, headers, "enableCleartext", {"sn": self._sn, "mac": self._mac}
                )
                await self._write_and_wait(client, bytes.fromhex(enable_cmd), wait=False)

        except Exception as err:
            await client.stop_notify(self._uuid_encrypted)
            raise RuntimeError(f"R2 cloud handshake failed: {err}") from err

        await client.stop_notify(self._uuid_encrypted)
        self.data.authenticated = True
        self.async_set_updated_data(self.data)
        _LOGGER.info("R2 %s: handshake complete (SN=%s)", self.address, self._sn)

        await client.write_gatt_char(self._uuid_cleartext, _CMD_GET_FIRMWARE, response=False)

        import time as _time
        self._last_activity_time = _time.monotonic()
        if self._shutdown_task and not self._shutdown_task.done():
            self._shutdown_task.cancel()
        self._shutdown_task = self.hass.async_create_background_task(
            self._auto_shutdown_loop(), name="difluid_r2_auto_shutdown"
        )

    async def _srv_cmd_request(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        cmd_type: str,
        extra: dict | None = None,
    ) -> str:
        payload = {"model": DEFAULT_MODEL_R2, "type": cmd_type, **(extra or {})}
        async with session.post(
            f"{R2_API_URL}/sdk/cmdRequest", json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
            _LOGGER.debug("Server %s: code=%s msg=%s", cmd_type, body.get("code"), body.get("message"))
            if body.get("data") is None:
                raise RuntimeError(
                    f"Server error for {cmd_type}: code={body.get('code')}, {body.get('message')}"
                )
            return body["data"]

    async def _srv_dev_respond(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        content: str,
        sn: str = "",
        mac: str = "",
    ) -> dict:
        payload = {"model": DEFAULT_MODEL_R2, "content": content, "sn": sn, "mac": mac}
        async with session.post(
            f"{R2_API_URL}/sdk/devRespond", json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
            if body.get("data") is None:
                raise RuntimeError(
                    f"devRespond returned null: code={body.get('code')}, {body.get('message')}"
                )
            return body["data"]

    async def _write_and_wait(
        self,
        client: BleakClientWithServiceCache,
        cmd: bytes,
        wait: bool = True,
        timeout: float = 10.0,
    ) -> bytes:
        if wait:
            loop = asyncio.get_event_loop()
            self._auth_response = loop.create_future()

        await client.write_gatt_char(self._uuid_encrypted, cmd, response=False)

        if not wait:
            return b""

        return await asyncio.wait_for(self._auth_response, timeout=timeout)

    def _on_auth_notification(self, _sender: Any, raw: bytearray) -> None:
        if self._auth_response and not self._auth_response.done():
            self._auth_response.set_result(bytes(raw))

    def _on_data_notification(self, _sender: Any, raw: bytearray) -> None:
        _LOGGER.info("R2 notification: %s", bytes(raw).hex())

        if len(raw) >= 2 and raw[0] == 0xDA and raw[1] == 0xDA:
            _LOGGER.debug("R2: encrypted DA DA packet on aa01 — handshake required")
            return

        if self._direct_probe and not self._direct_probe.done():
            if len(raw) >= 2 and raw[0] == 0xDF and raw[1] == 0xDF:
                self._direct_probe.set_result(bytes(raw))

        frame = _decode_frame(bytes(raw))
        if frame is None:
            _LOGGER.warning("R2: unexpected packet on aa01: %s", bytes(raw).hex())
            return

        func, cmd, payload = frame
        if func == _R2_FUNC_FIRMWARE and cmd == _R2_CMD_FIRMWARE:
            _LOGGER.info(
                "R2 firmware version: %s",
                payload.decode("ascii", errors="replace").rstrip("\x00"),
            )
            return

        parser = _R2_FUNC_PARSERS.get(func)
        fields = parser(cmd, payload) if parser else None
        if not fields:
            return

        for name, value in fields.items():
            if name not in _SECTION_FIELDS:
                raise KeyError(f"unknown R2 field {name!r}")
            setattr(self.data, name, value)
        import time as _time
        self._last_activity_time = _time.monotonic()
        self.async_set_updated_data(self.data)

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        import time as _time
        self.data.authenticated = False
        self.async_set_updated_data(self.data)
        if _time.monotonic() < self._no_reconnect_until:
            _LOGGER.info("Difluid R2 %s disconnected (power-off cooldown, won't reconnect for 60 s)", self.address)
            return
        _LOGGER.warning("Difluid R2 %s disconnected, will retry", self.address)
        self._ensure_reconnect_loop()

    async def _reconnect_loop(self) -> None:
        delay = _PRESENT_RETRY_SECONDS
        while True:
            await asyncio.sleep(delay)
            remaining = self._no_reconnect_until - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining + 1)
                delay = _PRESENT_RETRY_SECONDS
                continue
            present = (
                bluetooth.async_ble_device_from_address(
                    self.hass, self.address, connectable=True
                )
                is not None
            )
            try:
                if present:
                    await self._do_connect()
                    _LOGGER.info("Reconnected to Difluid R2 %s", self.address)
                    return
            except Exception as err:
                _LOGGER.warning(
                    "R2 %s is in range but the connection failed: %s", self.address, err
                )
            delay = _next_retry_delay(present, delay)

    async def _async_update_data(self) -> R2Data:
        return self.data
