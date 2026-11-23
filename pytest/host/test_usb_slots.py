"""Slot matching must use the real USB bus (pi/usb_slots.py).

The paths below are copied from a Pi 4 whose 4-port hub sits on the USB-C
(dwc2) port, bus 3. udev reported the ESP32-S3 at 3-1.3 as ID_PATH
"...usb-0:1.3:1.0"; matching on that bound it to the slot for 1-1.3, and every
JTAG test failed because OpenOCD was pointed at 1-1.3.
"""
import pytest

import usb_slots as us

S3_DEVPATH = "/devices/platform/soc/fe980000.usb/usb3/3-1/3-1.3/3-1.3:1.0/tty/ttyACM0"
S3_ID_PATH = "platform-fe980000.usb-usb-0:1.3:1.0"

# What _detect_usb_hub_ports produces on that Pi: the built-in VL805 hub at
# 1-1 and the external hub at 3-1, four ports each.
AUTO_PREFIXES = [f"0:1.{p}" for p in range(1, 5)] + [f"2:1.{p}" for p in range(1, 5)]


@pytest.mark.parametrize("devpath, key", [
    (S3_DEVPATH, "2:1.3"),
    ("/devices/platform/scb/fd500000.pcie/pci0000:00/0000:00:00.0/0000:01:00.0"
     "/usb1/1-1/1-1.2/1-1.2:1.0/ttyUSB0/tty/ttyUSB0", "0:1.2"),
    # A board with its own internal hub (dual-USB S3): the deepest device wins.
    ("/devices/platform/soc/fe980000.usb/usb3/3-1/3-1.3/3-1.3.4/3-1.3.4:1.0/tty/ttyACM1",
     "2:1.3.4"),
    # Directly on a root port (Pi 5 style).
    ("/devices/platform/axi/1000120000.pcie/1f00200000.usb/xhci-hcd.0/usb1/1-2/1-2:1.0"
     "/tty/ttyACM0", "0:2"),
    ("", None),
    (None, None),
    ("/devices/virtual/tty/ttyS0", None),
])
def test_key_from_devpath(devpath, key):
    assert us.key_from_devpath(devpath) == key


def test_bus3_device_goes_to_the_bus3_slot_not_bus1():
    """The observed failure: 3-1.3 must land on 2:1.3, never 0:1.3."""
    assert us.match_prefix(AUTO_PREFIXES, devpath=S3_DEVPATH,
                           id_path=S3_ID_PATH) == "2:1.3"


def test_id_path_alone_would_have_picked_the_wrong_slot():
    """Guards the premise: ID_PATH really does spell bus 3 as 0."""
    assert "0:1.3" in S3_ID_PATH and "2:1.3" not in S3_ID_PATH


def test_unmatched_device_does_not_fall_back_to_id_path_on_a_bused_config():
    """A bus-3 device with no bus-3 slot must not be bound to bus 1's slot."""
    bus1_only_plus_bus3_elsewhere = ["0:1.3", "2:1.1"]
    assert us.match_prefix(bus1_only_plus_bus3_elsewhere, devpath=S3_DEVPATH,
                           id_path=S3_ID_PATH) is None


def test_legacy_bus_less_config_still_matches_via_id_path():
    """Configs written by the old rfc2217-learn-slots say "0:" for every bus.

    They keep working as before rather than every board turning dynamic.
    """
    assert us.match_prefix(["0:1.1", "0:1.3"], devpath=S3_DEVPATH,
                           id_path=S3_ID_PATH) == "0:1.3"


def test_device_behind_a_board_hub_matches_its_slot_port():
    devpath = "/devices/platform/soc/fe980000.usb/usb3/3-1/3-1.3/3-1.3.4/3-1.3.4:1.0/tty/ttyACM1"
    assert us.match_prefix(AUTO_PREFIXES, devpath=devpath) == "2:1.3"


def test_port_boundary_one_does_not_match_ten():
    assert not us.prefix_matches("2:1.1", "2:1.10")
    assert us.prefix_matches("2:1.1", "2:1.1")
    assert us.prefix_matches("2:1.1", "2:1.1.2")


def test_longest_prefix_wins():
    devpath = "/devices/x/usb3/3-1/3-1.3/3-1.3.2/3-1.3.2:1.0/tty/ttyACM0"
    assert us.match_prefix(["2:1.3", "2:1.3.2"], devpath=devpath) == "2:1.3.2"


def test_no_devpath_uses_id_path():
    """A hotplug event without DEVPATH behaves exactly as before."""
    assert us.match_prefix(AUTO_PREFIXES, id_path=S3_ID_PATH) == "0:1.3"


@pytest.mark.parametrize("prefix, name", [
    ("2:1.3", "3-1.3"), ("0:1.1", "1-1.1"), ("0:2", "1-2"),
    ("", None), ("bogus", None), ("x:1.1", None),
])
def test_sysfs_name(prefix, name):
    assert us.sysfs_name(prefix) == name


def test_sysfs_name_agrees_with_openocd_location():
    """Both directions must use the same bus arithmetic."""
    import debug_controller as dc   # stdlib-only, safe on the host
    for p in AUTO_PREFIXES:
        assert dc._usb_location(p) == us.sysfs_name(p)
