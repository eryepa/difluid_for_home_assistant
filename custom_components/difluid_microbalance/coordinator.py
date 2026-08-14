from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .brew_session import BrewSession

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
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

from .cloud_auth import DifluidCloudAuth
from .const import (
    CHARACTERISTIC_UUID_MICROBALANCE,
    CHARACTERISTIC_UUID_MICROBALANCE_TI,
    DEVICE_STATUS_MAP,
    DOMAIN,
    WEIGHT_UNITS,
)

_LOGGER = logging.getLogger(__name__)

_HEADER = bytes([0xDF, 0xDF])
_ENCRYPTED_HEADER = bytes([0xDA, 0xDA])

# Reconnect pacing.  Keyed on whether the scale is advertising, not on how many
# attempts have failed — see _reconnect_loop for why that distinction matters.
# The absent poll is an in-memory lookup against Home Assistant's own BLE table,
# so it costs nothing on air and can afford to be frequent.
_ABSENT_POLL_SECONDS = 15.0
_PRESENT_RETRY_SECONDS = 5.0
_PRESENT_RETRY_MAX_SECONDS = 30.0


def _build_cmd(func: int, cmd: int, data: bytes = b"") -> bytes:
    frame = bytes([func, cmd, len(data)]) + data
    full = _HEADER + frame
    return full + bytes([sum(full) & 0xFF])


_CMD_AUTO_SEND_ON   = _build_cmd(0x01, 0x00, bytes([0x01]))
_CMD_GET_STATUS     = _build_cmd(0x03, 0x05)
_CMD_TARE           = _build_cmd(0x03, 0x02, bytes([0x01]))  # Power Button Single Click
_CMD_POWER_OFF      = _build_cmd(0x03, 0x04, bytes([0x01]))  # Power Button Long Press
_CMD_GET_AUTO_DETECT = _build_cmd(0x01, 0x01)
_CMD_GET_AUTO_STOP   = _build_cmd(0x01, 0x02)

# Weight / flow / timer arrive as high-priority push notifications and are NOT
# affected by this interval.  This only controls how often we poll the secondary
# status packet (battery, charging, device_status) via GET_STATUS.
_STATUS_POLL_INTERVAL = 1


@dataclass
class MicrobalanceData:
    weight: float = 0.0
    weight_unit: str = "g"
    flow_rate: float = 0.0
    timer: int = 0
    battery: int = 0
    charging: bool = False
    device_status: str = "Unknown"
    auto_detect_timing: bool = False
    auto_stop_timing: bool = False
    connected: bool = False


