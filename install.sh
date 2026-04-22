#!/bin/bash
# Automated installer for dual ILITEK touchscreen setup.
#
# What this script does:
#   1. Installs touch_proxy.py to /usr/local/bin
#   2. Installs systemd service (enabled, auto-start at boot)
#   3. Installs udev rules for device ignore + calibration matrices
#   4. Installs EDID override firmware file
#   5. Adds drm.edid_firmware kernel parameter to GRUB
#   6. Updates initramfs and GRUB
#   7. Writes dconf touchscreen-to-display mappings
#
# Requires: sudo access, GNOME Wayland 46+, two ILITEK touchscreens on USB 6.2 and 8.2
#
# Usage: bash install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FILES_DIR="$SCRIPT_DIR/files"

if [ ! -d "$FILES_DIR" ]; then
    echo "ERROR: files/ directory not found at $FILES_DIR"
    exit 1
fi

echo "=== Dual ILITEK Touchscreen Installer ==="
echo ""
echo "This will modify:"
echo "  /usr/local/bin/touch_proxy.py"
echo "  /etc/systemd/system/touch-proxy.service"
echo "  /etc/udev/rules.d/99-ilitek-touchscreen.rules"
echo "  /lib/firmware/edid/hdmi7_unique.bin"
echo "  /etc/default/grub (backup at /etc/default/grub.bak)"
echo "  dconf /org/gnome/desktop/peripherals/touchscreens/"
echo ""
read -p "Continue? [y/N] " confirm
[ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "Aborted."; exit 0; }

# 1. Proxy script
echo ""
echo "[1/7] Installing touch_proxy.py..."
sudo cp "$FILES_DIR/touch_proxy.py" /usr/local/bin/touch_proxy.py
sudo chmod +x /usr/local/bin/touch_proxy.py

# 2. Systemd service
echo "[2/7] Installing systemd service..."
sudo cp "$FILES_DIR/touch-proxy.service" /etc/systemd/system/touch-proxy.service
sudo systemctl daemon-reload
sudo systemctl enable touch-proxy.service

# 3. udev rules
echo "[3/7] Installing udev rules..."
sudo cp "$FILES_DIR/99-ilitek-touchscreen.rules" /etc/udev/rules.d/99-ilitek-touchscreen.rules
sudo udevadm control --reload-rules

# 4. EDID override
echo "[4/7] Installing EDID override..."
sudo mkdir -p /lib/firmware/edid
sudo cp "$FILES_DIR/hdmi7_unique.bin" /lib/firmware/edid/hdmi7_unique.bin
sudo chmod 644 /lib/firmware/edid/hdmi7_unique.bin

# 5. GRUB cmdline
echo "[5/7] Updating GRUB cmdline..."
if ! grep -q "drm.edid_firmware=HDMI-A-7" /etc/default/grub; then
    [ ! -f /etc/default/grub.bak ] && sudo cp /etc/default/grub /etc/default/grub.bak
    sudo sed -i 's|GRUB_CMDLINE_LINUX_DEFAULT="\([^"]*\)"|GRUB_CMDLINE_LINUX_DEFAULT="\1 drm.edid_firmware=HDMI-A-7:edid/hdmi7_unique.bin"|' /etc/default/grub
fi

# 6. Update initramfs + GRUB
echo "[6/7] Updating initramfs and GRUB (this takes a minute)..."
sudo update-initramfs -u
sudo update-grub

# 7. dconf mappings
echo "[7/7] Setting dconf touchscreen mappings..."
dconf reset -f /org/gnome/desktop/peripherals/touchscreens/
dconf write /org/gnome/desktop/peripherals/touchscreens/222A:0001/output "['HDMI-3', 'WCS', 'YF13T', 'demoset-1']"
dconf write /org/gnome/desktop/peripherals/touchscreens/222A:0002/output "['HDMI-7', 'WCS', 'YF13T', 'demoset-2']"

# Start service
sudo systemctl start touch-proxy.service

echo ""
echo "=== Installation complete ==="
echo ""
echo "REBOOT REQUIRED for EDID override to take effect."
echo ""
echo "After reboot:"
echo "  - HDMI-7 will report serial 'demoset-2' (distinguishable from HDMI-3)"
echo "  - touch-proxy.service will create virtual device 222A:0002"
echo "  - Each touchscreen maps to its respective display"
echo ""
echo "IMPORTANT: After reboot, saved display arrangement in ~/.config/monitors.xml"
echo "may reset because HDMI-7's EDID serial changed. Re-arrange displays in Settings"
echo "if needed."
echo ""
echo "To verify after reboot:"
echo "  systemctl status touch-proxy.service"
echo "  cat /sys/class/drm/card2-HDMI-A-7/edid | strings | grep demoset"
echo "  dconf dump /org/gnome/desktop/peripherals/touchscreens/"
