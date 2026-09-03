#!/usr/bin/env bash
# ============================================================================
#  USB gadget bring-up: the sensor appears as a network device over one cable.
#
#  Plug the microscope host into the Pi's OTG port and the host gets a new
#  ethernet interface. That one cable carries power and the link, and the
#  sensor answers on http://192.168.7.2:8321/ (and focussensor.local).
#
#  Choices that matter:
#
#  * **CDC-ECM by default, not NCM.** NCM is the newer and faster protocol,
#    but macOS only gained native support in Ventura, while ECM has worked on
#    macOS and Linux for a decade. The link carries a few kB/s of JSON and an
#    occasional JPEG, so ECM's lower throughput is irrelevant and its
#    compatibility is not. Override in /boot/firmware/usb-gadget.txt with
#    `mode=ncm` (Linux hosts, newer macOS) or `mode=rndis` (Windows).
#
#  * **MAC addresses derived from the Pi's serial**, so they are the same on
#    every boot. With random MACs the host invents a brand new interface each
#    time -- en5, en6, en7 on a Mac -- and any address configuration has to be
#    redone. Locally-administered range, so they cannot collide with real
#    hardware, and two sensors on one host stay distinct.
#
#  * **The address is set here, not by systemd-networkd.** Enabling networkd
#    beside NetworkManager on Bookworm invites the two to fight over
#    interfaces; a single `ip addr add` on an interface NetworkManager has
#    been told to ignore has no such failure mode.
# ============================================================================
set -uo pipefail

GADGET=/sys/kernel/config/usb_gadget/focussensor
USB0_ADDR="${USB0_ADDR:-192.168.7.2}"
USB0_MASK="${USB0_MASK:-24}"

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot

MODE=ecm
if [ -f "$BOOT/usb-gadget.txt" ]; then
    want=$(sed -n 's/^[[:space:]]*mode[[:space:]]*=[[:space:]]*\([a-zA-Z]*\).*/\1/p' \
           "$BOOT/usb-gadget.txt" | tail -1 | tr 'A-Z' 'a-z')
    case "$want" in ecm|ncm|rndis|eem) MODE="$want" ;; esac
fi

# Last 6 hex digits of the CPU serial: unique per board, stable across boots.
SERIAL=$(sed -n 's/^Serial[[:space:]]*:[[:space:]]*//p' /proc/cpuinfo | tail -1)
SERIAL=${SERIAL: -6}
SERIAL=${SERIAL:-000001}
# 02: locally administered, unicast.
HOST_MAC="02:1a:11:${SERIAL:0:2}:${SERIAL:2:2}:${SERIAL:4:2}"
DEV_MAC="02:1a:22:${SERIAL:0:2}:${SERIAL:2:2}:${SERIAL:4:2}"

modprobe libcomposite || { echo "libcomposite unavailable"; exit 1; }
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config

if [ ! -d "$GADGET" ]; then
    mkdir -p "$GADGET"
    cd "$GADGET" || exit 1
    echo 0x1d6b > idVendor          # Linux Foundation
    echo 0x0104 > idProduct         # Multifunction composite gadget
    echo 0x0100 > bcdDevice
    echo 0x0200 > bcdUSB

    mkdir -p strings/0x409
    echo "openUC2"                 > strings/0x409/manufacturer
    echo "Pi focus sensor"         > strings/0x409/product
    echo "${SERIAL}"               > strings/0x409/serialnumber

    mkdir -p configs/c.1/strings/0x409
    echo "Ethernet (${MODE})"      > configs/c.1/strings/0x409/configuration
    echo 250                       > configs/c.1/MaxPower

    if ! mkdir -p "functions/${MODE}.usb0" 2>/dev/null; then
        echo "function ${MODE} unavailable, falling back to ecm"
        MODE=ecm
        mkdir -p functions/ecm.usb0
    fi
    echo "$HOST_MAC" > "functions/${MODE}.usb0/host_addr" 2>/dev/null || true
    echo "$DEV_MAC"  > "functions/${MODE}.usb0/dev_addr"  2>/dev/null || true
    ln -sf "functions/${MODE}.usb0" configs/c.1/

    UDC=$(ls /sys/class/udc 2>/dev/null | head -1)
    if [ -z "$UDC" ]; then
        echo "no USB device controller: is dtoverlay=dwc2 set and the cable in the OTG port?"
        exit 1
    fi
    echo "$UDC" > UDC || { echo "could not bind gadget to $UDC"; exit 1; }
    echo "gadget up: ${MODE}, dev ${DEV_MAC}, host ${HOST_MAC}, udc ${UDC}"
fi

# The interface appears a moment after the gadget binds.
for _ in $(seq 1 40); do
    ip link show usb0 >/dev/null 2>&1 && break
    sleep 0.25
done
if ! ip link show usb0 >/dev/null 2>&1; then
    echo "usb0 never appeared"
    exit 1
fi

ip link set usb0 up
ip addr show usb0 | grep -q "$USB0_ADDR" \
    || ip addr add "${USB0_ADDR}/${USB0_MASK}" dev usb0
echo "usb0 up at ${USB0_ADDR}/${USB0_MASK}"
