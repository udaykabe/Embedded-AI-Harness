"""Which slot a USB device belongs to: pure logic, no hardware.

A slot is a physical hub port, named by a *usb_prefix* "B:P" where B is the
kernel bus number minus one and P is the port path ("2:1.3" is port 3 of the
hub on port 1 of bus 3, kernel name "3-1.3").

The bus must come from DEVPATH, never from ID_PATH. udev's ID_PATH spells the
USB part as a literal "usb-0:<path>" on every bus; it tells controllers apart
only by the platform/PCI text in front. Matching a prefix against ID_PATH
therefore binds a device at 3-1.3 to the slot for 1-1.3, and OpenOCD is then
sent to "adapter usb location 1-1.3", where nothing is plugged in. Observed on
a Pi 4 with a hub on the USB-C (dwc2, bus 3) port: every JTAG test failed with
"esp_usb_jtag: could not find or open device!".
"""

import re

# A USB device's kernel name: "<bus>-<port>[.<port>...]". Interfaces carry a
# ":<config>.<if>" suffix and root hubs are "usbN"; neither matches.
_DEVICE_SEG = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")


def key_from_devpath(devpath: str | None) -> str | None:
    """DEVPATH → "B:P" for the USB device it lives under, or None.

    /devices/platform/soc/fe980000.usb/usb3/3-1/3-1.3/3-1.3:1.0/tty/ttyACM0
    → "2:1.3" (the deepest device segment, 3-1.3).
    """
    if not devpath:
        return None
    found = None
    for seg in devpath.split("/"):
        m = _DEVICE_SEG.match(seg)
        if m:
            found = m
    if not found:
        return None
    return f"{int(found.group(1)) - 1}:{found.group(2)}"


def prefix_matches(prefix: str, key: str) -> bool:
    """Is the device at `key` on the port `prefix` names, or behind it?

    Port-boundary aware: "2:1.1" covers "2:1.1" and "2:1.1.4" (a board's own
    internal hub), never "2:1.10".
    """
    return key == prefix or key.startswith(prefix + ".")


def is_bus_less(prefixes) -> bool:
    """True when every prefix says bus 0: a config written from ID_PATH.

    Such configs predate this module. Their "0" means "whatever bus", so they
    can only be honoured by the old ID_PATH substring match. Any prefix on
    another bus proves the config records real buses, and then the old match
    must not run: it is exactly what bound bus-3 devices to bus-1 slots.
    """
    prefixes = [p for p in prefixes if p]
    return bool(prefixes) and all(p.split(":", 1)[0] == "0" for p in prefixes)


def match_prefix(prefixes, devpath: str | None = None,
                 id_path: str | None = None) -> str | None:
    """The configured prefix a device belongs to, or None.

    Longest prefix wins. DEVPATH decides when it is known; the ID_PATH
    substring match is kept only for bus-less configs (see is_bus_less).
    """
    ordered = sorted((p for p in prefixes if p), key=len, reverse=True)
    key = key_from_devpath(devpath)
    if key:
        for p in ordered:
            if prefix_matches(p, key):
                return p
    if id_path and (not key or is_bus_less(ordered)):
        for p in ordered:
            if p in id_path:
                return p
    return None


def sysfs_name(prefix: str) -> str | None:
    """"B:P" → kernel device name "<B+1>-P" ("2:1.3" → "3-1.3")."""
    if not prefix or ":" not in prefix:
        return None
    bus, path = prefix.split(":", 1)
    try:
        return f"{int(bus) + 1}-{path}"
    except ValueError:
        return None
