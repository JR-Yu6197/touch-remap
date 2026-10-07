#!/usr/bin/env python3
"""
Map each touchscreen to its own display, following the current GNOME display
arrangement (Settings -> Displays).

Lightweight alternative to install.sh: no uinput proxy, no EDID override, no
reboot. It relies on a single fact: Mutter leaves these touchscreens unmapped
(identical devices, no size/EDID match), so each one spans the whole virtual
screen. A per-USB-port LIBINPUT_CALIBRATION_MATRIX then squeezes every
touchscreen into the rectangle of its own display.

Usage (as the desktop user, inside the GNOME session):
    python3 calibrate_layout.py            # detect, write udev rule, re-probe
    python3 calibrate_layout.py --dry-run  # only show layout and matrices
    python3 calibrate_layout.py --remove   # delete the udev rule

Re-run after changing the display arrangement, resolution or scale, or after
moving a touchscreen to another USB port.
"""

import argparse
import glob
import json
import os
import select
import shutil
import struct
import subprocess
import sys
import time

RULES_PATH = '/etc/udev/rules.d/99-ilitek-touchscreen.rules'
USBHID_DRIVER = '/sys/bus/usb/drivers/usbhid'

EV_ABS = 0x03
EVENT = struct.Struct('llHHi')

TOUCH_TIMEOUT = 30
REPROBE_TIMEOUT = 10


def fmt(value):
    return f'{value:.6g}'


# ---------------------------------------------------------------------------
# Display layout (runs as the desktop user)
# ---------------------------------------------------------------------------

def read_layout():
    """Return the logical monitors, sorted top-to-bottom then left-to-right,
    each with the calibration matrix that confines a touchscreen to it."""
    from gi.repository import Gio

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    _, monitors, logical_monitors, props = bus.call_sync(
        'org.gnome.Mutter.DisplayConfig',
        '/org/gnome/Mutter/DisplayConfig',
        'org.gnome.Mutter.DisplayConfig',
        'GetCurrentState',
        None, None, Gio.DBusCallFlags.NONE, -1, None,
    ).unpack()

    current_mode = {}
    for spec, modes, _ in monitors:
        for mode in modes:
            if mode[6].get('is-current'):
                current_mode[spec[0]] = (mode[1], mode[2])

    # layout-mode 1 = logical (sizes divided by scale), 2 = physical
    logical_layout = props.get('layout-mode', 1) == 1

    layout = []
    for x, y, scale, transform, primary, specs, _ in logical_monitors:
        connector = specs[0][0]
        if transform != 0:
            sys.exit(f'ERROR: {connector} is rotated/flipped (transform {transform}); '
                     'only the normal orientation is supported.')
        if connector not in current_mode:
            sys.exit(f'ERROR: no active mode found for {connector}')
        width, height = current_mode[connector]
        if logical_layout:
            width, height = round(width / scale), round(height / scale)
        layout.append({'connector': connector, 'x': x, 'y': y,
                       'w': width, 'h': height, 'primary': primary})

    stage_w = max(m['x'] + m['w'] for m in layout)
    stage_h = max(m['y'] + m['h'] for m in layout)
    for m in layout:
        m['matrix'] = ' '.join(fmt(v) for v in (
            m['w'] / stage_w, 0, m['x'] / stage_w,
            0, m['h'] / stage_h, m['y'] / stage_h))

    layout.sort(key=lambda m: (m['y'], m['x']))
    label_positions(layout)
    return layout, (stage_w, stage_h)


def label_positions(layout):
    names = None
    if len(layout) == 2:
        if layout[0]['x'] == layout[1]['x']:
            names = ('TOP', 'BOTTOM')
        elif layout[0]['y'] == layout[1]['y']:
            names = ('LEFT', 'RIGHT')
    for i, m in enumerate(layout):
        m['label'] = names[i] if names else f"at {m['x']},{m['y']}"


def describe(m):
    return f"{m['label']} display ({m['connector']}, {m['w']}x{m['h']} at {m['x']},{m['y']})"


# ---------------------------------------------------------------------------
# Touchscreen discovery (sysfs + udev)
# ---------------------------------------------------------------------------

