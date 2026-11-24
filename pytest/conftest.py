"""Pytest fixtures for the testbench (HTTP-only, Pi backend).

Usage:
    pytest testbench_test.py --wt-url http://<pi-ip>:8080
"""

import glob
import json
import os
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import pytest

from testbench_driver import TestbenchDriver


def pytest_addoption(parser):
    parser.addoption(
        "--wt-url",
        default=os.environ.get("TESTBENCH_URL", "http://localhost:8080"),
        help="Portal URL for the Embedded Testbench Pi",
    )
    parser.addoption(
        "--run-dut",
        action="store_true",
        default=False,
        help="Run tests that require a partner connected",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "requires_dut: test needs a DUT or second WiFi device connected",
    )


def pytest_collection_modifyitems(config, items):
    """Only `requires_dut` is gated by a flag now.

    `requires_wifi_dut` used to hide twelve tests behind --run-wifi-dut,
    because nothing on the bench could join an AP and the suite had no way
    to tell "no device" from "device broken". The bench now owns that
    device, and the `test_partner` fixture answers the question directly: it
    provisions the board, or skips naming exactly what is absent. A flag on
    top of that would only let a real failure hide as an opt-out.
    """
    if not config.getoption("--run-dut", default=False):
        skip_dut = pytest.mark.skip(reason="Requires a DUT (use --run-dut)")
        for item in items:
            if "requires_dut" in item.keywords:
                item.add_marker(skip_dut)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Attach test outcome to the node so fixtures can read it."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(scope="session")
def testbench(request):
    """Session-scoped connection to the Embedded Testbench.

    The run begins with a bench reset (FR-036), which is what that endpoint
    was built for and what nothing was calling. Skipping it is not a
    theoretical risk: a run started on a bench carrying two live debug
    sessions and leftover SDR and WiFi state took the portal down partway
    through and failed nineteen tests, none of which had anything wrong with
    them. The same suite, on the same hardware, minutes later and after a
    reset: no failures.

    A reset that cannot be performed is reported, not swallowed — starting
    from an unknown state is exactly what this prevents.
    """
    url = request.config.getoption("--wt-url")
    driver = TestbenchDriver(url)
    driver.open()
    driver.ping()
    r = driver.bench_reset()
    if not r.get("ok"):
        pytest.exit(f"bench reset failed, refusing to run from an unknown "
                    f"state: {r}", returncode=3)
    if r.get("changed"):
        print(f"\nbench reset: {', '.join(r['changed'])}")
    restored = ensure_test_partner_firmware(driver)
    if restored:
        print(f"test partner: {restored}")
    yield driver
    try:
        driver.ap_stop()
    except Exception:
        pass
    driver.close()


@pytest.fixture
def wifi_network(testbench):
    """Start a fresh AP for this test, stop on teardown."""
    ssid = f"WT-{uuid.uuid4().hex[:6].upper()}"
    password = "testpass123"
    testbench.drain_events()
    testbench.ap_start(ssid, password)
    yield {"ssid": ssid, "password": password, "ap_ip": "192.168.4.1"}
    testbench.ap_stop()


# The bench's own test partner — the firmware in test-firmware/, built by CI.
TEST_PARTNER_PORTAL = "WB-Test-Setup"   # SSID it advertises unprovisioned
TEST_PARTNER_HTTP_PORT = 8080           # its own server, not the portal's

# Where a known-good bench-partner image lives, and the opt-in to restore it.
# Either a directory holding the images and the `flash_args` that names their
# offsets, exactly as CI publishes it; or `release` (the latest GitHub release
# of TEST_PARTNER_REPO) or `release:<tag>`, downloaded once and cached. Unset,
# the suite never flashes the partner: a board that does not answer the
# partner's console may be someone's project, not a partner that lost its
# image.
TEST_PARTNER_IMAGE_ENV = "WT_TEST_PARTNER_IMAGE"
TEST_PARTNER_REPO_ENV = "WT_TEST_PARTNER_REPO"
TEST_PARTNER_REPO = "udaykabe/Embedded-AI-Harness"
TEST_PARTNER_CHIPS = ("esp32c3", "esp32s3")   # what release.yml builds

# Partner transmit power, quarter-dBm (52 = 13 dBm). Unset leaves the
# partner as it is. For a bench that cannot hear the partner's 5 dBm default:
# a metal enclosure, or a USB 3 device beside the Pi's antenna.
TEST_PARTNER_TXPOWER_ENV = "WT_TEST_PARTNER_TXPOWER"
TEST_PARTNER_APCHAN_ENV = "WT_TEST_PARTNER_APCHAN"


