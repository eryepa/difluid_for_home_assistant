from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp
from bleak_retry_connector import BleakClientWithServiceCache

from .const import R2_API_URL

_LOGGER = logging.getLogger(__name__)


class DifluidCloudAuth:
    """Runs the DiFluid 3-step cloud handshake that unlocks the cleartext channel.

    Newer DiFluid firmware (Microbalance and R2) encrypts its BLE traffic: the
    encrypted characteristic streams frames starting with 0xDADA. The device only
    emits cleartext ``DF DF`` sensor data after a handshake brokered by DiFluid's
    cloud. This mirrors the flow in the SDK demo (``pages/r2Detail.vue``):

      1. Ask the server for ``cmd1`` and write it to the encrypted characteristic.
      2. Relay the device's response back to the server; receive ``cmd2``.
      3. Repeat for ``cmd2`` / ``cmd3``.
      4. Ask the server for the ``enableCleartext`` command and write it.

    All cryptography happens server-side and requires a valid SDK license key.
    """

    def __init__(
        self,
        client: BleakClientWithServiceCache,
        encrypted_uuid: str,
        license_key: str,
        model: str,
    ) -> None:
        self._client = client
        self._encrypted_uuid = encrypted_uuid
        self._license_key = license_key
        self._model = model
        self._auth_response: Optional[asyncio.Future] = None
        self.sn = ""
        self.mac = ""

    def feed_notification(self, raw: bytes) -> None:
        """Hand an encrypted-channel notification to the pending handshake step."""
        if self._auth_response is not None and not self._auth_response.done():
            self._auth_response.set_result(bytes(raw))

    async def run(self) -> None:
        """Execute the full handshake. Raises on failure."""
        headers = {"Content-Type": "application/json", "license": self._license_key}
        async with aiohttp.ClientSession() as session:
            # Step 1: cmd1 → device → relay response → sn/mac
            cmd1 = await self._cmd_request(session, headers, "cmd1")
            resp1 = await self._write_and_wait(bytes.fromhex(cmd1))
            result1 = await self._dev_respond(session, headers, resp1.hex())
            self.sn = result1.get("sn", "")
            self.mac = result1.get("mac", "")
            _LOGGER.info("DiFluid handshake: SN=%s MAC=%s", self.sn, self.mac)

            # Step 2: cmd2 → device → relay response → cmd3
            cmd2 = await self._cmd_request(
                session, headers, "cmd2", {"sn": self.sn, "mac": self.mac}
            )
            resp2 = await self._write_and_wait(bytes.fromhex(cmd2))
            result2 = await self._dev_respond(
                session, headers, resp2.hex(), self.sn, self.mac
            )

            # Step 3: cmd3 (instructContent) → device → relay response
            cmd3 = result2.get("instructContent", "")
            resp3 = await self._write_and_wait(bytes.fromhex(cmd3))
            await self._dev_respond(session, headers, resp3.hex(), self.sn, self.mac)

            # Step 4: enableCleartext → device (no response expected)
            enable_cmd = await self._cmd_request(
                session, headers, "enableCleartext", {"sn": self.sn, "mac": self.mac}
            )
            await self._write_and_wait(bytes.fromhex(enable_cmd), wait=False)

    async def _cmd_request(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        cmd_type: str,
        extra: dict | None = None,
    ) -> str:
        payload = {"model": self._model, "type": cmd_type, **(extra or {})}
        async with session.post(
            f"{R2_API_URL}/sdk/cmdRequest", json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["data"]

    async def _dev_respond(
        self,
        session: aiohttp.ClientSession,
        headers: dict,
        content: str,
        sn: str = "",
        mac: str = "",
    ) -> dict:
        payload = {"model": self._model, "content": content, "sn": sn, "mac": mac}
        async with session.post(
            f"{R2_API_URL}/sdk/devRespond", json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["data"]

    async def _write_and_wait(
        self, cmd: bytes, wait: bool = True, timeout: float = 10.0
    ) -> bytes:
        if wait:
            loop = asyncio.get_event_loop()
            self._auth_response = loop.create_future()

        await self._client.write_gatt_char(self._encrypted_uuid, cmd, response=False)

        if not wait:
            return b""

        return await asyncio.wait_for(self._auth_response, timeout=timeout)
