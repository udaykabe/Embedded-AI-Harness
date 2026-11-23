"""When the bench reloads its radio driver after a failed station join.

A Pi 4's built-in radio (brcmfmac) can wedge so that every join times out in
the driver itself, and only a driver reload clears it (runbook §8). The bench
does that reload once, automatically, but only for that failure: reloading on
any other failure would cost time and hide a different fault.
"""
import wifi_controller as wc

# As the wedged Pi logged it: the driver gave up, so no BSSID is named.
WEDGED = (
    "wlan0: Trying to associate with 1c:db:d4:45:8a:8d (SSID='WB-Test-Setup' freq=2412 MHz)\n"
    "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16\n"
)


def test_wedged_brcmfmac_is_reloaded():
    assert wc.needs_radio_reload(WEDGED, "brcmfmac")


def test_other_drivers_are_left_alone():
    # The cure is shown only for brcmfmac; a USB adapter's driver is not touched.
    assert not wc.needs_radio_reload(WEDGED, "p54usb")
    assert not wc.needs_radio_reload(WEDGED, "")


def test_a_real_rejection_by_the_ap_is_not_a_wedge():
    # The AP answered and refused: it is named, and a reload would not help.
    log = "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=1c:db:d4:45:8a:8d status_code=16\n"
    assert not wc.needs_radio_reload(log, "brcmfmac")


def test_other_failures_are_not_a_wedge():
    for log in (
        "",
        None,
        "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"X\" auth_failures=1 reason=WRONG_KEY\n",
        "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=1\n",
        "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=160\n",
        "wlan0: CTRL-EVENT-NETWORK-NOT-FOUND\n",
    ):
        assert not wc.needs_radio_reload(log, "brcmfmac"), log
