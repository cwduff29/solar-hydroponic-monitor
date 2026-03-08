# Solar Hydroponic NFT Monitor

A Raspberry Pi-based monitoring and control system for a solar-powered NFT (Nutrient Film Technique) hydroponic system. Monitors a Renogy solar charge controller via Modbus and controls/monitors water pumps, aeration, fans, and environmental sensors. Exports metrics to Prometheus for Grafana visualization.

## Hardware

- Raspberry Pi (Zero or equivalent)
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

```
renogy.py          ──┐
                     ├──▶  /ramdisk/*.prom  ──▶  node_exporter :9100  ──▶  Prometheus :9090  ──▶  Grafana (Proxmox)
waterflow.py       ──┘        (tmpfs)
```

| Service | Purpose |
|---------|---------|
| `renogy.py` | Reads Renogy charge controller via Modbus, exports solar/battery metrics |
| `waterflow_enhanced_failsafe.py` | Controls pumps/aeration/fan, monitors all sensors |
| `prometheus-node-exporter` | Exposes `/ramdisk/*.prom` textfiles + system metrics on port 9100 |
| `prometheus` | Scrapes node_exporter locally, serves metrics API on port 9090 |

Shared code lives in `monitor_common.py` (alert management, email, watchdog, daily summaries).
All tunable values are in `config.json` — no code edits needed for threshold changes.

## Installation

### On the Raspberry Pi

```bash
git clone https://github.com/cwduff29/solar-hydroponic-monitor.git
cd solar-hydroponic-monitor
sudo bash install.sh
sudo nano /etc/solar-hydroponic/credentials.env   # fill in Gmail credentials
sudo reboot
```

After reboot, verify all services are running:

```bash
sudo systemctl status renogy waterflow prometheus prometheus-node-exporter
```

### Gmail App Password

Email credentials are stored in `/etc/solar-hydroponic/credentials.env` (outside the repo, root-protected). The system uses Gmail SMTP with an **App Password** — not your Google account password.

**Setup:**
1. Enable 2-factor authentication on your Google account
2. Go to **Google Account → Security → App Passwords**
3. Create an App Password for "Mail"
4. Paste the 16-character password (no spaces) into `credentials.env`

**`/etc/solar-hydroponic/credentials.env`:**
```bash
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASSWORD=yoursixteencharap
SMTP_RECIPIENTS=recipient@gmail.com
```

The file is owned by `root`, readable only by `root` and the service user — credentials never touch the project directory or git.

To send to multiple recipients, comma-separate them:
```bash
SMTP_RECIPIENTS=you@gmail.com,other@gmail.com
```

## Configuration

All tunable values (thresholds, GPIO pins, timing, sensor IDs) are in `config.json`. Edit it on the Pi and reload without restarting:

```bash
nano /path/to/solar-hydroponic-monitor/config.json
sudo kill -HUP $(systemctl show -p MainPID --value renogy)
sudo kill -HUP $(systemctl show -p MainPID --value waterflow)
```

Key sections:

| Section | Contents |
|---------|---------|
| `renogy.thresholds` | Battery voltage/SOC/temperature alert levels |
| `waterflow.flow` | Flow sensor calibration, min threshold, leak detection |
| `waterflow.temperature` | Temperature alert thresholds (°C) |
| `waterflow.battery_load_shedding` | SOC thresholds for aeration load shedding |
| `waterflow.aeration` | Aeration on/off cycle timing |
| `waterflow.gpio` | GPIO pin assignments for relays and flow sensors |
| `alerts` | Email cooldown period and daily summary hour |

## DS18B20 Sensor Identification

To map sensor IDs to logical names in `config.json`:

```bash
ls /sys/bus/w1/devices/28-*/
# Place sensors one at a time in known locations to identify each ID
```

Update `config.json` under `waterflow.temperature.sensor_map`:
```json
"sensor_map": {
  "28-xxxxxxxxxxxx": "reservoir",
  "28-xxxxxxxxxxxx": "nft_drain",
  "28-xxxxxxxxxxxx": "outdoor"
}
```

## Prometheus & Grafana Setup

### How Metrics Flow

The Python scripts write Prometheus text-format files to `/ramdisk` (tmpfs — avoids SD card writes). `prometheus-node-exporter` is configured with `--collector.textfile.directory=/ramdisk`, picking up `*.prom` files and exposing them on port 9100 alongside standard system metrics. The local `prometheus` service scrapes port 9100 every 15 seconds and stores the time-series data. Grafana on Proxmox connects directly to the Pi's Prometheus.

