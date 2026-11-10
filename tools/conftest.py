"""Pytest setup for the Home Assistant tests in this directory.

Only test_migration.py needs it — test_detector.py is a plain script over the pure
detector and runs without pytest or Home Assistant installed.
"""

from unittest.mock import patch

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant see custom_components/ at all."""
    yield


@pytest.fixture(autouse=True)
def no_bluez_mgmt_socket():
    """Stop habluetooth from opening a real BlueZ management socket.

    The harness already has an autouse `mock_bluetooth_adapters`, which patches
    bluetooth_adapters into reporting an hci0 that is not there.  That used to be the
    whole story; since habluetooth grew its own BlueZ channel, believing in hci0 is
    exactly what makes it try to *talk* to it — MGMTBluetoothCtl.setup opens an
    AF_BLUETOOTH socket, pytest-socket refuses, and setting up `bluetooth` fails.  Our
    manifest depends on `bluetooth`, so every test that sets the integration up dies
    there: four of them did, on any machine without a usable BlueZ mgmt socket, which
    is every CI runner and most desks.

    Patched to raise habluetooth's own BluetoothSocketError rather than to succeed,
    because "the socket cannot be opened" is a state it already knows how to be in —
    it is what a host with no adapter looks like from in there.  Faking a working
    socket would be inventing a Bluetooth stack for the tests to talk to, and none of
    these tests is about Bluetooth: they are about config-entry migration and which
    entities get registered.

    Narrow on purpose.  It patches one function, so anything else habluetooth does is
    still live and a real failure in it still surfaces.
    """
    from habluetooth.channels import bluez

    with patch.object(
        bluez.btmgmt_socket,
        "open",
        side_effect=bluez.BluetoothSocketError("no BlueZ socket in tests"),
    ):
        yield


#: What Home Assistant's own Bluetooth integration leaves behind on a host with no
#: adapter.  The manifest depends on `bluetooth`, so setting this integration up sets
#: that one up too, and off it goes:
#:
#:   task   habluetooth scanner retry, after it fails to start and then fails to
#:          force-stop ('NoneType' object has no attribute 'send' — no D-Bus)
#:   timer  BaseHaScanner._async_expire_devices_schedule_next, scheduled from
#:          homeassistant/components/bluetooth/__init__.py
#:
#: Neither is ours; both are gone on a host that has an adapter.  Recorded here by
#: name rather than waved through, because the same two switches would also hide a
#: task or timer *we* leak — so if a future test starts failing on this, read the
#: handle in the message before assuming it is the same thing.


@pytest.fixture
def expected_lingering_tasks() -> bool:
    """Allow the habluetooth scanner's retry task — see the note above."""
    return True


@pytest.fixture
def expected_lingering_timers() -> bool:
    """Allow the Bluetooth device-expiry timer — see the note above."""
    return True
