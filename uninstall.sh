#!/bin/bash
# Uninstall the dual ILITEK touchscreen setup.
# Reverts all changes made by install.sh.

set -e

echo "=== Dual ILITEK Touchscreen Uninstaller ==="
echo ""
echo "This will remove:"
echo "  /usr/local/bin/touch_proxy.py"
echo "  /etc/systemd/system/touch-proxy.service"
echo "  /etc/udev/rules.d/99-ilitek-touchscreen.rules"
echo "  /lib/firmware/edid/hdmi7_unique.bin"
echo "  drm.edid_firmware kernel parameter"
echo "  dconf touchscreen mappings"
echo ""
read -p "Continue? [y/N] " confirm
[ "$confirm" != "y" ] && [ "$confirm" != "Y" ] && { echo "Aborted."; exit 0; }

echo ""
echo "Stopping service..."
sudo systemctl disable --now touch-proxy.service 2>/dev/null || true

echo "Removing installed files..."
sudo rm -f /etc/systemd/system/touch-proxy.service
sudo rm -f /usr/local/bin/touch_proxy.py
sudo rm -f /etc/udev/rules.d/99-ilitek-touchscreen.rules
sudo rm -f /lib/firmware/edid/hdmi7_unique.bin
sudo systemctl daemon-reload
sudo udevadm control --reload-rules

echo "Restoring GRUB..."
if [ -f /etc/default/grub.bak ]; then
    sudo cp /etc/default/grub.bak /etc/default/grub
else
    sudo sed -i 's| drm.edid_firmware=HDMI-A-7:edid/hdmi7_unique.bin||' /etc/default/grub
fi
sudo update-grub
sudo update-initramfs -u

echo "Clearing dconf mappings..."
dconf reset -f /org/gnome/desktop/peripherals/touchscreens/

echo ""
echo "=== Uninstall complete ==="
echo "Reboot required to apply GRUB changes."
