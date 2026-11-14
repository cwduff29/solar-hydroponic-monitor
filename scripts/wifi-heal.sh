#!/bin/bash
# WiFi self-heal for the Pi Zero's USB WiFi dongle, run every 2 min by
# wifi-heal.timer. While the gateway stays unreachable it escalates:
#   check 2:    cycle the radio (nmcli)
#   check 4:    de-authorize and re-authorize the USB dongle
#   checks 6-9: reboot, if the Pi has been up for more than 10 min
# The failure count lives in /run, so it starts again after a reboot.
#
# At most MAX_REBOOTS reboots in a row: if they did not bring the gateway back,
# the problem is the router, not the Pi, and every further reboot would only
# stop the main pump for another boot. After that it repeats the radio cycle
# and USB re-auth every RETRY_EVERY checks until the gateway answers again.
#
# The gateway is the current default route on the WiFi interface. While WiFi
# is down there is no route, so the last gateway seen is used; install.sh's
# value (__GW__) is the fallback before one has been seen.
RUN=${WIFI_HEAL_RUN_DIR:-/run}
STATE=${WIFI_HEAL_STATE_DIR:-/var/lib/wifi-heal}
IFACE=${WIFI_HEAL_IFACE:-wlan0}
USB_DEV=${WIFI_HEAL_USB_DEV:-/sys/bus/usb/devices/1-1}   # the dongle on the Pi Zero's only USB port
UPTIME_FILE=${WIFI_HEAL_UPTIME_FILE:-/proc/uptime}
MAX_REBOOTS=3
RETRY_EVERY=15

S=$RUN/wifi-heal.fails        # failed checks since boot / last success
R=$STATE/reboots              # reboots in a row made by this script (survives a reboot)
G=$STATE/gateway              # last gateway seen
mkdir -p "$STATE"

GW=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [ -n "$GW" ]; then
  [ "$GW" = "$(cat "$G" 2>/dev/null)" ] || echo "$GW" > "$G"     # write only when it changes
else
  GW=$(cat "$G" 2>/dev/null)
  [ -n "$GW" ] || GW=__GW__
fi

if ping -c2 -W3 -I "$IFACE" "$GW" >/dev/null 2>&1; then
  [ -f "$S" ] && logger -t wifi-heal "recovered after $(cat "$S") failed checks"
  rm -f "$S"
  [ "$(cat "$R" 2>/dev/null || echo 0)" = 0 ] || echo 0 > "$R"
  exit 0
fi

n=$(( $(cat "$S" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$S"
reboots=$(cat "$R" 2>/dev/null || echo 0)
logger -t wifi-heal "gw $GW unreachable, check $n, reboots in a row $reboots, $(vcgencmd get_throttled 2>/dev/null)"

radio_cycle() { logger -t wifi-heal "step: radio cycle"; nmcli radio wifi off; sleep 3; nmcli radio wifi on; }
usb_reauth()  { logger -t wifi-heal "step: USB re-auth"; echo 0 > "$USB_DEV/authorized"; sleep 5; echo 1 > "$USB_DEV/authorized"; }

case $n in
  2) radio_cycle ;;
  4) usb_reauth ;;
  6|7|8|9)
    if [ "$reboots" -ge "$MAX_REBOOTS" ]; then
      [ "$n" = 6 ] && logger -t wifi-heal "not rebooting: $reboots reboots in a row did not bring the gateway back"
    elif [ "$(cut -d. -f1 "$UPTIME_FILE")" -gt 600 ]; then
      echo $(( reboots + 1 )) > "$R"; sync
      logger -t wifi-heal "step: reboot ($(( reboots + 1 )) of $MAX_REBOOTS)"
      systemctl reboot
    fi ;;
  *)
    if [ "$reboots" -ge "$MAX_REBOOTS" ] && [ $(( n % RETRY_EVERY )) -eq 0 ]; then
      radio_cycle; usb_reauth
    fi ;;
esac
exit 0
