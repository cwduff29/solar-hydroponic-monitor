#!/bin/bash
# WiFi self-heal for the Pi Zero's USB WiFi dongle, run every 2 min by
# wifi-heal.timer. While the gateway stays unreachable it escalates:
#   check 2:    cycle the radio (nmcli)
#   check 4:    de-authorize and re-authorize the USB dongle
#   checks 6-9: reboot, if the Pi has been up for more than 10 min
# The failure count lives in /run, so it starts again after a reboot.
# install.sh replaces __GW__ with the default gateway.
GW=__GW__; S=/run/wifi-heal.fails
USB_DEV=/sys/bus/usb/devices/1-1   # the dongle on the Pi Zero's only USB port
if ping -c2 -W3 -I wlan0 "$GW" >/dev/null 2>&1; then
  [ -f $S ] && logger -t wifi-heal "recovered after $(cat $S) failed checks"; rm -f $S; exit 0; fi
n=$(( $(cat $S 2>/dev/null || echo 0) + 1 )); echo $n > $S
logger -t wifi-heal "gw unreachable, check $n, $(vcgencmd get_throttled)"
case $n in
  2) logger -t wifi-heal "step: radio cycle"; nmcli radio wifi off; sleep 3; nmcli radio wifi on ;;
  4) logger -t wifi-heal "step: USB re-auth"; echo 0 > $USB_DEV/authorized; sleep 5; echo 1 > $USB_DEV/authorized ;;
  6|7|8|9) if [ "$(cut -d. -f1 /proc/uptime)" -gt 600 ]; then logger -t wifi-heal "step: reboot"; systemctl reboot; fi ;;
esac