def _dut_answers(testbench, slot, timeout=6.0) -> bool:
    """Does this slot's device answer the test partner console?"""
    try:
        since = time.time()
        testbench.serial_write(slot, text="ping")
    except Exception:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = testbench.serial_output(slot, lines=200, since=since)
        except Exception:
            return False
        if any("OK pong" in ln.get("text", "")
               for ln in out.get("lines", [])):
            return True
        time.sleep(0.5)
    return False


def _partner_cache_dir():
    base = (os.environ.get("XDG_CACHE_HOME")
            or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "embedded-ai-harness", "test-partner")


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={
        "User-Agent": "embedded-ai-harness-tests",
        "Accept": "application/vnd.github+json, application/octet-stream",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def release_partner_image(chip, tag="latest"):
    """Directory with a release's partner firmware for `chip`; downloads once.

    A release asset does not expire and needs no token, unlike a CI artifact.
    Cached under ~/.cache/embedded-ai-harness/test-partner/<tag>/. Offline,
    "latest" falls back to the newest cached copy. Raises RuntimeError saying
    what went wrong.
    """
    if chip not in TEST_PARTNER_CHIPS:
        raise RuntimeError(f"releases carry no partner firmware for {chip}")
    repo = os.environ.get(TEST_PARTNER_REPO_ENV, TEST_PARTNER_REPO)
    cache = _partner_cache_dir()
    name = f"test-partner-{chip}"
    if tag == "latest":
        try:
            with _http_get(f"https://api.github.com/repos/{repo}/releases/latest",
                           timeout=15) as r:
                tag = json.load(r)["tag_name"]
        except Exception as exc:
            cached = sorted(glob.glob(os.path.join(cache, "*", name, "flash_args")),
                            key=os.path.getmtime)
            if cached:
                return os.path.dirname(cached[-1])
            raise RuntimeError(
                f"could not look up the latest release of {repo}: {exc}") from exc

    dest = os.path.join(cache, tag)
    image_dir = os.path.join(dest, name)
    if os.path.isfile(os.path.join(image_dir, "flash_args")):
        return image_dir

    url = f"https://github.com/{repo}/releases/download/{tag}/{name}.tar.gz"
    os.makedirs(dest, exist_ok=True)
    tmp = tempfile.mkdtemp(dir=dest)
    try:
        archive = os.path.join(tmp, "fw.tar.gz")
        try:
            with _http_get(url, timeout=60) as r, open(archive, "wb") as f:
                shutil.copyfileobj(r, f)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"could not download {url}: HTTP {exc.code}") from exc
        with tarfile.open(archive) as tar:
            tar.extractall(tmp, filter="data")   # no paths outside tmp
        got = os.path.join(tmp, name)
        if not os.path.isfile(os.path.join(got, "flash_args")):
            raise RuntimeError(f"{url} holds no {name}/flash_args")
        os.replace(got, image_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return image_dir


def _partner_image_for(source, chip):
    """Resolve WT_TEST_PARTNER_IMAGE to a directory for `chip`."""
    if source == "release" or source.startswith("release:"):
        return release_partner_image(chip, source.partition(":")[2] or "latest")
    return source


def _flash_args_offsets(image_dir):
    """Parse CI's `flash_args` into [(offset, path)] — never guess offsets.

    They move whenever the partition table does; a guessed offset produces a
    hash-verified write and a device that will not boot.
    """
    args_path = os.path.join(image_dir, "flash_args")
    with open(args_path) as f:
        tokens = f.read().split()
    parts = []
    for i, tok in enumerate(tokens):
        if tok.startswith("0x") and i + 1 < len(tokens):
            parts.append((tok, os.path.join(image_dir,
                                            os.path.basename(tokens[i + 1]))))
    return parts


def ensure_test_partner_firmware(testbench):
    """Put the bench's own test partner back to the image the suite assumes.

    A run has to begin from a defined bench *and* a defined partner. FR-036
    gives the first; this gives the second, and without it the suite is not
    repeatable: WT-1800 deliberately flashes a throwaway image over the
    test partner to prove flashing works, and nothing ever puts the real one
    back. The next run then starts on a board that answers nothing, and the
    console and provisioning fixtures report absent hardware — on hardware
    that is present and was working an hour ago.

    Returns a description of what it did, or None when nothing was needed.
    Absence of a configured image is not an error: the fixtures downstream
    already report an unmet precondition naming what to flash.
    """
    source = os.environ.get(TEST_PARTNER_IMAGE_ENV, "").strip()
    slots = [d for d in testbench.get_devices() if d.get("present")]
    for dev in slots:
        if _dut_answers(testbench, dev["label"]):
            return None                     # already the image we want
    if not source:
        return None                         # restoring was not asked for
    from_release = source == "release" or source.startswith("release:")
    if from_release:
        # One image per chip, so only slots holding a chip releases build for.
        slots = [d for d in slots if d.get("detected_chip") in TEST_PARTNER_CHIPS]
    else:
        if not os.path.isdir(source):
            return None                     # nothing to restore from
        # The image directory is built for one target, and the bench may now
        # hold more than one kind of part. Flashing an esp32s3 image at an
        # esp32c6 gets as far as esptool refusing it — "This chip is
        # ESP32-C6, not ESP32-S3" — so prefer the slot the image is for.
        want = os.path.basename(source.rstrip("/")).rsplit("-", 1)[-1]
        slots = ([d for d in slots if d.get("detected_chip") == want]
                 or [d for d in slots if d.get("detected_chip")])

    for dev in slots:
        chip = dev.get("detected_chip")
        if not chip:
            continue
        try:
            image_dir = _partner_image_for(source, chip)
            images = dict(_flash_args_offsets(image_dir))
            r = testbench.flash(dev["label"], images, chip=chip)
            if not r.get("ok"):
                return (f"could not restore {dev['label']}: "
                        f"{r.get('error') or r}")
        except Exception as exc:
            return f"could not restore {dev['label']}: {exc}"
        time.sleep(10)
        if _dut_answers(testbench, dev["label"], timeout=15):
            return f"restored test partner firmware on {dev['label']}"
        return (f"flashed {dev['label']} from {image_dir} but it does not "
                f"answer the console")
    return None


def _send_partner_setting(testbench, verb, env):
    """Send `<verb> <$env>` to the partner's console, if the variable is set.

    Returns what happened, or None when the variable is unset. Never raises:
    a partner built before the command existed answers "ERR unknown command",
    and that must read as "update the partner", not as a failed suite.
    """
    want = os.environ.get(env)
    if not want:
        return None
    for dev in testbench.get_devices():
        if not dev.get("present") or not _dut_answers(testbench, dev["label"]):
            continue
        slot = dev["label"]
        since = time.time()
        testbench.serial_write(slot, text=f"{verb} {want}")
        deadline = time.time() + 6
        while time.time() < deadline:
            for ln in testbench.serial_output(slot, lines=200,
                                              since=since).get("lines", []):
                text = ln.get("text", "")
                if text.startswith((f"OK {verb}", "ERR")):
                    return f"{slot}: {text}"
            time.sleep(0.5)
        return f"{slot}: no answer to '{verb} {want}'"
    return "no partner answered the console"


def apply_test_partner_txpower(testbench):
    """Send `txpower <WT_TEST_PARTNER_TXPOWER>` to the partner, if set."""
    return _send_partner_setting(testbench, "txpower", TEST_PARTNER_TXPOWER_ENV)


def apply_test_partner_apchan(testbench):
    """Send `apchan <WT_TEST_PARTNER_APCHAN>` to the partner, if set.

    For a bench whose neighbours crowd the partner's default channel (1):
    the Pi's station then times out joining the partner (status 16) while
    every link on the bench's own channel works.
    """
    return _send_partner_setting(testbench, "apchan", TEST_PARTNER_APCHAN_ENV)


@pytest.fixture(scope="session")
def _test_partner_session(testbench):
    """Provision the test partner onto a fresh AP, once per run.

    Provisioning costs a reboot and half a minute, and every test that
    needs a joined partner needs the same one; doing it per test would
    re-provision a dozen times and prove nothing extra. The SSID is still
    fresh per session, so a pass still shows the partner used what it was just
    given rather than something cached.

    The device is the bench's own — never a project's board. A testbench
    test that asserts on project firmware goes red when that project ships,
    which is the dependency backwards.
    """
    txp = apply_test_partner_txpower(testbench)
    if txp:
        print(f"[test partner] {TEST_PARTNER_TXPOWER_ENV}: {txp}", flush=True)
    chan = apply_test_partner_apchan(testbench)
    if chan:
        print(f"[test partner] {TEST_PARTNER_APCHAN_ENV}: {chan}", flush=True)
    ssid = f"WT-{uuid.uuid4().hex[:6].upper()}"
    password = "testpass123"
    testbench.drain_events()
    testbench.ap_start(ssid, password, internet=True)

    station = _wait_for_any_station(testbench, timeout=20)

    if not station and _provision_over_serial(testbench, ssid, password):
        # The wire first. Credentials used to arrive only through the partner's
        # own captive portal, which means every station and HTTP test
        # depended on the portal working — so a portal defect failed nine
        # tests that are not about provisioning at all, and a radio that
        # would not transmit failed all of them with no way in. The portal
        # path is still exercised, deliberately, by WT-2100.
        station = _wait_for_any_station(testbench, timeout=90)

    if not station:
        # Neither joined nor reachable over the wire: either unprovisioned,
        # or holding a previous run's SSID. Its portal is the way in — the
        # firmware clears stale credentials and returns to the portal by
        # itself.
        if not _portal_is_up(testbench):
            _ap_stop_quietly(testbench)
            pytest.skip(
                f"precondition unmet: no test partner. Nothing joined the AP "
                f"and no '{TEST_PARTNER_PORTAL}' portal is on the air. Flash "
                f"test-firmware/ to a free slot (CI publishes it as "
                f"test-partner-<target>)."
            )
        testbench.provision_wifimanager(
            TEST_PARTNER_PORTAL, ssid, password,
            save_path="/connect", field_ssid="ssid",
            field_password="password", internet=True)
        station = _wait_for_any_station(testbench, timeout=120)

    if not station:
        _ap_stop_quietly(testbench)
        pytest.skip(
            "precondition unmet: the test partner was provisioned but never "
            f"joined '{ssid}'"
        )

    yield {
        "ssid": ssid, "password": password, "ap_ip": "192.168.4.1",
        "mac": station["mac"], "ip": station["ip"],
        "url": f"http://{station['ip']}:{TEST_PARTNER_HTTP_PORT}",
    }
    _ap_stop_quietly(testbench)


@pytest.fixture
def test_partner(testbench, _test_partner_session):
    """The provisioned test partner, confirmed reachable for *this* test.

    The provisioning is session-scoped; the AP is not, and must not be
    assumed. Other tests legitimately take the radio — TestSTAMode joins an
    external network, the scan tests stop the AP to scan — and when the AP
    goes the route to 192.168.4.0/24 goes with it. The relay then fails with
    "No route to host" against a partner that is perfectly healthy, which reads
    as a broken relay or a dead device.

    Re-provisioning is not needed to recover: the partner still holds the
    credentials, so raising the same AP again brings it back by itself.
    """
    d = _test_partner_session
    status = testbench.ap_status()
    if not (status.get("active") and status.get("ssid") == d["ssid"]):
        testbench.ap_start(d["ssid"], d["password"], internet=True)

    station = _wait_for_any_station(testbench, timeout=60)
    if not station:
        # It may have forgotten the network rather than merely lost it:
        # WT-301 proves the disconnect event by asking the partner to erase its
        # credentials, which is the correct way to cause a disconnect and
        # leaves the next test with a partner that cannot rejoin anything. Hand
        # them back over the wire — it costs a reboot, not a re-flash.
        if _provision_over_serial(testbench, d["ssid"], d["password"]):
            station = _wait_for_any_station(testbench, timeout=90)
    if not station:
        pytest.skip(
            f"precondition unmet: the test partner did not rejoin '{d['ssid']}'"
        )
    # The lease can differ from the session's first one.
    url = f"http://{station['ip']}:{TEST_PARTNER_HTTP_PORT}"

    # A DHCP lease is not a served port. The lease appears the moment the
    # partner associates, and every consumer of this fixture then asks it a
    # question — so returning here handed out a partner that was still coming
    # up, and the first request timed out against hardware that was about
    # to be fine. Wait for the device to actually answer.
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            if testbench.wifi_http(f"{url}/status",
                                   timeout=5).get("status") == 200:
                break
        except Exception:
            pass
        time.sleep(3)
    else:
        pytest.skip(f"precondition unmet: the test partner joined '{d['ssid']}' "
                    f"as {station['ip']} but never served {url}/status")

    return {**d, "mac": station["mac"], "ip": station["ip"], "url": url}


def _ap_stop_quietly(testbench):
    """Bring the AP down without letting the attempt become the verdict.

    ap_stop reconfigures hostapd and dnsmasq and can exceed the driver's
    timeout when the radio is busy. Raised out of a fixture, that turns a
    truthful skip ("no test partner") into nine ERRORs about a POST — and the
    next test then meets a radio nobody finished putting away.
    """
    try:
        testbench.ap_stop()
    except Exception:
        pass


@pytest.fixture(scope="session")
def present_slot(testbench):
    """A slot that currently holds a device, with its proxy running.

    Discovered, not written down. Two classes named SLOT3 as a constant —
    "no JTAG, so `debugging` never pre-empts these" — and when the boards
    moved they failed with `SLOT3: proxy not running`, which reads as a
    broken proxy rather than an empty slot. Which slot is populated is a
    property of the bench today, not of the test.
    """
    for dev in testbench.get_devices():
        if dev.get("present") and dev.get("running"):
            return dev["label"]
    pytest.skip("precondition unmet: no slot holds a device with a running "
                "proxy")


@pytest.fixture(scope="session")
def console_dut(testbench):
    """A slot whose device answers the test partner console (`ping` → `OK pong`).

    Found by asking, not configured: the board moves between slots and a
    written-down label goes stale silently. Absence is an unmet
    precondition and says which firmware to flash.

    Asked more than once, because by the time this runs the partner has been
    through the WiFi tests and may be part-way through a reboot it was
    deliberately given — and "no slot answers" would then report absent
    hardware for a board that answers perfectly a few seconds later. That
    is the FR-030 proof skipping itself for the wrong reason.
    """
    for _ in range(4):
        slot = _find_console_slot(testbench)
        if slot:
            return slot
        time.sleep(8)
    pytest.skip(
        "precondition unmet: no slot answers `ping` with `OK pong`. Flash "
        "test-firmware/ to a slot (CI publishes it as test-partner-<target>)."
    )


def _find_console_slot(testbench):
    """One pass over the present slots, asking each whether it answers."""
    import threading
    for dev in testbench.get_devices():
        if not dev.get("present"):
            continue
        slot = dev["label"]
        result = {}

        def watch():
            try:
                result["r"] = testbench.serial_monitor(
                    slot, pattern="OK pong", timeout=8)
            except Exception:
                result["r"] = {}

        th = threading.Thread(target=watch)
        th.start()
        time.sleep(1.0)
        try:
            testbench.serial_write(slot, text="ping\n")
        except Exception:
            pass
        th.join()
        if result.get("r", {}).get("matched"):
            return slot
    return None


def _provision_over_serial(testbench, ssid: str, password: str) -> bool:
    """Hand the bench's credentials to whichever slot answers the console.

    Returns True if a device acknowledged. The device reboots itself into
    STA mode afterwards, so the caller still has to wait for the join —
    an acknowledgement is not an association.
    """
    for dev in testbench.get_devices():
        if not dev.get("present"):
            continue
        slot = dev["label"]
        try:
            since = time.time()
            testbench.serial_write(slot, text=f"wifi {ssid} {password}")
            deadline = time.time() + 6
            while time.time() < deadline:
                out = testbench.serial_output(slot, lines=200, since=since)
                if any("OK wifi stored" in ln.get("text", "")
                       for ln in out.get("lines", [])):
                    return True
                time.sleep(0.5)
        except Exception:
            continue
    return False


def _wait_for_any_station(testbench, timeout: float):
    """Poll for a station on the bench AP. Returns it, or None on timeout.

    A failed poll is not an answer. The radio is still settling into AP mode
    while this runs, and `ap_status` can take longer than its own timeout —
    at which point the exception propagated out of the session fixture and
    ERRORed nine tests that had not been attempted. Whether a station joined
    is decided by the deadline, not by one slow reply.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            stations = testbench.ap_status().get("stations") or []
        except Exception:
            stations = []
        if stations and stations[0].get("ip"):
            return stations[0]
        time.sleep(3)
    return None


def _portal_is_up(testbench) -> bool:
    """Is the partner advertising its provisioning portal?

    Scanning needs the radio, which the AP is using, so this is best-effort:
    a scan that cannot run is not evidence the portal is absent.
    """
    try:
        nets = testbench.scan().get("networks", [])
    except Exception:
        return True          # cannot tell — let provisioning try and fail loudly
    return any(n["ssid"] == TEST_PARTNER_PORTAL for n in nets)