class DifluidMicrobalanceCoordinator(DataUpdateCoordinator[MicrobalanceData]):

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        is_ti: bool = False,
        license_key: str = "",
        model: str = "",
        brew: Optional["BrewSession"] = None,
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{address}", update_interval=None)
        self.address = address
        self.is_ti = is_ti
        self.license_key = license_key
        self.model = model
        self.brew = brew
        self._preferred_char_uuid = (
            CHARACTERISTIC_UUID_MICROBALANCE_TI if is_ti else CHARACTERISTIC_UUID_MICROBALANCE
        )
        self._write_char_uuid: Optional[str] = None
        self._all_difluid_char_uuids: list[str] = []
        self._encrypted_uuid: Optional[str] = None
        self._cleartext_uuid: Optional[str] = None
        self._auth: Optional[DifluidCloudAuth] = None
        self._client: Optional[BleakClientWithServiceCache] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._bt_cancel: Optional[Callable] = None
        self._auto_shutdown_minutes: int = 0
        self._no_reconnect_until: float = 0.0  # monotonic timestamp; reconnect suppressed until then
        self._last_weight_change_time: float = 0.0
        self._last_weight_value: float = 0.0
        self.data = MicrobalanceData()

    # ── public API for button / select / number entities ─────────────────────

    async def async_send_command(self, cmd: bytes) -> None:
        """Send a control command to all known writable channels.

        Protocol docs specify FF01 as the write channel for non-Ti Microbalance, but
        sensor notifications arrive on AA01.  We write to both channels so the command
        reaches the device regardless of which one it actually listens on.
        """
        if not self._client or not self._client.is_connected:
            raise RuntimeError("Device not connected")
        sent = False
        for char_uuid in dict.fromkeys(filter(None, [
            self._write_char_uuid,
            self._encrypted_uuid,   # FF01 — documented write channel for non-Ti
            self._cleartext_uuid,   # AA01 — cleartext channel
        ])):
            try:
                await self._client.write_gatt_char(char_uuid, cmd, response=False)
                _LOGGER.debug("Command %s sent to %s", cmd.hex(), char_uuid)
                sent = True
            except Exception as err:
                _LOGGER.debug("Write to %s failed (ignored): %s", char_uuid, err)
        if not sent:
            raise RuntimeError("Device not connected or no writable characteristic found")

    async def async_power_off(self) -> None:
        """Disconnect BLE and suppress reconnect for 60 s.

        The scale has a hardware auto-off timer that fires when no BLE client
        is connected.  Dropping the connection and holding off reconnect for
        60 seconds gives the device time to power itself off.

        After the cooldown a _reconnect_loop is started so the integration
        reconnects when the scale is turned on again — even if the BT
        advertisement callback doesn't fire (device still in HA BT cache).
        """
        import time as _time
        self._no_reconnect_until = _time.monotonic() + 60.0

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        client = self._client
        if client and client.is_connected:
            try:
                await client.disconnect()
            except Exception:
                pass
        _LOGGER.info("Power-off: BLE disconnected, reconnect suppressed for 60 s")

        # After the cooldown, proactively start the reconnect loop so we
        # reconnect when the scale is turned on — the BT advertisement callback
        # may miss the first advertisement if the device is still in HA's cache.
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_loop_after_poweroff(), name="difluid_reconnect_after_poweroff"
        )

    async def _reconnect_loop_after_poweroff(self) -> None:
        """Wait for the power-off cooldown to expire, then start reconnecting."""
        import time as _time
        remaining = self._no_reconnect_until - _time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining + 1)  # +1 s buffer
        if self._client and self._client.is_connected:
            return  # BT callback already reconnected us
        _LOGGER.info("Power-off cooldown expired; starting reconnect loop")
        await self._reconnect_loop()

    def set_auto_shutdown_minutes(self, minutes: int) -> None:
        self._auto_shutdown_minutes = max(0, minutes)
        _LOGGER.debug("Auto-shutdown set to %d min", self._auto_shutdown_minutes)

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        # Register a BLE advertisement callback so we reconnect immediately when
        # the device turns on, instead of waiting for HA's ConfigEntry retry timer.
        #
        # PASSIVE on purpose.  Active scanning makes every peripheral answer a
        # SCAN_REQ with a SCAN_RSP, which drains the batteries of unrelated BLE
        # sensors across the house, and requesting it here forces the mode on every
        # proxy.  We gain nothing from it: DiFluid devices put their full name in
        # the advertisement itself (`12 09 "DiFluid R2 301055"`), and connecting is
        # by MAC.  A connection establishes fine from a passive scan.
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
                "Device %s not available at startup (%s) — will connect when seen in BLE scan",
                self.address, err,
            )
            # Don't raise — entities stay unavailable until the BT callback fires

    async def async_stop(self) -> None:
        if self._bt_cancel:
            self._bt_cancel()
            self._bt_cancel = None
        for task in (self._poll_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    # ── BLE advertisement callback ────────────────────────────────────────────

    @callback
    def _on_bt_advertisement(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Triggered when the device starts advertising — attempt immediate connection."""
        import time as _time
        if _time.monotonic() < self._no_reconnect_until:
            return  # power-off cooldown active
        if self._client and self._client.is_connected:
            return
        # Cancel any sleeping reconnect loop and connect right now.
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        _LOGGER.info("Device %s detected in BLE scan, connecting…", self.address)
        self._reconnect_task = self.hass.async_create_background_task(
            self._connect_once(), name="difluid_connect_once"
        )

    async def _connect_once(self) -> None:
        """Single connection attempt; restarts reconnect loop on failure."""
        import time as _time
        if _time.monotonic() < self._no_reconnect_until:
            return  # power-off cooldown active
        try:
            await self._do_connect()
        except Exception as err:
            _LOGGER.debug("BT-triggered connection to %s failed: %s; resuming retry loop", self.address, err)
            self._reconnect_task = self.hass.async_create_background_task(
                self._reconnect_loop(), name="difluid_reconnect_loop"
            )

    # ── connection ────────────────────────────────────────────────────────────

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
        _LOGGER.info("Connected to Difluid Microbalance %s", self.address)

        for svc in client.services:
            _LOGGER.info("  Service: %s", svc.uuid)
            for char in svc.characteristics:
                _LOGGER.info("    Characteristic: %s  props=%s", char.uuid, char.properties)

        difluid_chars = [
            c
            for svc in client.services
            for c in svc.characteristics
            if svc.uuid.lower() in (
                "000000ee-0000-1000-8000-00805f9b34fb",
                "000000dd-0000-1000-8000-00805f9b34fb",
            )
        ]
        self._all_difluid_char_uuids = [c.uuid.lower() for c in difluid_chars]

        write_uuid, notify_uuids = self._pick_characteristics(client)

        if not notify_uuids:
            await client.disconnect()
            raise RuntimeError("No notifiable characteristics found")

        for uuid in notify_uuids:
            try:
                await client.start_notify(uuid, self._on_notification)
                _LOGGER.info("Subscribed to notifications on %s", uuid)
            except BleakError as err:
                _LOGGER.warning("Could not subscribe to %s: %s", uuid, err)

        self._write_char_uuid = write_uuid
        self._encrypted_uuid = next(
            (u for u in self._all_difluid_char_uuids if "ff01" in u), None
        )
        self._cleartext_uuid = next(
            (u for u in self._all_difluid_char_uuids if "aa01" in u), None
        )
        self._client = client

        if self._encrypted_uuid:
            try:
                await self._run_handshake(client)
            except Exception as err:
                _LOGGER.warning("Cloud handshake failed (%s); trying cleartext channel directly", err)
                fallback_uuid = self._cleartext_uuid or write_uuid
                self._write_char_uuid = fallback_uuid
                await client.write_gatt_char(fallback_uuid, _CMD_AUTO_SEND_ON, response=False)
                await asyncio.sleep(1.0)
                await client.write_gatt_char(fallback_uuid, _CMD_GET_STATUS, response=False)
                _LOGGER.info("Sent AUTO_SEND_ON to cleartext channel %s", fallback_uuid)
        else:
            await client.write_gatt_char(write_uuid, _CMD_AUTO_SEND_ON, response=False)
            await asyncio.sleep(1.0)
            await client.write_gatt_char(write_uuid, _CMD_GET_STATUS, response=False)
            _LOGGER.info("Auto-send enabled; waiting for notifications")

        # Query current mode settings so the select entity shows the right value.
        await client.write_gatt_char(self._write_char_uuid, _CMD_GET_AUTO_DETECT, response=False)
        await client.write_gatt_char(self._write_char_uuid, _CMD_GET_AUTO_STOP, response=False)

        self.data.connected = True
        self._last_weight_change_time = asyncio.get_event_loop().time()
        self._last_weight_value = 0.0
        self.async_set_updated_data(self.data)

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = self.hass.async_create_background_task(
            self._poll_loop(), name="difluid_poll_loop"
        )

    async def _run_handshake(self, client: BleakClientWithServiceCache) -> None:
        _LOGGER.info("Encrypted firmware detected; running cloud handshake (model=%s)", self.model)
        self._auth = DifluidCloudAuth(client, self._encrypted_uuid, self.license_key, self.model)
        try:
            await self._auth.run()
        except Exception as err:
            self._auth = None
            raise RuntimeError(f"Difluid cloud handshake failed: {err}") from err
        self._auth = None
        cleartext = self._cleartext_uuid or self._write_char_uuid
        self._write_char_uuid = cleartext
        await client.write_gatt_char(cleartext, _CMD_AUTO_SEND_ON, response=False)
        await asyncio.sleep(1.0)
        await client.write_gatt_char(cleartext, _CMD_GET_STATUS, response=False)
        _LOGGER.info("Handshake complete; auto-send enabled on cleartext channel %s", cleartext)

    def _pick_characteristics(
        self, client: BleakClientWithServiceCache
    ) -> tuple[str, list[str]]:
        difluid_chars: list[BleakGATTCharacteristic] = [
            c
            for svc in client.services
            for c in svc.characteristics
            if svc.uuid.lower() in (
                "000000ee-0000-1000-8000-00805f9b34fb",
                "000000dd-0000-1000-8000-00805f9b34fb",
            )
        ]
        notify_chars = [
            c for c in difluid_chars
            if "notify" in c.properties or "indicate" in c.properties
        ]
        write_chars = [
            c for c in difluid_chars
            if "write" in c.properties or "write-without-response" in c.properties
        ]
        preferred_lower = self._preferred_char_uuid.lower()
        write_uuids_lower = {c.uuid.lower() for c in write_chars}
        if preferred_lower in write_uuids_lower:
            write_uuid = preferred_lower
        elif write_chars:
            write_uuid = write_chars[0].uuid.lower()
        else:
            write_uuid = preferred_lower
        notify_uuids = [c.uuid.lower() for c in notify_chars]
        return write_uuid, notify_uuids

    # ── poll loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(_STATUS_POLL_INTERVAL)
            client = self._client
            if client is None or not client.is_connected or not self._write_char_uuid:
                continue
            try:
                await client.write_gatt_char(self._write_char_uuid, _CMD_GET_STATUS, response=False)
            except Exception as err:
                _LOGGER.warning("Status poll write failed: %s", err)

            # Auto-shutdown: power off if weight hasn't changed for N minutes
            if self._auto_shutdown_minutes > 0 and self._last_weight_change_time > 0:
                idle_sec = asyncio.get_event_loop().time() - self._last_weight_change_time
                if idle_sec >= self._auto_shutdown_minutes * 60:
                    _LOGGER.info(
                        "Auto-shutdown: idle for %.1f min, sending power-off", idle_sec / 60
                    )
                    # Reset timer so we don't resend every 30 s until the device disconnects
                    self._last_weight_change_time = asyncio.get_event_loop().time()
                    try:
                        await self.async_power_off()
                    except Exception as err:
                        _LOGGER.warning("Auto-shutdown failed: %s", err)

    # ── disconnect / reconnect ────────────────────────────────────────────────

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        import time as _time
        self.data.connected = False
        self.async_set_updated_data(self.data)
        # The stream is gone; samples either side of the gap are not comparable.
        if self.brew is not None:
            self.brew.reset()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if _time.monotonic() < self._no_reconnect_until:
            _LOGGER.info("Difluid Microbalance %s disconnected (power-off cooldown, won't reconnect for 60 s)", self.address)
            return
        _LOGGER.warning("Difluid Microbalance %s disconnected, will retry", self.address)
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_background_task(
            self._reconnect_loop(), name="difluid_reconnect_loop"
        )

    async def _reconnect_loop(self) -> None:
        """Retry until connected, pacing on whether the scale is actually there.

        Backing off on consecutive failures is the wrong instinct for this device.
        Nearly every failure means the same thing — the scale is switched off — and
        a failure count keeps growing the delay for as long as that stays true.  So
        by the time the scale is switched back on, the delay is always at its
        maximum: the one moment that matters is the one the loop is least ready
        for.  On 2026-08-14 the scale went off at 08:37, came back on at 08:47:11,
        and the loop was asleep in a 120 s window; the brew was never measured.

        Presence is the variable that actually matters, so pace on that instead.
        An absent scale is not worth calling _do_connect for at all — it would fail
        instantly with "not found" — so poll cheaply and stay ready.  A scale that
        is advertising is retried hard, because it powers itself off again after a
        few idle minutes and every second of that budget spent asleep is a brew
        that goes unrecorded.

        This also stops the loop depending on the advertisement callback, which is
        the fast path but not a reliable one: on 2026-08-14 Home Assistant recorded
        an advertisement at 08:47:11 and _on_bt_advertisement never fired.
        """
        import time as _time
        delay = _PRESENT_RETRY_SECONDS
        while True:
            await asyncio.sleep(delay)
            # Respect power-off cooldown even if the loop was already running.
            remaining = self._no_reconnect_until - _time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining + 1)
                delay = _PRESENT_RETRY_SECONDS
                continue

            if bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            ) is None:
                # Switched off, or out of reach of every connectable proxy.
                delay = _ABSENT_POLL_SECONDS
                continue

            try:
                await self._do_connect()
                _LOGGER.info("Reconnected to Difluid Microbalance %s", self.address)
                return
            except Exception as err:
                # Worth a warning, not a debug line: the scale is advertising and we
                # still cannot reach it, which is a broken proxy rather than a scale
                # sitting switched off in a drawer.  That is what cost the
                # 2026-08-14 08:47 brew, and it was invisible below WARNING.
                _LOGGER.warning(
                    "Difluid Microbalance %s is advertising but the connection failed "
                    "(retry in %.0fs): %s",
                    self.address, min(_PRESENT_RETRY_MAX_SECONDS, delay * 2), err,
                )
                delay = min(_PRESENT_RETRY_MAX_SECONDS, delay * 2)

    # ── notification handler ──────────────────────────────────────────────────

    def _on_notification(self, sender: Any, raw: bytearray) -> None:
        if len(raw) >= 2 and raw[0] == 0xDA and raw[1] == 0xDA:
            if self._auth is not None:
                self._auth.feed_notification(bytes(raw))
            else:
                _LOGGER.debug("Ignoring encrypted heartbeat: %s", raw.hex())
            return

        _LOGGER.info("Notification from %s: %s", getattr(sender, "uuid", sender), raw.hex())

        if len(raw) < 6 or raw[0] != 0xDF or raw[1] != 0xDF:
            _LOGGER.warning("Non-Difluid packet on BLE notify: %s", raw.hex())
            return

        func, cmd, data_len = raw[2], raw[3], raw[4]
        if len(raw) < 5 + data_len + 1:
            return
        payload = raw[5 : 5 + data_len]
        updated = False

        if func == 0x03 and cmd == 0x00 and len(payload) >= 13:
            weight_raw = int.from_bytes(payload[0:4], "big", signed=True)
            unit_idx = payload[12]
            self.data.weight_unit = WEIGHT_UNITS.get(unit_idx, "g")
            self.data.weight = weight_raw / (1000.0 if unit_idx == 1 else 10.0)
            self.data.flow_rate = int.from_bytes(payload[4:6], "big", signed=True) / 10.0
            self.data.timer = int.from_bytes(payload[6:8], "big")
            # Track weight changes for auto-shutdown
            if abs(self.data.weight - self._last_weight_value) > 0.2:
                self._last_weight_change_time = asyncio.get_event_loop().time()
                self._last_weight_value = self.data.weight
            # Feed the dose/pour detector.  Never raises — see BrewSession.feed.
            if self.brew is not None:
                self.brew.feed(self.data.weight, self.data.flow_rate)
            updated = True

        elif func == 0x03 and cmd == 0x05 and len(payload) >= 3:
            self.data.device_status = DEVICE_STATUS_MAP.get(payload[0], "Unknown")
            self.data.battery = payload[1]
            self.data.charging = payload[2] == 1
            updated = True

        elif func == 0x01 and cmd == 0x01 and len(payload) >= 1:
            self.data.auto_detect_timing = payload[0] == 1
            updated = True

        elif func == 0x01 and cmd == 0x02 and len(payload) >= 1:
            self.data.auto_stop_timing = payload[0] == 1
            updated = True

        else:
            _LOGGER.debug(
                "Unhandled Difluid packet func=0x%02x cmd=0x%02x payload=%s",
                func, cmd, raw.hex(),
            )

        if updated:
            self.async_set_updated_data(self.data)

    async def _async_update_data(self) -> MicrobalanceData:
        return self.data
