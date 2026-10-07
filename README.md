# Dual ILITEK Touchscreen Setup (GNOME Wayland)

Workaround for mapping two **identical** ILITEK touchscreens to two **identical** displays on GNOME Wayland, where the desktop environment cannot distinguish them out of the box.

## Problem

- Hardware: 2× ILITEK touchscreens (all report USB `222A:0001`) and 2× WCS YF13T displays (all report EDID `WCS / YF13T / demoset-1`)
- GNOME/Mutter's touchscreen mapping keys dconf by `vendor:product`, and matches outputs by EDID `vendor/product/serial`. With identical hardware on both sides, there is no way to distinguish device A from B, or output A from B.
- Symptoms observed:
  - Both touches land on the same display
  - Touch coordinates are offset (touchscreens span the full virtual screen instead of a single display)
  - `LIBINPUT_IGNORE_DEVICE`, udev rules, and GNOME Settings panel are all insufficient on their own

## Solution Overview

Four layers combined:

| # | Layer | What it does |
|---|-------|--------------|
| 1 | **uinput proxy** (systemd service) | Reads raw events from one physical touchscreen and re-emits them from a virtual device with a different USB product ID (`222A:0002`), so GNOME sees two *different* devices |
| 2 | **udev rule** `LIBINPUT_IGNORE_DEVICE=1` | Hides the original USB 8.2 touchscreen from libinput, so only the virtual copy is visible |
| 3 | **EDID override** (kernel cmdline + firmware blob) | Rewrites the bottom display's EDID serial from `demoset-1` to `demoset-2`, so GNOME sees two *different* outputs |
| 4 | **`LIBINPUT_CALIBRATION_MATRIX`** | Since Mutter still routes both touches to the full virtual screen, this per-device matrix compresses each touchscreen's Y range to cover only its own display |

## Quick Alternative: `calibrate_layout.py` (matrix only)

Layer 4 on its own is enough whenever Mutter leaves both touchscreens unmapped (each one spans the whole virtual screen). That is the default with this hardware as long as no dconf mapping is set: Mutter's `GetDeviceMapping` reports "Device is not mapped to any output" for both. `calibrate_layout.py` installs only that layer, with nothing hardcoded:

```bash
python3 calibrate_layout.py            # asks for sudo, then asks you to touch one display
python3 calibrate_layout.py --dry-run  # show the detected layout and matrices only
python3 calibrate_layout.py --remove   # delete the udev rule
```

- Reads the current arrangement from Mutter (Settings → Displays) and derives one matrix per display, so any position, resolution or scale works (rotation is not supported)
- Finds out which USB port belongs to which display by asking you to touch it
- Writes `/etc/udev/rules.d/99-ilitek-touchscreen.rules` and rebinds `usbhid` so the matrices apply immediately — no proxy, no EDID override, no reboot

Re-run it after changing the display arrangement or scale, or after moving a touchscreen to another USB port. Use it **instead of** `install.sh`, not together with it (both write the same rules file).

## Hardware Assumptions

| | Top display | Bottom display |
|---|-------------|----------------|
| HDMI connector | `HDMI-A-3` | `HDMI-A-7` |
| USB port for touchscreen | `1-6.2` | `1-8.2` |
| Effective resolution (logical) | 1536×864 (1920×1080 @ 1.25×) | 1536×864 (1920×1080 @ 1.25×) |
| Global Y range | 0 to 864 | 864 to 1728 |

If your hardware differs, edit:
- `files/touch_proxy.py` → `TARGET_USB_PHYS_PREFIX`
- `files/99-ilitek-touchscreen.rules` → `KERNELS==` values and calibration matrices
- `install.sh` → `drm.edid_firmware=HDMI-A-7:...`

## Project Files

```
touch-remap/
├── README.md                  # This file
├── install.sh                 # Automated installer
├── uninstall.sh               # Reverter
├── calibrate_layout.py        # Matrix-only setup that follows the current display arrangement
├── create_edid_override.py    # Generates the EDID firmware blob from the current HDMI-7 EDID
├── files/
│   ├── touch_proxy.py         # uinput proxy service
│   ├── touch-proxy.service    # systemd unit
│   ├── 99-ilitek-touchscreen.rules  # udev rules
│   └── hdmi7_unique.bin       # EDID override (serial demoset-2)
└── docs/
    └── (diagnostics / reference data)
```

## Installation

```bash
git clone <this repo>
cd touch-remap
bash install.sh
sudo reboot   # required for EDID override to take effect
```

