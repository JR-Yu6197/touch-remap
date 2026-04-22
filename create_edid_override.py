#!/usr/bin/env python3
"""
Create a modified EDID for HDMI-A-7 with a unique serial number,
so GNOME can distinguish it from HDMI-A-3 (which has identical EDID).

Changes serial from "demoset-1" to "demoset-2" and recalculates checksums.
"""

import sys

EDID_PATH = "/sys/class/drm/card2-HDMI-A-7/edid"
OUTPUT_PATH = "/tmp/hdmi7_unique.bin"


def fix_checksum(block):
    """Recalculate checksum for a 128-byte EDID block."""
    block = bytearray(block)
    total = sum(block[:-1]) & 0xFF
    block[-1] = (256 - total) & 0xFF
    return bytes(block)


def main():
    with open(EDID_PATH, 'rb') as f:
        edid = bytearray(f.read())

    if len(edid) < 128:
        print(f"ERROR: EDID too short ({len(edid)} bytes)")
        sys.exit(1)

    print(f"Read EDID: {len(edid)} bytes")

    # Find the Display Product Serial descriptor (type 0xFF) in base block
    # Descriptors are at 0x36, 0x48, 0x5A, 0x6C (each 18 bytes)
    serial_offset = None
    for desc_off in (0x36, 0x48, 0x5A, 0x6C):
        if desc_off + 18 > 128:
            break
        # Descriptor header: 00 00 00 TYPE 00 (for non-timing descriptors)
        if edid[desc_off] == 0 and edid[desc_off + 1] == 0 and edid[desc_off + 2] == 0 and edid[desc_off + 3] == 0xFF:
            serial_offset = desc_off + 5  # serial text starts at offset 5 in descriptor
            break

    if serial_offset is None:
        print("ERROR: Could not find serial descriptor in EDID")
        sys.exit(1)

    # Extract current serial (ASCII, terminated by 0x0A, padded with 0x20)
    serial_bytes = edid[serial_offset:serial_offset + 13]
    current_serial = serial_bytes.split(b'\x0a')[0].decode('ascii', errors='replace')
    print(f"Current serial: '{current_serial}'")

    # Change "demoset-1" → "demoset-2" (just change last character)
    new_serial = "demoset-2"
    new_serial_bytes = new_serial.encode('ascii') + b'\x0a' + b'\x20' * 13
    new_serial_bytes = new_serial_bytes[:13]

    edid[serial_offset:serial_offset + 13] = new_serial_bytes
    print(f"New serial: '{new_serial}'")

    # Recalculate base block checksum
    base_block = fix_checksum(edid[:128])
    edid[:128] = base_block
    print(f"Base block checksum fixed: 0x{edid[127]:02x}")

    # If extension blocks exist, leave their checksums alone (they're independent)
    if len(edid) > 128:
        print(f"  (preserving {len(edid) - 128} bytes of extension blocks)")

    # Write to output
    with open(OUTPUT_PATH, 'wb') as f:
        f.write(bytes(edid))
    print(f"Written to: {OUTPUT_PATH}")

    # Verify base block checksum
    total = sum(edid[:128]) & 0xFF
    if total == 0:
        print("Checksum verification: PASS")
    else:
        print(f"Checksum verification: FAIL (sum={total:#x})")
        sys.exit(1)


if __name__ == '__main__':
    main()
