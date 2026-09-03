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
    avahi-daemon iproute2 fake-hwclock curl

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
append_once "$CONFIG_TXT" "camera_auto_detect=1"
append_once "$CONFIG_TXT" "dtoverlay=disable-bt"  # nothing here needs bluetooth
append_once "$CONFIG_TXT" "disable_splash=1"
append_once "$CONFIG_TXT" "boot_delay=0"

echo "==> Removing any USB gadget wiring from earlier installs"
# The sensor is reached over the Pi's normal network (WiFi or ethernet). An
# earlier version of this script set the OTG port up as a USB ethernet gadget;
# leaving that behind is not harmless -- dwc2 in peripheral mode stops the OTG
# port working as a host too, and enabling systemd-networkd alongside
# NetworkManager on Bookworm invites the two to fight over interfaces.
CMDLINE="$BOOT_DIR/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    sed -i 's/ *modules-load=dwc2[^ ]*//g' "$CMDLINE"
fi
sed -i '/^dtoverlay=dwc2$/d' "$CONFIG_TXT" 2>/dev/null || true
rm -f /etc/systemd/network/10-focussensor-usb0.network \
      /etc/NetworkManager/conf.d/99-focussensor-unmanaged-usb0.conf \
      /usr/local/sbin/focussensor-usb-gadget \
      /etc/systemd/system/focussensor-usb-gadget.service
systemctl disable focussensor-usb-gadget.service 2>/dev/null || true

echo "==> systemd services"
cp "$REPO_ROOT/software/systemd/focussensor.service"            /etc/systemd/system/
systemctl daemon-reload 2>/dev/null || true

echo "==> Hostname / mDNS: ${HOSTNAME_NEW}.local"
# mDNS is how the microscope host finds the sensor without anyone having to
# know or configure an IP address.
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

# Raspberry Pi OS Bookworm ships with NO default user: `userconfig.service`
# takes over tty1 on first boot and asks for a username and password, which on
# a headless sensor means a Pi that never finishes booting into anything
# useful. Only turn it off once a real user exists, or the machine would have
# no way in at all.
if [ -n "$(ls -A /home 2>/dev/null)" ]; then
    echo "==> Disabling the first-boot user wizard (a user already exists)"
    systemctl disable userconfig.service 2>/dev/null || true
    systemctl mask    userconfig.service 2>/dev/null || true
    rm -f /etc/systemd/system/getty@tty1.service.d/autologin-userconf.conf
else
    echo "==> NOTE: no user account exists yet, so the first-boot wizard is"
    echo "    left enabled and will prompt for a username on the HDMI console."
    echo "    To avoid it, create a user first (or put a userconf.txt on the"
    echo "    boot partition) and re-run this installer."
fi

if id pi >/dev/null 2>&1; then
    echo "==> Console auto-login on tty1 (field debugging over HDMI)"
    install -d /etc/systemd/system/getty@tty1.service.d
    cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<'AUTOLOGIN'
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin pi --noclear %I $TERM
AUTOLOGIN
fi

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
echo "  Reach it at:"
echo "    http://focussensor.local:8321/"
ip -4 -o addr show scope global 2>/dev/null \
  | awk '{split($4,a,"/"); printf "    http://%s:8321/   (%s)\n", a[1], $2}'
STATUS=$(curl -s --max-time 1 http://127.0.0.1:8321/api/status 2>/dev/null)
if [ -z "$STATUS" ]; then
  echo "  Camera:   service not answering yet"
elif echo "$STATUS" | grep -q '"simulated": *true'; then
  echo "  Camera:   SIMULATED - no Pi camera detected"
else
  echo "  Camera:   $(echo "$STATUS" | sed -n 's/.*"model": *"\([^"]*\)".*/\1/p' | head -1)"
fi
echo "  Config:   /boot/firmware/focussensor.yaml"
echo "  Logs:     journalctl -u focussensor -f"
echo "  Check:    /opt/focussensor/venv/bin/focussensor-client status"
BANNER
chmod +x /usr/local/bin/focussensor-info
if [ -d /home/pi ] && ! grep -q focussensor-info /home/pi/.bashrc 2>/dev/null; then
    printf '\n[ -x /usr/local/bin/focussensor-info ] && /usr/local/bin/focussensor-info\n' \
        >> /home/pi/.bashrc
fi

echo "==> Done. Reboot, then reach the sensor at http://focussensor.local:8321/"
