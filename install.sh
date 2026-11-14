#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Solar Hydroponic Monitor - Installation Script
# ============================================================

INSTALL_DIR="$(dirname "$(realpath "$0")")"
SERVICE_USER="${SUDO_USER:-pi}"
CREDS_DIR="/etc/solar-hydroponic"
CREDS_FILE="$CREDS_DIR/credentials.env"

# Local Prometheus on the Pi is optional and off by default: the Pi is scraped
# by a central Prometheus, and a local TSDB is the largest source of SD writes.
#   sudo INSTALL_LOCAL_PROMETHEUS=1 bash install.sh
INSTALL_LOCAL_PROMETHEUS="${INSTALL_LOCAL_PROMETHEUS:-0}"
# Hosts allowed to reach node_exporter (:9100), and :9090 if local Prometheus is on.
SCRAPE_HOSTS="${SCRAPE_HOSTS:-192.168.99.201 192.168.99.224}"
# WiFi self-heal timer: cycles the radio, re-authorizes the USB dongle and, after
# about 12 min without the gateway, reboots the Pi. INSTALL_WIFI_HEAL=0 skips it.
INSTALL_WIFI_HEAL="${INSTALL_WIFI_HEAL:-1}"

# Check root
if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo bash install.sh"
    exit 1
fi

echo "================================================="
echo " Solar Hydroponic Monitor - Installer"
echo "================================================="
echo " Install directory : $INSTALL_DIR"
echo " Service user      : $SERVICE_USER"
echo " Credentials file  : $CREDS_FILE"
echo " Local Prometheus  : $([[ $INSTALL_LOCAL_PROMETHEUS == 1 ]] && echo yes || echo 'no (INSTALL_LOCAL_PROMETHEUS=1 to enable)')"
echo " Scrape hosts      : $SCRAPE_HOSTS"
echo " WiFi self-heal    : $([[ $INSTALL_WIFI_HEAL == 1 ]] && echo yes || echo 'no (INSTALL_WIFI_HEAL=0)')"
echo "================================================="
echo ""

# --- [0/13] Preflight dependency check ---
echo "[0/13] Checking system dependencies..."

MISSING_PKGS=()

# Helper: check a command and record the package needed if absent
need_cmd() {
    local cmd="$1" pkg="$2"
    if ! command -v "$cmd" &>/dev/null; then
        echo "      MISSING: $cmd (package: $pkg)"
        MISSING_PKGS+=("$pkg")
    fi
}

need_cmd pip3            python3-pip
need_cmd python3         python3
need_cmd systemctl       systemd
need_cmd apt-get         apt          # shouldn't be missing, but catch it
need_cmd logrotate       logrotate
need_cmd ufw             ufw          # optional — handled gracefully later

