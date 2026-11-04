# Solar Hydroponic NFT Monitor

A Raspberry Pi-based monitoring and control system for a solar-powered NFT (Nutrient Film Technique) hydroponic system. Monitors a Renogy solar charge controller via Modbus and controls/monitors water pumps, aeration, fans, and environmental sensors. Exports metrics to Prometheus for Grafana visualization.

## Hardware

- Raspberry Pi (Zero or equivalent)
- Renogy solar charge controller (Modbus/RS-485)
- Dual YF-S201 water flow sensors (inlet + outlet)
- BME280 I2C sensor (enclosure temperature, humidity, pressure)
- DS18B20 1-Wire temperature sensors (reservoir, NFT drain, outdoor)
- 4-channel relay board (backup pump, aerator, ventilation fan; main pump relay only in non-PWM mode)
- PWM MOSFET switch module for the brushless main pump (GPIO12)
- LiFePO4 battery (50Ah 12V system)

### Power and fail-safe wiring

Every load (both pumps, aerator, fan, the Pi and its 5 V buck converter, the TTL/RS-485
converter, logic level converter and all sensors) is powered from the **Renogy load
terminal**, through a breaker, onto a 12 V rail. The controller's load current therefore
measures the whole system, and its low-voltage disconnect (LVD) cuts everything.

| Load | Switched by | If the Pi dies / loses power |
|------|-------------|------------------------------|
| Main pump (brushless) | MOSFET module, PWM on GPIO12 (`main_pump_brushless_pwm.enabled`) | **Off** (no gate signal) |
| Backup pump | Relay **NC** contact (`backup_pump_terminal: "NC"`) | **On** (relay de-energized) |
| Ventilation fan | Relay **NC** contact | On |
| Aerator | Relay **NO** contact | Off |

So when the Pi is down the **backup pump** keeps water moving, at full power and with no
load shedding. `main_pump_relay` / `main_pump_terminal` are only used when brushless PWM
mode is disabled.

## Features

