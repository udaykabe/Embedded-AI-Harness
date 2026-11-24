"""How the bench classifies a station join that times out authenticating.

A join that fails with `ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16`
never heard the AP's reply. On the bench that was a neighbouring network on the
AP's channel, louder than the AP (runbook §8). The bench records each one, and
reloads the radio driver only when asked to: the reload was added when these
were taken for a wedged radio, and it never recovered a join.
"""
import wifi_controller as wc

# As the Pi logged it: the driver gave up, so no BSSID is named.
TIMEOUT = (
    "wlan0: Trying to associate with 1c:db:d4:45:8a:8d (SSID='WB-Test-Setup' freq=2412 MHz)\n"
    "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16\n"
)


def test_an_unheard_reply_is_an_auth_timeout():
    assert wc.is_auth_timeout(TIMEOUT)


def test_a_real_rejection_by_the_ap_is_not():
    # The AP answered and refused: it is named.
    log = "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=1c:db:d4:45:8a:8d status_code=16\n"
    assert not wc.is_auth_timeout(log)


def test_other_failures_are_not():
    for log in (
        "",
        None,
        "wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid=\"X\" auth_failures=1 reason=WRONG_KEY\n",
        "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=1\n",
        "wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=160\n",
        "wlan0: CTRL-EVENT-NETWORK-NOT-FOUND\n",
    ):
        assert not wc.is_auth_timeout(log), log


def test_the_driver_reload_is_off_unless_asked_for(monkeypatch):
    import importlib
    monkeypatch.delenv("WIFI_STA_RADIO_RELOAD", raising=False)
    assert importlib.reload(wc).STA_RADIO_RELOAD is False
    monkeypatch.setenv("WIFI_STA_RADIO_RELOAD", "1")
    assert importlib.reload(wc).STA_RADIO_RELOAD is True
    monkeypatch.delenv("WIFI_STA_RADIO_RELOAD")
    importlib.reload(wc)


def test_an_opted_in_reload_applies_only_to_known_drivers():
    assert wc.needs_radio_reload(TIMEOUT, "brcmfmac")
    assert not wc.needs_radio_reload(TIMEOUT, "p54usb")
    assert not wc.needs_radio_reload(TIMEOUT, "")
