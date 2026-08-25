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


@pytest.fixture
def expected_lingering_tasks() -> bool:
    """Tolerate the Bluetooth scanner's retry task outliving a test.

    The manifest depends on `bluetooth`, so setting up this integration sets that one
    up too, and on a machine with no adapter its scanner fails to start, fails again to
    force-stop, and leaves a retry task behind.  None of that is ours: it happens
    before any DiFluid code runs, and the same test passes with the task cleaned up on
    a host with an adapter.

    Narrow on purpose.  This says nothing about tasks *we* leak — those would show up
    the same way, so if this ever needs widening, look first.
    """
    return True
