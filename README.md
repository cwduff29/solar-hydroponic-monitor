# Solar Hydroponic NFT Monitor

A Raspberry Pi-based monitoring and control system for a solar-powered NFT (Nutrient Film Technique) hydroponic system. Monitors a Renogy solar charge controller via Modbus and controls/monitors water pumps, aeration, fans, and environmental sensors. Exports metrics to Prometheus for Grafana visualization.

## Hardware

- Raspberry Pi Zero (or equivalent)
- Renogy solar charge controller (Modbus/RS-485)
- Dual YF-S201 water flow sensors (inlet + outlet)
- BME280 I2C sensor (enclosure temperature, humidity, pressure)
- DS18B20 1-Wire temperature sensors (reservoir, NFT drain, outdoor)
- 4-channel relay board (main pump, backup pump, aerator, ventilation fan)
- LiFePO4 battery (50Ah 12V system)

## Features

- Solar charge controller monitoring (battery SOC, voltage, solar power, faults)
- Dual flow sensor monitoring with leak detection and flow imbalance alerts
- Automatic pump recovery and backup pump failover
- Battery-based load shedding (aeration reduced/disabled at low SOC)
- Environmental monitoring (temperature, humidity, dew point)
- Smart ventilation fan control
- Freeze risk warnings
- NFT pipe temperature tracking (solar heating analysis)
- Flow trend analysis (24h baseline, degradation detection)
- Daily water volume tracking
- Email alerts with cooldown/reminder throttling
- Alert state persistence across reboots
- Prometheus metrics export via node_exporter textfile collector
- Hardware watchdog (Pi BCM2835)
- Daily summary emails
- SIGHUP config reload (no restart needed for threshold changes)

## Architecture

Three Python scripts run as systemd services:

| Script | Purpose |
|--------|---------|
| `renogy.py` | Reads Renogy charge controller via Modbus, exports solar/battery metrics |
| `waterflow_enhanced_failsafe.py` | Controls pumps/aeration/fan, monitors sensors |
| *(node_exporter)* | Ships `/ramdisk/*.prom` textfiles to Prometheus |

Shared code lives in `monitor_common.py` (alert management, email, watchdog, daily summaries).
All configuration is in `config.json` — no code changes needed for threshold tuning.

## Installation

### On the Raspberry Pi

```bash
git clone https://github.com/cwduff29/solar-hydroponic-monitor.git
cd solar-hydroponic-monitor
cp credentials.py.example credentials.py
nano credentials.py          # fill in Gmail address and app password
sudo bash install.sh
sudo reboot
```

After reboot, verify services are running:

```bash
sudo systemctl status renogy waterflow prometheus-node-exporter
```

### Gmail App Password

The system sends alerts via Gmail SMTP. You need a Gmail **App Password** (not your account password):

1. Enable 2-factor authentication on your Google account
2. Go to Google Account → Security → App Passwords
3. Create an app password for "Mail"
4. Use that 16-character password in `credentials.py`

## Configuration

All tunable values are in `config.json`. Edit it directly on the Pi:

```bash
nano /path/to/solar-hydroponic-monitor/config.json
```

Then reload without restarting services:

```bash
sudo kill -HUP $(systemctl show -p MainPID --value renogy)
sudo kill -HUP $(systemctl show -p MainPID --value waterflow)
```

Key configuration sections:

- `renogy.thresholds` — battery voltage/SOC/temperature alert levels
- `waterflow.flow` — flow sensor calibration and alert thresholds
- `waterflow.temperature` — temperature alert thresholds (in °C)
- `waterflow.battery_load_shedding` — SOC thresholds for load shedding
- `waterflow.aeration` — aeration on/off timing
- `alerts` — email cooldown period and daily summary hour

## DS18B20 Sensor Identification

To map sensor IDs to logical names in `config.json`:

```bash
ls /sys/bus/w1/devices/28-*/
# Place sensors one at a time in known locations and record the ID
```

Update `config.json` under `waterflow.temperature.sensor_map`.

## Prometheus & Grafana Setup

### How Metrics Flow

```
renogy.py          ──┐
                     ├──▶  /ramdisk/*.prom  ──▶  node_exporter  ──▶  Prometheus  ──▶  Grafana
waterflow.py       ──┘        (textfiles)         (port 9100)      (Proxmox)
```

