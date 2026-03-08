#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Solar Hydroponic Monitor - Installation Script
# ============================================================

INSTALL_DIR="$(dirname "$(realpath "$0")")"
SERVICE_USER="${SUDO_USER:-pi}"

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
echo "================================================="
echo ""

# --- Credentials ---
if [[ ! -f "$INSTALL_DIR/credentials.py" ]]; then
    echo ">>> credentials.py not found. Copying from example..."
    cp "$INSTALL_DIR/credentials.py.example" "$INSTALL_DIR/credentials.py"
    echo "    IMPORTANT: Edit $INSTALL_DIR/credentials.py before starting services."
    echo ""
fi

# --- Python dependencies ---
echo "[1/9] Installing Python packages..."
# Try with --break-system-packages for Raspberry Pi OS Bookworm+, fall back for older
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280 --break-system-packages 2>/dev/null || \
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280
echo "      Done."

# --- Enable hardware interfaces ---
echo "[2/9] Installing Prometheus node_exporter..."
apt-get install -y prometheus-node-exporter

# Configure textfile collector to pick up /ramdisk/*.prom files
OVERRIDE_DIR="/etc/systemd/system/prometheus-node-exporter.service.d"
mkdir -p "$OVERRIDE_DIR"
cat > "$OVERRIDE_DIR/textfile.conf" << 'EOF'
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
echo "      Textfile collector: /ramdisk/*.prom"
echo "      Metrics endpoint:   http://$(hostname -I | awk '{print $1}'):9100/metrics"

# --- Enable hardware interfaces ---
echo "[3/9] Enabling hardware interfaces..."
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_serial_hw 0    # Enable UART hardware
    raspi-config nonint do_serial_cons 1  # Disable serial login console (needed for /dev/serial0)
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

# --- Hardware watchdog ---
echo "[4/9] Configuring hardware watchdog..."

# Enable the Pi hardware watchdog in /boot/firmware/config.txt (Bookworm+)
# or /boot/config.txt (Bullseye and older)
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
        echo "# Hardware watchdog (required for solar_hydroponic monitor)" >> "$CONFIG_TXT"
        echo "dtparam=watchdog=on" >> "$CONFIG_TXT"
        echo "      Enabled hardware watchdog in $CONFIG_TXT"
    fi
else
    echo "      WARNING: Could not find /boot/firmware/config.txt or /boot/config.txt"
    echo "      Manually add 'dtparam=watchdog=on' to your Pi config file."
fi

# Configure watchdog daemon timeout (60 seconds = 2× our 30s keepalive interval)
# We do this via /etc/systemd/system.conf RuntimeWatchdogSec or modprobe options.
# The simplest approach on Pi OS is to set the bcm2835_wdt module option.
MODPROBE_CONF="/etc/modprobe.d/bcm2835_wdt.conf"
if [[ ! -f "$MODPROBE_CONF" ]]; then
    echo "options bcm2835_wdt heartbeat=60 nowayout=0" > "$MODPROBE_CONF"
    echo "      Created $MODPROBE_CONF (watchdog timeout=60s, nowayout=0)"
else
    echo "      $MODPROBE_CONF already exists, not overwriting."
fi

echo "      NOTE: A reboot is required for watchdog changes to take effect."

# --- User group memberships ---
echo "[5/9] Adding $SERVICE_USER to hardware groups..."
usermod -aG dialout,gpio,i2c "$SERVICE_USER"
echo "      Added to: dialout (serial), gpio, i2c"
echo "      NOTE: Group changes take effect after next login / reboot."

# --- Ramdisk ---
echo "[6/9] Setting up ramdisk at /ramdisk..."
mkdir -p /ramdisk
if grep -q '/ramdisk' /etc/fstab; then
    echo "      /ramdisk already in /etc/fstab, skipping."
else
    echo "tmpfs /ramdisk tmpfs nodev,nosuid,size=50M 0 0" >> /etc/fstab
    echo "      Added tmpfs /ramdisk entry to /etc/fstab."
fi
mount /ramdisk 2>/dev/null && echo "      Mounted /ramdisk." || echo "      /ramdisk already mounted."
chown "$SERVICE_USER":root /ramdisk
chmod 775 /ramdisk

# --- Persistent state directory ---
echo "[7/9] Creating persistent state directory /var/lib/renogy/..."
mkdir -p /var/lib/renogy
chown "$SERVICE_USER":root /var/lib/renogy
chmod 750 /var/lib/renogy
echo "      /var/lib/renogy/ created (stores alert state across reboots)"

# --- Log files ---
echo "[8/9] Creating log files..."
touch /var/log/renogy.log /var/log/waterflow.log
chown "$SERVICE_USER":adm /var/log/renogy.log /var/log/waterflow.log
chmod 664 /var/log/renogy.log /var/log/waterflow.log
echo "      /var/log/renogy.log"
echo "      /var/log/waterflow.log"

cp "$INSTALL_DIR/logrotate/renogy"    /etc/logrotate.d/renogy
cp "$INSTALL_DIR/logrotate/waterflow" /etc/logrotate.d/waterflow
echo "      Logrotate configs installed."

# --- Systemd services ---
echo "[9/9] Installing and enabling systemd services..."
sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/renogy.service" > /etc/systemd/system/renogy.service

sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/systemd/waterflow.service" > /etc/systemd/system/waterflow.service

systemctl daemon-reload
systemctl enable renogy.service waterflow.service
echo "      Services enabled."

if [[ -f "$INSTALL_DIR/credentials.py" ]] && ! grep -q "your_email" "$INSTALL_DIR/credentials.py"; then
    systemctl start renogy.service waterflow.service
    echo "      Services started."
else
    echo "      Services NOT started — edit credentials.py first, then run:"
    echo "        sudo systemctl start renogy waterflow"
fi

echo ""
echo "================================================="
echo " Installation complete!"
echo "================================================="
echo ""
echo " Next steps:"
echo "   1. Edit credentials.py with your email settings"
echo "   2. Reboot to apply interface, watchdog, and group changes:"
echo "        sudo reboot"
echo ""
echo " Config reload without restart (SIGHUP):"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value renogy)"
echo "   sudo kill -HUP \$(systemctl show -p MainPID --value waterflow)"
echo ""
echo " Service management:"
echo "   sudo systemctl status renogy waterflow prometheus-node-exporter"
echo "   sudo journalctl -u renogy -f"
echo "   sudo journalctl -u waterflow -f"
echo ""
echo " Verify metrics are being exported:"
echo "   curl http://localhost:9100/metrics | grep waterflow_inlet_lpm"
echo ""