def udev_properties(node):
    out = subprocess.run(['udevadm', 'info', '--query=property', f'--name={node}'],
                         capture_output=True, text=True).stdout
    return dict(line.split('=', 1) for line in out.splitlines() if '=' in line)


def read_attr(path):
    with open(path) as f:
        return f.read().strip()


def usb_parent(sys_path):
    """Walk up from an input device to the USB device that owns it."""
    path = sys_path
    while path != '/':
        if os.path.exists(os.path.join(path, 'idVendor')):
            return path
        path = os.path.dirname(path)
    return None


def find_touchscreens():
    found = []
    for entry in sorted(glob.glob('/sys/class/input/event*')):
        node = f'/dev/input/{os.path.basename(entry)}'
        if udev_properties(node).get('ID_INPUT_TOUCHSCREEN') != '1':
            continue
        usb_path = usb_parent(os.path.realpath(os.path.join(entry, 'device')))
        if not usb_path:
            continue  # not USB (e.g. a uinput virtual device)
        found.append({
            'node': node,
            'name': read_attr(os.path.join(entry, 'device', 'name')),
            'usb': os.path.basename(usb_path),
            'usb_path': usb_path,
            'vendor': read_attr(os.path.join(usb_path, 'idVendor')),
            'product': read_attr(os.path.join(usb_path, 'idProduct')),
        })
    return found


def wait_for_touch(devices, timeout=TOUCH_TIMEOUT):
    """Return the device that reports a touch first. The devices are read
    without EVIOCGRAB (a grab resets this ILITEK controller)."""
    fds = {os.open(d['node'], os.O_RDONLY | os.O_NONBLOCK): d for d in devices}
    try:
        for fd in fds:  # drop anything queued before the prompt
            try:
                while os.read(fd, EVENT.size * 64):
                    pass
            except BlockingIOError:
                pass

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            readable, _, _ = select.select(list(fds), [], [], remaining)
            for fd in readable:
                try:
                    data = os.read(fd, EVENT.size * 64)
                except BlockingIOError:
                    continue
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    if EVENT.unpack_from(data, offset)[2] == EV_ABS:
                        return fds[fd]
    finally:
        for fd in fds:
            os.close(fd)


def assign_touchscreens(layout, devices):
    """Ask the user to touch each display to learn which USB port drives it."""
    pairs = []
    remaining = list(devices)
    for i, monitor in enumerate(layout):
        if not remaining:
            print(f'WARNING: no touchscreen left for the {describe(monitor)}')
            continue
        last_pair = len(remaining) == 1 and i == len(layout) - 1
        if last_pair:
            device = remaining[0]
        else:
            print(f'\n>>> Touch the {describe(monitor)} now '
                  f'(waiting {TOUCH_TIMEOUT}s)...', flush=True)
            device = wait_for_touch(remaining)
            if device is None:
                sys.exit('ERROR: no touch detected, nothing was changed.')
        remaining.remove(device)
        pairs.append((monitor, device))
        print(f"    {monitor['label']} display <- USB {device['usb']} ({device['node']})")
    return pairs


# ---------------------------------------------------------------------------
# udev rule + re-probe (runs as root)
# ---------------------------------------------------------------------------

def write_rules(pairs):
    lines = [
        '# Generated by calibrate_layout.py -- re-run it instead of editing this file.',
        '# Each touchscreen is unmapped in Mutter (spans the whole virtual screen);',
        '# the matrix squeezes it into the rectangle of its own display.',
        '# Matrix format: "a b c d e f" where x\'=a*x+b*y+c, y\'=d*x+e*y+f (normalized 0..1)',
        '',
    ]
    for monitor, device in pairs:
        lines += [
            f'# {describe(monitor)}',
            f'SUBSYSTEM=="input", KERNELS=="{device["usb"]}", '
            f'ATTRS{{idVendor}}=="{device["vendor"]}", ATTRS{{idProduct}}=="{device["product"]}", \\',
            f'    ENV{{LIBINPUT_CALIBRATION_MATRIX}}="{monitor["matrix"]}"',
            '',
        ]

    if os.path.exists(RULES_PATH):
        shutil.copy2(RULES_PATH, RULES_PATH + '.bak')
        print(f'\nExisting rule saved as {RULES_PATH}.bak')
    with open(RULES_PATH, 'w') as f:
        f.write('\n'.join(lines))
    print(f'Wrote {RULES_PATH}')