The Python scripts write Prometheus-format text files to `/ramdisk` (a tmpfs ramdisk to avoid SD card wear). `prometheus-node-exporter` is configured with `--collector.textfile.directory=/ramdisk`, which picks up these files and exposes them on port 9100 alongside standard system metrics (CPU, memory, disk, network).

### Prometheus Configuration (on Proxmox)

Add a scrape job to your Prometheus config (`prometheus.yml`):

```yaml
scrape_configs:
  - job_name: 'solar_hydroponic_pi'
    static_configs:
      - targets: ['<PI_IP_ADDRESS>:9100']
    scrape_interval: 15s
    labels:
      instance: 'solar_hydroponic_pi'
```

Replace `<PI_IP_ADDRESS>` with the Pi's static IP or hostname. After editing, reload Prometheus:

```bash
# On Proxmox, typically:
systemctl reload prometheus
# or if running in a container:
curl -X POST http://localhost:9090/-/reload
```

Verify the Pi is being scraped at `http://<PROXMOX_IP>:9090/targets`.

### Grafana Dashboard

A pre-built dashboard is included: `Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json`

**Import steps:**

1. In Grafana, go to **Dashboards → Import**
2. Click **Upload JSON file** and select the JSON file
3. Select your Prometheus datasource when prompted
4. Click **Import**

**Dashboard sections:**

- **System Status** — maintenance mode flags, monitor health, last update age
- **Solar & Battery** — SOC, voltage, charging state, solar power/current, daily generation
- **Battery Details** — temperature, capacity, over-discharge events, cumulative stats
- **Water Flow** — inlet/outlet flow rates, smoothed vs raw, imbalance, flow trend analysis, daily volume
- **Pump Status** — main/backup pump state, recovery status, backup failover
- **Aeration & Fan** — aeration state/mode, fan state, load shedding
- **Temperature** — reservoir, NFT drain, outdoor temps (°C and °F), solar heating differential, freeze risk
- **Enclosure Environment** — BME280 temperature, humidity, dew point, pressure
- **Active Alerts** — all alert states at a glance
- **System Health** — Pi CPU, memory, disk, uptime, network, Modbus error rate
- **Historical** — cumulative solar generation, total operating days, battery cycles

### Datasource UID

The dashboard JSON references a Prometheus datasource. If your datasource UID differs from the one in the JSON, update it after import via Grafana UI, or do a find-and-replace in the JSON file before importing:

```bash
sed -i 's/"uid": "prometheus"/"uid": "YOUR_UID_HERE"/g' Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json
```

Find your datasource UID in Grafana under **Configuration → Data Sources → (your Prometheus) → URL** — the UID is in the browser URL.

### Port Access

Ensure port 9100 is accessible from Prometheus on Proxmox. On the Pi:

```bash
# Check node_exporter is listening
curl http://localhost:9100/metrics | grep waterflow_inlet_lpm
```

If using a firewall:

```bash
sudo ufw allow from <PROXMOX_IP> to any port 9100
```

## Service Management

```bash
# Status
sudo systemctl status renogy waterflow prometheus-node-exporter

# Logs (live)
sudo journalctl -u renogy -f
sudo journalctl -u waterflow -f

# Restart
sudo systemctl restart renogy waterflow

# Maintenance mode (edit config.json then SIGHUP)
sudo kill -HUP $(systemctl show -p MainPID --value renogy)
sudo kill -HUP $(systemctl show -p MainPID --value waterflow)
```

## Maintenance Mode

Edit `config.json` to set disable flags, then send SIGHUP:

```json
"maintenance": {
  "disable_emails": false,
  "disable_low_battery_alerts": false,
  "disable_fault_alerts": false,
  "disable_temperature_alerts": false,
  "disable_capacity_alerts": false,
  "disable_pump_testing": false,
  "disable_flow_alerts": false,
  "disable_aerator": false,
  "disable_fan": false
}
```

## File Structure

```
solar-hydroponic-monitor/
├── install.sh                    # One-command installer
├── config.json                   # All configuration (edit this, not the scripts)
├── credentials.py.example        # Email credentials template
├── credentials.py                # Your credentials (not in git)
├── monitor_common.py             # Shared: AlertManager, email, watchdog, config
├── renogy.py                     # Renogy charge controller monitor
├── renogy_extended.py            # Renogy Modbus driver with batch reads
├── waterflow_enhanced_failsafe.py # Hydroponic system controller
├── systemd/
│   ├── renogy.service
│   └── waterflow.service
├── logrotate/
│   ├── renogy
│   └── waterflow
└── Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json
```
