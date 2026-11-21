#!/bin/sh
# Read-only helper for support requests when setup cannot show the Remote Report.
# Prints what support needs about one paired Bluetooth remote. It writes nothing to the remote,
# uses no GATT access and presses no keys. Addresses are masked to their first three bytes and
# the remote's Name, Alias and HID_NAME lines are left out.
#
# Usage (Home Assistant host shell, remote awake and connected): capture_remote_info.sh AA:BB:CC:DD:EE:FF
set -eu

address="${1:?Usage: $0 <remote Bluetooth address>}"

mask() {
  sed -E 's/([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})[:_-]([0-9A-Fa-f]{2})([:_-][0-9A-Fa-f]{2}){3}/\1:\2:\3:xx:xx:xx/g'
}

echo "== bluetoothctl info =="
bluetoothctl info "$address" | grep -v -E '^[[:space:]]*(Name|Alias):' | mask

for dir in /sys/class/hidraw/hidraw*; do
  [ -e "$dir/device/uevent" ] || continue
  if grep -qi "^HID_UNIQ=$address" "$dir/device/uevent"; then
    echo "== $(basename "$dir") uevent =="
    grep -v '^HID_NAME=' "$dir/device/uevent" | mask
    echo "== $(basename "$dir") report descriptor (hex) =="
    od -An -v -tx1 "$dir/device/report_descriptor" | tr -d ' \n'
    echo
  fi
done
