#!/usr/bin/env python3
"""
Touch input proxy: reads from one ILITEK touchscreen (USB 8.2) and forwards
events to a virtual device with a different product ID.

The original USB 8.2 device is IGNORED by libinput via udev rule
(LIBINPUT_IGNORE_DEVICE=1), so only the virtual device reaches GNOME/Mutter.

USB 6.2 -> kept as-is    (222A:0001) -> HDMI-3 (top, display 1)
USB 8.2 -> ignored by libinput, proxied to virtual (222A:0002) -> HDMI-7 (bottom, display 2)
"""

import os
import sys
import time
import signal
import struct
import fcntl
import errno
import select

# ioctl constants
UI_SET_EVBIT = 0x40045564
UI_SET_ABSBIT = 0x40045567
UI_SET_KEYBIT = 0x40045565
UI_SET_PROPBIT = 0x4004556e
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502

# Event types
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
EV_MSC = 0x04

# input_event struct
if struct.calcsize('l') == 8:
    EVENT_FORMAT = 'llHHi'
else:
    EVENT_FORMAT = 'IIHHi'
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# ABS codes
ABS_X = 0x00
ABS_Y = 0x01
ABS_MT_SLOT = 0x2f
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

BTN_TOUCH = 0x14a
INPUT_PROP_DIRECT = 0x01

TARGET_USB_PHYS_PREFIX = "usb-0000:80:14.0-8.2"
VIRTUAL_PRODUCT_ID = 0x0002
VIRTUAL_NAME = "ILITEK ILITEK-TP-BOTTOM"

UINPUT_USER_DEV_FORMAT = '80sHHHHi' + 'i' * 64 * 4


def find_target_device():
    input_dir = '/sys/class/input'
    for entry in sorted(os.listdir(input_dir)):
        if not entry.startswith('event'):
            continue
        try:
            name_path = os.path.join(input_dir, entry, 'device', 'name')
            phys_path = os.path.join(input_dir, entry, 'device', 'phys')
            if not os.path.exists(name_path) or not os.path.exists(phys_path):
                continue
            with open(name_path) as f:
                name = f.read().strip()
            with open(phys_path) as f:
                phys = f.read().strip()
            if 'ILITEK' in name and 'Mouse' not in name and phys.startswith(TARGET_USB_PHYS_PREFIX):
                return f'/dev/input/{entry}', name, phys
        except (IOError, OSError):
            continue
    return None, None, None


def read_abs_info(fd, code):
    buf = bytearray(24)
    request = 0x80184540 + code
    try:
        fcntl.ioctl(fd, request, buf)
        value, minimum, maximum, fuzz, flat, resolution = struct.unpack('iiiiii', buf)
        return minimum, maximum, fuzz, flat, resolution
    except OSError:
        return 0, 0, 0, 0, 0


def create_virtual_device(source_fd):
    uinput_fd = os.open('/dev/uinput', os.O_WRONLY | os.O_NONBLOCK)

    for ev_type in [EV_SYN, EV_KEY, EV_ABS, EV_MSC]:
        fcntl.ioctl(uinput_fd, UI_SET_EVBIT, ev_type)

    fcntl.ioctl(uinput_fd, UI_SET_KEYBIT, BTN_TOUCH)

    abs_codes = [ABS_X, ABS_Y, ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID]
    abs_info = {}
    for code in abs_codes:
        fcntl.ioctl(uinput_fd, UI_SET_ABSBIT, code)
        abs_info[code] = read_abs_info(source_fd, code)

    fcntl.ioctl(uinput_fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)

    absmax = [0] * 64
    absmin = [0] * 64
    absfuzz = [0] * 64
    absflat = [0] * 64

    for code, (minimum, maximum, fuzz, flat, _) in abs_info.items():
        if code < 64:
            absmin[code] = minimum
            absmax[code] = maximum
            absfuzz[code] = fuzz
            absflat[code] = flat

    name_bytes = VIRTUAL_NAME.encode('utf-8')[:79] + b'\x00'
    name_bytes = name_bytes.ljust(80, b'\x00')

    dev_data = struct.pack(
        UINPUT_USER_DEV_FORMAT,
        name_bytes,
        0x03, 0x222a, VIRTUAL_PRODUCT_ID, 0x0110, 0,
        *absmax, *absmin, *absfuzz, *absflat,
    )

    os.write(uinput_fd, dev_data)
    fcntl.ioctl(uinput_fd, UI_DEV_CREATE)
    return uinput_fd


def run_proxy():
    event_path = None
    for attempt in range(60):
        event_path, name, phys = find_target_device()
        if event_path:
            break
        print(f"Waiting for ILITEK on USB 8.2... ({attempt + 1}/60)", flush=True)
        time.sleep(1)

    if not event_path:
        print("ERROR: Could not find ILITEK touchscreen on USB 8.2", flush=True)
        return False

    print(f"Found: {name} at {event_path} ({phys})", flush=True)

    source_fd = None
    uinput_fd = None

    try:
        # Open read-only, NO grab (grab crashes this ILITEK controller)
        source_fd = os.open(event_path, os.O_RDONLY)
        print(f"Opened {event_path} (fd={source_fd})", flush=True)

        uinput_fd = create_virtual_device(source_fd)
        print(f"Virtual device created: {VIRTUAL_NAME} (222a:{VIRTUAL_PRODUCT_ID:04x})", flush=True)

        time.sleep(1)
        print("Proxying touch events (no-grab mode)...", flush=True)

        while True:
            r, _, _ = select.select([source_fd], [], [], 5.0)
            if not r:
                # Verify device still exists
                if not os.path.exists(event_path):
                    print("Device path gone, will retry", flush=True)
                    return True
                continue

            try:
                data = os.read(source_fd, EVENT_SIZE * 64)
                if not data:
                    print("EOF on source device", flush=True)
                    return True

                offset = 0
                while offset + EVENT_SIZE <= len(data):
                    os.write(uinput_fd, data[offset:offset + EVENT_SIZE])
                    offset += EVENT_SIZE

            except OSError as e:
                if e.errno == errno.EAGAIN:
                    continue
                elif e.errno == errno.ENODEV:
                    print("Device disconnected (ENODEV)", flush=True)
                    return True
                else:
                    print(f"OS error: {e}", flush=True)
                    return True

    except Exception as e:
        print(f"Error: {e}", flush=True)
        return True
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if uinput_fd is not None:
            try:
                fcntl.ioctl(uinput_fd, UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(uinput_fd)


def main():
    running = True

    def signal_handler(signum, frame):
        nonlocal running
        running = False
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    print("Touch proxy starting (no-grab mode)...", flush=True)

    retry_count = 0
    while running and retry_count < 20:
        should_retry = run_proxy()
        if not should_retry:
            break
        retry_count += 1
        wait = min(5 * retry_count, 30)
        print(f"Retrying in {wait}s... ({retry_count}/20)", flush=True)
        time.sleep(wait)

    print("Touch proxy exiting.", flush=True)


if __name__ == '__main__':
    main()
