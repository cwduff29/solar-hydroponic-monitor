#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Renogy Solar Charge Controller Monitor - Enhanced Version
- Battery capacity tracking and remaining capacity alerts
- Hardware fault detection and monitoring
- Extended statistics from all MODBUS registers
- Improved error handling with retry logic
- Data validation and sanity checking
- Email throttling (prevents spam)
- Alert cooldown periods
- ERROR-ONLY logging to /ramdisk (no data logging)
- Alert state tracking (AlertManager from monitor_common)
- Prometheus metrics export to /ramdisk
- Batch Modbus reads (fewer bus transactions)
- Hardware watchdog keepalive
- Daily summary email
- SIGHUP config reload
- Startup notification email
"""

import time
import logging
import os
import json
import signal
import traceback
from datetime import datetime, timedelta
from renogy_extended import RenogyRoverExtended

# Load shared modules
from monitor_common import (
    AlertManager,
    send_email,
    Watchdog,
    DailySummary,
    load_config,
    get_config,
    reload_config,
    validate_config,
    startup_selftest,
)

# ============================================================================
# CONFIG LOADING
# ============================================================================

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'config.json')
load_config(_CONFIG_PATH)
validate_config()

# ============================================================================
# MAINTENANCE CONTROLS - INDIVIDUAL SUBSYSTEM DISABLE FLAGS
# ============================================================================
# Set any of these to True to disable specific functions during maintenance:

DISABLE_EMAILS = False              # True = No email alerts sent (all types)
DISABLE_LOW_BATTERY_ALERTS = False  # True = No low battery/SOC warnings
DISABLE_FAULT_ALERTS = False        # True = No hardware fault warnings
DISABLE_TEMPERATURE_ALERTS = False  # True = No temperature warnings
DISABLE_CAPACITY_ALERTS = False     # True = No battery capacity warnings

# Quick presets (uncomment one to use):
# BATTERY_MAINTENANCE: DISABLE_LOW_BATTERY_ALERTS=True, DISABLE_CAPACITY_ALERTS=True
# TESTING_MODE: DISABLE_EMAILS=True
# FULL_MAINTENANCE: All True
# ============================================================================

# Serial port
DEV_NAME = get_config('renogy.serial_port', '/dev/serial0')
SLEEP_TIME = get_config('renogy.poll_interval_seconds', 10)

# Battery Configuration
BATTERY_CAPACITY_AH = get_config('renogy.battery_capacity_ah', 50)
BATTERY_NOMINAL_VOLTAGE = get_config('renogy.battery_nominal_voltage', 12.8)
BATTERY_CHEMISTRY = get_config('renogy.battery_chemistry', 'lifepo4')

# Voltage-to-SOC lookup tables (open-circuit voltage at ~0.1C load, 12 V 4S pack)
_SOC_TABLES = {
    'lifepo4': [
        (13.60, 100), (13.40, 90), (13.30, 70), (13.20, 50),
        (13.10, 30), (13.00, 20), (12.80, 10), (12.50,  5), (12.00,  0),
    ],
    'lead_acid': [
        (12.70, 100), (12.50, 90), (12.42, 75), (12.32, 50),
        (12.20, 25), (12.06, 10), (11.90,  0),
    ],
}

def _voltage_to_soc(voltage, chemistry=None):
    """Linearly interpolate SOC from battery voltage using the chemistry table."""
    if chemistry is None:
        chemistry = BATTERY_CHEMISTRY
    table = _SOC_TABLES.get(chemistry, _SOC_TABLES['lifepo4'])
    if voltage >= table[0][0]:
        return table[0][1]
    if voltage <= table[-1][0]:
        return table[-1][1]
    for i in range(len(table) - 1):
        v_hi, soc_hi = table[i]
        v_lo, soc_lo = table[i + 1]
        if v_lo <= voltage <= v_hi:
            t = (voltage - v_lo) / (v_hi - v_lo)
            return soc_lo + t * (soc_hi - soc_lo)
    return None

# File paths (all in ramdisk to minimize SD writes)
TEMP_FILE_PATH = get_config('paths.renogy_prom_tmp', '/ramdisk/Renogy.prom.tmp')
FINAL_FILE_PATH = get_config('paths.renogy_prom', '/ramdisk/Renogy.prom')
LOG_FILE_PATH = get_config('paths.renogy_log', '/var/log/renogy.log')
STATE_FILE_PATH = get_config('paths.renogy_alert_state', '/ramdisk/renogy_alerts.json')
PERSISTENT_STATE_FILE = get_config('paths.renogy_persistent_state', '/var/lib/renogy/renogy_state.json')

# Critical Thresholds (loaded from config, can be reloaded via SIGHUP)
def _load_thresholds():
    global LOW_BATTERY_VOLTAGE_THRESHOLD, HIGH_CONTROLLER_TEMPERATURE_THRESHOLD
    global HIGH_BATTERY_TEMPERATURE_THRESHOLD, LOW_BATTERY_TEMPERATURE_THRESHOLD
    global LOW_BATTERY_SOC_THRESHOLD, LOW_BATTERY_CAPACITY_AH_THRESHOLD
    global MAX_REASONABLE_VOLTAGE, MIN_REASONABLE_VOLTAGE
    global MAX_REASONABLE_TEMPERATURE, MIN_REASONABLE_TEMPERATURE
    global MAX_REASONABLE_CURRENT, MAX_REASONABLE_POWER
    global EMAIL_COOLDOWN_MINUTES, EMAIL_REMINDER_HOURS

    LOW_BATTERY_VOLTAGE_THRESHOLD         = get_config('renogy.thresholds.low_battery_voltage', 12.8)
    HIGH_CONTROLLER_TEMPERATURE_THRESHOLD = get_config('renogy.thresholds.high_controller_temp_c', 45.0)
    HIGH_BATTERY_TEMPERATURE_THRESHOLD    = get_config('renogy.thresholds.high_battery_temp_c', 50.0)
    LOW_BATTERY_TEMPERATURE_THRESHOLD     = get_config('renogy.thresholds.low_battery_temp_c', 0.0)
    LOW_BATTERY_SOC_THRESHOLD             = get_config('renogy.thresholds.low_battery_soc_pct', 20.0)
    LOW_BATTERY_CAPACITY_AH_THRESHOLD     = get_config('renogy.thresholds.low_battery_capacity_ah', 10.0)
    MAX_REASONABLE_VOLTAGE                = get_config('renogy.thresholds.max_voltage', 20.0)
    MIN_REASONABLE_VOLTAGE                = get_config('renogy.thresholds.min_voltage', 8.0)
    MAX_REASONABLE_TEMPERATURE            = get_config('renogy.thresholds.max_temp_c', 80.0)
    MIN_REASONABLE_TEMPERATURE            = get_config('renogy.thresholds.min_temp_c', -40.0)
    MAX_REASONABLE_CURRENT                = get_config('renogy.thresholds.max_current_a', 30.0)
    MAX_REASONABLE_POWER                  = get_config('renogy.thresholds.max_power_w', 500.0)
    EMAIL_COOLDOWN_MINUTES                = get_config('alerts.email_cooldown_minutes', 60)
    EMAIL_REMINDER_HOURS                  = get_config('alerts.email_reminder_hours', 12)

_load_thresholds()

# Connection retry configuration
MAX_CONNECTION_RETRIES = get_config('renogy.connection.max_retries', 3)
RETRY_DELAY_SECONDS    = get_config('renogy.connection.retry_delay_seconds', 5)
CONNECTION_TIMEOUT_MINUTES = get_config('renogy.connection.timeout_minutes', 10)

# ============================================================================
# LOGGING CONFIGURATION
# ============================================================================

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler()
    ]
)

# ============================================================================
# ALERT STATE MANAGEMENT (uses AlertManager from monitor_common)
# ============================================================================

alert_manager = AlertManager(
    ramdisk_path=STATE_FILE_PATH,
    persistent_path=PERSISTENT_STATE_FILE,
    cooldown_minutes=EMAIL_COOLDOWN_MINUTES,
    reminder_hours=EMAIL_REMINDER_HOURS,
)

# Pre-register known alert types
_KNOWN_ALERTS = [
    'low_battery_voltage',
    'high_controller_temp',
    'high_battery_temp',
    'low_battery_temp',
    'low_battery_soc',
    'low_battery_capacity',
    'hardware_fault',
    'critical_hardware_fault',
    'data_quality_issue',
    'over_discharge_event',
]

# Legacy compatibility shim: expose alert_state dict-like access for
# write_metrics_to_file (Prometheus export reads alert_state directly)
class _AlertStateProxy:
    """Thin proxy so write_metrics_to_file can iterate alert_state like a dict."""
    def __init__(self, manager):
        self._mgr = manager
    def __iter__(self):
        return iter(self._mgr.all_states())
    def __getitem__(self, key):
        return self._mgr.all_states().get(key, {'active': False})
    def keys(self):
        return self._mgr.all_states().keys()

alert_state = _AlertStateProxy(alert_manager)

# Load saved state
alert_manager.load()

# ============================================================================
# SIGHUP HANDLER - reload config without restart
# ============================================================================

def _sighup_handler(signum, frame):
    logging.warning("SIGHUP received: reloading config")
    if reload_config():
        _load_thresholds()
        # Update AlertManager cooldown/reminder from new config
        alert_manager._cooldown_minutes = get_config('alerts.email_cooldown_minutes', 60)
        alert_manager._reminder_hours   = get_config('alerts.email_reminder_hours', 12)
        logging.warning("Config reloaded and thresholds updated")
    else:
        logging.error("Config reload failed")

signal.signal(signal.SIGHUP, _sighup_handler)

# ============================================================================
# CONNECTION MANAGEMENT
# ============================================================================

rover = None
last_successful_connection = None
connection_failures = 0
_using_voltage_soc = False   # hysteresis state for SOC source selection
_cc_soc = None               # coulomb-counted SOC (%), None when not active
_cc_last_time = None         # monotonic timestamp of last integration step

def initialize_rover():
    """Initialize Renogy Rover with retry logic"""
    global rover, last_successful_connection, connection_failures

    for attempt in range(MAX_CONNECTION_RETRIES):
        try:
            rover = RenogyRoverExtended(DEV_NAME, 1, max_retries=3, retry_delay=1.0)
            print(f"Successfully connected to Renogy Rover on {DEV_NAME}")
            last_successful_connection = datetime.now()
            connection_failures = 0
            return True
        except Exception as e:
            connection_failures += 1
            if attempt < MAX_CONNECTION_RETRIES - 1:
                logging.warning(
                    f"Failed to connect to Renogy Rover "
                    f"(attempt {attempt+1}/{MAX_CONNECTION_RETRIES}): {e}"
                )
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
            else:
                logging.critical(
                    f"Failed to initialize Renogy Rover after {MAX_CONNECTION_RETRIES} attempts: {e}"
                )
                return False
    return False

def check_connection_health():
    """Check if connection needs to be re-established"""
    global last_successful_connection

    if last_successful_connection is None:
        return False

    time_since_connection = datetime.now() - last_successful_connection
    if time_since_connection > timedelta(minutes=CONNECTION_TIMEOUT_MINUTES):
        logging.warning(
            f"No successful reads for {CONNECTION_TIMEOUT_MINUTES} minutes, "
            f"reinitializing connection"
        )
        return initialize_rover()

    return True

# ============================================================================
# INITIALIZATION
# ============================================================================

if not initialize_rover():
    logging.critical("Cannot start monitoring without controller connection")
    exit(1)

watchdog = None

# ============================================================================
# DAILY SUMMARY
# ============================================================================

daily_summary = DailySummary(state_file='/var/lib/renogy/renogy_summary_state.json')
DAILY_SUMMARY_HOUR = get_config('alerts.daily_summary_hour', 7)
SUMMARY_INTERVAL_DAYS = get_config('alerts.summary_interval_days', 7)

# ============================================================================
# EMAIL HELPERS
# ============================================================================

def send_email_alert(subject, content):
    """Send an email alert (wraps monitor_common.send_email with disable flag)."""
    return send_email(subject, content, disabled=DISABLE_EMAILS)

# ============================================================================
# STARTUP NOTIFICATION (#4)
# ============================================================================

def send_startup_notification():
    """Run startup self-test and send a startup notification email."""
    try:
        extra_checks = [
            (
                "Serial port",
                lambda: (
                    ("PASS", f"{DEV_NAME} exists")
                    if os.path.exists(DEV_NAME)
                    else ("FAIL", f"{DEV_NAME} not found")
                ),
            ),
            (
                "Renogy connection",
                lambda: (
                    ("PASS", "rover initialized successfully")
                    if rover is not None
                    else ("FAIL", "rover object is None")
                ),
            ),
        ]
        startup_selftest("renogy", _CONFIG_PATH, LOG_FILE_PATH, extra_checks)
    except Exception as e:
        logging.warning(f"Failed to send startup notification: {e}")

send_startup_notification()

# ============================================================================
# DATA VALIDATION
# ============================================================================

def validate_metrics(metrics):
    """
    Validate sensor readings are within reasonable ranges.
    Returns (is_valid, list_of_issues)
    """
    issues = []

    if metrics["battery_voltage"] is not None:
        if (metrics["battery_voltage"] > MAX_REASONABLE_VOLTAGE or
                metrics["battery_voltage"] < MIN_REASONABLE_VOLTAGE):
            issues.append(f"Battery voltage out of range: {metrics['battery_voltage']:.2f}V")

    if metrics["solar_input_voltage"] is not None:
        if metrics["solar_input_voltage"] > MAX_REASONABLE_VOLTAGE and metrics["solar_input_voltage"] > 0:
            issues.append(f"Solar voltage out of range: {metrics['solar_input_voltage']:.2f}V")

    if metrics["controller_temperature"] is not None:
        if (metrics["controller_temperature"] > MAX_REASONABLE_TEMPERATURE or
                metrics["controller_temperature"] < MIN_REASONABLE_TEMPERATURE):
            issues.append(f"Controller temp out of range: {_c_to_f(metrics['controller_temperature']):.1f}°F")

    if metrics["battery_temperature"] is not None:
        if (metrics["battery_temperature"] > MAX_REASONABLE_TEMPERATURE or
                metrics["battery_temperature"] < MIN_REASONABLE_TEMPERATURE):
            issues.append(f"Battery temp out of range: {_c_to_f(metrics['battery_temperature']):.1f}°F")

    if metrics["solar_input_current"] is not None:
        if metrics["solar_input_current"] > MAX_REASONABLE_CURRENT:
            issues.append(f"Solar current out of range: {metrics['solar_input_current']:.2f}A")

    if metrics["solar_input_power"] is not None:
        if metrics["solar_input_power"] > MAX_REASONABLE_POWER:
            issues.append(f"Solar power out of range: {metrics['solar_input_power']:.0f}W")

    critical_metrics = ["battery_voltage", "battery_soc", "controller_temperature"]
    for metric in critical_metrics:
        if metrics.get(metric) is None:
            issues.append(f"Critical metric '{metric}' is None")

    return (len(issues) == 0, issues)

# ============================================================================
# DATA COLLECTION
# ============================================================================

# Track previous over-discharge count — persisted across restarts so events
# that occur while this script is down are not silently missed.
previous_over_discharge_count = None

_COUNTERS_FILE = os.path.join(os.path.dirname(PERSISTENT_STATE_FILE), 'renogy_counters.json')


def _load_persisted_counters():
    """Load persistent counter values (e.g. over-discharge count) at startup."""
    global previous_over_discharge_count
    try:
        if os.path.exists(_COUNTERS_FILE):
            with open(_COUNTERS_FILE, 'r') as f:
                data = json.load(f)
            previous_over_discharge_count = data.get('previous_over_discharge_count')
            logging.info(
                f"Loaded persisted over-discharge count: {previous_over_discharge_count}"
            )
    except Exception as e:
        logging.warning(f"Could not load persisted counters from {_COUNTERS_FILE}: {e}")


def _save_persisted_counters():
    """Save persistent counter values so they survive restarts."""
    try:
        os.makedirs(os.path.dirname(_COUNTERS_FILE), exist_ok=True)
        tmp = _COUNTERS_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump({'previous_over_discharge_count': previous_over_discharge_count}, f)
        os.rename(tmp, _COUNTERS_FILE)
    except Exception as e:
        logging.warning(f"Could not save persisted counters to {_COUNTERS_FILE}: {e}")


_load_persisted_counters()

def read_rover_metrics():
    """Read metrics from Renogy Rover with enhanced error handling and batch reads."""
    global last_successful_connection, previous_over_discharge_count

    try:
        # Batch read all registers in 2-3 Modbus calls (#7)
        rover.batch_read()

        active_faults = rover.get_active_faults() or []
        load_status = rover.get_load_status()

        current_over_discharge_count = rover.get_total_battery_over_discharges()
        new_over_discharge_event = False
        if previous_over_discharge_count is not None and current_over_discharge_count is not None:
            if current_over_discharge_count > previous_over_discharge_count:
                new_over_discharge_event = True
        if current_over_discharge_count is not None:
            previous_over_discharge_count = current_over_discharge_count
            _save_persisted_counters()

        battery_soc = rover.get_battery_state_of_charge()
        battery_capacity_ah_remaining = (
            (battery_soc / 100.0) * BATTERY_CAPACITY_AH if battery_soc is not None else None
        )

        metrics = {
            "battery_soc": battery_soc,
            "battery_voltage": rover.get_battery_voltage(),
            "solar_input_voltage": rover.get_solar_voltage(),
            "solar_input_current": rover.get_solar_current(),
            "solar_input_power": rover.get_solar_power(),
            "load_voltage": rover.get_load_voltage(),
            "load_current": rover.get_load_current(),
            "load_power": rover.get_load_power(),
            "controller_temperature": rover.get_controller_temperature(),
            "battery_temperature": rover.get_battery_temperature(),
            "maximum_solar_power": rover.get_maximum_solar_power_today(),
            "minimum_solar_power": rover.get_minimum_solar_power_today(),
            "maximum_battery_voltage": rover.get_maximum_battery_voltage_today(),
            "minimum_battery_voltage": rover.get_minimum_battery_voltage_today(),
            "battery_capacity_ah_remaining": battery_capacity_ah_remaining,
            "daily_min_battery_voltage": rover.get_daily_min_battery_voltage(),
            "daily_max_battery_voltage": rover.get_daily_max_battery_voltage(),
            "daily_max_charging_current": rover.get_daily_max_charging_current(),
            "daily_max_discharging_current": rover.get_daily_max_discharging_current(),
            "daily_max_charging_power": rover.get_daily_max_charging_power(),
            "daily_max_discharging_power": rover.get_daily_max_discharging_power(),
            "daily_charging_ah": rover.get_daily_charging_ah(),
            "daily_discharging_ah": rover.get_daily_discharging_ah(),
            "daily_power_generation_kwh": rover.get_daily_power_generation(),
            "daily_power_consumption_kwh": rover.get_daily_power_consumption(),
            "total_operating_days": rover.get_total_operating_days(),
            "total_battery_over_discharges": current_over_discharge_count,
            "total_battery_full_charges": rover.get_total_battery_full_charges(),
            "new_over_discharge_event": new_over_discharge_event,
            "total_charging_ah": rover.get_total_charging_ah(),
            "total_discharging_ah": rover.get_total_discharging_ah(),
            "cumulative_power_generation_kwh": rover.get_cumulative_power_generation(),
            "cumulative_power_consumption_kwh": rover.get_cumulative_power_consumption(),
            "load_is_on": 1 if (load_status and load_status['is_on']) else 0,
            "load_brightness": load_status['brightness'] if load_status else None,
            "charging_state_code": {
                'deactivated': 0, 'activated': 1, 'mppt': 2, 'equalizing': 3,
                'boost': 4, 'floating': 5, 'current_limiting': 6
            }.get(load_status['charging_state'] if load_status else 'unknown', -1),
            "active_faults": active_faults,
            "active_faults_count": len(active_faults),
            "modbus_error_rate": rover.get_error_rate(),
            "modbus_total_reads": rover.total_reads,
            "modbus_failed_reads": rover.read_errors,
        }

        # Override hardware SOC with coulomb counting when solar is off.
        # The Renogy controller freezes its SOC register at the last charged value
        # on LiFePO4 — it only updates during active charging cycles. We seed the
        # counter from battery voltage at the moment solar stops (no load transients,
        # most reliable reading), then integrate load current each poll interval.
        #
        # Hysteresis: switch TO coulomb mode when solar < 5W, back to hardware
        # when solar clearly recovers above 25W.
        global _using_voltage_soc, _cc_soc, _cc_last_time
        _solar = metrics.get("solar_input_power")
        _batt_v = metrics.get("battery_voltage")
        _load_i = metrics.get("load_current")

        if _solar is not None:
            if _solar < 5:
                _using_voltage_soc = True
            elif _solar > 25:
                _using_voltage_soc = False

        if _using_voltage_soc and _batt_v is not None:
            now = time.monotonic()
            if _cc_soc is None:
                # Seed from battery voltage at the transition point. Voltage is
                # most reliable here: charger just stopped, no active load spike.
                seed = _voltage_to_soc(_batt_v)
                _cc_soc = seed if seed is not None else 50.0
                _cc_last_time = now
                logging.info(
                    f"Coulomb counter seeded at {_cc_soc:.1f}% "
                    f"from battery voltage {_batt_v:.2f}V"
                )
            else:
                # Integrate discharge since last poll.
                # Prefer load_current; fall back to load_power / battery_voltage.
                if _cc_last_time is not None:
                    dt_hours = (now - _cc_last_time) / 3600.0
                    if _load_i is not None:
                        discharge_a = _load_i
                    elif metrics.get("load_power") is not None:
                        discharge_a = metrics["load_power"] / _batt_v
                    else:
                        discharge_a = None
                    if discharge_a is not None:
                        _cc_soc -= (discharge_a * dt_hours / BATTERY_CAPACITY_AH) * 100.0
                        _cc_soc = max(0.0, min(100.0, _cc_soc))
                _cc_last_time = now

            metrics["battery_soc"] = round(_cc_soc, 1)
            metrics["battery_capacity_ah_remaining"] = round(
                (_cc_soc / 100.0) * BATTERY_CAPACITY_AH, 2
            )
            metrics["battery_soc_source"] = 0   # 0 = coulomb counter
        else:
            _cc_soc = None
            _cc_last_time = None
            metrics["battery_soc_source"] = 1   # 1 = hardware register

        # Invalidate batch cache after reading so next poll starts fresh
        rover.invalidate_batch_cache()

        last_successful_connection = datetime.now()
        return metrics

    except Exception as e:
        logging.error(f"Failed to read metrics from Renogy Rover: {e}")
        logging.error(traceback.format_exc())
        rover.invalidate_batch_cache()
        return None

# ============================================================================
# DAILY SUMMARY (#11)
# ============================================================================

def update_daily_summary(metrics):
    """Update DailySummary with current metrics values."""
    if metrics is None:
        return
    if metrics.get('daily_power_generation_kwh') is not None:
        daily_summary.update('solar_kwh', metrics['daily_power_generation_kwh'])
    if metrics.get('battery_soc') is not None:
        daily_summary.update('battery_soc', metrics['battery_soc'])
    if metrics.get('solar_input_power') is not None:
        daily_summary.update('solar_power_w', metrics['solar_input_power'])
    if metrics.get('controller_temperature') is not None:
        daily_summary.update('controller_temp_c', metrics['controller_temperature'])
    if metrics.get('battery_temperature') is not None:
        daily_summary.update('battery_temp_c', metrics['battery_temperature'])

def check_daily_summary():
    """Send daily summary email if due."""
    if not daily_summary.should_send(DAILY_SUMMARY_HOUR, SUMMARY_INTERVAL_DAYS):
        return

    active_alert_count = sum(
        1 for s in alert_manager.all_states().values() if s.get('active', False)
    )

    solar_kwh_today    = daily_summary.get_sum('solar_kwh')
    soc_min            = daily_summary.get_min('battery_soc')
    soc_max            = daily_summary.get_max('battery_soc')
    soc_avg            = daily_summary.get_avg('battery_soc')
    solar_power_max    = daily_summary.get_max('solar_power_w')
    ctrl_temp_min      = daily_summary.get_min('controller_temp_c')
    ctrl_temp_max      = daily_summary.get_max('controller_temp_c')
    batt_temp_min      = daily_summary.get_min('battery_temp_c')
    batt_temp_max      = daily_summary.get_max('battery_temp_c')

    def _fmt(v, fmt='.1f'):
        return f"{v:{fmt}}" if v is not None else "N/A"

    def _fmt_temp(c):
        if c is None:
            return "N/A"
        return f"{c:.1f}°C ({c * 9/5 + 32:.1f}°F)"

    body = (
        f"Renogy Solar Daily Summary\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Generated: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"Solar Generation:\n"
        f"  Today's generation: {_fmt(solar_kwh_today, '.4f')} kWh\n"
        f"  Peak solar power:   {_fmt(solar_power_max, '.0f')} W\n\n"
        f"Battery:\n"
        f"  SOC min:  {_fmt(soc_min)}%\n"
        f"  SOC max:  {_fmt(soc_max)}%\n"
        f"  SOC avg:  {_fmt(soc_avg)}%\n\n"
        f"Temperatures:\n"
        f"  Controller min: {_fmt_temp(ctrl_temp_min)}  max: {_fmt_temp(ctrl_temp_max)}\n"
        f"  Battery    min: {_fmt_temp(batt_temp_min)}  max: {_fmt_temp(batt_temp_max)}\n\n"
        f"Active alerts: {active_alert_count}\n"
    )
    if active_alert_count > 0:
        body += "\nCurrently active alerts:\n"
        for atype, s in alert_manager.all_states().items():
            if s.get('active'):
                since = s.get('first_detected', 'unknown')
                body += f"  - {atype} (since {since})\n"

    summary_label = "Daily" if SUMMARY_INTERVAL_DAYS == 1 else f"{SUMMARY_INTERVAL_DAYS}-Day"
    if send_email_alert(f"{summary_label} Summary: Renogy {datetime.now().strftime('%Y-%m-%d')}", body):
        daily_summary.mark_sent()

# ============================================================================
# ALERT CHECKING
# ============================================================================

def _c_to_f(c):
    """Convert Celsius to Fahrenheit."""
    return c * 9 / 5 + 32


def check_critical_conditions(metrics):
    """Check for critical conditions and send alerts (with throttling)"""
    active_alerts = []

    # Data quality check
    is_valid, validation_issues = validate_metrics(metrics)
    if not is_valid:
        alert_type = 'data_quality_issue'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            issues_text = "\n".join([f"  - {issue}" for issue in validation_issues])
            subject = "Warning: Data Quality Issue Detected"
            content = (
                f"Invalid sensor readings detected:\n\n{issues_text}\n\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Alert reason: {reason}\n"
                f"This may indicate sensor failure or communication errors."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(f"Data quality alert sent: {len(validation_issues)} issues")
        active_alerts.append(alert_type)
    else:
        alert_manager.clear('data_quality_issue')

    # Over-discharge event
    if metrics.get("new_over_discharge_event"):
        alert_type = 'over_discharge_event'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: Battery Over-Discharge Event Detected"
            content = (
                f"A new over-discharge event has been recorded!\n\n"
                f"Total over-discharge events: {metrics['total_battery_over_discharges']}\n"
                f"Current battery SOC: {metrics['battery_soc']}%\n"
                f"Current battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Over-discharging reduces battery lifespan. "
                f"Consider increasing low-voltage disconnect threshold."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning("Over-discharge event alert sent")
        active_alerts.append(alert_type)
    else:
        alert_manager.clear('over_discharge_event')

    # Hardware faults
    active_faults = metrics.get("active_faults", [])

    critical_faults = [
        'charge_mos_short_circuit',
        'anti_reverse_mos_short',
        'pv_input_short_circuit',
        'load_short_circuit',
        'battery_over_voltage',
        'battery_over_discharge'
    ]

    critical_faults_present = [f for f in active_faults if f in critical_faults]

    if critical_faults_present and not DISABLE_FAULT_ALERTS:
        alert_type = 'critical_hardware_fault'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            faults_text = "\n".join([f"  - {fault}" for fault in critical_faults_present])
            subject = "CRITICAL Hardware Fault Detected"
            content = (
                f"CRITICAL HARDWARE FAULT(S) DETECTED:\n\n{faults_text}\n\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Solar power: {metrics['solar_input_power']}W\n"
                f"Controller temp: {_c_to_f(metrics['controller_temperature']):.1f}°F\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"IMMEDIATE ACTION REQUIRED! System may be damaged or unsafe."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.error(f"Critical hardware fault alert sent: {critical_faults_present}")
        active_alerts.append(alert_type)
    else:
        if alert_manager.clear('critical_hardware_fault'):
            send_email_alert(
                "Critical Hardware Faults Cleared",
                f"All critical hardware faults have been resolved.\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

    # Non-critical faults
    warning_faults = [f for f in active_faults if f not in critical_faults]

    if warning_faults and not DISABLE_FAULT_ALERTS:
        alert_type = 'hardware_fault'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            faults_text = "\n".join([f"  - {fault}" for fault in warning_faults])
            subject = "Hardware Warning Detected"
            content = (
                f"Hardware warning(s) detected:\n\n{faults_text}\n\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Solar power: {metrics['solar_input_power']}W\n"
                f"Controller temp: {_c_to_f(metrics['controller_temperature']):.1f}°F\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Monitor the situation and investigate if warnings persist."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(f"Hardware warning alert sent: {warning_faults}")
        active_alerts.append(alert_type)
    else:
        if alert_manager.clear('hardware_fault') and not critical_faults_present:
            send_email_alert(
                "Hardware Warnings Cleared",
                f"All hardware warnings have been resolved.\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )

    # Low battery voltage
    if metrics["battery_voltage"] is not None and metrics["battery_voltage"] < LOW_BATTERY_VOLTAGE_THRESHOLD and not DISABLE_LOW_BATTERY_ALERTS:
        alert_type = 'low_battery_voltage'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: Low Battery Voltage Alert"
            content = (
                f"Battery voltage is critically low: {metrics['battery_voltage']:.2f}V\n"
                f"Threshold: {LOW_BATTERY_VOLTAGE_THRESHOLD}V\n"
                f"Battery SOC: {metrics['battery_soc']}%\n"
                f"Remaining capacity: {metrics['battery_capacity_ah_remaining']:.1f}Ah / {BATTERY_CAPACITY_AH}Ah\n"
                f"Solar input: {metrics['solar_input_power']}W\n"
                f"Load power: {metrics['load_power']}W\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Immediate action required!"
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(f"Low battery voltage alert sent: {metrics['battery_voltage']:.2f}V")
        active_alerts.append(alert_type)
    else:
        if alert_manager.clear('low_battery_voltage') and metrics.get("battery_voltage") is not None:
            send_email_alert(
                "Battery Voltage Recovered",
                f"Battery voltage has recovered to {metrics['battery_voltage']:.2f}V"
            )

    # Low battery SOC
    if metrics["battery_soc"] is not None and metrics["battery_soc"] < LOW_BATTERY_SOC_THRESHOLD and not DISABLE_LOW_BATTERY_ALERTS:
        alert_type = 'low_battery_soc'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: Low Battery State of Charge"
            content = (
                f"Battery SOC is low: {metrics['battery_soc']}%\n"
                f"Threshold: {LOW_BATTERY_SOC_THRESHOLD}%\n"
                f"Remaining capacity: {metrics['battery_capacity_ah_remaining']:.1f}Ah / {BATTERY_CAPACITY_AH}Ah\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Solar input: {metrics['solar_input_power']}W\n"
                f"Load power: {metrics['load_power']}W\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Consider reducing load or waiting for solar charging."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(f"Low battery SOC alert sent: {metrics['battery_soc']}%")
        active_alerts.append(alert_type)
    else:
        if alert_manager.clear('low_battery_soc') and metrics.get("battery_soc") is not None:
            send_email_alert(
                "Battery SOC Recovered",
                f"Battery SOC has recovered to {metrics['battery_soc']}%"
            )

    # Low battery capacity (Ah remaining)
    if (metrics["battery_capacity_ah_remaining"] is not None and
            metrics["battery_capacity_ah_remaining"] < LOW_BATTERY_CAPACITY_AH_THRESHOLD and
            not DISABLE_CAPACITY_ALERTS):
        alert_type = 'low_battery_capacity'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: Low Battery Capacity Alert"
            content = (
                f"Remaining battery capacity is low: {metrics['battery_capacity_ah_remaining']:.1f}Ah\n"
                f"Threshold: {LOW_BATTERY_CAPACITY_AH_THRESHOLD}Ah\n"
                f"Total capacity: {BATTERY_CAPACITY_AH}Ah\n"
                f"Battery SOC: {metrics['battery_soc']}%\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Current discharge: {metrics['daily_discharging_ah']}Ah today\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Battery will be depleted soon if discharge continues."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(
                    f"Low battery capacity alert sent: "
                    f"{metrics['battery_capacity_ah_remaining']:.1f}Ah"
                )
        active_alerts.append(alert_type)
    else:
        if (alert_manager.clear('low_battery_capacity') and
                metrics.get("battery_capacity_ah_remaining") is not None):
            send_email_alert(
                "Battery Capacity Recovered",
                f"Battery capacity has recovered to "
                f"{metrics['battery_capacity_ah_remaining']:.1f}Ah ({metrics['battery_soc']}%)"
            )

    # High controller temperature
    if (metrics["controller_temperature"] is not None and
            metrics["controller_temperature"] > HIGH_CONTROLLER_TEMPERATURE_THRESHOLD and
            not DISABLE_TEMPERATURE_ALERTS):
        alert_type = 'high_controller_temp'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: High Controller Temperature Alert"
            content = (
                f"Controller temperature is dangerously high: "
                f"{_c_to_f(metrics['controller_temperature']):.1f}°F\n"
                f"Threshold: {_c_to_f(HIGH_CONTROLLER_TEMPERATURE_THRESHOLD):.1f}°F\n"
                f"Solar input: {metrics['solar_input_power']}W\n"
                f"Charging current: {metrics['solar_input_current']:.1f}A\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Immediate action required! Improve ventilation or reduce load."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(
                    f"High controller temp alert sent: {metrics['controller_temperature']:.1f}C"
                )
        active_alerts.append(alert_type)
    else:
        if (alert_manager.clear('high_controller_temp') and
                metrics.get("controller_temperature") is not None):
            send_email_alert(
                "Controller Temperature Normal",
                f"Controller temperature has returned to normal: "
                f"{_c_to_f(metrics['controller_temperature']):.1f}°F"
            )

    # High battery temperature
    if (metrics["battery_temperature"] is not None and
            metrics["battery_temperature"] > HIGH_BATTERY_TEMPERATURE_THRESHOLD and
            not DISABLE_TEMPERATURE_ALERTS):
        alert_type = 'high_battery_temp'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: High Battery Temperature Alert"
            content = (
                f"Battery temperature is dangerously high: "
                f"{_c_to_f(metrics['battery_temperature']):.1f}°F\n"
                f"Threshold: {_c_to_f(HIGH_BATTERY_TEMPERATURE_THRESHOLD):.1f}°F\n"
                f"Charging current: {metrics['solar_input_current']:.1f}A\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"DANGER! Battery may be damaged. Improve cooling immediately!"
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.error(
                    f"High battery temp alert sent: {metrics['battery_temperature']:.1f}C"
                )
        active_alerts.append(alert_type)
    else:
        if (alert_manager.clear('high_battery_temp') and
                metrics.get("battery_temperature") is not None):
            send_email_alert(
                "Battery Temperature Normal",
                f"Battery temperature has returned to safe levels: "
                f"{_c_to_f(metrics['battery_temperature']):.1f}°F"
            )

    # Low battery temperature
    if (metrics["battery_temperature"] is not None and
            metrics["battery_temperature"] < LOW_BATTERY_TEMPERATURE_THRESHOLD and
            not DISABLE_TEMPERATURE_ALERTS):
        alert_type = 'low_battery_temp'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            subject = "Warning: Low Battery Temperature Alert"
            content = (
                f"Battery temperature is critically low: "
                f"{_c_to_f(metrics['battery_temperature']):.1f}°F\n"
                f"Threshold: {_c_to_f(LOW_BATTERY_TEMPERATURE_THRESHOLD):.1f}°F\n"
                f"Battery SOC: {metrics['battery_soc']}%\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Battery performance reduced. Consider insulation or heating."
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(
                    f"Low battery temp alert sent: {metrics['battery_temperature']:.1f}C"
                )
        active_alerts.append(alert_type)
    else:
        if (alert_manager.clear('low_battery_temp') and
                metrics.get("battery_temperature") is not None):
            send_email_alert(
                "Battery Temperature Normal",
                f"Battery temperature has risen to safe levels: "
                f"{_c_to_f(metrics['battery_temperature']):.1f}°F"
            )

    return active_alerts

# ============================================================================
# SD CARD HEALTH METRICS
# ============================================================================

# Rolling write-rate state (module-level)
_sd_prev_write_sectors = None
_sd_prev_timestamp = None


def get_sd_card_metrics():
    """
    Read /proc/diskstats for the root SD card device (mmcblk0).
    Returns dict with write_sectors, write_ios, or None on failure.
    """
    try:
        with open('/proc/diskstats') as f:
            for line in f:
                parts = line.split()
                # mmcblk0 is the Pi SD card (not mmcblk0p1 partition)
                if len(parts) >= 10 and parts[2] == 'mmcblk0':
                    return {
                        'write_ios': int(parts[7]),      # writes completed
                        'write_sectors': int(parts[9]),  # sectors written
                    }
    except Exception:
        pass
    return None


def get_sd_card_prometheus_metrics():
    """
    Collect SD card I/O and disk space metrics.

    Returns dict of metric_name → value (floats), or empty dict on failure.
    """
    global _sd_prev_write_sectors, _sd_prev_timestamp

    out = {}
    now = time.time()

    # --- Disk I/O from /proc/diskstats ---
    sd = get_sd_card_metrics()
    if sd is not None:
        write_sectors = sd['write_sectors']
        write_ios     = sd['write_ios']

        out['sd_card_writes_total_sectors'] = write_sectors

        if _sd_prev_write_sectors is not None and _sd_prev_timestamp is not None:
            elapsed = now - _sd_prev_timestamp
            if elapsed > 0:
                sector_delta = write_sectors - _sd_prev_write_sectors
                # 1 sector = 512 bytes; convert to kbps
                kbps = (sector_delta * 512) / elapsed / 1024.0
                out['sd_card_write_rate_kbps'] = max(0.0, kbps)

        _sd_prev_write_sectors = write_sectors
        _sd_prev_timestamp = now

    # --- Disk space from os.statvfs ---
    try:
        st = os.statvfs('/')
        total_bytes = st.f_blocks * st.f_frsize
        avail_bytes = st.f_bavail * st.f_frsize
        used_bytes  = total_bytes - avail_bytes
        if total_bytes > 0:
            used_pct    = used_bytes / total_bytes * 100.0
            avail_gb    = avail_bytes / 1024 ** 3
            out['sd_card_available_gb'] = round(avail_gb, 3)
            out['sd_card_used_pct']     = round(used_pct, 2)
    except Exception as e:
        logging.warning(f"Could not read disk space via statvfs: {e}")

    return out


def check_disk_space_alert(sd_metrics, active_alerts):
    """
    Alert if SD card used % exceeds threshold from config.

    Mutates active_alerts list in place.
    """
    used_pct = sd_metrics.get('sd_card_used_pct')
    if used_pct is None:
        return

    disk_alert_threshold = get_config('system.disk_alert_pct', 85)

    if used_pct > disk_alert_threshold:
        alert_type = 'high_disk_usage'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            avail_gb = sd_metrics.get('sd_card_available_gb', 0)
            subject = f"Warning: High SD Card Usage ({used_pct:.1f}%)"
            content = (
                f"SD card (root filesystem) usage is high: {used_pct:.1f}%\n"
                f"Threshold: {disk_alert_threshold}%\n"
                f"Available space: {avail_gb:.2f} GB\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Consider running:\n"
                f"  sudo journalctl --vacuum-size=100M\n"
                f"  sudo apt-get autoremove\n"
                f"  du -sh /var/log/*"
            )
            if send_email_alert(subject, content):
                alert_manager.mark_sent(alert_type)
                logging.warning(f"High disk usage alert sent: {used_pct:.1f}%")
        active_alerts.append(alert_type)
    else:
        if alert_manager.clear('high_disk_usage'):
            avail_gb = sd_metrics.get('sd_card_available_gb', 0)
            send_email_alert(
                "SD Card Usage Normal",
                f"SD card usage has returned to {used_pct:.1f}% "
                f"({avail_gb:.2f} GB available)"
            )


# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

def write_metrics_to_file(metrics, active_alerts, temp_file_path, final_file_path):
    """Write metrics to file in Prometheus format with atomic rename"""
    try:
        with open(temp_file_path, 'w') as temp_file:
            # Write all numeric metrics
            for key, value in metrics.items():
                if key in ['active_faults', 'charging_state', 'new_over_discharge_event']:
                    continue
                if value is None:
                    continue
                try:
                    float(value)
                except (ValueError, TypeError):
                    logging.warning(f"Skipping non-numeric metric {key}={value}")
                    continue

                temp_file.write(f'# HELP {key} Renogy solar charge controller metric\n')
                temp_file.write(f'# TYPE {key} gauge\n')
                temp_file.write(f'{key}{{source="renogy"}} {value}\n')

            # Battery capacity configuration
            temp_file.write(f'# HELP battery_capacity_total Total battery capacity in Ah\n')
            temp_file.write(f'# TYPE battery_capacity_total gauge\n')
            temp_file.write(f'battery_capacity_total{{source="renogy"}} {BATTERY_CAPACITY_AH}\n')

            # Fault flags
            for fault in metrics.get('active_faults', []):
                safe_fault_name = fault.replace('-', '_')
                temp_file.write(f'# HELP renogy_fault_{safe_fault_name} Fault status\n')
                temp_file.write(f'# TYPE renogy_fault_{safe_fault_name} gauge\n')
                temp_file.write(f'renogy_fault_{safe_fault_name}{{source="renogy"}} 1\n')

            # Alert status
            temp_file.write('# HELP renogy_alerts_active Active alert count\n')
            temp_file.write('# TYPE renogy_alerts_active gauge\n')
            temp_file.write(f'renogy_alerts_active{{source="renogy"}} {len(active_alerts)}\n')

            for alert_type, s in alert_manager.all_states().items():
                status = 1 if s['active'] else 0
                temp_file.write(f'# HELP renogy_alert_{alert_type} Alert status\n')
                temp_file.write(f'# TYPE renogy_alert_{alert_type} gauge\n')
                temp_file.write(f'renogy_alert_{alert_type}{{source="renogy"}} {status}\n')

            # Maintenance mode flags
            temp_file.write('# HELP renogy_emails_disabled Email alerts disabled\n')
            temp_file.write('# TYPE renogy_emails_disabled gauge\n')
            temp_file.write(f'renogy_emails_disabled{{source="renogy"}} {1 if DISABLE_EMAILS else 0}\n')

            temp_file.write('# HELP renogy_low_battery_alerts_disabled Low battery alerts disabled\n')
            temp_file.write('# TYPE renogy_low_battery_alerts_disabled gauge\n')
            temp_file.write(
                f'renogy_low_battery_alerts_disabled{{source="renogy"}} '
                f'{1 if DISABLE_LOW_BATTERY_ALERTS else 0}\n'
            )

            temp_file.write('# HELP renogy_fault_alerts_disabled Fault alerts disabled\n')
            temp_file.write('# TYPE renogy_fault_alerts_disabled gauge\n')
            temp_file.write(
                f'renogy_fault_alerts_disabled{{source="renogy"}} '
                f'{1 if DISABLE_FAULT_ALERTS else 0}\n'
            )

            temp_file.write('# HELP renogy_temperature_alerts_disabled Temperature alerts disabled\n')
            temp_file.write('# TYPE renogy_temperature_alerts_disabled gauge\n')
            temp_file.write(
                f'renogy_temperature_alerts_disabled{{source="renogy"}} '
                f'{1 if DISABLE_TEMPERATURE_ALERTS else 0}\n'
            )

            temp_file.write('# HELP renogy_capacity_alerts_disabled Capacity alerts disabled\n')
            temp_file.write('# TYPE renogy_capacity_alerts_disabled gauge\n')
            temp_file.write(
                f'renogy_capacity_alerts_disabled{{source="renogy"}} '
                f'{1 if DISABLE_CAPACITY_ALERTS else 0}\n'
            )

            # SD card / disk metrics
            sd_metrics = get_sd_card_prometheus_metrics()
            check_disk_space_alert(sd_metrics, active_alerts)
            for sd_key, sd_val in sd_metrics.items():
                temp_file.write(f'# HELP {sd_key} SD card metric\n')
                temp_file.write(f'# TYPE {sd_key} gauge\n')
                temp_file.write(f'{sd_key}{{source="renogy"}} {sd_val}\n')

            # Health metric
            temp_file.write('# HELP renogy_monitor_healthy Monitor health status\n')
            temp_file.write('# TYPE renogy_monitor_healthy gauge\n')
            temp_file.write(f'renogy_monitor_healthy{{source="renogy"}} 1\n')

            # Last update timestamp
            temp_file.write('# HELP renogy_last_update Last update timestamp\n')
            temp_file.write('# TYPE renogy_last_update gauge\n')
            temp_file.write(f'renogy_last_update{{source="renogy"}} {int(time.time())}\n')

        os.rename(temp_file_path, final_file_path)
    except IOError as e:
        logging.error(f"Failed to write metrics to file: {e}")

# ============================================================================
# MAIN LOOP
# ============================================================================

print("Renogy monitor started - Enhanced Version")
print(f"Battery capacity: {BATTERY_CAPACITY_AH}Ah ({BATTERY_NOMINAL_VOLTAGE}V system)")
print(f"Email throttling: {EMAIL_COOLDOWN_MINUTES}min cooldown, {EMAIL_REMINDER_HOURS}h reminders")
print(f"Error logging: {LOG_FILE_PATH}")
print(f"Data export: {FINAL_FILE_PATH}")
print(f"Features: Extended stats, Fault monitoring, Capacity tracking, Data validation")
print(f"New: Batch Modbus reads, Hardware watchdog, Daily summary, SIGHUP reload")

disabled_systems = []
if DISABLE_EMAILS:
    disabled_systems.append("EMAILS")
if DISABLE_LOW_BATTERY_ALERTS:
    disabled_systems.append("LOW BATTERY ALERTS")
if DISABLE_FAULT_ALERTS:
    disabled_systems.append("FAULT ALERTS")
if DISABLE_TEMPERATURE_ALERTS:
    disabled_systems.append("TEMPERATURE ALERTS")
if DISABLE_CAPACITY_ALERTS:
    disabled_systems.append("CAPACITY ALERTS")

if disabled_systems:
    print()
    print("=" * 70)
    print("  MAINTENANCE MODE ACTIVE")
    print(f"  DISABLED: {', '.join(disabled_systems)}")
    print("=" * 70)
print()

consecutive_failures = 0
max_consecutive_failures = 5

if get_config('watchdog.enabled', False):
    watchdog = Watchdog(
        device=get_config('watchdog.device', '/dev/watchdog'),
        interval=get_config('watchdog.interval_seconds', 30),
    )
    watchdog.start()

try:
    while True:
        metrics = read_rover_metrics()

        if metrics:
            consecutive_failures = 0
            active_alerts = check_critical_conditions(metrics)
            update_daily_summary(metrics)
            check_daily_summary()
            write_metrics_to_file(metrics, active_alerts, TEMP_FILE_PATH, FINAL_FILE_PATH)
        else:
            consecutive_failures += 1
            logging.error(f"Failed to read metrics ({consecutive_failures}/{max_consecutive_failures})")

            if consecutive_failures >= max_consecutive_failures:
                logging.critical(
                    f"Failed to read metrics {consecutive_failures} times in a row, "
                    f"attempting reconnection"
                )
                if not initialize_rover():
                    logging.critical("Reconnection failed, will retry on next cycle")
                    try:
                        with open(TEMP_FILE_PATH, 'w') as f:
                            f.write('# HELP renogy_monitor_healthy Monitor health status\n')
                            f.write('# TYPE renogy_monitor_healthy gauge\n')
                            f.write('renogy_monitor_healthy{source="renogy"} 0\n')
                        os.rename(TEMP_FILE_PATH, FINAL_FILE_PATH)
                    except Exception:
                        pass
                consecutive_failures = 0

        check_connection_health()
        time.sleep(SLEEP_TIME)

except KeyboardInterrupt:
    print("\nShutdown requested")
except Exception as e:
    logging.critical(f"Unexpected error in main loop: {e}")
    logging.critical(traceback.format_exc())
finally:
    alert_manager.save()
    if watchdog:
        watchdog.stop()
    print("Renogy monitor stopped")
