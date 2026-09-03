#!/usr/bin/env bash
# ============================================================================
#  openUC2 Pi focus sensor — system installer
#
#  Turns a stock Raspberry Pi OS (Bookworm, 64-bit) into a focus sensor that
#  appears to the microscope host as a USB network device.  Runs in two
#  contexts, doing exactly the same thing:
#    * inside the qemu chroot of the GitHub Actions SD-image build
#    * on a live Raspberry Pi:  sudo bash sd-image/install-focussensor.sh
#
#  Idempotent: safe to run again after a git pull to update the sensor.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
APP_DIR=/opt/focussensor
BOOT_DIR=/boot/firmware
HOSTNAME_NEW=focussensor
USB0_ADDR=192.168.7.2
export DEBIAN_FRONTEND=noninteractive

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo"; exit 1; }
[ -d "$BOOT_DIR" ] || BOOT_DIR=/boot   # pre-Bookworm fallback

echo "==> Installing packages"
apt-get update
# numpy, Pillow and picamera2 come from apt on purpose: building them with pip
# on a Pi is slow at best and a compiler hunt at worst.
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip \
    python3-numpy python3-pil python3-yaml \
    python3-picamera2 \
    avahi-daemon iproute2 fake-hwclock

echo "==> Staging the source in $APP_DIR/src"
mkdir -p "$APP_DIR"
rm -rf "$APP_DIR/src"
mkdir -p "$APP_DIR/src"
cp -r "$REPO_ROOT/software" "$REPO_ROOT/tools" "$REPO_ROOT/config" \
      "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/README.md" "$APP_DIR/src/"

echo "==> Python environment (system site-packages for numpy/picamera2)"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    python3 -m venv --system-site-packages "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --no-cache-dir --upgrade pip
# Dependencies come from pyproject.toml. numpy, Pillow and PyYAML are left
# unpinned there precisely so the apt versions already visible through
# --system-site-packages satisfy them, instead of pip rebuilding them here.
"$APP_DIR/venv/bin/pip" install --no-cache-dir "$APP_DIR/src"

echo "==> Default configuration on the boot partition"
# On the boot partition so it can be edited with the card in a laptop.
if [ ! -f "$BOOT_DIR/focussensor.yaml" ]; then
    cp "$REPO_ROOT/config/focussensor.yaml" "$BOOT_DIR/focussensor.yaml"
fi


append_once() {  # append_once <file> <line>
    grep -qxF "$2" "$1" 2>/dev/null || echo "$2" >> "$1"
}
CONFIG_TXT="$BOOT_DIR/config.txt"
append_once "$CONFIG_TXT" "# --- openUC2 focus sensor ---"
append_once "$CONFIG_TXT" "dtoverlay=dwc2"        # OTG port in peripheral mode
append_once "$CONFIG_TXT" "camera_auto_detect=1"
append_once "$CONFIG_TXT" "dtoverlay=disable-bt"  # nothing here needs bluetooth
append_once "$CONFIG_TXT" "disable_splash=1"
append_once "$CONFIG_TXT" "boot_delay=0"

# The kernel needs dwc2 up before userspace; modules-load in cmdline.txt is the
# only reliable way to get that on a Pi.
CMDLINE="$BOOT_DIR/cmdline.txt"
if [ -f "$CMDLINE" ] && ! grep -q "modules-load=dwc2" "$CMDLINE"; then
    sed -i '1 s|$| modules-load=dwc2|' "$CMDLINE"
fi

echo "==> Static address on usb0 ($USB0_ADDR)"
# systemd-networkd owns usb0 so the address is deterministic; NetworkManager,
# which owns everything else on Bookworm, is told to keep off it.
install -d /etc/systemd/network
cat > /etc/systemd/network/10-focussensor-usb0.network <<NET
[Match]
Name=usb0

[Network]
Address=${USB0_ADDR}/24
LinkLocalAddressing=ipv4
ConfigureWithoutCarrier=yes
NET
install -d /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/99-focussensor-unmanaged-usb0.conf <<'NMCONF'
[keyfile]
unmanaged-devices=interface-name:usb0
NMCONF
systemctl enable systemd-networkd 2>/dev/null || true

echo "==> systemd services"
cp "$REPO_ROOT/software/systemd/focussensor.service"            /etc/systemd/system/
systemctl daemon-reload 2>/dev/null || true

echo "==> Hostname / mDNS: ${HOSTNAME_NEW}.local"
# mDNS works over the USB link too, so the host can reach the sensor by name
# without configuring an address on its own end.
echo "$HOSTNAME_NEW" > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t${HOSTNAME_NEW}/" /etc/hosts
else
    printf '127.0.1.1\t%s\n' "$HOSTNAME_NEW" >> /etc/hosts
fi
systemctl enable avahi-daemon 2>/dev/null || true

echo "==> Keep the SD card healthy (cap journal size)"
install -d /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/focussensor.conf <<'JOURNAL'
[Journal]
SystemMaxUse=32M
JOURNAL

echo "==> SSH"
systemctl enable ssh 2>/dev/null || true

echo "==> Login banner helper"
cat > /usr/local/bin/focussensor-info <<'BANNER'
#!/bin/bash
echo "┌──────────────────────────────────────────────┐"
echo "│        openUC2 Pi focus sensor               │"
echo "└──────────────────────────────────────────────┘"
for s in focussensor; do
  systemctl is-active "$s" >/dev/null 2>&1 \
    && echo "  ● $s: running" || echo "  ○ $s: NOT running"
done
IP=$(ip -4 addr show usb0 2>/dev/null | sed -n 's/^\s*inet\s\+\([0-9.]\+\).*/\1/p')
echo "  USB link: ${IP:-not up}   →   http://${IP:-192.168.7.2}:8321/"
echo "  Also at:  http://focussensor.local:8321/"
echo "  Config:   /boot/firmware/focussensor.yaml"
echo "  Logs:     journalctl -u focussensor -f"
echo "  Check:    /opt/focussensor/venv/bin/focussensor-client status"
BANNER
chmod +x /usr/local/bin/focussensor-info
if [ -d /home/pi ] && ! grep -q focussensor-info /home/pi/.bashrc 2>/dev/null; then
    printf '\n[ -x /usr/local/bin/focussensor-info ] && /usr/local/bin/focussensor-info\n' \
        >> /home/pi/.bashrc
fi

echo "==> Done. Reboot, then plug the OTG port into the microscope host."