After reboot, the saved monitor arrangement in `~/.config/monitors.xml` may reset (HDMI-7's EDID serial changed). Re-arrange displays in Settings → Displays if needed.

## Uninstall

```bash
bash uninstall.sh
sudo reboot
```

## How Each Layer Works

### 1. uinput proxy (`touch_proxy.py`)

Runs as a systemd service at boot. Finds the ILITEK touchscreen at USB `1-8.2`, opens it **without `EVIOCGRAB`** (grab crashes this ILITEK controller with USB error `-71`), creates a uinput virtual device cloning the ABS ranges but reporting `vendor=0x222a, product=0x0002`, and forwards every event. The original USB 8.2 is hidden from libinput by the udev rule, so GNOME only sees the virtual copy.

### 2. EDID override (`hdmi7_unique.bin`)

Copy of the current HDMI-7 EDID with the Display Product Serial descriptor changed from `demoset-1` to `demoset-2`, and base-block checksum recomputed. Loaded via kernel cmdline:
```
drm.edid_firmware=HDMI-A-7:edid/hdmi7_unique.bin
```
Requires `update-initramfs` so the blob is available before DRM init.

### 3. udev rule (`99-ilitek-touchscreen.rules`)

```
# Ignore raw USB 8.2 (handled by proxy)
SUBSYSTEM=="input", ATTRS{idVendor}=="222a", ATTRS{idProduct}=="0001", KERNELS=="1-8.2",
    ENV{LIBINPUT_IGNORE_DEVICE}="1"

# TOP touchscreen: compress Y to the top half of virtual screen
SUBSYSTEM=="input", ATTRS{idVendor}=="222a", ATTRS{idProduct}=="0001", KERNELS=="1-6.2",
    ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 0.5 0"

# Virtual BOTTOM device: shift Y to the bottom half
SUBSYSTEM=="input", ATTRS{id/vendor}=="222a", ATTRS{id/product}=="0002",
    ENV{ID_INPUT_WIDTH_MM}="309", ENV{ID_INPUT_HEIGHT_MM}="174",
    ENV{LIBINPUT_CALIBRATION_MATRIX}="1 0 0 0 0.5 0.5"
```

Matrix format is row-major 2×3: `[a b c d e f]` where `x' = a·x + b·y + c`, `y' = d·x + e·y + f`, applied to normalized 0..1 coordinates.

- Top: `y' = 0.5·y + 0` — raw 0..1 becomes 0..0.5, which after Mutter's full-screen mapping lands in global 0..864 (top display)
- Bottom: `y' = 0.5·y + 0.5` — raw 0..1 becomes 0.5..1.0, mapping to global 864..1728 (bottom display)

### 4. dconf (`org.gnome.desktop.peripherals.touchscreens`)

```
[222A:0001]
output=['HDMI-3', 'WCS', 'YF13T', 'demoset-1']

[222A:0002]
output=['HDMI-7', 'WCS', 'YF13T', 'demoset-2']
```

Set for completeness, though Mutter's actual per-output mapping wasn't fully honored in testing — the calibration matrices are what make it work in practice.

## Verification

```bash
# EDID override active?
cat /sys/class/drm/card2-HDMI-A-7/edid | strings | grep demoset-2

# Proxy running?
systemctl status touch-proxy.service

# Virtual device present?
grep -B1 -A3 "ILITEK-TP-BOTTOM" /proc/bus/input/devices

# Calibration matrices applied?
TOP_EV=$(grep -B3 "usb-0000:80:14.0-6.2/input0" /proc/bus/input/devices | grep -oP 'event\d+' | head -1)
BOT_EV=$(grep -B3 "ILITEK-TP-BOTTOM" /proc/bus/input/devices | grep -oP 'event\d+')
udevadm info --query=property --name=/dev/input/$TOP_EV | grep LIBINPUT
udevadm info --query=property --name=/dev/input/$BOT_EV | grep LIBINPUT
```

## Known Quirks

- **`EVIOCGRAB` kills the ILITEK controller** — Triggering an exclusive grab causes the device to reset with USB `-71` and never come back until a hub rebind. The proxy therefore reads without grabbing and relies on `LIBINPUT_IGNORE_DEVICE` to prevent the original events from reaching the compositor.
- **Mutter `GetDeviceMapping` D-Bus API misreports** — Returns "Device is not mapped to any output" even when touches route correctly. Don't rely on this API to verify the setup; test with actual touches.
- **`monitors.xml` resets after EDID change** — The saved display arrangement references the old EDID serial; after the first reboot with the override, GNOME creates a fresh arrangement.
- **udev rule timing on boot** — libinput picks up properties at device-creation time. If the virtual device is created by the proxy before udev settles, the calibration matrix may not apply. The service uses `After=systemd-udevd.service` to reduce the risk, but a `sudo udevadm trigger --subsystem-match=input` after login is a safety net.

## Regenerating the EDID Override for Different Hardware

```bash
python3 create_edid_override.py
# Reads /sys/class/drm/card2-HDMI-A-7/edid, writes /tmp/hdmi7_unique.bin
# Change the EDID path / new serial string at the top of the script as needed
sudo cp /tmp/hdmi7_unique.bin /lib/firmware/edid/hdmi7_unique.bin
sudo update-initramfs -u
```

## Tested Environment

- Ubuntu 24.04, kernel 6.17.0-20-generic
- GNOME Shell 46.0 on Wayland
- Mutter 46.2
- libinput (default Ubuntu package)
