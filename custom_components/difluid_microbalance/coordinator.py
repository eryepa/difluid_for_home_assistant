from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
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


def _build_cmd(func: int, cmd: int, data: bytes = b"") -> bytes:
    frame = bytes([func, cmd, len(data)]) + data
    full = _HEADER + frame
    return full + bytes([sum(full) & 0xFF])


_CMD_AUTO_SEND_ON = _build_cmd(0x01, 0x00, bytes([0x01]))
_CMD_GET_SENSOR_DATA = _build_cmd(0x03, 0x00)
_CMD_GET_STATUS = _build_cmd(0x03, 0x05)

_DATA_POLL_INTERVAL = 2    # poll sensor data every 2 s
_STATUS_POLL_INTERVAL = 30  # poll battery/status every 30 s


@dataclass
class MicrobalanceData:
    weight: float = 0.0
    weight_unit: str = "g"
    flow_rate: float = 0.0
    timer: int = 0
    battery: int = 0
    charging: bool = False
    device_status: str = "Unknown"


class DifluidMicrobalanceCoordinator(DataUpdateCoordinator[MicrobalanceData]):

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        is_ti: bool = False,
        license_key: str = "",
        model: str = "",
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{address}", update_interval=None)
        self.address = address
        self.is_ti = is_ti
        self.license_key = license_key
        self.model = model
        self._preferred_char_uuid = (
            CHARACTERISTIC_UUID_MICROBALANCE_TI if is_ti else CHARACTERISTIC_UUID_MICROBALANCE
        )
        self._write_char_uuid: Optional[str] = None
        self._all_difluid_char_uuids: list[str] = []
        # Encrypted-firmware support: the device only streams cleartext DF DF data
        # after a license-authenticated cloud handshake on the encrypted channel.
        self._encrypted_uuid: Optional[str] = None
        self._cleartext_uuid: Optional[str] = None
        self._auth: Optional[DifluidCloudAuth] = None
        self._client: Optional[BleakClientWithServiceCache] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self.data = MicrobalanceData()

    async def async_start(self) -> None:
        await self._do_connect()

    async def async_stop(self) -> None:
        for task in (self._poll_task, self._reconnect_task):
            if task and not task.done():
                task.cancel()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

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

        # Log discovered services/characteristics to help with diagnostics
        for svc in client.services:
            _LOGGER.info("  Service: %s", svc.uuid)
            for char in svc.characteristics:
                _LOGGER.info(
                    "    Characteristic: %s  props=%s", char.uuid, char.properties
                )

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
            raise RuntimeError(
                "No notifiable characteristics found — check HA logs for discovered UUIDs"
            )

        for uuid in notify_uuids:
            try:
                await client.start_notify(uuid, self._on_notification)
                _LOGGER.info("Subscribed to notifications on %s", uuid)
            except BleakError as err:
                _LOGGER.warning("Could not subscribe to %s: %s", uuid, err)

        self._write_char_uuid = write_uuid
        _LOGGER.info("Using %s for write commands", write_uuid)

        # Identify encrypted (ff01) and cleartext (aa01) channels for firmware
        # that gates sensor data behind a license-authenticated handshake.
        self._encrypted_uuid = next(
            (u for u in self._all_difluid_char_uuids if "ff01" in u), None
        )
        self._cleartext_uuid = next(
            (u for u in self._all_difluid_char_uuids if "aa01" in u), None
        )

        self._client = client

        if self._encrypted_uuid:
            # Encrypted-capable firmware detected (ff01 channel present).
            # The DiFluid server no longer requires a license key; handshake works
            # with an empty key.  If it fails for any reason, fall back to direct
            # cleartext so the user still sees data on unencrypted devices.
            try:
                await self._run_handshake(client)
            except Exception as err:
                _LOGGER.warning(
                    "Cloud handshake failed (%s); falling back to direct cleartext", err
                )
                await client.write_gatt_char(write_uuid, _CMD_AUTO_SEND_ON, response=False)
                await client.write_gatt_char(write_uuid, _CMD_GET_STATUS, response=False)
        else:
            # Cleartext firmware: enable streaming directly on the command channel.
            await client.write_gatt_char(write_uuid, _CMD_AUTO_SEND_ON, response=False)
            await client.write_gatt_char(write_uuid, _CMD_GET_STATUS, response=False)
            _LOGGER.info("Auto-send enabled; waiting for notifications")

        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = self.hass.async_create_task(
            self._poll_loop(), eager_start=False
        )

    async def _run_handshake(self, client: BleakClientWithServiceCache) -> None:
        """Authenticate the encrypted channel, then stream cleartext data."""
        _LOGGER.info(
            "Encrypted firmware detected; running cloud handshake (model=%s)",
            self.model,
        )
        self._auth = DifluidCloudAuth(
            client, self._encrypted_uuid, self.license_key, self.model
        )
        try:
            await self._auth.run()
        except Exception as err:
            self._auth = None
            raise RuntimeError(f"Difluid cloud handshake failed: {err}") from err
        self._auth = None

        # Cleartext is now unlocked. Stream sensor data on the cleartext channel.
        cleartext = self._cleartext_uuid or self._write_char_uuid
        self._write_char_uuid = cleartext
        await client.write_gatt_char(cleartext, _CMD_AUTO_SEND_ON, response=False)
        await client.write_gatt_char(cleartext, _CMD_GET_STATUS, response=False)
        _LOGGER.info("Handshake complete; auto-send enabled on cleartext channel %s", cleartext)

    def _pick_characteristics(
        self, client: BleakClientWithServiceCache
    ) -> tuple[str, list[str]]:
        # Only consider characteristics from the Difluid service (000000ee / 000000dd),
        # ignoring standard BLE services (00001800, 00001801, etc.)
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
            _LOGGER.warning(
                "Preferred write characteristic %s not found; falling back to %s",
                preferred_lower, write_uuid,
            )
        else:
            write_uuid = preferred_lower

        notify_uuids = [c.uuid.lower() for c in notify_chars]
        return write_uuid, notify_uuids

    async def _poll_loop(self) -> None:
        """Lightweight status poll — battery/charge only, every 30 s.

        Sensor data (weight/flow/timer) arrives via BLE notifications because
        AUTO_SEND is enabled at connect time. We deliberately do NOT read or
        poll the sensor characteristic: reading it only returns an echo of the
        last written command, and frequent read/write traffic over a marginal
        ESPHome Bluetooth Proxy link saturates the connection and suppresses
        the notification stream. A single status write every 30 s is enough to
        refresh battery state without disturbing notifications.
        """
        while True:
            await asyncio.sleep(_STATUS_POLL_INTERVAL)
            client = self._client
            if client is None or not client.is_connected or not self._write_char_uuid:
                continue
            try:
                await client.write_gatt_char(
                    self._write_char_uuid, _CMD_GET_STATUS, response=False
                )
            except Exception as err:
                _LOGGER.warning("Status poll write failed: %s", err)

    def _on_disconnect(self, _client: BleakClientWithServiceCache) -> None:
        _LOGGER.warning("Difluid Microbalance %s disconnected, will retry", self.address)
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self.hass.async_create_task(
            self._reconnect_loop(), eager_start=False
        )

    async def _reconnect_loop(self) -> None:
        for delay in (5, 15, 30, 60, 120):
            await asyncio.sleep(delay)
            try:
                await self._do_connect()
                _LOGGER.info("Reconnected to Difluid Microbalance %s", self.address)
                return
            except Exception as err:
                _LOGGER.debug("Reconnect attempt failed (%ss delay): %s", delay, err)
        _LOGGER.error(
            "Failed to reconnect to Difluid Microbalance %s after retries", self.address
        )

    def _on_notification(self, sender: Any, raw: bytearray) -> None:
        # Encrypted-channel frames (0xDADA …) belong to the handshake. While auth
        # is running, hand them to it; afterwards they are heartbeats we ignore.
        if len(raw) >= 2 and raw[0] == 0xDA and raw[1] == 0xDA:
            if self._auth is not None:
                self._auth.feed_notification(bytes(raw))
            else:
                _LOGGER.debug("Ignoring encrypted heartbeat: %s", raw.hex())
            return

        _LOGGER.info(
            "Notification from %s: %s",
            getattr(sender, "uuid", sender),
            raw.hex(),
        )

        if len(raw) < 6 or raw[0] != 0xDF or raw[1] != 0xDF:
            _LOGGER.warning("Non-Difluid packet on BLE notify: %s", raw.hex())
            return

        func, cmd, data_len = raw[2], raw[3], raw[4]
        if len(raw) < 5 + data_len + 1:
            _LOGGER.warning(
                "Packet too short (got %d, expected %d): %s",
                len(raw), 5 + data_len + 1, raw.hex(),
            )
            return
        payload = raw[5 : 5 + data_len]
        updated = False

        if func == 0x03 and cmd == 0x00 and len(payload) >= 13:
            weight_raw = int.from_bytes(payload[0:4], "big")
            unit_idx = payload[12]
            self.data.weight_unit = WEIGHT_UNITS.get(unit_idx, "g")
            self.data.weight = weight_raw / (1000.0 if unit_idx == 1 else 10.0)
            self.data.flow_rate = int.from_bytes(payload[4:6], "big") / 10.0
            self.data.timer = int.from_bytes(payload[6:8], "big")
            updated = True

        elif func == 0x03 and cmd == 0x05 and len(payload) >= 3:
            self.data.device_status = DEVICE_STATUS_MAP.get(payload[0], "Unknown")
            self.data.battery = payload[1]
            self.data.charging = payload[2] == 1
            updated = True

        else:
            _LOGGER.warning(
                "Unhandled Difluid packet func=0x%02x cmd=0x%02x payload=%s",
                func, cmd, raw.hex(),
            )

        if updated:
            self.async_set_updated_data(self.data)

    async def _async_update_data(self) -> MicrobalanceData:
        return self.data
