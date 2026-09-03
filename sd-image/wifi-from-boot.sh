#!/usr/bin/env bash
# ============================================================================
#  Bring up WiFi from a file on the boot partition.
#
#  A headless sensor has a chicken-and-egg problem: you need the network to
#  configure it, and you need to configure it to get on the network. Raspberry
#  Pi Imager solves this at flash time, but only on the very first boot -- and
#  only if you remember. This reads /boot/firmware/wifi.txt on EVERY boot, so
#  moving the sensor to a different lab is a matter of putting the card in a
#  laptop and editing a text file on the one partition every OS can mount.
#
#  Format (whitespace and blank lines ignored, '#' at line start is a comment):
#      ssid=MyNetwork
#      password=secret
#      country=DE
#  Surrounding whitespace is stripped; quote the value to keep it.
#  or, for the impatient, a single line:
#      MyNetwork:secret
#
#  The password sits in clear text on a FAT partition, exactly as
#  wpa_supplicant.conf always did. Treat the card accordingly.
# ============================================================================
set -uo pipefail

BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot
CONF="$BOOT/wifi.txt"
PROFILE="focussensor-wifi"

[ -f "$CONF" ] || exit 0
command -v nmcli >/dev/null 2>&1 || { echo "nmcli not present"; exit 0; }

trim() { printf '%s' "$1" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

# Surrounding whitespace is stripped, because someone lining up an `=` should
# not end up with a password that has a space in it. Wrap the value in single
# or double quotes when the spaces are real -- the same escape hatch
# wpa_supplicant.conf always had.
unquote() {
    local v; v="$(trim "$1")"
    case "$v" in
        \"*\") v="${v#\"}"; v="${v%\"}" ;;
        \'*\') v="${v#\'}"; v="${v%\'}" ;;
    esac
    printf '%s' "$v"
}

ssid=""; password=""; country=""
while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim "$line")"
    # Only a leading # is a comment: a password may legitimately contain one.
    case "$line" in ""|"#"*) continue ;; esac
    case "$line" in
        ssid=*|SSID=*)                ssid="$(unquote "${line#*=}")" ;;
        password=*|PASSWORD=*|psk=*)  password="$(unquote "${line#*=}")" ;;
        country=*|COUNTRY=*)          country="$(trim "${line#*=}")" ;;
        *:*)                          ssid="$(unquote "${line%%:*}")"
                                      password="$(unquote "${line#*:}")" ;;
    esac
done < "$CONF"

[ -n "$ssid" ] || { echo "no ssid in $CONF"; exit 0; }

if [ -n "$country" ]; then
    # The regulatory domain gates which channels the radio will even scan;
    # without it a 5 GHz network can be invisible rather than merely refused.
    raspi-config nonint do_wifi_country "$country" >/dev/null 2>&1 || true
fi
rfkill unblock wifi >/dev/null 2>&1 || true

# Recreate rather than edit, so a changed password actually takes effect.
nmcli connection delete "$PROFILE" >/dev/null 2>&1 || true
if [ -n "$password" ]; then
    nmcli connection add type wifi con-name "$PROFILE" ifname wlan0 \
        ssid "$ssid" \
        wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$password" \
        connection.autoconnect yes >/dev/null
else
    nmcli connection add type wifi con-name "$PROFILE" ifname wlan0 \
        ssid "$ssid" connection.autoconnect yes >/dev/null
fi

echo "configured WiFi for SSID '$ssid'"
nmcli connection up "$PROFILE" >/dev/null 2>&1 \
    || echo "could not join '$ssid' yet; NetworkManager will keep retrying"
