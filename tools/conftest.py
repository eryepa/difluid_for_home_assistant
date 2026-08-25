"""Pytest setup for the Home Assistant tests in this directory.

Only test_migration.py needs it — test_detector.py is a plain script over the pure
detector and runs without pytest or Home Assistant installed.
"""

import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant see custom_components/ at all."""
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
