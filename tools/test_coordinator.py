"""What the scale's coordinator leaves behind when a connection half-succeeds.

    python -m pytest tools/test_coordinator.py

`_do_connect` is a long function whose last third is GATT writes over a proxy link
this install loses for minutes at a time.  Everything here is about the state the
coordinator is in when one of those writes fails: not whether the scale connects —
that needs hardware — but whether a failure looks like a failure to the three things
that decide whether we ever try again.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.difluid_microbalance.coordinator import (  # noqa: E402
    DifluidMicrobalanceCoordinator,
)

_ADDRESS = "AA:BB:CC:DD:EE:FF"
_NOTIFY_UUID = "0000aa02-0000-1000-8000-00805f9b34fb"
_WRITE_UUID = "0000aa01-0000-1000-8000-00805f9b34fb"


def _coordinator(hass: HomeAssistant) -> DifluidMicrobalanceCoordinator:
    return DifluidMicrobalanceCoordinator(
        hass, address=_ADDRESS, is_ti=False, license_key="", model="Microbalance"
    )


def _client(*, write_fails: bool) -> MagicMock:
    """A connected client whose writes either work or all fail.

    `services` is empty, which is why `_pick_characteristics` is patched at the call
    site: the point of these tests is the tail of _do_connect, and building a fake
    GATT tree to reach it would be testing bleak rather than us.
    """
    client = MagicMock()
    client.is_connected = True
    client.services = []
    client.start_notify = AsyncMock()
    client.disconnect = AsyncMock()
    client.write_gatt_char = AsyncMock(
        side_effect=OSError("proxy went away mid-write") if write_fails else None
    )
    return client


async def _connect(hass, coordinator, client):
    """Run _do_connect against `client`, stubbing only what stands before the writes."""
    with (
        patch(
            "custom_components.difluid_microbalance.coordinator"
            ".bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ),
        patch(
            "custom_components.difluid_microbalance.coordinator.establish_connection",
            AsyncMock(return_value=client),
        ),
        patch.object(
            DifluidMicrobalanceCoordinator,
            "_pick_characteristics",
            return_value=(_WRITE_UUID, [_NOTIFY_UUID]),
        ),
    ):
        await coordinator._do_connect()


async def test_a_connect_that_works_leaves_a_usable_client(hass: HomeAssistant) -> None:
    """The control case, so the failure tests below cannot pass by connecting badly."""
    coordinator = _coordinator(hass)
    client = _client(write_fails=False)

    await _connect(hass, coordinator, client)

    assert coordinator._client is client
    assert coordinator.data.connected is True
    client.disconnect.assert_not_awaited()

    await coordinator.async_stop()


async def test_a_write_failure_mid_connect_does_not_leave_a_wedged_client(
    hass: HomeAssistant,
) -> None:
    """The 'available, 0.0 g, forever' failure.

    `self._client = client` is assigned before the last six GATT writes.  When one of
    them failed, the coordinator was left holding a *connected* client that nothing
    would replace: `_on_bt_advertisement` returns early for as long as `is_connected`,
    the entities' `available` reads the same field and answers True, and `async_start`
    only logged.  The scale reported itself present at 0.0 g until Home Assistant was
    restarted, and the only trace was one INFO line.

    So: the exception must propagate (callers decide what to retry), the client must
    be put down rather than left holding the device's one connection slot, and
    `_client` must be None so the advertisement fast path and entity availability both
    tell the truth.
    """
    coordinator = _coordinator(hass)
    client = _client(write_fails=True)

    with pytest.raises(OSError):
        await _connect(hass, coordinator, client)

    assert coordinator._client is None, (
        "a half-connected client was left in place; the advertisement callback and "
        "every entity's availability read this field and would both report a working "
        "scale that is streaming nothing"
    )
    assert coordinator.data.connected is False
    client.disconnect.assert_awaited(), "the device's connection slot was not released"

    await coordinator.async_stop()


async def test_a_scale_that_is_not_there_at_startup_still_gets_a_retry(
    hass: HomeAssistant,
) -> None:
    """async_start must arm the loop, not lean on the advertisement callback alone.

    The callback fires on a *change* in what the scanner sees, so a scale already
    advertising when the connect failed need not produce another one; this file's
    sibling notes record it silently failing to fire on 2026-08-14.  Before this,
    `async_start` swallowed the error and armed nothing at all.
    """
    coordinator = _coordinator(hass)

    with (
        patch(
            "custom_components.difluid_microbalance.coordinator"
            ".bluetooth.async_register_callback",
            return_value=lambda: None,
        ),
        patch.object(
            DifluidMicrobalanceCoordinator,
            "_do_connect",
            AsyncMock(side_effect=OSError("nothing there")),
        ),
    ):
        await coordinator.async_start()

    assert coordinator._reconnect_task is not None, (
        "a startup failure left no way back except a BLE advertisement that may "
        "never come"
    )
    assert not coordinator._reconnect_task.done()

    await coordinator.async_stop()


async def test_the_reconnect_loop_is_never_started_twice(hass: HomeAssistant) -> None:
    """Two loops would race for the scale's single connection slot, each reading the
    other's half-open client as the reason it could not get in."""
    coordinator = _coordinator(hass)

    coordinator._ensure_reconnect_loop()
    first = coordinator._reconnect_task
    coordinator._ensure_reconnect_loop()

    assert coordinator._reconnect_task is first

    await coordinator.async_stop()