### Grafana Datasource (on Proxmox)

Add a new Prometheus datasource in Grafana pointing at the Pi:

1. In Grafana go to **Connections → Data Sources → Add new**
2. Choose **Prometheus**
3. Set the URL to `http://<PI_IP_ADDRESS>:9090`
4. Click **Save & Test**

Ensure port 9090 is reachable from Proxmox. On the Pi:
```bash
sudo ufw allow from <PROXMOX_IP> to any port 9090
sudo ufw allow from <PROXMOX_IP> to any port 9100
```

Verify Prometheus is scraping correctly:
```bash
curl http://localhost:9090/api/v1/query?query=waterflow_inlet_lpm
```

### Grafana Dashboard

A pre-built dashboard is included: `Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json`

**Import steps:**
1. In Grafana go to **Dashboards → Import**
2. Click **Upload JSON file** and select the file
3. Select your Pi Prometheus datasource when prompted
4. Click **Import**

**Dashboard sections:**

| Section | Panels |
|---------|--------|
| System Status | Maintenance flags, monitor health, last update age |
| Solar & Battery | SOC, voltage, charging state, solar power/current, daily generation |
| Battery Details | Temperature, capacity, over-discharge events, cumulative stats |
| Water Flow | Inlet/outlet flow, smoothed vs raw, imbalance, trend analysis, daily volume |
| Pump Status | Main/backup pump state, recovery status, backup failover |
| Aeration & Fan | Aeration state/mode, fan state, load shedding active |
| Temperature | Reservoir, NFT drain, outdoor (°C and °F), solar heating differential |
| Enclosure Environment | BME280 temperature, humidity, dew point, pressure |
| Active Alerts | All alert states at a glance |
| System Health | Pi CPU, memory, disk, uptime, network, Modbus error rate |
| Historical | Cumulative solar generation, total operating days, battery cycles |

### Datasource UID

The dashboard JSON hardcodes a datasource UID. If yours differs, replace it before importing:

```bash
sed -i 's/"uid": "prometheus"/"uid": "YOUR_UID_HERE"/g' \
  Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json
```

Find your UID in Grafana under **Connections → Data Sources → (your datasource)** — it appears in the browser URL.

## Service Management

```bash
# Status
sudo systemctl status renogy waterflow prometheus prometheus-node-exporter

# Live logs
sudo journalctl -u renogy -f
sudo journalctl -u waterflow -f

# Restart
sudo systemctl restart renogy waterflow

# Reload config without restart
sudo kill -HUP $(systemctl show -p MainPID --value renogy)
sudo kill -HUP $(systemctl show -p MainPID --value waterflow)

# Update credentials
sudo nano /etc/solar-hydroponic/credentials.env
sudo systemctl restart renogy waterflow
```

## Maintenance Mode

Disable specific subsystems by editing `config.json` and sending SIGHUP. The disable flags are in each script's top-level config section and are reflected as Prometheus metrics so you can see maintenance mode status in Grafana.

Common scenarios:

| Scenario | Flags to set |
|----------|-------------|
| Reservoir empty / refilling | `disable_flow_alerts`, `disable_pump_testing` |
| Aerator servicing | `disable_aerator` |
| Fan servicing | `disable_fan` |
| Battery maintenance | `disable_low_battery_alerts`, `disable_capacity_alerts` |
| Testing (no emails) | `disable_emails` |

## File Structure

```
solar-hydroponic-monitor/
├── install.sh                    # One-command installer (11 steps)
├── config.json                   # All configuration (edit this, not the scripts)
├── credentials.env.example       # Email credentials template
├── monitor_common.py             # Shared: AlertManager, email, watchdog, config
├── renogy.py                     # Renogy charge controller monitor
├── renogy_extended.py            # Renogy Modbus driver with batch reads
├── waterflow_enhanced_failsafe.py # Hydroponic system controller
├── prometheus/
│   └── prometheus.yml            # Prometheus scrape config (installed to /etc/prometheus/)
├── systemd/
│   ├── renogy.service
│   └── waterflow.service
├── logrotate/
│   ├── renogy
│   └── waterflow
└── Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json

# Created by install.sh (not in repo):
/etc/solar-hydroponic/credentials.env   # Email credentials (root:serviceuser, mode 640)
/var/lib/renogy/                         # Persistent alert state across reboots
/ramdisk/                                # tmpfs for *.prom metric files
```
