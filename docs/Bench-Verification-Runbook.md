# Bench Verification Runbook

How to prove a testbench works end to end, and how to diagnose and fix a bench
whose slots point at the wrong USB port.

Background on each service is in the [User Manual](Harness-User-Manual.md),
notably [§16 Validating the bench](Harness-User-Manual.md#16-validating-the-bench)
and [§17 Troubleshooting](Harness-User-Manual.md#17-troubleshooting). This file
is the order to do things in and what each result means.

Throughout, `BENCH` is the bench's IP address (never a `.local` name, which does
not resolve from a devcontainer), and `SLOT` is the slot under test:

```bash
BENCH=<bench-ip>
SLOT=SLOT1
```

---

## 1. Host tier: no hardware

Proves the code is sane. It does not prove the bench works.

```bash
pip install -r requirements-dev.txt
pytest pytest/host/ -q          # expect all pass
ruff check .                    # CI pins the ruff version; match it
```

## 2. Reach the bench

```bash
curl -s -m 8 http://$BENCH:8080/api/info
curl -s -m 8 http://$BENCH:8080/api/devices | jq '.slots[] | select(.present)'
```

If this times out, tell the three causes apart before changing anything:

| Host browser opens `http://$BENCH:8080/api/devices` | `curl` from the container | Meaning |
|---|---|---|
| Works | Works | Fine |
| Works | Times out | The container cannot reach the LAN (VPN, Docker network) |
| Times out | Times out | The bench is down: on the Pi, `systemctl status rfc2217-portal` and `hostname -I` |

## 3. Know what is in every slot before touching it

Three checks, least intrusive first:

1. **`/api/devices`**: `present`, `devnode`, `detected_chip`, `usb_devices`.
   `ttyACM*` is a native-USB ESP32; `ttyUSB*` is behind a bridge chip (CP210x,
   CH340), which has no JTAG and so never gets a `detected_chip`.
2. **What it is running**: the bench records every slot whether or not anyone
   is watching. This is passive:
   ```bash
   curl -s "http://$BENCH:8080/api/serial/output?slot=$SLOT&lines=200" | jq -r '.lines[].text'
   ```
   Look for `Project name:` and `App version:` in the boot banner. The buffer
   belongs to the *slot*, not the board, so after a swap check the timestamps:
   older lines came from the previous board.
3. **What the silicon is**: this reboots the board.
   ```bash
   curl -s -X POST http://$BENCH:8080/api/chip/info \
        -H 'Content-Type: application/json' -d "{\"slot\":\"$SLOT\"}" | jq 'del(.output)'
   ```
   Trust this over the label on the board: boards sold or labelled as one
   variant can carry another. A non-ESP device (a bare USB-serial adapter, for
   example) fails with `Invalid head of packet (0x00)`.

On the Pi, `lsusb -t` and `ls -l /dev/serial/by-id/` name every device and
show which bus and hub port it is on.

## 4. Back up before flashing anything

`--run-dut` and the partner setup **overwrite the board's firmware**, and the
bench does not keep a copy. If the board holds anything worth keeping, read
the whole flash first:

```bash
PORT=<the slot's tcp_port from /api/devices>
esptool --port rfc2217://$BENCH:$PORT read-flash 0 ALL backup-$SLOT.bin
# restore later:
esptool --port rfc2217://$BENCH:$PORT write-flash 0 backup-$SLOT.bin
```

(`POST /api/flash/read` does the same for a region, returned as base64.)

## 5. Put the test partner on a board

The bench suite needs a board running `test-firmware/`, which is built only for
the **targets in `.github/workflows/test-firmware.yml`** (esp32c3 and esp32s3
at the time of writing). `debug-test/` has its own target list. A board of any
other chip cannot be the partner.

Get the image. CI artifacts need `gh auth login`; release assets do not:

```bash
TARGET=esp32s3          # or esp32c3
VERSION=<release tag, e.g. v1.0.1>
D=/tmp/partner && mkdir -p $D && cd $D
curl -sLO https://github.com/SensorsIot/Embedded-AI-Harness/releases/download/$VERSION/test-partner-$TARGET.tar.gz
tar xzf test-partner-$TARGET.tar.gz
```

Flash it through the bench and check that it answers:

```bash
cd pytest && python3 - "http://$BENCH:8080" /tmp/partner/test-partner-$TARGET $SLOT $TARGET <<'EOF'
import sys, time
from testbench_driver import TestbenchDriver
from conftest import _flash_args_offsets, _dut_answers
url, image_dir, slot, chip = sys.argv[1:5]
wt = TestbenchDriver(url)
r = wt.flash(slot, dict(_flash_args_offsets(image_dir)), chip=chip)
print({k: v for k, v in r.items() if k != "output"})
time.sleep(10)
print("answers ping:", _dut_answers(wt, slot, timeout=15))
EOF
```

The suite can restore the partner itself (`WT_TEST_PARTNER_IMAGE`), but only
on a slot with a `detected_chip`. On a bench with the slot fault in §7 that
never happens, so flash it by hand.

## 6. Run the bench suite

Without board tests: nothing is flashed, but boards are reset and the Pi's AP
and broker start and stop. Takes a few minutes:

```bash
pytest pytest/testbench_test.py --wt-url http://$BENCH:8080 -q -rfEs
```

With board tests: this flashes the partner slot and takes roughly twice as
long. The output only appears at the end; follow progress in the bench's
activity log meanwhile:

```bash
WT_TEST_PARTNER_IMAGE=/tmp/partner/test-partner-$TARGET \
  pytest pytest/testbench_test.py --wt-url http://$BENCH:8080 --run-dut -q -rfEs
curl -s "http://$BENCH:8080/api/log?limit=20" | jq -r '.entries[] | "\(.ts[11:19]) \(.msg)"'
```

Do not pass `--timeout` unless `pytest-timeout` is installed; pytest refuses to
start on an unknown flag.

### Reading the results

| Skip reason | Meaning |
|---|---|
| `Requires a DUT (use --run-dut)` | Expected without `--run-dut` |
| `no RTL-SDR dongle available` | No SDR fitted; expected |
| `no slot answers ping` | No partner running; see §5 |
| `no chip-detected partner present`, `no present slot to debug` | Expected on a bridge-chip board; on a native-USB board, suspect §7 |
| `WT-1800 (flash) did not pass` | Knock-on from a WT-1800 failure; fix that first |
| `the test partner was provisioned but never joined` | The partner cannot hear the bench's AP; see §8 |

Failures that all mention chip detection or `esp_usb_jtag` point to §7.
Station-mode failures (`sta_join … within 15s`, captive portal, HTTP relay
via STA) with everything else green point to §8.

Not covered by the suite: RF without the optional SDR or signal generator
fitted, and the AI loop itself (`/define` → `/harness` → `/commission` →
`/build` on a real project).

## 7. Slot recognition: when the bench points at the wrong USB port

### Symptoms

- Every JTAG and chip-detection test fails: WT-1400–1406, WT-1704–1709,
  WT-1800, WT-2201/2202/2207, per-slot isolation.
- `detected_chip` stays `null` for a native-USB board.
- Starting a debug session by hand fails like this:
  ```bash
  curl -s -X POST http://$BENCH:8080/api/debug/start \
       -H 'Content-Type: application/json' -d "{\"slot\":\"$SLOT\",\"chip\":\"esp32s3\"}"
  ```
  ```
  adapter usb location: 1-1.3
  Error: esp_usb_jtag: could not find or open device!
  ```
- `slots_configured` is larger than the hub you use, and boards on an external
  hub show up as if they were on the Pi's own ports.

### Diagnose: on the Pi

```bash
lsusb -t                                            # which bus is the hub on?
lsusb -d 303a:1001 -v 2>/dev/null | grep -E "^Bus|iSerial"
udevadm info -q property -n /dev/ttyACM0 | grep -E "ID_PATH=|DEVPATH="
ls -l /etc/rfc2217/                                 # testbench.json or old workbench.json?
```

The fault is confirmed when the bus numbers disagree. For example:

```
Bus 003 Device 007: ID 303a:1001 Espressif USB JTAG/serial debug unit
DEVPATH=/devices/platform/soc/fe980000.usb/usb3/3-1/3-1.3/3-1.3:1.0/tty/ttyACM0
ID_PATH=platform-fe980000.usb-usb-0:1.3:1.0
```

The board is at `3-1.3`, on bus 3. On a Pi 4 that is the USB-C port, `dwc2`,
so a hub plugged into USB-C triggers it. But OpenOCD was told `1-1.3`.

### Cause

udev's `ID_PATH` writes the USB part as a literal `usb-0:<path>` on every bus.
Slots were matched against `ID_PATH`, so a board at `3-1.3` landed in the slot
for `1-1.3` (port 3 of the Pi's own bus-1 hub). The debug code then turned that
slot back into `adapter usb location 1-1.3`, where nothing is plugged in. The
device listing for a slot also assumed bus 1.

### Fix

- `pi/usb_slots.py` (new): reads the bus from `DEVPATH`, and matches ports on
  a boundary, so `1.1` no longer matches `1.10`.
- `pi/portal.py`: slot matching and `_scan_usb_devices` use it.
- `pi/rfc2217-learn-slots`: writes the real bus into generated configs.
- `pi/install.sh`: installs `usb_slots.py`.
- `pytest/host/test_usb_slots.py`: host tests built from the paths above.

Configs written by the old `rfc2217-learn-slots` (every prefix `0:…`) keep the
old matching, so pinned slots on existing benches do not move. If such a
config has a hub off bus 1, regenerate it with `sudo rfc2217-learn-slots`.

### Deploy a portal change to the Pi

Nothing is built on GitHub for the bench: it is Python, copied into
`/usr/local/bin`. `install.sh --update` copies from the Pi's **local clone**,
so if that clone is not on the branch with the change, it reinstalls the old
code and the portal keeps running it.

With git, in the Pi's clone:

```bash
git status                      # must be clean, or checkout refuses to switch
git fetch <repo-url> <branch> && git checkout -B <branch> FETCH_HEAD
git log --oneline -1            # must show the commit you expect
cd pi && sudo bash install.sh --update
sudo systemctl restart rfc2217-portal
```

Or fetch just the changed files, which avoids the clone entirely:

```bash
RAW=https://raw.githubusercontent.com/<owner>/<repo>/<branch>/pi
cd /tmp
for f in portal.py usb_slots.py rfc2217-learn-slots; do curl -fsSLO $RAW/$f; done
sudo cp portal.py /usr/local/bin/rfc2217-portal
sudo cp usb_slots.py rfc2217-learn-slots /usr/local/bin/
sudo chmod +x /usr/local/bin/rfc2217-learn-slots
sudo systemctl restart rfc2217-portal
```

Either way, prove what is running: `md5sum /usr/local/bin/rfc2217-portal`
on the Pi must equal `md5sum pi/portal.py` in your checkout of that branch.

### Verify

```bash
curl -s http://$BENCH:8080/api/devices | \
  jq -r '.slots[] | select(.present) | "\(.label) \(.devnode) \(.detected_chip) \(.usb_devices)"'
```

A native-USB board on the external hub now has a `detected_chip` and a
`usb_devices` entry (`303a:1001`), and sits in a slot of its own hub.

**Slot numbers change after the fix:** on a Pi 4 with a hub on USB-C, SLOT1–4
are the Pi's own USB-A ports and SLOT5–8 are the hub. Update anything that
hard-codes a slot label or port.

## 8. WiFi: the bench and the partner cannot hear each other

The bench's AP and the partner both transmit at **5 dBm** on purpose: at a
few centimetres, full power only drives receivers into compression. That
leaves little margin, and some benches lose it entirely.

### Symptoms

Either direction can fail on its own.

| Direction | Seen as |
|---|---|
| Partner cannot hear the bench | `provisioned but never joined` skips; the partner's log shows `STA disconnect (reason=201)` (no AP found), 20 retries; a phone does not list the bench's AP |
| Bench cannot hear the partner | `sta_join: Failed to connect to 'WB-Test-Setup'` or `'WT-PARTNER-AP'`; `/tmp/wifi-tester/wpa_supplicant.log` on the Pi shows `CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16` (authentication timed out: the partner's replies never arrived) |

`/api/wifi/ap_status` says `active` throughout, and hostapd logs
`AP-ENABLED`: software reports nothing wrong.

### Measure both directions

Each radio reports what it hears. Neither alone can tell "not transmitting"
from "not hearing".

```bash
# Partner -> bench: the bench scans (its AP must be stopped)
curl -s http://$BENCH:8080/api/wifi/scan | jq '.networks[] | select(.ssid|test("^W[BT]-"))'

# Bench -> partner: start an AP, then ask the partner what it hears.
# Wait for a fresh "OK scan end": /api/serial/monitor also matches old
# buffered lines, so poll /api/serial/output with since= instead.
curl -s -X POST http://$BENCH:8080/api/wifi/ap_start \
     -H 'Content-Type: application/json' -d '{"ssid":"WT-DIAG","password":"testpass123"}'
T=$(date +%s)
curl -s -X POST http://$BENCH:8080/api/serial/write \
     -H 'Content-Type: application/json' -d "{\"slot\":\"$SLOT\",\"text\":\"scan\"}"
sleep 5; curl -s "http://$BENCH:8080/api/serial/output?slot=$SLOT&since=$T" | jq -r '.lines[].text'
```

At bench distance, around -20 to -40 dBm in each direction is healthy. A
large gap between the two directions means something near one radio is in
the way.

### Causes found so far

- **A metal enclosure** (for example the DeskPi Pro) encloses the Pi 4's
  onboard antenna. At 5 dBm the bench's AP did not get out at all. With the
  Pi out of the case, the partner heard it at -20 dBm.
- **The partner's power supply.** Fed from an enclosure's front-panel hub,
  the partner reported its AP up (`status` → `ap_mode=1`) and still heard
  other networks, but its AP came and went for the Pi and a phone alike.
  Powered on its own and plugged into one of the Pi's own ports, it held
  -28 to -30 dBm at the Pi. A board that receives but stops transmitting
  points at its supply first.
- **Placement of the partner.** Its PCB antenna (the zig-zag trace at one end
  of the module) needs a clear view of the Pi: not against metal, under a
  hub, or wrapped in cables.
- **The radio's transmit-power control.** The bench asks for 5 dBm with
  `iw … set txpower fixed`. Some Pi radios refuse it and keep transmitting at
  full power; others accept it. On the latter the margin is genuinely 5 dBm,
  and the setting **outlives AP mode**: `iw dev` showed a station-mode
  `wlan0` still at 5 dBm. Check with `iw dev wlan0 info | grep txpower`.
- **Suspected, not confirmed: a USB 3 device beside the Pi's antenna.** USB 3
  is a known 2.4 GHz noise source. The Pi kept hearing strong networks
  (-13 dBm) while losing a weak partner, which fits, but the partner's power
  supply turned out to explain most of it. If a USB 3 device sits near the
  antenna, try a USB 2 port or more distance.

### Remedies

- Run the Pi uncased or in a plastic case, and move USB 3 devices away.
- Raise the bench AP's power: set `WIFI_AP_TXPOWER_MBM` (mBm; 1300 = 13 dBm)
  in the portal's service environment and restart it.
- Raise the partner's power, which needs a partner build that has the
  `txpower` command:
  ```bash
  # once, by hand (quarter-dBm: 52 = 13 dBm; stored, survives reboots)
  curl -s -X POST http://$BENCH:8080/api/serial/write \
       -H 'Content-Type: application/json' -d "{\"slot\":\"$SLOT\",\"text\":\"txpower 52\"}"
  # or on every suite run
  WT_TEST_PARTNER_TXPOWER=52 pytest pytest/testbench_test.py ...
  ```
  `txpower` alone reports the current setting; `txpower default` removes the
  override.

If the partner's AP is visible to a phone across the room but not to the Pi
next to it, the problem is at the Pi, not the partner.

### Open issue: the bench as a station times out authenticating

Seen on a Pi 4 (Broadcom `brcmfmac`). With both directions measured healthy
(partner → Pi -30 dBm, Pi → partner -20 dBm), the Pi still could not join the
partner's open portal, while a phone joined it at once:

```
wlan0: Trying to associate with <partner-ap-mac> (SSID='WB-Test-Setup' freq=2412 MHz)
wlan0: CTRL-EVENT-ASSOC-REJECT bssid=00:00:00:00:00:00 status_code=16
```

The all-zeros BSSID means the Pi's own driver gave up: status 16 is an
authentication timeout, a request with no reply heard. The partner logged no
attempt at all.

Ruled out: signal (above), the Pi's transmit power (also failed at 31 dBm),
a leftover `hostapd` (none running), a second interface (only `wlan0`, type
managed, right channel), and the regulatory domain (`country CH`, channel 1
allowed). The same tests passed earlier on the same bench, so the fault is
intermittent rather than permanent.

Not yet tried, in order:

1. A clean boot, then a station join **before** the bench raises any AP, to
   test whether the AP → station switch is what breaks it.
2. `sudo modprobe -r brcmfmac && sudo modprobe brcmfmac`, then the join.
3. `wpa_supplicant -dd` output for one attempt (the bench's config is
   `/tmp/wifi-tester/wpa_supplicant.conf`), and `dmesg | grep brcmf` right
   after it.

Until it is resolved, expect these to fail on an affected bench and read them
as this issue: WT-401, WT-404, WT-405, WT-506 (HTTP via STA), and the captive
portal tests WT-2101, WT-2103, WT-2104.