def reprobe(devices):
    """Rebind the usbhid interfaces so libinput re-reads the udev properties
    (it only looks at them when a device is added)."""
    subprocess.run(['udevadm', 'control', '--reload-rules'], check=True)

    ok = True
    for device in devices:
        interfaces = [os.path.basename(p)
                      for p in glob.glob(os.path.join(device['usb_path'], f"{device['usb']}:*"))
                      if os.path.realpath(os.path.join(p, 'driver')) == USBHID_DRIVER]
        for interface in interfaces:
            with open(os.path.join(USBHID_DRIVER, 'unbind'), 'w') as f:
                f.write(interface)
        time.sleep(0.5)
        for interface in interfaces:
            with open(os.path.join(USBHID_DRIVER, 'bind'), 'w') as f:
                f.write(interface)

        deadline = time.monotonic() + REPROBE_TIMEOUT
        node = None
        while time.monotonic() < deadline and node is None:
            time.sleep(0.5)
            node = next((d['node'] for d in find_touchscreens()
                         if d['usb'] == device['usb']), None)
        if node is None:
            ok = False
            print(f"WARNING: USB {device['usb']} did not come back after the rebind.")
            continue
        subprocess.run(['udevadm', 'settle'])
        matrix = udev_properties(node).get('LIBINPUT_CALIBRATION_MATRIX', '(none)')
        print(f"USB {device['usb']} -> {node}: LIBINPUT_CALIBRATION_MATRIX={matrix}")
    return ok


def apply_as_root(layout):
    devices = find_touchscreens()
    if not devices:
        sys.exit('ERROR: no USB touchscreen found.')
    print(f'Found {len(devices)} touchscreen(s):')
    for d in devices:
        print(f"    {d['name']} [{d['vendor']}:{d['product']}] on USB {d['usb']} ({d['node']})")

    pairs = assign_touchscreens(layout, devices)
    write_rules(pairs)
    if reprobe([device for _, device in pairs]):
        print('\nDone. Touch each display to verify.')
    else:
        print('\nThe rule is installed; log out and back in (or reboot) to apply it.')


def remove_as_root():
    if not os.path.exists(RULES_PATH):
        print(f'{RULES_PATH} does not exist, nothing to remove.')
        return
    os.remove(RULES_PATH)
    print(f'Removed {RULES_PATH}')
    reprobe(find_touchscreens())


def run_as_root(*args):
    os.execvp('sudo', ['sudo', sys.executable, os.path.abspath(__file__), *args])


def main():
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--dry-run', action='store_true',
                        help='show the layout and matrices without changing anything')
    parser.add_argument('--remove', action='store_true', help='delete the udev rule')
    parser.add_argument('--apply', metavar='LAYOUT_JSON', help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.apply:
        apply_as_root(json.loads(args.apply))
        return
    if args.remove:
        if os.geteuid() == 0:
            remove_as_root()
        else:
            run_as_root('--remove')
        return
    if os.geteuid() == 0:
        sys.exit('ERROR: run this as your desktop user (it calls sudo itself); '
                 'root cannot read the GNOME display layout.')

    layout, (stage_w, stage_h) = read_layout()
    print(f'Virtual screen: {stage_w}x{stage_h}')
    for m in layout:
        primary = ', primary' if m['primary'] else ''
        print(f"    {describe(m)}{primary}: matrix \"{m['matrix']}\"")

    if args.dry_run:
        print('\nTouchscreens:')
        for d in find_touchscreens():
            print(f"    {d['name']} [{d['vendor']}:{d['product']}] on USB {d['usb']} ({d['node']})")
        return

    print('\nRoot is needed to read the touchscreens and write the udev rule.')
    run_as_root('--apply', json.dumps(layout))


if __name__ == '__main__':
    main()