if [[ ${#MISSING_PKGS[@]} -gt 0 ]]; then
    echo "      Installing missing packages: ${MISSING_PKGS[*]}"
    apt-get update -qq
    apt-get install -y "${MISSING_PKGS[@]}"
    echo "      Done installing missing packages."
    # Re-verify critical commands after installation
    for cmd in pip3 python3 systemctl; do
        if ! command -v "$cmd" &>/dev/null; then
            echo "ERROR: '$cmd' still not found after installation. Cannot continue."
            exit 1
        fi
    done
else
    echo "      All dependencies present."
fi

# --- [1/13] Python dependencies ---
echo "[1/13] Installing Python packages..."

# Detect which GPIO library to use based on the kernel's GPIO chip numbering.
# Older kernels (Pi OS Bullseye and earlier) expose gpiochip0 and work with RPi.GPIO.
# Newer kernels (Pi OS Bookworm+) expose gpiochip512+ and require rpi-lgpio, which is
# a drop-in RPi.GPIO replacement backed by lgpio instead of the broken sysfs interface.
# A kernel without the sysfs GPIO class at all is newer still and also needs rpi-lgpio.
# (|| true: under pipefail an empty match would otherwise end the installer silently.)
GPIO_PKG="RPi.GPIO"
GPIO_CHIP=$(ls /sys/class/gpio/ 2>/dev/null | grep -o 'gpiochip[0-9]*' | head -1 || true)
GPIO_CHIP_NUM=${GPIO_CHIP#gpiochip}
if [[ -z "$GPIO_CHIP" && -e /dev/gpiochip0 ]] \
        || [[ -n "$GPIO_CHIP_NUM" && "$GPIO_CHIP_NUM" -ge 512 ]]; then
    echo "      Detected new-style GPIO (${GPIO_CHIP:-no sysfs GPIO class}) — using rpi-lgpio"
    GPIO_PKG="rpi-lgpio"
    # Remove pip-installed RPi.GPIO if present; it shadows rpi-lgpio and breaks edge detection
    pip3 uninstall -y RPi.GPIO --break-system-packages 2>/dev/null || \
    pip3 uninstall -y RPi.GPIO 2>/dev/null || true
else
    echo "      Detected legacy GPIO chip (${GPIO_CHIP:-unknown}) — using RPi.GPIO"
fi

pip3 install renogymodbus "$GPIO_PKG" smbus2 RPi.bme280 --break-system-packages 2>/dev/null || \
pip3 install renogymodbus "$GPIO_PKG" smbus2 RPi.bme280
echo "      Done."

# --- [2/13] Prometheus node_exporter ---
echo "[2/13] Installing Prometheus node_exporter..."
apt-get install -y prometheus-node-exporter

# Override service to add textfile collector pointing at /ramdisk.
# Off to keep the Pi Zero's scrape cheap: systemd and processes (CPU), and
# hwmon, which reads every DS18B20 on the 1-wire bus (~2 s each, contending
# with waterflow's reads). CPU temperature comes from the thermal_zone
# collector; waterflow exports the 5V under-voltage alarm itself.
NEXPORTER_OVERRIDE="/etc/systemd/system/prometheus-node-exporter.service.d"
mkdir -p "$NEXPORTER_OVERRIDE"
cat > "$NEXPORTER_OVERRIDE/textfile.conf" << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter \
  --collector.textfile.directory=/ramdisk \
  --no-collector.systemd \
  --no-collector.processes \
  --no-collector.hwmon
EOF
systemctl daemon-reload
systemctl enable prometheus-node-exporter
systemctl restart prometheus-node-exporter
echo "      node_exporter installed and configured."
echo "      Textfile collector : /ramdisk/*.prom"
echo "      Metrics endpoint   : http://$(hostname -I | awk '{print $1}'):9100/metrics"

# --- [3/13] Prometheus (optional) ---
if [[ "$INSTALL_LOCAL_PROMETHEUS" == "1" ]]; then
    echo "[3/13] Installing local Prometheus..."
    apt-get install -y prometheus

    # Install our scrape config
    mkdir -p /etc/prometheus
    cp "$INSTALL_DIR/prometheus/prometheus.yml" /etc/prometheus/prometheus.yml
    chown prometheus:prometheus /etc/prometheus/prometheus.yml 2>/dev/null || true
    systemctl enable prometheus
    systemctl restart prometheus
    echo "      Prometheus installed and configured."
    echo "      Scraping            : localhost:9100"
    echo "      Web UI              : http://$(hostname -I | awk '{print $1}'):9090"
else
    echo "[3/13] Local Prometheus skipped (scrape :9100 from your central Prometheus)."
    if systemctl list-unit-files prometheus.service &>/dev/null \
            && systemctl is-enabled --quiet prometheus 2>/dev/null; then
        systemctl disable --now prometheus
        echo "      Existing local prometheus service stopped and disabled."
        echo "      Its data in /var/lib/prometheus was left in place; remove it with:"
        echo "        sudo apt-get purge prometheus && sudo rm -rf /var/lib/prometheus"
    fi
fi

# --- [4/13] Firewall (ufw) ---
echo "[4/13] Configuring firewall..."
if command -v ufw &>/dev/null; then
    # Ensure SSH is allowed before enabling ufw (don't lock ourselves out)
    ufw allow OpenSSH 2>/dev/null || ufw allow 22/tcp

    # Remove the broad private-range rules older versions of this script added
    for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16; do
        for port in 9090 9100; do
            ufw delete allow from "$net" to any port "$port" >/dev/null 2>&1 || true
        done
    done

    # Allow only the scrape/Grafana hosts (ufw skips rules that already exist)
    for host in $SCRAPE_HOSTS; do
        ufw allow from "$host" to any port 9100 proto tcp comment 'node_exporter (solar monitor)'
        if [[ "$INSTALL_LOCAL_PROMETHEUS" == "1" ]]; then
            ufw allow from "$host" to any port 9090 proto tcp comment 'Prometheus (solar monitor)'
        else
            ufw delete allow from "$host" to any port 9090 proto tcp >/dev/null 2>&1 || true
        fi
    done

    # Enable ufw non-interactively
    ufw --force enable
    echo "      Firewall configured. Port 9100$([[ $INSTALL_LOCAL_PROMETHEUS == 1 ]] && echo ' and 9090') open to: $SCRAPE_HOSTS"
    echo "      Change the list with: sudo SCRAPE_HOSTS='a.b.c.d e.f.g.h' bash install.sh"
else
    echo "      ufw not found — skipping firewall setup."
    echo "      Manually allow port 9100 from: $SCRAPE_HOSTS"
fi

# --- [5/13] Hardware interfaces ---
echo "[5/13] Enabling hardware interfaces..."
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_serial_hw 0    # Enable UART hardware
    raspi-config nonint do_serial_cons 1  # Disable serial login console (/dev/serial0)
    raspi-config nonint do_i2c 0          # Enable I2C (BME280)
    raspi-config nonint do_onewire 0      # Enable 1-Wire (DS18B20 temp sensors)
    echo "      Serial (UART, no console), I2C, and 1-Wire enabled."
    echo "      NOTE: A reboot is required for these changes to take effect."
else
    echo "      raspi-config not found. Manually enable:"
    echo "        - Serial port (hardware ON, login shell OFF)"
    echo "        - I2C"
    echo "        - 1-Wire"
fi

# MediaTek MT76 USB WiFi dongle (vendor 0e8d, e.g. Panda PAU0B / MT7610U):
# turn off USB scatter-gather, which stalls on the Pi Zero's USB controller.
if grep -qs '^mt76_usb ' /proc/modules || grep -qsx '0e8d' /sys/bus/usb/devices/*/idVendor; then
    cp "$INSTALL_DIR/modprobe/mt76-usb.conf" /etc/modprobe.d/mt76-usb.conf
    echo "      MT76 USB WiFi found: installed /etc/modprobe.d/mt76-usb.conf (disable_usb_sg=1, after reboot)."
fi

# --- [6/13] Hardware watchdog and crash capture ---
echo "[6/13] Enabling the hardware watchdog (systemd)..."
# systemd pets the BCM2835 watchdog, so a frozen Pi resets itself after 60 s.
# The Pi kernel loads bcm2835_wdt without dtparam=watchdog=on.
mkdir -p /etc/systemd/system.conf.d
cp "$INSTALL_DIR/systemd/watchdog.conf" /etc/systemd/system.conf.d/watchdog.conf
echo "      Installed /etc/systemd/system.conf.d/watchdog.conf (60 s runtime, 2 min reboot)."
echo "      Takes effect at the next reboot."
echo "      Keep watchdog.enabled=false in config.json: systemd owns /dev/watchdog."

# Crash capture: ramoops keeps the kernel console in RAM across a watchdog
# reset, and systemd-pstore saves it to /var/lib/systemd/pstore at the next boot.
echo "      Enabling crash capture (ramoops)..."
BOOT_CONFIG=/boot/firmware/config.txt
[[ -f $BOOT_CONFIG ]] || BOOT_CONFIG=/boot/config.txt
RAMOOPS_LINE='dtoverlay=ramoops,total-size=0x20000,record-size=0x4000,console-size=0x8000'
if [[ ! -f $BOOT_CONFIG ]]; then
    echo "      config.txt not found. Add this line under [all] manually:"
    echo "        $RAMOOPS_LINE"
elif grep -q '^dtoverlay=ramoops' "$BOOT_CONFIG"; then
    echo "      ramoops overlay already in $BOOT_CONFIG."
else
    cp -p "$BOOT_CONFIG" "$BOOT_CONFIG.bak-install-$(date +%Y%m%d)"
    # The overlay must apply to every board, so it goes in an [all] section.
    if [[ "$(grep -E '^\[' "$BOOT_CONFIG" | tail -1 || true)" != "[all]" ]]; then
        echo "[all]" >> "$BOOT_CONFIG"
    fi
    {
        echo "# Keep kernel crash/console logs across a watchdog reset (see /var/lib/systemd/pstore)"
        echo "$RAMOOPS_LINE"
    } >> "$BOOT_CONFIG"
    echo "      Added the ramoops overlay to $BOOT_CONFIG (backup: $BOOT_CONFIG.bak-install-$(date +%Y%m%d))."
fi
cp "$INSTALL_DIR/sysctl/99-zz-crash-capture.conf" /etc/sysctl.d/99-zz-crash-capture.conf
sysctl -q -p /etc/sysctl.d/99-zz-crash-capture.conf
echo "      Installed /etc/sysctl.d/99-zz-crash-capture.conf (kernel errors reach ramoops)."

# --- [7/13] User group memberships ---
echo "[7/13] Adding $SERVICE_USER to hardware groups..."

# Create watchdog group and udev rule if needed (not present by default on Pi OS)
if ! getent group watchdog &>/dev/null; then
    groupadd --system watchdog
    echo "      Created system group: watchdog"
fi
WATCHDOG_UDEV="/etc/udev/rules.d/60-watchdog.rules"
if [[ ! -f "$WATCHDOG_UDEV" ]]; then
    echo 'KERNEL=="watchdog", GROUP="watchdog", MODE="0660"' > "$WATCHDOG_UDEV"
    udevadm control --reload-rules
    udevadm trigger --name-match=watchdog 2>/dev/null || true
    echo "      Created udev rule: $WATCHDOG_UDEV"
fi

usermod -aG dialout,gpio,i2c,watchdog "$SERVICE_USER"
echo "      Added to: dialout (serial), gpio, i2c, watchdog"
echo "      NOTE: Group changes take effect after next login / reboot."

# --- [8/13] Ramdisk ---
echo "[8/13] Setting up ramdisk at /ramdisk..."
mkdir -p /ramdisk
if grep -q '/ramdisk' /etc/fstab; then
    echo "      /ramdisk already in /etc/fstab, skipping."
else
    echo "tmpfs /ramdisk tmpfs nodev,nosuid,size=50M 0 0" >> /etc/fstab
    echo "      Added tmpfs /ramdisk to /etc/fstab."
fi
mount /ramdisk 2>/dev/null && echo "      Mounted /ramdisk." || echo "      /ramdisk already mounted."
chown "$SERVICE_USER":root /ramdisk
chmod 775 /ramdisk

# --- [9/13] Persistent state directory ---
echo "[9/13] Creating persistent state directory..."
mkdir -p /var/lib/renogy
chown "$SERVICE_USER":root /var/lib/renogy
chmod 750 /var/lib/renogy
echo "      /var/lib/renogy/ created (alert state survives reboots)"

# --- [10/13] Email credentials ---
echo "[10/13] Setting up email credentials..."
mkdir -p "$CREDS_DIR"
chmod 750 "$CREDS_DIR"

if [[ -f "$CREDS_FILE" ]]; then
    echo "      $CREDS_FILE already exists, skipping."
    echo "      To update credentials: sudo nano $CREDS_FILE"
else
    cp "$INSTALL_DIR/credentials.env.example" "$CREDS_FILE"
    chmod 640 "$CREDS_FILE"
    chown root:"$SERVICE_USER" "$CREDS_FILE"
    echo "      Created $CREDS_FILE"
    echo "      *** IMPORTANT: Edit this file with your Gmail credentials ***"
    echo "      sudo nano $CREDS_FILE"
fi

# --- [11/13] Log files & logrotate ---
echo "[11/13] Creating log files..."
touch /var/log/renogy.log /var/log/waterflow.log
chown "$SERVICE_USER":adm /var/log/renogy.log /var/log/waterflow.log
chmod 664 /var/log/renogy.log /var/log/waterflow.log
echo "      /var/log/renogy.log"
echo "      /var/log/waterflow.log"

cp "$INSTALL_DIR/logrotate/renogy"    /etc/logrotate.d/renogy
cp "$INSTALL_DIR/logrotate/waterflow" /etc/logrotate.d/waterflow
echo "      Logrotate configs installed."

# --- [12/13] Systemd services ---
echo "[12/13] Installing and enabling monitor services..."
sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/renogy.service" > /etc/systemd/system/renogy.service

sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/waterflow.service" > /etc/systemd/system/waterflow.service

sed -e "s|__DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/battery_shutdown.service" > /etc/systemd/system/battery_shutdown.service

# Marks orderly shutdowns; waterflow uses it to tell reboots from watchdog resets
cp "$INSTALL_DIR/systemd/clean-shutdown-marker.service" /etc/systemd/system/clean-shutdown-marker.service

systemctl daemon-reload
systemctl enable renogy.service waterflow.service battery_shutdown.service
systemctl enable --now clean-shutdown-marker.service
echo "      Services enabled (renogy, waterflow, battery_shutdown, clean-shutdown-marker)."

if [[ "$INSTALL_WIFI_HEAL" == "1" ]]; then
    GW="${WIFI_HEAL_GATEWAY:-$(ip -4 route show default 2>/dev/null | awk '{print $3; exit}' || true)}"
    if [[ -z "$GW" ]]; then
        echo "      wifi-heal skipped: no default gateway found (set WIFI_HEAL_GATEWAY=a.b.c.d)."
    else
        sed -e "s|__GW__|$GW|g" "$INSTALL_DIR/scripts/wifi-heal.sh" > /usr/local/bin/wifi-heal.sh
        chmod 755 /usr/local/bin/wifi-heal.sh
        cp "$INSTALL_DIR/systemd/wifi-heal.service" "$INSTALL_DIR/systemd/wifi-heal.timer" /etc/systemd/system/
        systemctl daemon-reload
        systemctl enable --now wifi-heal.timer
        echo "      wifi-heal.timer enabled (pings gateway $GW every 2 min)."
    fi
else
    echo "      wifi-heal skipped (INSTALL_WIFI_HEAL=0)."
fi

# Only start if credentials have been filled in
if grep -q 'your_' "$CREDS_FILE" 2>/dev/null; then
    echo ""
    echo "      *** Services NOT started ***"
    echo "      Fill in your email credentials first:"
    echo "        sudo nano $CREDS_FILE"
    echo "      Then start services:"
    echo "        sudo systemctl start renogy waterflow battery_shutdown"
else
    systemctl start renogy.service waterflow.service battery_shutdown.service
    echo "      Services started."
fi

echo ""
echo "================================================="
echo " Installation complete!"
echo "================================================="
echo ""
echo " Required next steps:"
echo "   1. Fill in email credentials (if not done):"
echo "        sudo nano $CREDS_FILE"
echo "   2. Reboot to apply interface, watchdog, and group changes:"
echo "        sudo reboot"
echo ""
echo " Prometheus scrape job (on your central Prometheus):"
echo "   - job_name: solar_hydroponic"
echo "     static_configs: [{targets: ['$(hostname -I | awk '{print $1}'):9100']}]"
echo ""
echo " Config reload without restart (SIGHUP):"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value renogy)"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value waterflow)"
echo ""
echo " Service management:"
echo "   sudo systemctl status renogy waterflow battery_shutdown prometheus-node-exporter"
echo "   sudo journalctl -u renogy -f"
echo "   sudo journalctl -u waterflow -f"
echo "   sudo journalctl -u battery_shutdown -f"
echo ""
echo " Verify metrics:"
echo "   curl http://localhost:9100/metrics | grep waterflow_inlet_lpm"
if [[ "$INSTALL_LOCAL_PROMETHEUS" == "1" ]]; then
    echo "   curl http://localhost:9090/api/v1/query?query=waterflow_inlet_lpm"
fi
echo ""
