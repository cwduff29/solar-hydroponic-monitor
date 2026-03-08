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
echo "[1/6] Installing Python packages..."
# Try with --break-system-packages for Raspberry Pi OS Bookworm+, fall back for older
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280 --break-system-packages 2>/dev/null || \
pip3 install renogymodbus RPi.GPIO smbus2 RPi.bme280
echo "      Done."

# --- Enable hardware interfaces ---
echo "[2/6] Enabling hardware interfaces..."
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

# --- User group memberships ---
echo "[3/6] Adding $SERVICE_USER to hardware groups..."
usermod -aG dialout,gpio,i2c "$SERVICE_USER"
echo "      Added to: dialout (serial), gpio, i2c"
echo "      NOTE: Group changes take effect after next login / reboot."

# --- Ramdisk ---
echo "[4/6] Setting up ramdisk at /ramdisk..."
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

# --- Log files ---
echo "[5/6] Creating log files..."
touch /var/log/renogy.log /var/log/waterflow.log
chown "$SERVICE_USER":adm /var/log/renogy.log /var/log/waterflow.log
chmod 664 /var/log/renogy.log /var/log/waterflow.log
echo "      /var/log/renogy.log"
echo "      /var/log/waterflow.log"

cp "$INSTALL_DIR/logrotate/renogy"    /etc/logrotate.d/renogy
cp "$INSTALL_DIR/logrotate/waterflow" /etc/logrotate.d/waterflow
echo "      Logrotate configs installed."

# --- Systemd services ---
echo "[6/6] Installing and enabling systemd services..."
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
echo "   2. Reboot to apply interface and group changes:"
echo "        sudo reboot"
echo ""
echo " Service management:"
echo "   sudo systemctl status renogy"
echo "   sudo systemctl status waterflow"
echo "   sudo journalctl -u renogy -f"
echo "   sudo journalctl -u waterflow -f"
echo ""