- Solar charge controller monitoring (battery SOC, voltage, solar power, faults)
- Dual flow sensor monitoring with leak detection and flow imbalance alerts
- Automatic pump recovery and backup pump failover
- Battery SOC estimator (net-current coulomb counting with voltage anchors; see below)
- Battery-based load shedding (aeration reduced/disabled at low SOC; the SOC tier caps the water-temperature tier)
- Optional low-SOC main pump power saving (reduced PWM speed, bounded on/off cycling; off by default)
- Critical-battery alert (optional halt) via `battery_shutdown.py`
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
renogy.py            ──┐
waterflow.py         ──┼──▶  /ramdisk/*.prom  ──▶  node_exporter :9100  ──▶  central Prometheus  ──▶  Grafana
battery_shutdown.py  ──┘        (tmpfs)
```

| Service | Purpose |
|---------|---------|
| `renogy.py` | Reads Renogy charge controller via Modbus, exports solar/battery metrics |
| `waterflow_enhanced_failsafe.py` | Controls pumps/aeration/fan, monitors all sensors |
| `battery_shutdown.py` | Critical-battery alert (default) or halt; runs as root |
| `prometheus-node-exporter` | Exposes `/ramdisk/*.prom` textfiles + system metrics on port 9100 |
| `prometheus` (optional) | Local scraper on port 9090, only with `INSTALL_LOCAL_PROMETHEUS=1` |

Shared code lives in `monitor_common.py` (alert management, email, watchdog, daily summaries).
Pure decision logic lives in `soc_estimator.py` and `load_shedding.py` (no hardware access, unit-tested).
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

Installer options (environment variables):

| Variable | Default | Effect |
|----------|---------|--------|
| `INSTALL_LOCAL_PROMETHEUS` | `0` | `1` installs Prometheus on the Pi (port 9090). With `0`, an existing local Prometheus service is stopped and disabled. |
| `SCRAPE_HOSTS` | `192.168.99.201 192.168.99.224` | Hosts allowed through ufw to port 9100 (and 9090 with local Prometheus). Older broad private-range rules are removed. |

```bash
sudo INSTALL_LOCAL_PROMETHEUS=0 SCRAPE_HOSTS="192.168.99.201 192.168.99.224" bash install.sh
```

After reboot, verify all services are running:

```bash
sudo systemctl status renogy waterflow battery_shutdown prometheus-node-exporter
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
| `renogy.thresholds` | Battery voltage/SOC/temperature alert levels (voltage is compared at rest, sustained) |
| `renogy.soc_estimator` | SOC estimator parameters (internal resistance, anchors, persistence) |
| `waterflow.flow` | Flow sensor calibration, min threshold, leak detection |
| `waterflow.temperature` | Temperature alert thresholds (°C) |
| `waterflow.battery_load_shedding` | SOC thresholds for aeration load shedding (+ tier hysteresis) |
| `waterflow.main_pump_low_soc` | Optional low-SOC main pump tiers (`enabled: false` by default) |
| `shutdown` | `battery_shutdown.py` mode (`alert` / `halt` / `off`) and critical condition |
| `waterflow.aeration` | Aeration on/off cycle timing |
| `waterflow.gpio` | GPIO pin assignments for relays and flow sensors |
| `alerts` | Email cooldown period and daily summary hour |

## Battery State of Charge

The Renogy SOC register is driven by voltage and is unreliable on LiFePO4 (on 2026-10-30 it
read 57–92 % while the battery was at 0–8 %). `renogy.py` therefore exports the output of
`soc_estimator.py` as `battery_soc` and the raw register as `battery_soc_hw`.

The estimator:
- integrates **net** current: charge current (solar power × MPPT efficiency ÷ battery voltage)
  minus load-terminal current;
- anchors to **100 %** on charge completion (boost/float at ≥ 14.0 V for 10 min) or a sustained
  rest-equivalent voltage ≥ 13.4 V;
- **clamps down** when the rest-compensated voltage implies ≤ 20 %;
- bridges restarts and Pi outages using the controller's cumulative Ah counters;
- flags an **external charger** on the battery terminals (invisible to the controller) when the
  voltage is far above what the metered current allows;
- exports `battery_soc_confidence` (0–1), `battery_soc_hw_rejected`,
  `battery_external_charge_suspected`, `battery_voltage_rest_estimate` and `battery_net_current`.

State is saved to `/var/lib/renogy/soc_estimator.json` every 5 minutes and on stop. Estimator
events (seed, anchors, clamps, bridges) are logged at WARNING: `journalctl -u renogy | grep "SOC estimator"`.

The voltage-to-SOC table is an **at-rest** LiFePO4 table (12.8 V = 20 %, matching the 12.8 V /
20 % alert thresholds). Voltages under load or charge are compensated by I × R (≈ 26 mΩ) first.

## Battery Protection

The Renogy load-terminal **LVD/LVR** is the real battery protection: it cuts every load,
including the backup pump. Recommended settings for this 50 Ah LiFePO4 pack: LVD ≈ 11.8 V
(with the controller's delay), LVR ≈ 12.9 V, under-voltage warning ≈ 12.2 V. A low LVR lets the
load back on with an almost empty battery. The BMS (10.0 V) is the last resort.

`battery_shutdown.py` (`shutdown.mode`):
- `alert` (default): when estimated SOC ≤ `critical_soc_pct`, estimator confidence ≥
  `min_confidence` and rest voltage ≤ `max_rest_voltage` hold for `sustained_seconds`, send one
  email and set `battery_shutdown_condition` = 1. Never halts.
- `halt`: same condition halts the Pi. Note that a halted Pi stops the main pump and releases the
  NC backup pump (full power, no monitoring), and nothing restarts the Pi unless the LVD cycles.
- `off`: idle.

To read the controller's LVD/LVR settings (read-only; run as the service user while the
services are running; it waits for `renogy.py` to finish a poll):

```bash
python3 -c "import minimalmodbus,os,time;p='/ramdisk/Renogy.prom';m=os.path.getmtime(p)
while os.path.getmtime(p)==m: time.sleep(0.2)
i=minimalmodbus.Instrument('/dev/serial0',1);i.serial.baudrate=9600;i.serial.timeout=1
[print(hex(0xE000+k),v) for k,v in enumerate(i.read_registers(0xE000,21,functioncode=3))]"
```
(0xE00B = LVR, 0xE00C = under-voltage warning, 0xE00D = LVD, in units of 0.1 V.)

## Low-SOC Main Pump Tiers (optional)

`waterflow.main_pump_low_soc` has two tiers, both `"enabled": false` by default:
- `reduced_speed`: below `enter_below_soc_pct` (40 %), run the main pump at `duty_cycle_pct` (55 %)
  until SOC is back above `exit_above_soc_pct` (50 %).
- `cycling`: below 20 % (back above 30 %), alternate `on_minutes` / `off_minutes` windows. An
  episode always starts ON and an OFF window is hard-capped at 30 minutes.

Both fall back to normal running if the SOC is unknown, estimator confidence < `min_confidence`,
or the backup pump is active. Cycling is also skipped with any flow/leak alert active or the water
at or above `skip_at_or_above_water_temp_c`. Planned OFF windows (plus `restart_grace_seconds`)
never trigger pump recovery/failover; a pump that fails to restart is detected normally after
that. Metrics: `waterflow_main_pump_mode`, `waterflow_main_pump_planned_off`.

Measure the pump's current at 70/60/50 % duty before enabling `reduced_speed`.

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

The Python scripts write Prometheus text-format files to `/ramdisk` (tmpfs — avoids SD card writes). `prometheus-node-exporter` picks up `*.prom` files and exposes them on port 9100 alongside standard system metrics. A central Prometheus scrapes port 9100; Grafana uses that Prometheus as its datasource. (A local Prometheus on the Pi is optional — `INSTALL_LOCAL_PROMETHEUS=1` — and is the largest source of SD card writes.)

### Scrape Job

On the central Prometheus:

```yaml
scrape_configs:
  - job_name: 'solar_hydroponic'
    static_configs:
      - targets: ['<PI_IP_ADDRESS>:9100']
    scrape_interval: 15s
```

Ensure port 9100 is reachable from that host (`SCRAPE_HOSTS` in `install.sh`):
```bash
sudo ufw allow from <PROMETHEUS_HOST_IP> to any port 9100 proto tcp
curl -s http://<PI_IP_ADDRESS>:9100/metrics | grep battery_soc
```

### Grafana Dashboard

A pre-built dashboard is included: `Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json`

**Import steps:**
1. In Grafana go to **Dashboards → Import**
2. Click **Upload JSON file** and select the file
3. Select the Prometheus datasource that scrapes the Pi when prompted
4. Click **Import**

**Dashboard sections:**

| Section | Panels |
|---------|--------|
| System Status | Maintenance flags, monitor health, last update age |
| Solar & Battery | SOC estimate vs hardware register + confidence, voltage, charging state, solar power/current, daily generation |
| Battery Details | Temperature, capacity, over-discharge events, cumulative stats |
| Water Flow | Inlet/outlet flow, smoothed vs raw, imbalance, trend analysis, daily volume |
| Pump Status | Main/backup pump state, recovery status, backup failover |
| Aeration & Fan | Aeration state/mode, fan state, load shed tier |
| Temperature | Reservoir, NFT drain, outdoor (°C and °F), solar heating differential |
| Enclosure Environment | BME280 temperature, humidity, dew point, pressure |
| Active Alerts | All alert states at a glance |
| System Health | Pi CPU, memory, disk, uptime, network, Modbus error rate |
| Historical | Cumulative solar generation, total operating days, battery cycles |
| Energy budget | Daily Ah in/out/net from the controller counters, main pump low-SOC mode, critical battery condition, estimator confidence, external charge |

### Datasource UID

The dashboard JSON hardcodes a datasource UID. If yours differs, replace it before importing:

```bash
sed -i 's/"uid": "bev6xt3wf7n5sb"/"uid": "YOUR_UID_HERE"/g' \
  Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json
```

Find your UID in Grafana under **Connections → Data Sources → (your datasource)** — it appears in the browser URL.

### Live dashboard export

`grafana/solar-enhanced-dashboard.json` is an export of the dashboard actually
running in Grafana (uid `solar_enhanced`), including the boot-cause panels and
the Pi system panels filtered to `job="solar_hydroponic"`, and the Battery &
Energy and Flow & Alerts sections taken from the pre-built dashboard (all of its
panels 1000-1016 except the duplicate Water Temperatures and Leak Detected).
Every query uses the `prometheus` datasource (LXC 108, uid `efun0ldncfhtsc`);
replace that UID as above if yours differs.

## Service Management

```bash
# Status
sudo systemctl status renogy waterflow battery_shutdown prometheus-node-exporter

# Live logs
sudo journalctl -u renogy -f
sudo journalctl -u waterflow -f

# Restart
sudo systemctl restart renogy waterflow

# Reload config without restart
sudo kill -HUP $(systemctl show -p MainPID --value renogy)
sudo kill -HUP $(systemctl show -p MainPID --value waterflow)
sudo kill -HUP $(systemctl show -p MainPID --value battery_shutdown)

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

## Hardware Setup Scripts

Two helper scripts in `scripts/` make initial hardware wiring verification much easier.

### Identify DS18B20 Sensors

```bash
python3 scripts/identify_sensors.py
```

Refreshes every 5 seconds showing each sensor ID and its current temperature. Place sensors one at a time in known locations to identify which ID belongs to which sensor. Press Ctrl+C to print a ready-to-paste `sensor_map` snippet for `config.json`.

### Test Hardware Wiring

```bash
python3 scripts/test_hardware.py
```

Interactive 6-section test run before starting the services for the first time:
1. **Serial port** — confirms `/dev/serial0` is accessible for Renogy Modbus
2. **BME280** — reads temperature, humidity, and pressure over I2C
3. **DS18B20** — lists all 1-Wire sensors and reads temperatures
4. **Relays** — toggles each relay (main pump, backup pump, aerator, fan) with user confirmation
5. **Flow sensors** — counts pulses on inlet/outlet GPIOs for 10 seconds
6. **Summary** — color-coded PASS/WARN/FAIL table

## Tests

The decision logic is tested without hardware (fake GPIO, no Modbus, no email):

```bash
python3 -m unittest discover -s tests -v
```

`tests/test_soc_estimator.py` includes a replay of the recorded 2026-10-30 deep discharge
(`tests/fixtures/oct30_deep_discharge.csv`).

## File Structure

```
solar-hydroponic-monitor/
├── install.sh                    # One-command installer (12 steps)
├── config.json                   # All configuration (edit this, not the scripts)
├── credentials.env.example       # Email credentials template
├── monitor_common.py             # Shared: AlertManager, email, watchdog, config
├── renogy.py                     # Renogy charge controller monitor
├── renogy_extended.py            # Renogy Modbus driver with batch reads
├── soc_estimator.py              # Battery SOC estimator (pure, no hardware)
├── load_shedding.py              # Load-shedding / low-SOC pump decisions (pure)
├── battery_shutdown.py           # Critical-battery alert (or halt)
├── waterflow_enhanced_failsafe.py # Hydroponic system controller
├── scripts/
│   ├── identify_sensors.py       # Map DS18B20 sensor IDs to locations
│   └── test_hardware.py          # Interactive hardware wiring verification
├── tests/                        # Unit tests (python3 -m unittest discover -s tests)
├── prometheus/
│   └── prometheus.yml            # Optional local Prometheus scrape config
├── systemd/
│   ├── renogy.service
│   ├── waterflow.service
│   └── battery_shutdown.service  # Runs as root, critical-battery alert/halt
├── logrotate/
│   ├── renogy
│   └── waterflow
└── Solar___Hydroponic_NFT_System_-_Complete_Monitoring-updated.json

# Created by install.sh (not in repo):
/etc/solar-hydroponic/credentials.env   # Email credentials (root:serviceuser, mode 640)
/var/lib/renogy/                         # Persistent alert + SOC estimator state across reboots
/ramdisk/                                # tmpfs for *.prom metric files
```
