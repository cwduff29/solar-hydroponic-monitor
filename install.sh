#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Solar Hydroponic Monitor - Installation Script
# ============================================================

INSTALL_DIR="$(dirname "$(realpath "$0")")"
SERVICE_USER="${SUDO_USER:-pi}"
CREDS_DIR="/etc/solar-hydroponic"
CREDS_FILE="$CREDS_DIR/credentials.env"

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
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280 --break-system-packages 2>/dev/null || \
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280
echo "      Done."

# --- [2/13] Prometheus node_exporter ---
echo "[2/13] Installing Prometheus node_exporter..."
apt-get install -y prometheus-node-exporter

# Override service to add textfile collector pointing at /ramdisk
NEXPORTER_OVERRIDE="/etc/systemd/system/prometheus-node-exporter.service.d"
mkdir -p "$NEXPORTER_OVERRIDE"
cat > "$NEXPORTER_OVERRIDE/textfile.conf" << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus-node-exporter \
  --collector.textfile.directory=/ramdisk \
  --collector.systemd \
  --collector.processes
EOF
systemctl daemon-reload
systemctl enable prometheus-node-exporter
systemctl restart prometheus-node-exporter
echo "      node_exporter installed and configured."
echo "      Textfile collector : /ramdisk/*.prom"
echo "      Metrics endpoint   : http://$(hostname -I | awk '{print $1}'):9100/metrics"

# --- [3/13] Prometheus ---
echo "[3/13] Installing Prometheus..."
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
echo "      NOTE: Point your Grafana datasource at http://<PI_IP>:9090"

# --- [4/13] Firewall (ufw) ---
echo "[4/13] Configuring firewall..."
if command -v ufw &>/dev/null; then
    # Ensure SSH is allowed before enabling ufw (don't lock ourselves out)
    ufw allow OpenSSH 2>/dev/null || ufw allow 22/tcp

    # Allow Prometheus and node_exporter from local network
    # Default to whole RFC1918 private ranges; user can tighten later
    ufw allow from 10.0.0.0/8 to any port 9090 comment 'Prometheus (solar monitor)'
    ufw allow from 172.16.0.0/12 to any port 9090 comment 'Prometheus (solar monitor)'
    ufw allow from 192.168.0.0/16 to any port 9090 comment 'Prometheus (solar monitor)'
    ufw allow from 10.0.0.0/8 to any port 9100 comment 'node_exporter (solar monitor)'
    ufw allow from 172.16.0.0/12 to any port 9100 comment 'node_exporter (solar monitor)'
    ufw allow from 192.168.0.0/16 to any port 9100 comment 'node_exporter (solar monitor)'

    # Enable ufw non-interactively
    ufw --force enable
    echo "      Firewall configured. Ports 9090 and 9100 open to private networks."
    echo "      To restrict to a specific host:"
    echo "        sudo ufw delete allow from 192.168.0.0/16 to any port 9090"
    echo "        sudo ufw allow from <HOST_IP> to any port 9090"
else
    echo "      ufw not found — skipping firewall setup."
    echo "      Manually allow ports 9090 and 9100 from your Grafana host."
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

# --- [6/13] Hardware watchdog ---
echo "[6/13] Configuring hardware watchdog..."
CONFIG_TXT=""
if [[ -f /boot/firmware/config.txt ]]; then
    CONFIG_TXT="/boot/firmware/config.txt"
elif [[ -f /boot/config.txt ]]; then
    CONFIG_TXT="/boot/config.txt"
fi

if [[ -n "$CONFIG_TXT" ]]; then
    if grep -q 'dtparam=watchdog=on' "$CONFIG_TXT"; then
        echo "      Hardware watchdog already enabled in $CONFIG_TXT"
    else
        echo "" >> "$CONFIG_TXT"
        echo "# Hardware watchdog (solar_hydroponic monitor)" >> "$CONFIG_TXT"
        echo "dtparam=watchdog=on" >> "$CONFIG_TXT"
        echo "      Enabled hardware watchdog in $CONFIG_TXT"
    fi
else
    echo "      WARNING: Could not find Pi config.txt — manually add: dtparam=watchdog=on"
fi

MODPROBE_CONF="/etc/modprobe.d/bcm2835_wdt.conf"
if [[ ! -f "$MODPROBE_CONF" ]]; then
    echo "options bcm2835_wdt heartbeat=60 nowayout=0" > "$MODPROBE_CONF"
    echo "      Created $MODPROBE_CONF (watchdog timeout=60s)"
else
    echo "      $MODPROBE_CONF already exists, skipping."
fi
echo "      NOTE: A reboot is required for watchdog changes to take effect."

# --- [7/13] User group memberships ---
echo "[7/13] Adding $SERVICE_USER to hardware groups..."
usermod -aG dialout,gpio,i2c "$SERVICE_USER"
echo "      Added to: dialout (serial), gpio, i2c"
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

systemctl daemon-reload
systemctl enable renogy.service waterflow.service battery_shutdown.service
echo "      Services enabled (renogy, waterflow, battery_shutdown)."

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
echo " Grafana datasource:"
echo "   Add a Prometheus datasource pointing at:"
echo "   http://$(hostname -I | awk '{print $1}'):9090"
echo ""
echo " Config reload without restart (SIGHUP):"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value renogy)"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value waterflow)"
echo ""
echo " Service management:"
echo "   sudo systemctl status renogy waterflow battery_shutdown prometheus prometheus-node-exporter"
echo "   sudo journalctl -u renogy -f"
echo "   sudo journalctl -u waterflow -f"
echo "   sudo journalctl -u battery_shutdown -f"
echo ""
echo " Verify metrics:"
echo "   curl http://localhost:9100/metrics | grep waterflow_inlet_lpm"
echo "   curl http://localhost:9090/api/v1/query?query=waterflow_inlet_lpm"
echo ""
