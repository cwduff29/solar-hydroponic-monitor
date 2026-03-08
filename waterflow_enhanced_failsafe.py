#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hydroponic Water Flow and Equipment Monitor - Enhanced Version
Non-blocking event-driven architecture with reliability improvements

Features:
- Non-blocking flow measurement (background interrupt-driven, thread-safe)
- Dual flow sensor monitoring (inlet/outlet)
- Smart aeration control with battery-based load shedding
- Environmental monitoring (temperature, humidity, pressure)
- Automatic ventilation control
- Email alerts with throttling (AlertManager from monitor_common)
- Prometheus metrics export to /ramdisk
- Health monitoring and failure tracking
- Graceful degradation on sensor failures
- Renogy monitor health checking
- Hardware watchdog keepalive
- Daily summary email
- Flow trend analysis (24h baseline, degradation warning)
- Daily flow volume tracking
- SIGHUP config reload
- Startup notification email
- Cached DS18B20 sensor paths
- Cached BME280 calibration parameters
- Skip pump tests on low battery SOC
"""

import RPi.GPIO as GPIO
import time
import os
import json
import glob
import signal
import threading
import traceback
from collections import deque
from datetime import datetime, timedelta
import credentials as cr

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
DISABLE_PUMP_TESTING = False        # True = No backup pump tests
DISABLE_FLOW_ALERTS = False         # True = No low flow or leak warnings
DISABLE_AERATOR = False             # True = Aerator stays OFF
DISABLE_FAN = False                 # True = Fan stays OFF

# Quick presets (uncomment one to use):
# FULL_MAINTENANCE: DISABLE_EMAILS=True, DISABLE_PUMP_TESTING=True, DISABLE_FLOW_ALERTS=True
# RESERVOIR_EMPTY: DISABLE_FLOW_ALERTS=True, DISABLE_PUMP_TESTING=True
# AERATOR_SERVICE: DISABLE_AERATOR=True
# FAN_SERVICE: DISABLE_FAN=True
# ============================================================================

# ============================================================================
# RELAY CONFIGURATION - TERMINAL TYPE & BOARD LOGIC
# ============================================================================

MAIN_PUMP_TERMINAL   = get_config('waterflow.relay.main_pump_terminal',   'NC')
BACKUP_PUMP_TERMINAL = get_config('waterflow.relay.backup_pump_terminal', 'NO')
AERATION_TERMINAL    = get_config('waterflow.relay.aeration_terminal',    'NO')
FAN_TERMINAL         = get_config('waterflow.relay.fan_terminal',         'NC')
RELAY_BOARD_ACTIVE_LOW = get_config('waterflow.relay.active_low_board',   True)

# GPIO Pin Assignments
FLOW_SENSOR_INLET_GPIO  = get_config('waterflow.gpio.flow_sensor_inlet',  17)
FLOW_SENSOR_OUTLET_GPIO = get_config('waterflow.gpio.flow_sensor_outlet', 27)
MAIN_PUMP_RELAY_GPIO    = get_config('waterflow.gpio.main_pump_relay',    22)
BACKUP_PUMP_RELAY_GPIO  = get_config('waterflow.gpio.backup_pump_relay',  23)
AERATION_PUMP_GPIO      = get_config('waterflow.gpio.aeration_pump',      24)
FAN_CONTROL_GPIO        = get_config('waterflow.gpio.fan_control',        25)

# Flow Sensor Configuration
FLOW_CALIBRATION_FACTOR  = get_config('waterflow.flow.calibration_factor',           7.5)
FLOW_MEASUREMENT_DURATION = get_config('waterflow.flow.measurement_duration_seconds', 10)
MIN_FLOW_THRESHOLD       = get_config('waterflow.flow.min_flow_threshold_lpm',        0.25)
FLOW_WARNING_DELAY       = get_config('waterflow.flow.warning_delay_seconds',         600)
FLOW_IMBALANCE_THRESHOLD = get_config('waterflow.flow.imbalance_threshold_lpm',       0.5)
FLOW_IMBALANCE_DURATION  = get_config('waterflow.flow.imbalance_duration_seconds',    600)
FLOW_HISTORY_SIZE        = get_config('waterflow.flow.history_size',                  5)

# Main Loop Timing
MAIN_LOOP_INTERVAL = 1  # seconds (non-blocking architecture)

# Temperature Sensor Paths
DS18B20_BASE_DIR    = '/sys/bus/w1/devices/'
DS18B20_DEVICE_PREFIX = '28-'

# DS18B20 Sensor ID Mapping - loaded from config, keyed by hardware ID
DS18B20_SENSOR_MAP = get_config('waterflow.temperature.sensor_map', {
    '28-000000b18b1c': 'reservoir',
    '28-000000baada8': 'nft_drain',
    '28-000000b26508': 'outdoor',
})

# BME280 I2C Configuration
BME280_I2C_ADDRESS = get_config('waterflow.bme280.i2c_address', 0x76)

# ============================================================================
# THRESHOLD LOADING (reloaded on SIGHUP)
# ============================================================================

def _load_thresholds():
    """Load/reload all config-driven thresholds into module globals."""
    global NFT_RETURN_EXTREME_TEMP_C, ENABLE_NFT_EXTREME_TEMP_ALERT
    global OUTDOOR_COOLING_DELTA_C, OUTDOOR_HEAT_DELTA_C
    global OUTDOOR_FREEZE_RISK_C, RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT_C
    global WATER_TEMP_HOT_C, WATER_TEMP_WARM_C, WATER_TEMP_MODERATE_C
    global FAN_TEMP_ON_C, FAN_TEMP_OFF_C, FAN_TEMP_FORCE_ON_C
    global FAN_HUMIDITY_ON, FAN_HUMIDITY_OFF, FAN_MIN_TOGGLE_INTERVAL
    global HUMIDITY_WARNING, HUMIDITY_CRITICAL, HUMIDITY_EMERGENCY
    global BATTERY_THRESHOLD_DISABLE, BATTERY_THRESHOLD_REDUCE, BATTERY_THRESHOLD_NORMAL
    global BATTERY_SKIP_PUMP_TEST_BELOW
    global AERATION_ON_DURATION, AERATION_OFF_DURATION
    global AERATION_ON_DURATION_REDUCED, AERATION_OFF_DURATION_REDUCED
    global PUMP_TEST_INTERVAL, PUMP_TEST_DURATION
    global PUMP_CYCLE_PAUSE, PUMP_RECOVERY_WAIT, PUMP_CYCLE_MAX_ATTEMPTS
    global BATTERY_DATA_FILE, BATTERY_DATA_TIMEOUT
    global EMAIL_COOLDOWN_MINUTES, EMAIL_REMINDER_HOURS

    # Temperature thresholds (all in degC internally)
    NFT_RETURN_EXTREME_TEMP_C             = get_config('waterflow.temperature.nft_extreme_temp_c',                29.4)
    ENABLE_NFT_EXTREME_TEMP_ALERT         = get_config('waterflow.temperature.enable_nft_extreme_temp_alert',     True)
    OUTDOOR_COOLING_DELTA_C               = get_config('waterflow.temperature.outdoor_cooling_delta_c',           2.8)
    OUTDOOR_HEAT_DELTA_C                  = get_config('waterflow.temperature.outdoor_heat_delta_c',              5.6)
    OUTDOOR_FREEZE_RISK_C                 = get_config('waterflow.temperature.outdoor_freeze_risk_c',             1.7)
    RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT_C = get_config('waterflow.temperature.reservoir_min_for_freeze_alert_c',  7.2)
    WATER_TEMP_HOT_C                      = get_config('waterflow.temperature.water_temp_hot_c',                  25.6)
    WATER_TEMP_WARM_C                     = get_config('waterflow.temperature.water_temp_warm_c',                 22.8)
    WATER_TEMP_MODERATE_C                 = get_config('waterflow.temperature.water_temp_moderate_c',             20.0)

    # Fan thresholds (degC)
    FAN_TEMP_ON_C         = get_config('waterflow.fan.temp_on_c',                 35.0)
    FAN_TEMP_OFF_C        = get_config('waterflow.fan.temp_off_c',                29.4)
    FAN_TEMP_FORCE_ON_C   = get_config('waterflow.fan.temp_force_on_c',           43.3)
    FAN_HUMIDITY_ON       = get_config('waterflow.fan.humidity_on_pct',            80)
    FAN_HUMIDITY_OFF      = get_config('waterflow.fan.humidity_off_pct',           70)
    FAN_MIN_TOGGLE_INTERVAL = get_config('waterflow.fan.min_toggle_interval_seconds', 120)

    # Humidity alerts
    HUMIDITY_WARNING  = get_config('waterflow.humidity_alerts.warning_pct',   80)
    HUMIDITY_CRITICAL = get_config('waterflow.humidity_alerts.critical_pct',  85)
    HUMIDITY_EMERGENCY = get_config('waterflow.humidity_alerts.emergency_pct', 90)

    # Battery load shedding
    BATTERY_THRESHOLD_DISABLE      = get_config('waterflow.battery_load_shedding.disable_threshold_pct', 30)
    BATTERY_THRESHOLD_REDUCE       = get_config('waterflow.battery_load_shedding.reduce_threshold_pct',  50)
    BATTERY_THRESHOLD_NORMAL       = get_config('waterflow.battery_load_shedding.normal_threshold_pct',  70)
    BATTERY_SKIP_PUMP_TEST_BELOW   = get_config('waterflow.battery_load_shedding.skip_pump_test_below_pct', 50)

    # Aeration timing
    AERATION_ON_DURATION         = get_config('waterflow.aeration.on_duration_seconds',          360)
    AERATION_OFF_DURATION        = get_config('waterflow.aeration.off_duration_seconds',         1440)
    AERATION_ON_DURATION_REDUCED = get_config('waterflow.aeration.reduced_on_duration_seconds',  240)
    AERATION_OFF_DURATION_REDUCED = get_config('waterflow.aeration.reduced_off_duration_seconds', 2160)

    # Pump testing
    PUMP_TEST_INTERVAL    = get_config('waterflow.pump.test_interval_seconds',   172800)
    PUMP_TEST_DURATION    = get_config('waterflow.pump.test_duration_seconds',       60)
    PUMP_CYCLE_PAUSE      = get_config('waterflow.pump.cycle_pause_seconds',          5)
    PUMP_RECOVERY_WAIT    = get_config('waterflow.pump.recovery_wait_seconds',       90)
    PUMP_CYCLE_MAX_ATTEMPTS = get_config('waterflow.pump.cycle_max_attempts',         2)

    # Battery data
    BATTERY_DATA_FILE    = get_config('paths.battery_data_file',         '/ramdisk/Renogy.prom')
    BATTERY_DATA_TIMEOUT = get_config('paths.battery_data_timeout_seconds', 60)

    # Alert timing
    EMAIL_COOLDOWN_MINUTES = get_config('alerts.email_cooldown_minutes', 60)
    EMAIL_REMINDER_HOURS   = get_config('alerts.email_reminder_hours',   12)

_load_thresholds()

# File Paths
PROM_OUTPUT_FILE = get_config('paths.waterflow_prom',        '/ramdisk/waterflow.prom')
PROM_TEMP_FILE   = get_config('paths.waterflow_prom_tmp',    '/ramdisk/waterflow.prom.tmp')
STATE_FILE       = get_config('paths.waterflow_alert_state', '/ramdisk/waterflow_alerts.json')
PERSISTENT_STATE_FILE = get_config('paths.waterflow_persistent_state', '/var/lib/renogy/waterflow_state.json')
LOG_FILE_PATH    = get_config('paths.waterflow_log',         '/var/log/waterflow.log')

# Daily summary hour
DAILY_SUMMARY_HOUR = get_config('alerts.daily_summary_hour', 7)

# ============================================================================
# TEMPERATURE MONITORING (3-Sensor NFT System)
# ============================================================================

TEMP_DIFF_TRACKING_ENABLED = True  # Track in Prometheus for analysis

# ============================================================================
# LOGGING (ERROR LEVEL ONLY)
# ============================================================================

import logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler()
    ]
)

# ============================================================================
# RELAY CONTROL HELPER FUNCTIONS
# ============================================================================

def get_relay_state(device_on, terminal_type):
    """
    Calculate the correct GPIO state for a relay.

    Args:
        device_on: True = turn device ON, False = turn device OFF
        terminal_type: "NC" or "NO"

    Returns:
        GPIO.HIGH or GPIO.LOW
    """
    if terminal_type == "NC":
        relay_energized = not device_on
    else:  # "NO"
        relay_energized = device_on

    if RELAY_BOARD_ACTIVE_LOW:
        return GPIO.LOW if relay_energized else GPIO.HIGH
    else:
        return GPIO.HIGH if relay_energized else GPIO.LOW


def set_main_pump(state):
    """Set main pump ON or OFF"""
    gpio_state = get_relay_state(state, MAIN_PUMP_TERMINAL)
    GPIO.output(MAIN_PUMP_RELAY_GPIO, gpio_state)


def set_backup_pump(state):
    """Set backup pump ON or OFF"""
    gpio_state = get_relay_state(state, BACKUP_PUMP_TERMINAL)
    GPIO.output(BACKUP_PUMP_RELAY_GPIO, gpio_state)


def set_aeration(state):
    """Set aeration ON or OFF"""
    gpio_state = get_relay_state(state, AERATION_TERMINAL)
    GPIO.output(AERATION_PUMP_GPIO, gpio_state)


def set_fan(state):
    """Set fan ON or OFF"""
    gpio_state = get_relay_state(state, FAN_TERMINAL)
    GPIO.output(FAN_CONTROL_GPIO, gpio_state)


# ============================================================================
# GLOBAL STATE
# ============================================================================

# Flow counter lock (#1 - race condition fix)
_flow_lock = threading.Lock()

# Flow measurement state
flow_measurement_active = False
flow_measurement_start_time = None
count_inlet = 0
count_outlet = 0

# Smoothed flow values (weighted moving average)
smoothed_flow_inlet = 0.0
smoothed_flow_outlet = 0.0
flow_history_inlet = []
flow_history_outlet = []

# Flow alerts
low_flow_start_time = None
flow_imbalance_start_time = None
low_flow_alert_sent = False
leak_alert_sent = False

# Aeration state (start OFF, let control_aeration() turn it on based on schedule)
aeration_state = False
last_aeration_toggle = time.time()
aeration_mode = "normal"

# Fan state (start OFF, let control_ventilation_fan() turn it on based on temperature)
fan_running = False
fan_last_toggle = time.time()

# Battery state
battery_soc = 100.0

# Pump testing
last_pump_test = 0

# Pump recovery state
recovery_attempted = False
backup_pump_active = False

# Prometheus metrics
last_prometheus_write = 0

# Health monitoring
consecutive_failures = 0
renogy_monitor_healthy = True
sensors_available = {
    'bme280': True,
    'water_temp': True,
    'enclosure_temp': True,
    'flow_inlet': True,
    'flow_outlet': True
}

# ============================================================================
# CACHED DS18B20 SENSOR PATHS (#5)
# ============================================================================

_ds18b20_sensor_cache = []   # list of '/sys/bus/w1/devices/28-xxxx/w1_slave' paths
_ds18b20_cache_valid = False

def _discover_and_cache_sensors():
    """Discover DS18B20 sensors and store in module-level cache."""
    global _ds18b20_sensor_cache, _ds18b20_cache_valid
    try:
        device_folders = glob.glob(DS18B20_BASE_DIR + DS18B20_DEVICE_PREFIX + '*')
        _ds18b20_sensor_cache = [folder + '/w1_slave' for folder in device_folders]
        _ds18b20_cache_valid = bool(_ds18b20_sensor_cache)
        logging.info(f"DS18B20 sensors discovered and cached: {len(_ds18b20_sensor_cache)} sensors")
    except Exception as e:
        logging.error(f"Failed to discover temperature sensors: {e}")
        _ds18b20_sensor_cache = []
        _ds18b20_cache_valid = False

# ============================================================================
# CACHED BME280 CALIBRATION (#6)
# ============================================================================

_bme280_calibration = None   # cached calibration params object

# ============================================================================
# ALERT STATE MANAGEMENT (AlertManager from monitor_common)
# ============================================================================

alert_manager = AlertManager(
    ramdisk_path=STATE_FILE,
    persistent_path=PERSISTENT_STATE_FILE,
    cooldown_minutes=EMAIL_COOLDOWN_MINUTES,
    reminder_hours=EMAIL_REMINDER_HOURS,
)

alert_manager.load()

# ============================================================================
# SIGHUP HANDLER
# ============================================================================

def _sighup_handler(signum, frame):
    logging.warning("SIGHUP received: reloading config")
    if reload_config():
        _load_thresholds()
        alert_manager._cooldown_minutes = get_config('alerts.email_cooldown_minutes', 60)
        alert_manager._reminder_hours   = get_config('alerts.email_reminder_hours',   12)
        logging.warning("Config reloaded and thresholds updated")
    else:
        logging.error("Config reload failed")

signal.signal(signal.SIGHUP, _sighup_handler)

# ============================================================================
# EMAIL HELPER
# ============================================================================

def send_email_alert(subject, content):
    """Send email alert (wraps monitor_common.send_email)."""
    return send_email(subject, content, disabled=DISABLE_EMAILS)

# Alert management shims (keep old call-site names working)
def should_send_alert(alert_type):
    return alert_manager.should_send(alert_type)

def mark_alert_sent(alert_type):
    alert_manager.mark_sent(alert_type)

def clear_alert(alert_type):
    return alert_manager.clear(alert_type)

# ============================================================================
# GPIO SETUP
# ============================================================================

def setup_gpio():
    """Initialize GPIO pins"""
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)

    GPIO.setup(FLOW_SENSOR_INLET_GPIO,  GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(FLOW_SENSOR_OUTLET_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    GPIO.setup(MAIN_PUMP_RELAY_GPIO,   GPIO.OUT)
    GPIO.setup(BACKUP_PUMP_RELAY_GPIO, GPIO.OUT)
    GPIO.setup(AERATION_PUMP_GPIO,     GPIO.OUT)
    GPIO.setup(FAN_CONTROL_GPIO,       GPIO.OUT)

    set_main_pump(True)
    set_backup_pump(False)
    set_aeration(False)
    set_fan(False)

    logging.info(
        f"GPIO initialized - Relay config: "
        f"Main={MAIN_PUMP_TERMINAL}, Backup={BACKUP_PUMP_TERMINAL}, "
        f"Aeration={AERATION_TERMINAL}, Fan={FAN_TERMINAL}, "
        f"Board={'ACTIVE_LOW' if RELAY_BOARD_ACTIVE_LOW else 'ACTIVE_HIGH'}"
    )

    GPIO.add_event_detect(FLOW_SENSOR_INLET_GPIO,  GPIO.FALLING, callback=countPulse_inlet)
    GPIO.add_event_detect(FLOW_SENSOR_OUTLET_GPIO, GPIO.FALLING, callback=countPulse_outlet)

# ============================================================================
# FLOW SENSOR INTERRUPTS (thread-safe via lock - #1)
# ============================================================================

def countPulse_inlet(channel):
    """Interrupt handler for inlet flow sensor"""
    global count_inlet
    with _flow_lock:
        if flow_measurement_active:
            count_inlet += 1


def countPulse_outlet(channel):
    """Interrupt handler for outlet flow sensor"""
    global count_outlet
    with _flow_lock:
        if flow_measurement_active:
            count_outlet += 1


# ============================================================================
# FLOW MEASUREMENT (Non-blocking)
# ============================================================================

def start_flow_measurement():
    """Start a new flow measurement period"""
    global flow_measurement_active, flow_measurement_start_time, count_inlet, count_outlet
    with _flow_lock:
        count_inlet = 0
        count_outlet = 0
        flow_measurement_active = True
    flow_measurement_start_time = time.time()


def check_flow_measurement():
    """Check if flow measurement period is complete"""
    global flow_measurement_active

    if not flow_measurement_active:
        return False

    if time.time() - flow_measurement_start_time >= FLOW_MEASUREMENT_DURATION:
        with _flow_lock:
            flow_measurement_active = False
        return True

    return False


# ============================================================================
# FLOW TREND ANALYSIS (#12)
# ============================================================================

# 144 readings × 10s = 24 hours of history
_TREND_HISTORY_SIZE = 144
_TREND_1H_READINGS   = 360  # 360s / 10s = 36 readings for 1-hour average

# Deque automatically drops oldest when full
_flow_trend_history = deque(maxlen=_TREND_HISTORY_SIZE)   # 24h of inlet flow readings
_flow_trend_1h = deque(maxlen=_TREND_1H_READINGS)         # 1h of inlet flow readings

FLOW_TREND_DROP_PCT = 0.20   # 20% drop triggers degradation warning


def update_flow_trend(flow_inlet_lpm):
    """Record a flow reading into trend history buffers."""
    _flow_trend_history.append(flow_inlet_lpm)
    _flow_trend_1h.append(flow_inlet_lpm)


def get_flow_trend_metrics():
    """
    Return (avg_24h, avg_1h, degradation_flag).
    degradation_flag is True when 1h avg drops >20% below 24h baseline
    AND flow is above the hard minimum threshold (not already in low-flow alert).
    """
    if len(_flow_trend_history) < 10:
        return (None, None, False)

    avg_24h = sum(_flow_trend_history) / len(_flow_trend_history)

    if len(_flow_trend_1h) < 6:
        return (avg_24h, None, False)

    avg_1h = sum(_flow_trend_1h) / len(_flow_trend_1h)

    # Only flag degradation if flow is above the hard minimum (not already alerting)
    degraded = (
        avg_24h > 0 and
        avg_1h > MIN_FLOW_THRESHOLD and
        avg_1h < avg_24h * (1.0 - FLOW_TREND_DROP_PCT)
    )

    return (avg_24h, avg_1h, degraded)


# ============================================================================
# DAILY FLOW VOLUME TRACKING (#15)
# ============================================================================

_daily_volume_liters = 0.0
_daily_volume_date   = datetime.now().date()
_volume_lock         = threading.Lock()


def accumulate_flow_volume(flow_inlet_lpm):
    """
    Add the volume for one measurement period to the daily total.
    flow_inlet_lpm (L/min) × FLOW_MEASUREMENT_DURATION (s) / 60 = liters
    Resets at midnight automatically.
    """
    global _daily_volume_liters, _daily_volume_date
    today = datetime.now().date()
    liters_this_period = flow_inlet_lpm * FLOW_MEASUREMENT_DURATION / 60.0
    with _volume_lock:
        if today != _daily_volume_date:
            _daily_volume_liters = 0.0
            _daily_volume_date   = today
        _daily_volume_liters += liters_this_period


def get_daily_volume():
    """Return accumulated daily volume in liters."""
    with _volume_lock:
        return _daily_volume_liters


# ============================================================================
# DAILY SUMMARY (#11)
# ============================================================================

daily_summary = DailySummary()


def update_daily_summary(temps, conditions, flow_inlet, flow_outlet):
    """Update DailySummary with current readings."""
    daily_summary.update('flow_inlet_lpm', flow_inlet)
    daily_summary.update('flow_outlet_lpm', flow_outlet)
    daily_summary.update('daily_volume_l', get_daily_volume())

    for sensor_name, temp_c in temps.items():
        if sensor_name in ('water', 'reservoir', 'nft_drain', 'nft drain', 'outdoor', 'enclosure'):
            daily_summary.update(f'temp_c_{sensor_name.replace(" ", "_")}', temp_c)

    if conditions:
        if 'temp_c' in conditions:
            daily_summary.update('enclosure_temp_c', conditions['temp_c'])
        if 'humidity' in conditions:
            daily_summary.update('humidity_pct', conditions['humidity'])


def _fmt(v, spec='.1f'):
    return f"{v:{spec}}" if v is not None else "N/A"


def check_daily_summary():
    """Send daily summary email if due."""
    if not daily_summary.should_send(DAILY_SUMMARY_HOUR):
        return

    active_alert_count = sum(
        1 for s in alert_manager.all_states().values() if s.get('active', False)
    )

    body = (
        f"Waterflow Monitor Daily Summary\n"
        f"Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        f"Generated: {datetime.now().strftime('%H:%M:%S')}\n\n"
        f"Water Flow:\n"
        f"  Daily volume:     {_fmt(daily_summary.get_sum('daily_volume_l'), '.2f')} L\n"
        f"  Flow avg (inlet): {_fmt(daily_summary.get_avg('flow_inlet_lpm'), '.3f')} L/min\n"
        f"  Flow min (inlet): {_fmt(daily_summary.get_min('flow_inlet_lpm'), '.3f')} L/min\n\n"
        f"Temperatures (C):\n"
    )

    for sensor in ('reservoir', 'nft_drain', 'outdoor', 'enclosure'):
        body += (
            f"  {sensor:<12} "
            f"avg={_fmt(daily_summary.get_avg(f'temp_c_{sensor}'))} "
            f"min={_fmt(daily_summary.get_min(f'temp_c_{sensor}'))} "
            f"max={_fmt(daily_summary.get_max(f'temp_c_{sensor}'))}\n"
        )

    body += (
        f"\nEnclosure Humidity:\n"
        f"  avg={_fmt(daily_summary.get_avg('humidity_pct'))}% "
        f"max={_fmt(daily_summary.get_max('humidity_pct'))}%\n\n"
        f"Active alerts: {active_alert_count}\n"
    )

    if active_alert_count > 0:
        body += "\nCurrently active alerts:\n"
        for atype, s in alert_manager.all_states().items():
            if s.get('active'):
                since = s.get('first_detected', 'unknown')
                body += f"  - {atype} (since {since})\n"

    if send_email_alert(f"Daily Summary: Waterflow {datetime.now().strftime('%Y-%m-%d')}", body):
        daily_summary.mark_sent()


# ============================================================================
# MONITOR FLOW
# ============================================================================

def monitor_flow():
    """Process completed flow measurement and check for issues"""
    global smoothed_flow_inlet, smoothed_flow_outlet
    global flow_history_inlet, flow_history_outlet
    global low_flow_start_time, flow_imbalance_start_time
    global low_flow_alert_sent, leak_alert_sent
    global recovery_attempted, backup_pump_active
    global consecutive_failures

    try:
        # Snapshot counters with lock (#1)
        with _flow_lock:
            snapshot_inlet  = count_inlet
            snapshot_outlet = count_outlet

        # Calculate flow rates (L/min)
        duration = FLOW_MEASUREMENT_DURATION
        flow_inlet  = snapshot_inlet  / (FLOW_CALIBRATION_FACTOR * duration)
        flow_outlet = snapshot_outlet / (FLOW_CALIBRATION_FACTOR * duration)

        # Accumulate daily volume (#15)
        accumulate_flow_volume(flow_inlet)

        # Update flow trend history (#12)
        update_flow_trend(flow_inlet)

        # Update flow history
        flow_history_inlet.append(flow_inlet)
        flow_history_outlet.append(flow_outlet)

        if len(flow_history_inlet) > FLOW_HISTORY_SIZE:
            flow_history_inlet.pop(0)
        if len(flow_history_outlet) > FLOW_HISTORY_SIZE:
            flow_history_outlet.pop(0)

        # Weighted moving average (more weight to recent)
        weights = [1, 2, 3, 4, 5]
        weights = weights[-len(flow_history_inlet):]

        smoothed_flow_inlet  = sum(f * w for f, w in zip(flow_history_inlet,  weights)) / sum(weights)
        smoothed_flow_outlet = sum(f * w for f, w in zip(flow_history_outlet, weights)) / sum(weights)

        # Skip flow alert logic if disabled
        if DISABLE_FLOW_ALERTS:
            logging.debug(
                f"[FLOW ALERTS DISABLED] Flow: "
                f"{smoothed_flow_inlet:.3f}/{smoothed_flow_outlet:.3f} L/min"
            )
            consecutive_failures = 0
            sensors_available['flow_inlet'] = True
            sensors_available['flow_outlet'] = True
            return

        # Flow trend analysis (#12)
        avg_24h, avg_1h, flow_degraded = get_flow_trend_metrics()
        if flow_degraded and avg_1h is not None and avg_24h is not None:
            alert_type = 'flow_degradation_trend'
            should_send, reason = alert_manager.should_send(alert_type)
            if should_send:
                send_email_alert(
                    "Warning: Flow Degradation Trend Detected",
                    f"Water flow rate has dropped significantly below the 24h baseline.\n\n"
                    f"24h average flow: {avg_24h:.3f} L/min\n"
                    f"1h average flow:  {avg_1h:.3f} L/min\n"
                    f"Drop: {((avg_24h - avg_1h) / avg_24h * 100):.1f}% (threshold: 20%)\n\n"
                    f"Flow is still above the low-flow threshold ({MIN_FLOW_THRESHOLD} L/min)\n"
                    f"but this trend may indicate developing blockage or pump wear.\n\n"
                    f"Alert reason: {reason}\n"
                    f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                alert_manager.mark_sent(alert_type)
        else:
            # Clear trend alert once flow normalises
            if avg_24h is not None and avg_1h is not None and avg_1h >= avg_24h * 0.90:
                alert_manager.clear('flow_degradation_trend')

        # Check for low flow
        if smoothed_flow_inlet < MIN_FLOW_THRESHOLD:
            if low_flow_start_time is None:
                low_flow_start_time = time.time()
            elif time.time() - low_flow_start_time > FLOW_WARNING_DELAY:
                if not recovery_attempted:
                    attempt_pump_recovery()
                elif not low_flow_alert_sent:
                    alert_type = 'low_flow'
                    should_send, reason = alert_manager.should_send(alert_type)
                    if should_send:
                        recovery_note = (
                            "Backup pump is active." if backup_pump_active
                            else f"Main pump was cycled {PUMP_CYCLE_MAX_ATTEMPTS}x and backup pump was tried."
                        )
                        send_email_alert(
                            "Warning: Low Water Flow",
                            f"Water flow has been low for {FLOW_WARNING_DELAY/60:.0f} minutes.\n"
                            f"Automatic recovery was attempted but flow remains low.\n\n"
                            f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                            f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                            f"Recovery status: {recovery_note}\n"
                            f"Alert reason: {reason}\n"
                            f"Check pump and filters."
                        )
                        alert_manager.mark_sent(alert_type)
                        low_flow_alert_sent = True
        else:
            if low_flow_start_time is not None:
                if backup_pump_active:
                    logging.warning(
                        f"Flow restored on backup pump ({smoothed_flow_inlet:.3f} L/min) "
                        f"- remaining on backup until manual intervention"
                    )
                    if low_flow_alert_sent and alert_manager.clear('low_flow'):
                        send_email_alert(
                            "Flow Restored on Backup Pump",
                            f"Water flow has returned to normal, but the system is still running "
                            f"on the backup pump.\n\n"
                            f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"The main pump failed to recover automatically and requires manual "
                            f"inspection.\nRestart the script after servicing the main pump to "
                            f"restore normal operation."
                        )
                elif low_flow_alert_sent and alert_manager.clear('low_flow'):
                    send_email_alert(
                        "Water Flow Restored",
                        f"Water flow has returned to normal.\n"
                        f"Current flow: {smoothed_flow_inlet:.3f} L/min"
                    )
                alert_manager.clear('pump_recovery_failed')
                low_flow_start_time  = None
                low_flow_alert_sent  = False
                recovery_attempted   = False

        # Flow imbalance (leak detection)
        flow_diff = abs(smoothed_flow_inlet - smoothed_flow_outlet)

        if flow_diff > FLOW_IMBALANCE_THRESHOLD:
            if flow_imbalance_start_time is None:
                flow_imbalance_start_time = time.time()
            elif (time.time() - flow_imbalance_start_time > FLOW_IMBALANCE_DURATION
                    and not leak_alert_sent):
                alert_type = 'leak_detected'
                should_send, reason = alert_manager.should_send(alert_type)
                if should_send:
                    send_email_alert(
                        "CRITICAL: Possible Leak Detected",
                        f"Flow imbalance detected for {FLOW_IMBALANCE_DURATION/60:.0f} minutes.\n"
                        f"Inlet flow: {smoothed_flow_inlet:.3f} L/min\n"
                        f"Outlet flow: {smoothed_flow_outlet:.3f} L/min\n"
                        f"Difference: {flow_diff:.3f} L/min\n"
                        f"Threshold: {FLOW_IMBALANCE_THRESHOLD} L/min\n"
                        f"Alert reason: {reason}\n"
                        f"Check system for leaks!"
                    )
                    alert_manager.mark_sent(alert_type)
                    leak_alert_sent = True
        else:
            if flow_imbalance_start_time is not None:
                if leak_alert_sent and alert_manager.clear('leak_detected'):
                    send_email_alert(
                        "Flow Balance Restored",
                        f"Flow rates have returned to normal.\n"
                        f"Inlet: {smoothed_flow_inlet:.3f} L/min\n"
                        f"Outlet: {smoothed_flow_outlet:.3f} L/min"
                    )
                flow_imbalance_start_time = None
                leak_alert_sent           = False

        consecutive_failures = 0
        sensors_available['flow_inlet']  = True
        sensors_available['flow_outlet'] = True

    except Exception as e:
        consecutive_failures += 1
        logging.error(f"Flow monitoring failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
        sensors_available['flow_inlet']  = False
        sensors_available['flow_outlet'] = False

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logging.critical("Multiple consecutive flow measurement failures!")

# ============================================================================
# TEMPERATURE SENSORS (with cached paths - #5)
# ============================================================================

def discover_temp_sensors():
    """
    Return the cached list of DS18B20 sensor paths.
    Re-runs discovery if cache is empty or a sensor path has disappeared.
    """
    global _ds18b20_sensor_cache, _ds18b20_cache_valid

    if not _ds18b20_cache_valid or not _ds18b20_sensor_cache:
        _discover_and_cache_sensors()
        return _ds18b20_sensor_cache

    # Verify cached paths still exist; re-discover on any missing sensor
    for path in _ds18b20_sensor_cache:
        if not os.path.exists(path):
            logging.warning(f"DS18B20 sensor disappeared: {path} — re-running discovery")
            _discover_and_cache_sensors()
            return _ds18b20_sensor_cache

    return _ds18b20_sensor_cache


def read_temp_sensor(device_file):
    """
    Read temperature from DS18B20 sensor.

    Returns temperature in degrees Celsius (internal unit for all thresholds).
    The original code returned Fahrenheit; we now return Celsius (#13).
    """
    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()

        if lines[0].strip()[-3:] != 'YES':
            return None

        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_c = float(lines[1][equals_pos+2:]) / 1000.0
            return temp_c
    except Exception as e:
        logging.error(f"Failed to read temperature sensor {device_file}: {e}")

    return None


def read_all_temperatures():
    """
    Read all temperature sensors using ID mapping if available.

    All returned values are in degrees Celsius (#13).
    """
    sensors = discover_temp_sensors()
    temps = {}

    if DS18B20_SENSOR_MAP:
        for sensor_path in sensors:
            sensor_id = sensor_path.split('/')[-2]
            if sensor_id in DS18B20_SENSOR_MAP:
                logical_name = DS18B20_SENSOR_MAP[sensor_id]
                temp_c = read_temp_sensor(sensor_path)

                if temp_c is not None:
                    temps[logical_name] = temp_c

                    if logical_name == 'reservoir':
                        temps['water'] = temp_c
                        sensors_available['water_temp'] = True
                    elif logical_name == 'water':
                        temps['reservoir'] = temp_c
                        sensors_available['water_temp'] = True
                    elif logical_name == 'enclosure':
                        sensors_available['enclosure_temp'] = True
    else:
        logging.warning("DS18B20_SENSOR_MAP not configured - using discovery order (unreliable!)")
        for i, sensor in enumerate(sensors):
            temp_c = read_temp_sensor(sensor)
            if temp_c is not None:
                if i == 0:
                    temps['water'] = temp_c
                    temps['reservoir'] = temp_c
                    sensors_available['water_temp'] = True
                elif i == 1:
                    temps['enclosure'] = temp_c
                    sensors_available['enclosure_temp'] = True

    if 'water' not in temps and 'reservoir' not in temps:
        sensors_available['water_temp'] = False
    if 'enclosure' not in temps:
        sensors_available['enclosure_temp'] = False

    return temps

# ============================================================================
# BME280 ENVIRONMENTAL SENSOR (with cached calibration - #6)
# ============================================================================

def read_enclosure_conditions():
    """
    Read BME280 sensor data.

    Caches calibration parameters on first successful read.
    Reloads calibration if a read fails.

    Returns temps in Celsius (temp_c key added alongside temp_f for
    alerting thresholds; temp_f kept for backward-compatible Prometheus labels).
    """
    global _bme280_calibration

    try:
        import smbus2
        import bme280

        with smbus2.SMBus(1) as bus:
            # Use cached calibration (#6)
            if _bme280_calibration is None:
                _bme280_calibration = bme280.load_calibration_params(bus, BME280_I2C_ADDRESS)

            try:
                data = bme280.sample(bus, BME280_I2C_ADDRESS, _bme280_calibration)
            except Exception:
                # Reload calibration on read failure and retry once
                _bme280_calibration = bme280.load_calibration_params(bus, BME280_I2C_ADDRESS)
                data = bme280.sample(bus, BME280_I2C_ADDRESS, _bme280_calibration)

            temp_c = data.temperature
            temp_f = temp_c * 9.0 / 5.0 + 32.0
            humidity = data.humidity
            pressure = data.pressure

            a = 17.27
            b = 237.7
            alpha = ((a * temp_c) / (b + temp_c)) + (humidity / 100.0)
            dew_point_c = (b * alpha) / (a - alpha)
            dew_point_f = dew_point_c * 9.0 / 5.0 + 32.0

            sensors_available['bme280'] = True

            return {
                'temp_c': temp_c,
                'temp_f': temp_f,
                'humidity': humidity,
                'pressure': pressure,
                'dewpoint_c': dew_point_c,
                'dewpoint_f': dew_point_f,
            }
    except Exception as e:
        logging.error(f"BME280 read failed: {e}")
        _bme280_calibration = None   # Force re-load on next attempt
        sensors_available['bme280'] = False
        return None

# ============================================================================
# BATTERY DATA
# ============================================================================

def read_battery_soc():
    """Read battery SOC from Renogy data file and check monitor health"""
    global battery_soc, renogy_monitor_healthy

    try:
        if not os.path.exists(BATTERY_DATA_FILE):
            return None

        file_age = time.time() - os.path.getmtime(BATTERY_DATA_FILE)
        if file_age > BATTERY_DATA_TIMEOUT:
            logging.warning(f"Battery data file is stale ({file_age:.0f}s old)")
            return None

        renogy_healthy_found = False
        soc_found = False

        with open(BATTERY_DATA_FILE, 'r') as f:
            for line in f:
                if 'renogy_monitor_healthy' in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            renogy_monitor_healthy = float(parts[-1]) == 1.0
                            renogy_healthy_found = True
                        except ValueError:
                            pass

                if line.strip().startswith('battery_soc{'):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            battery_soc = float(parts[-1])
                            soc_found = True
                        except ValueError:
                            continue

        if renogy_healthy_found and not renogy_monitor_healthy:
            logging.warning("Renogy monitor reports unhealthy status - battery data may be unreliable")

        return battery_soc if soc_found else None

    except Exception as e:
        logging.error(f"Failed to read battery SOC: {e}")
        return None

# ============================================================================
# AERATION CONTROL
# ============================================================================

def get_aeration_by_temperature_c(water_temp_c):
    """Get aeration timing based on water temperature (degrees C)."""
    if water_temp_c > WATER_TEMP_HOT_C:
        return 420, 1380
    elif water_temp_c > WATER_TEMP_WARM_C:
        return 360, 1440
    elif water_temp_c > WATER_TEMP_MODERATE_C:
        return 300, 1800
    else:
        return 240, 2160


def control_aeration():
    """Control aeration pump with battery-based load shedding"""
    global aeration_state, last_aeration_toggle, aeration_mode

    if DISABLE_AERATOR:
        set_aeration(False)
        if aeration_state:
            logging.info("[AERATOR DISABLED] Aerator turned off")
        aeration_state = False
        aeration_mode = "disabled-manual"
        return

    soc = read_battery_soc()
    temps = read_all_temperatures()

    if soc is not None and soc < BATTERY_THRESHOLD_DISABLE:
        mode = "disabled"
        on_duration = 0
        off_duration = 9999999
    elif soc is not None and soc < BATTERY_THRESHOLD_REDUCE:
        mode = "reduced"
        on_duration = AERATION_ON_DURATION_REDUCED
        off_duration = AERATION_OFF_DURATION_REDUCED
    else:
        mode = "normal"
        on_duration = AERATION_ON_DURATION
        off_duration = AERATION_OFF_DURATION

    # Temperature-based adjustment (all thresholds in C now - #13)
    water_key = 'water' if 'water' in temps else 'reservoir' if 'reservoir' in temps else None
    if sensors_available['water_temp'] and water_key:
        on_duration, off_duration = get_aeration_by_temperature_c(temps[water_key])
        mode = f"{mode}-{temps[water_key]:.1f}C"

    # Alert if load shedding active
    if mode.startswith("disabled") or mode.startswith("reduced"):
        alert_type = 'load_shed_active'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            send_email_alert(
                "Warning: Aeration Load Shedding Active",
                f"Aeration reduced due to low battery.\n"
                f"Battery SOC: {soc}%\n"
                f"Mode: {mode}\n"
                f"Alert reason: {reason}"
            )
            alert_manager.mark_sent(alert_type)
    else:
        alert_manager.clear('load_shed_active')

    current_time = time.time()
    time_in_state = current_time - last_aeration_toggle

    if aeration_state:
        if time_in_state >= on_duration:
            set_aeration(False)
            aeration_state = False
            last_aeration_toggle = current_time
    else:
        if time_in_state >= off_duration:
            if on_duration > 0:
                set_aeration(True)
                aeration_state = True
                last_aeration_toggle = current_time

    aeration_mode = mode

# ============================================================================
# FAN CONTROL
# ============================================================================

def control_ventilation_fan(conditions, temps):
    """
    Control ventilation fan based on temperature and humidity.

    All temperature comparisons use degrees C internally (#13).
    The outdoor temperature from DS18B20 is also in C.
    """
    global fan_running, fan_last_toggle

    if DISABLE_FAN:
        set_fan(False)
        if fan_running:
            logging.info("[FAN DISABLED] Ventilation fan turned off")
        fan_running = False
        return

    enclosure_temp_c = None
    humidity = None

    # Priority 1: BME280 (inside enclosure)
    if sensors_available['bme280'] and conditions and 'temp_c' in conditions:
        enclosure_temp_c = conditions['temp_c']
        humidity = conditions.get('humidity')
    # Priority 2: DS18B20 enclosure sensor
    elif sensors_available['enclosure_temp'] and 'enclosure' in temps:
        enclosure_temp_c = temps['enclosure']

    if enclosure_temp_c is None:
        if fan_running:
            set_fan(False)
            fan_running = False
        return

    if time.time() - fan_last_toggle < FAN_MIN_TOGGLE_INTERVAL:
        return

    should_run = False

    if enclosure_temp_c >= FAN_TEMP_FORCE_ON_C:
        should_run = True
    elif enclosure_temp_c >= FAN_TEMP_ON_C:
        should_run = True
    elif enclosure_temp_c <= FAN_TEMP_OFF_C:
        should_run = False
    else:
        should_run = fan_running  # Hysteresis

    if humidity is not None:
        if humidity >= FAN_HUMIDITY_ON:
            should_run = True
        elif humidity <= FAN_HUMIDITY_OFF and enclosure_temp_c < FAN_TEMP_ON_C:
            should_run = False

    # Smart cooling: outdoor temperature vs ENCLOSURE (all in C - #13)
    if 'outdoor' in temps:
        outdoor_c = temps['outdoor']

        if outdoor_c < enclosure_temp_c - OUTDOOR_COOLING_DELTA_C and enclosure_temp_c > 21.1:
            should_run = True
            if not fan_running:
                logging.info(
                    f"Smart enclosure cooling: outdoor {outdoor_c:.1f}C < "
                    f"enclosure {enclosure_temp_c:.1f}C"
                )
        elif outdoor_c > enclosure_temp_c + OUTDOOR_HEAT_DELTA_C and enclosure_temp_c < 26.7:
            if enclosure_temp_c < FAN_TEMP_FORCE_ON_C:
                should_run = False
                if fan_running:
                    logging.info(
                        f"Reducing ventilation: outdoor {outdoor_c:.1f}C > "
                        f"enclosure {enclosure_temp_c:.1f}C"
                    )

    if should_run != fan_running:
        set_fan(should_run)
        fan_running = should_run
        fan_last_toggle = time.time()

# ============================================================================
# TEMPERATURE MONITORING (NFT System)
# ============================================================================

def monitor_temperature_differentials(temps):
    """
    Monitor temperature relationships for NFT hydroponic system.

    All temperatures are in degrees C (#13).
    Alert email text converts to F for readability.
    """

    # 1. FREEZE RISK WARNING
    if 'outdoor' in temps and 'reservoir' in temps:
        outdoor_c    = temps['outdoor']
        reservoir_c  = temps['reservoir']

        outdoor_f   = outdoor_c   * 9/5 + 32
        reservoir_f = reservoir_c * 9/5 + 32
        freeze_f    = OUTDOOR_FREEZE_RISK_C * 9/5 + 32
        res_min_f   = RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT_C * 9/5 + 32

        if outdoor_c < OUTDOOR_FREEZE_RISK_C and reservoir_c < RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT_C:
            alert_type = 'freeze_risk'
            should_send, reason = alert_manager.should_send(alert_type)
            if should_send:
                send_email_alert(
                    "Freeze Risk Warning",
                    f"Cold outdoor temperature with low reservoir temperature!\n\n"
                    f"Temperature Readings:\n"
                    f"  Outdoor:    {outdoor_f:.1f}F ({outdoor_c:.1f}C)"
                    f" (Freeze risk: <{freeze_f:.1f}F / {OUTDOOR_FREEZE_RISK_C:.1f}C)\n"
                    f"  Reservoir:  {reservoir_f:.1f}F ({reservoir_c:.1f}C)"
                    f" (Target: >{res_min_f:.1f}F / {RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT_C:.1f}C)\n\n"
                    f"Freeze Risks:\n"
                    f"  - Exposed NFT pipes may freeze\n"
                    f"  - Reservoir heat loss accelerating\n"
                    f"  - Plant stress from cold\n"
                    f"  - Pump may be affected\n\n"
                    f"Recommended Actions:\n"
                    f"  - Monitor temperatures closely\n"
                    f"  - Consider adding heat source to reservoir\n"
                    f"  - Insulate exposed NFT pipes if possible\n"
                    f"  - Protect pump from freezing\n\n"
                    f"Alert reason: {reason}"
                )
                alert_manager.mark_sent(alert_type)
        else:
            alert_manager.clear('freeze_risk')

    # 2. EXTREME NFT TEMPERATURE WARNING
    nft_temp_key = None
    for key in ['nft drain', 'nft_drain', 'nft_return']:
        if key in temps:
            nft_temp_key = key
            break

    if ENABLE_NFT_EXTREME_TEMP_ALERT and nft_temp_key:
        nft_c = temps[nft_temp_key]
        nft_f = nft_c * 9/5 + 32
        limit_f = NFT_RETURN_EXTREME_TEMP_C * 9/5 + 32

        if nft_c > NFT_RETURN_EXTREME_TEMP_C:
            alert_type = 'nft_extreme_temp'
            should_send, reason = alert_manager.should_send(alert_type)
            if should_send:
                reservoir_c = temps.get('reservoir')
                res_str = (
                    f"{reservoir_c * 9/5 + 32:.1f}F ({reservoir_c:.1f}C)"
                    if reservoir_c is not None else "N/A"
                )
                send_email_alert(
                    "Extreme NFT Temperature Warning",
                    f"NFT drain/return water temperature is very high!\n\n"
                    f"Temperature Readings:\n"
                    f"  NFT Drain:  {nft_f:.1f}F ({nft_c:.1f}C)"
                    f" (Limit: <{limit_f:.1f}F / {NFT_RETURN_EXTREME_TEMP_C:.1f}C)\n"
                    f"  Reservoir:  {res_str}\n\n"
                    f"Plant Stress Risks:\n"
                    f"  - Root zone too warm (reduces oxygen)\n"
                    f"  - Increased pathogen growth risk\n"
                    f"  - Nutrient uptake affected\n"
                    f"  - Plant wilting possible\n\n"
                    f"Recommended Actions:\n"
                    f"  - Add shade to NFT pipes\n"
                    f"  - Increase water flow rate\n"
                    f"  - Consider chilling reservoir\n"
                    f"  - Check for adequate aeration\n\n"
                    f"Alert reason: {reason}"
                )
                alert_manager.mark_sent(alert_type)
        else:
            alert_manager.clear('nft_extreme_temp')

    # 3. INFORMATIONAL LOGGING (no alerts, just data)
    if TEMP_DIFF_TRACKING_ENABLED and 'reservoir' in temps and nft_temp_key:
        differential_c = temps[nft_temp_key] - temps['reservoir']
        logging.debug(
            f"NFT thermal load: {nft_temp_key} {temps[nft_temp_key]:.1f}C - "
            f"reservoir {temps['reservoir']:.1f}C = {differential_c:+.2f}C"
        )

# ============================================================================
# HUMIDITY ALERTS
# ============================================================================

def check_humidity_alerts(conditions):
    """Check humidity levels and send alerts"""
    if conditions is None or 'humidity' not in conditions:
        return

    humidity = conditions['humidity']
    dew_str = (
        f"{conditions['dewpoint_f']:.1f}F"
        if 'dewpoint_f' in conditions else "N/A"
    )

    if humidity >= HUMIDITY_EMERGENCY:
        alert_type = 'humidity_critical'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            send_email_alert(
                "EMERGENCY: Critical Humidity Level",
                f"Humidity has reached emergency levels!\n"
                f"Current: {humidity:.1f}%\n"
                f"Threshold: {HUMIDITY_EMERGENCY}%\n"
                f"Dew point: {dew_str}\n"
                f"Alert reason: {reason}\n"
                f"High risk of condensation damage!"
            )
            alert_manager.mark_sent(alert_type)
    elif humidity >= HUMIDITY_WARNING:
        alert_type = 'humidity_warning'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            send_email_alert(
                "Warning: High Humidity",
                f"Humidity is elevated.\n"
                f"Current: {humidity:.1f}%\n"
                f"Threshold: {HUMIDITY_WARNING}%\n"
                f"Dew point: {dew_str}\n"
                f"Alert reason: {reason}\n"
                f"Monitor for condensation."
            )
            alert_manager.mark_sent(alert_type)
    else:
        if alert_manager.clear('humidity_critical'):
            send_email_alert(
                "Humidity Normalized",
                f"Humidity has returned to safe levels.\n"
                f"Current: {humidity:.1f}%"
            )
        if alert_manager.clear('humidity_warning'):
            send_email_alert(
                "Humidity Normal",
                f"Humidity has decreased.\n"
                f"Current: {humidity:.1f}%"
            )

# ============================================================================
# PUMP TESTING
# ============================================================================

def should_run_pump_test():
    """Check if backup pump test is due"""
    global last_pump_test

    if DISABLE_PUMP_TESTING:
        return False

    if last_pump_test == 0:
        return False

    # Skip pump test if battery SOC is too low (#14)
    soc = read_battery_soc()
    if soc is not None and soc < BATTERY_SKIP_PUMP_TEST_BELOW:
        logging.warning(
            f"Skipping pump test: battery SOC {soc:.1f}% < "
            f"{BATTERY_SKIP_PUMP_TEST_BELOW}% (threshold)"
        )
        return False

    return time.time() - last_pump_test > PUMP_TEST_INTERVAL


def test_backup_pump():
    """
    Test backup pump functionality, or recheck main pump if currently in failover.
    """
    global last_pump_test, backup_pump_active

    if backup_pump_active:
        logging.info("Pump test: in failover mode - rechecking main pump")

        set_backup_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_main_pump(True)

        time.sleep(PUMP_TEST_DURATION)
        test_flow = smoothed_flow_inlet

        if test_flow >= MIN_FLOW_THRESHOLD:
            backup_pump_active = False
            alert_manager.clear('backup_pump_failover')
            alert_manager.clear('pump_recovery_failed')
            logging.warning(
                f"Pump test: main pump recovered ({test_flow:.3f} L/min) - "
                f"switched back from backup"
            )
            send_email_alert(
                "Main Pump Recovered - Failover Cleared",
                f"The scheduled pump test found the main pump is working again.\n"
                f"System has switched back to the main pump.\n\n"
                f"Flow during test: {test_flow:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"No further action required. Next pump test in "
                f"{PUMP_TEST_INTERVAL/3600:.0f} hours."
            )
        else:
            set_main_pump(False)
            time.sleep(PUMP_CYCLE_PAUSE)
            set_backup_pump(True)
            logging.warning(
                f"Pump test: main pump still failing ({test_flow:.3f} L/min) - "
                f"returned to backup"
            )
            send_email_alert(
                "Main Pump Still Failing - Remaining on Backup",
                f"The scheduled pump recheck found the main pump is still not working.\n"
                f"System has returned to the backup pump.\n\n"
                f"Flow during test: {test_flow:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Main pump requires manual inspection.\n"
                f"Next recheck in {PUMP_TEST_INTERVAL/3600:.0f} hours."
            )

    else:
        logging.info("Starting backup pump test")

        set_main_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_backup_pump(True)

        time.sleep(PUMP_TEST_DURATION)
        test_flow = smoothed_flow_inlet

        set_backup_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_main_pump(True)

        if test_flow >= MIN_FLOW_THRESHOLD:
            result = "PASSED"
            status = "OK"
        else:
            result = "FAILED"
            status = "FAIL"

        send_email_alert(
            f"{status} Backup Pump Test {result}",
            f"Backup pump test completed.\n"
            f"Result: {result}\n"
            f"Flow during test: {test_flow:.3f} L/min\n"
            f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
            f"Next test in: {PUMP_TEST_INTERVAL/3600:.0f} hours"
        )

    last_pump_test = time.time()

# ============================================================================
# PUMP RECOVERY
# ============================================================================

def attempt_pump_recovery():
    """
    Attempt to restore flow after sustained low flow is detected.

    Recovery sequence:
      1. Cycle main pump off/on up to PUMP_CYCLE_MAX_ATTEMPTS times,
         waiting PUMP_RECOVERY_WAIT seconds after each cycle to check flow.
      2. If flow is still low after all cycles, switch to backup pump.
      3. Send an appropriate alert regardless of outcome.
    """
    global recovery_attempted, backup_pump_active

    recovery_attempted = True
    logging.warning("Low flow recovery initiated - cycling main pump")

    for attempt in range(1, PUMP_CYCLE_MAX_ATTEMPTS + 1):
        logging.warning(f"Pump recovery: main pump cycle {attempt}/{PUMP_CYCLE_MAX_ATTEMPTS}")

        set_main_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_main_pump(True)

        time.sleep(PUMP_RECOVERY_WAIT)

        if smoothed_flow_inlet >= MIN_FLOW_THRESHOLD:
            logging.warning(
                f"Pump recovery: flow restored after cycle {attempt} "
                f"({smoothed_flow_inlet:.3f} L/min)"
            )
            send_email_alert(
                "Water Flow Restored - Main Pump Cycled",
                f"Low flow was resolved by cycling the main pump.\n\n"
                f"Cycles attempted: {attempt}\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return

    # Failover to backup pump
    logging.warning("Pump recovery: main pump cycles exhausted, switching to backup pump")

    set_main_pump(False)
    time.sleep(PUMP_CYCLE_PAUSE)
    set_backup_pump(True)
    backup_pump_active = True

    time.sleep(PUMP_RECOVERY_WAIT)

    if smoothed_flow_inlet >= MIN_FLOW_THRESHOLD:
        logging.warning(
            f"Pump recovery: backup pump restored flow ({smoothed_flow_inlet:.3f} L/min)"
        )
        alert_type = 'backup_pump_failover'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            send_email_alert(
                "Warning: Running on Backup Pump - Main Pump Failed",
                f"Main pump failed to restore flow after {PUMP_CYCLE_MAX_ATTEMPTS} cycles.\n"
                f"System has switched to the backup pump.\n\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Action required: inspect main pump and filters.\n"
                f"The main pump will be restored automatically once flow is confirmed stable."
            )
            alert_manager.mark_sent(alert_type)
    else:
        logging.error(
            f"Pump recovery: backup pump also failed ({smoothed_flow_inlet:.3f} L/min) "
            f"- manual intervention required"
        )
        alert_type = 'pump_recovery_failed'
        should_send, reason = alert_manager.should_send(alert_type)
        if should_send:
            send_email_alert(
                "CRITICAL: Both Pumps Failed - Manual Intervention Required",
                f"Automatic recovery failed. Neither main nor backup pump restored flow.\n\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Main pump cycles attempted: {PUMP_CYCLE_MAX_ATTEMPTS}\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Immediate manual inspection required!\n"
                f"Check: power supply, tubing, reservoir level, blockages."
            )
            alert_manager.mark_sent(alert_type)

# ============================================================================
# STATE PERSISTENCE
# ============================================================================

def load_state():
    """Load persistent state from file"""
    global last_pump_test, backup_pump_active

    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                state = json.load(f)
                last_pump_test   = state.get('last_pump_test',   time.time())
                backup_pump_active = state.get('backup_pump_active', False)
                if backup_pump_active:
                    set_main_pump(False)
                    set_backup_pump(True)
                    logging.warning(
                        "Reboot detected while on backup pump failover - "
                        "main pump remains off pending manual inspection"
                    )
    except Exception as e:
        logging.error(f"Failed to load state: {e}")
        last_pump_test = time.time()


def save_state():
    """Save persistent state to file (ramdisk). Also writes persistent path when active."""
    try:
        state = {
            'last_pump_test':    last_pump_test,
            'backup_pump_active': backup_pump_active,
            'last_save':         time.time()
        }

        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True) \
            if os.path.dirname(STATE_FILE) else None

        tmp = STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.rename(tmp, STATE_FILE)

        # Write to persistent storage when backup pump is active (#3)
        if backup_pump_active:
            try:
                os.makedirs(os.path.dirname(PERSISTENT_STATE_FILE), exist_ok=True)
                tmp_p = PERSISTENT_STATE_FILE + '.tmp'
                with open(tmp_p, 'w') as f:
                    json.dump(state, f)
                os.rename(tmp_p, PERSISTENT_STATE_FILE)
            except Exception as e:
                logging.error(f"Failed to save persistent state: {e}")

    except Exception as e:
        logging.error(f"Failed to save state: {e}")

# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

MAX_CONSECUTIVE_FAILURES = 5


def write_prometheus_metrics(temps=None, conditions=None):
    """Write all metrics to Prometheus format file"""
    global last_prometheus_write

    try:
        with open(PROM_TEMP_FILE, 'w') as f:
            # Flow metrics
            f.write("# HELP waterflow_inlet_lpm Smoothed inlet water flow\n")
            f.write("# TYPE waterflow_inlet_lpm gauge\n")
            f.write(f"waterflow_inlet_lpm{{source=\"waterflow\"}} {smoothed_flow_inlet:.3f}\n")

            f.write("# HELP waterflow_outlet_lpm Smoothed outlet water flow\n")
            f.write("# TYPE waterflow_outlet_lpm gauge\n")
            f.write(f"waterflow_outlet_lpm{{source=\"waterflow\"}} {smoothed_flow_outlet:.3f}\n")

            if len(flow_history_inlet) > 0:
                f.write("# HELP waterflow_inlet_raw_lpm Raw inlet water flow\n")
                f.write("# TYPE waterflow_inlet_raw_lpm gauge\n")
                f.write(f"waterflow_inlet_raw_lpm{{source=\"waterflow\"}} {flow_history_inlet[-1]:.3f}\n")

            if len(flow_history_outlet) > 0:
                f.write("# HELP waterflow_outlet_raw_lpm Raw outlet water flow\n")
                f.write("# TYPE waterflow_outlet_raw_lpm gauge\n")
                f.write(f"waterflow_outlet_raw_lpm{{source=\"waterflow\"}} {flow_history_outlet[-1]:.3f}\n")

            flow_diff = abs(smoothed_flow_inlet - smoothed_flow_outlet)
            f.write("# HELP waterflow_imbalance_lpm Flow difference between inlet and outlet\n")
            f.write("# TYPE waterflow_imbalance_lpm gauge\n")
            f.write(f"waterflow_imbalance_lpm{{source=\"waterflow\"}} {flow_diff:.3f}\n")

            # Flow trend metrics (#12)
            avg_24h, avg_1h, flow_degraded = get_flow_trend_metrics()
            if avg_24h is not None:
                f.write("# HELP waterflow_trend_24h_avg_lpm 24-hour average flow rate\n")
                f.write("# TYPE waterflow_trend_24h_avg_lpm gauge\n")
                f.write(f"waterflow_trend_24h_avg_lpm{{source=\"waterflow\"}} {avg_24h:.3f}\n")
            if avg_1h is not None:
                f.write("# HELP waterflow_trend_1h_avg_lpm 1-hour average flow rate\n")
                f.write("# TYPE waterflow_trend_1h_avg_lpm gauge\n")
                f.write(f"waterflow_trend_1h_avg_lpm{{source=\"waterflow\"}} {avg_1h:.3f}\n")
            f.write("# HELP waterflow_trend_degraded Flow degradation trend active\n")
            f.write("# TYPE waterflow_trend_degraded gauge\n")
            f.write(f"waterflow_trend_degraded{{source=\"waterflow\"}} {1 if flow_degraded else 0}\n")

            # Daily volume (#15)
            f.write("# HELP waterflow_daily_volume_liters Total water volume today\n")
            f.write("# TYPE waterflow_daily_volume_liters gauge\n")
            f.write(f"waterflow_daily_volume_liters{{source=\"waterflow\"}} {get_daily_volume():.2f}\n")

            # Alert flags
            f.write("# HELP waterflow_warning_active Low flow warning active\n")
            f.write("# TYPE waterflow_warning_active gauge\n")
            f.write(f"waterflow_warning_active{{source=\"waterflow\"}} {1 if low_flow_alert_sent else 0}\n")

            f.write("# HELP waterflow_backup_pump_active Backup pump running as failover\n")
            f.write("# TYPE waterflow_backup_pump_active gauge\n")
            f.write(f"waterflow_backup_pump_active{{source=\"waterflow\"}} {1 if backup_pump_active else 0}\n")

            f.write("# HELP waterflow_pump_recovery_attempted Recovery attempted for current low flow event\n")
            f.write("# TYPE waterflow_pump_recovery_attempted gauge\n")
            f.write(f"waterflow_pump_recovery_attempted{{source=\"waterflow\"}} {1 if recovery_attempted else 0}\n")

            f.write("# HELP waterflow_leak_detected Leak detection active\n")
            f.write("# TYPE waterflow_leak_detected gauge\n")
            f.write(f"waterflow_leak_detected{{source=\"waterflow\"}} {1 if leak_alert_sent else 0}\n")

            # Maintenance mode flags
            f.write("# HELP waterflow_emails_disabled Email alerts disabled\n")
            f.write("# TYPE waterflow_emails_disabled gauge\n")
            f.write(f"waterflow_emails_disabled{{source=\"waterflow\"}} {1 if DISABLE_EMAILS else 0}\n")

            f.write("# HELP waterflow_pump_testing_disabled Pump testing disabled\n")
            f.write("# TYPE waterflow_pump_testing_disabled gauge\n")
            f.write(f"waterflow_pump_testing_disabled{{source=\"waterflow\"}} {1 if DISABLE_PUMP_TESTING else 0}\n")

            f.write("# HELP waterflow_flow_alerts_disabled Flow alerts disabled\n")
            f.write("# TYPE waterflow_flow_alerts_disabled gauge\n")
            f.write(f"waterflow_flow_alerts_disabled{{source=\"waterflow\"}} {1 if DISABLE_FLOW_ALERTS else 0}\n")

            f.write("# HELP waterflow_aerator_disabled Aerator disabled\n")
            f.write("# TYPE waterflow_aerator_disabled gauge\n")
            f.write(f"waterflow_aerator_disabled{{source=\"waterflow\"}} {1 if DISABLE_AERATOR else 0}\n")

            f.write("# HELP waterflow_fan_disabled Fan disabled\n")
            f.write("# TYPE waterflow_fan_disabled gauge\n")
            f.write(f"waterflow_fan_disabled{{source=\"waterflow\"}} {1 if DISABLE_FAN else 0}\n")

            # Aeration
            f.write("# HELP aeration_state Aeration pump state\n")
            f.write("# TYPE aeration_state gauge\n")
            f.write(f"aeration_state{{source=\"waterflow\"}} {1 if aeration_state else 0}\n")

            f.write("# HELP aeration_mode Aeration mode\n")
            f.write("# TYPE aeration_mode gauge\n")
            f.write(f'aeration_mode{{mode="{aeration_mode}",source="waterflow"}} 1\n')

            # Fan
            f.write("# HELP fan_state Ventilation fan state\n")
            f.write("# TYPE fan_state gauge\n")
            f.write(f"fan_state{{source=\"waterflow\"}} {1 if fan_running else 0}\n")

            # Temperatures - in Celsius (#13) + backward-compat Fahrenheit label
            if temps is None:
                temps = read_all_temperatures()

            f.write("# HELP waterflow_temperature_celsius Temperature reading in Celsius\n")
            f.write("# TYPE waterflow_temperature_celsius gauge\n")
            for sensor_name in ['water', 'reservoir', 'nft drain', 'nft_drain', 'nft_return',
                                 'outdoor', 'enclosure']:
                if sensor_name in temps:
                    metric_name = sensor_name.replace(' ', '_')
                    f.write(
                        f"waterflow_temperature_celsius"
                        f"{{sensor=\"{metric_name}\",source=\"waterflow\"}} "
                        f"{temps[sensor_name]:.2f}\n"
                    )

            # Also export Fahrenheit for backward compatibility with existing dashboards
            f.write("# HELP waterflow_temperature_fahrenheit Temperature reading in Fahrenheit\n")
            f.write("# TYPE waterflow_temperature_fahrenheit gauge\n")
            for sensor_name in ['water', 'reservoir', 'nft drain', 'nft_drain', 'nft_return',
                                 'outdoor', 'enclosure']:
                if sensor_name in temps:
                    metric_name = sensor_name.replace(' ', '_')
                    temp_f = temps[sensor_name] * 9/5 + 32
                    f.write(
                        f"waterflow_temperature_fahrenheit"
                        f"{{sensor=\"{metric_name}\",source=\"waterflow\"}} "
                        f"{temp_f:.1f}\n"
                    )

            # Temperature differentials (Celsius)
            nft_temp_key = None
            for key in ['nft drain', 'nft_drain', 'nft_return']:
                if key in temps:
                    nft_temp_key = key
                    break

            if nft_temp_key and 'reservoir' in temps:
                temp_diff_c = temps[nft_temp_key] - temps['reservoir']
                f.write("# HELP waterflow_temperature_differential_celsius Temperature differential\n")
                f.write("# TYPE waterflow_temperature_differential_celsius gauge\n")
                f.write(
                    f"waterflow_temperature_differential_celsius"
                    f"{{diff_type=\"nft_solar_heating\",source=\"waterflow\"}} "
                    f"{temp_diff_c:.2f}\n"
                )

            if 'outdoor' in temps and 'reservoir' in temps:
                temp_diff_c = temps['outdoor'] - temps['reservoir']
                f.write(
                    f"waterflow_temperature_differential_celsius"
                    f"{{diff_type=\"outdoor_vs_reservoir\",source=\"waterflow\"}} "
                    f"{temp_diff_c:.2f}\n"
                )

            # Environmental conditions
            if conditions is None:
                conditions = read_enclosure_conditions()

            if conditions:
                if 'temp_c' in conditions:
                    sensors_available['enclosure_temp'] = True

                f.write("# HELP enclosure_temperature_celsius Enclosure temperature from BME280\n")
                f.write("# TYPE enclosure_temperature_celsius gauge\n")
                f.write(f"enclosure_temperature_celsius{{source=\"waterflow\"}} {conditions['temp_c']:.2f}\n")

                # Keep Fahrenheit label for backward compat
                f.write("# HELP enclosure_temperature_fahrenheit Enclosure temperature from BME280 (F)\n")
                f.write("# TYPE enclosure_temperature_fahrenheit gauge\n")
                f.write(f"enclosure_temperature_fahrenheit{{source=\"waterflow\"}} {conditions['temp_f']:.1f}\n")

                f.write("# HELP enclosure_humidity_percent Relative humidity\n")
                f.write("# TYPE enclosure_humidity_percent gauge\n")
                f.write(f"enclosure_humidity_percent{{source=\"waterflow\"}} {conditions['humidity']:.1f}\n")

                f.write("# HELP enclosure_pressure_hpa Barometric pressure\n")
                f.write("# TYPE enclosure_pressure_hpa gauge\n")
                f.write(f"enclosure_pressure_hpa{{source=\"waterflow\"}} {conditions['pressure']:.1f}\n")

                f.write("# HELP enclosure_dewpoint_f Dew point temperature (F)\n")
                f.write("# TYPE enclosure_dewpoint_f gauge\n")
                f.write(f"enclosure_dewpoint_f{{source=\"waterflow\"}} {conditions['dewpoint_f']:.1f}\n")
            else:
                sensors_available['enclosure_temp'] = False

            # Battery SOC
            f.write("# HELP battery_soc_percent Battery state of charge\n")
            f.write("# TYPE battery_soc_percent gauge\n")
            f.write(f"battery_soc_percent{{source=\"waterflow\"}} {battery_soc:.1f}\n")

            # Health metrics
            f.write("# HELP waterflow_monitor_healthy Monitor health status\n")
            f.write("# TYPE waterflow_monitor_healthy gauge\n")
            healthy = 1 if consecutive_failures < MAX_CONSECUTIVE_FAILURES else 0
            f.write(f"waterflow_monitor_healthy{{source=\"waterflow\"}} {healthy}\n")

            f.write("# HELP waterflow_consecutive_failures Consecutive failure count\n")
            f.write("# TYPE waterflow_consecutive_failures gauge\n")
            f.write(f"waterflow_consecutive_failures{{source=\"waterflow\"}} {consecutive_failures}\n")

            f.write("# HELP renogy_monitor_healthy_from_waterflow Renogy monitor health as seen by waterflow\n")
            f.write("# TYPE renogy_monitor_healthy_from_waterflow gauge\n")
            f.write(
                f"renogy_monitor_healthy_from_waterflow{{source=\"waterflow\"}} "
                f"{1 if renogy_monitor_healthy else 0}\n"
            )

            # Alert states
            for alert_type, s in alert_manager.all_states().items():
                safe = alert_type.replace('-', '_')
                f.write(f"# HELP waterflow_alert_{safe} Alert status\n")
                f.write(f"# TYPE waterflow_alert_{safe} gauge\n")
                f.write(
                    f"waterflow_alert_{safe}{{source=\"waterflow\"}} "
                    f"{1 if s['active'] else 0}\n"
                )

            # Sensor availability
            for sensor_name, available in sensors_available.items():
                f.write(f"# HELP waterflow_sensor_available_{sensor_name} Sensor availability\n")
                f.write(f"# TYPE waterflow_sensor_available_{sensor_name} gauge\n")
                f.write(
                    f"waterflow_sensor_available_{sensor_name}{{source=\"waterflow\"}} "
                    f"{1 if available else 0}\n"
                )

            # Timestamp
            f.write("# HELP waterflow_last_update Last update timestamp\n")
            f.write("# TYPE waterflow_last_update gauge\n")
            f.write(f"waterflow_last_update{{source=\"waterflow\"}} {int(time.time())}\n")

        os.rename(PROM_TEMP_FILE, PROM_OUTPUT_FILE)
        last_prometheus_write = time.time()

    except Exception as e:
        logging.error(f"Prometheus write error: {e}")


def write_unhealthy_status():
    """Write minimal metrics when system is failing"""
    try:
        with open(PROM_TEMP_FILE, 'w') as f:
            f.write('# HELP waterflow_monitor_healthy Monitor health status\n')
            f.write('# TYPE waterflow_monitor_healthy gauge\n')
            f.write('waterflow_monitor_healthy{source="waterflow"} 0\n')

            f.write('# HELP waterflow_consecutive_failures Consecutive failure count\n')
            f.write('# TYPE waterflow_consecutive_failures gauge\n')
            f.write(f'waterflow_consecutive_failures{{source="waterflow"}} {consecutive_failures}\n')

            f.write('# HELP waterflow_last_update Last update timestamp\n')
            f.write('# TYPE waterflow_last_update gauge\n')
            f.write(f'waterflow_last_update{{source="waterflow"}} {int(time.time())}\n')

        os.rename(PROM_TEMP_FILE, PROM_OUTPUT_FILE)
    except Exception as e:
        logging.error(f"Failed to write unhealthy status: {e}")

# ============================================================================
# STARTUP NOTIFICATION (#4)
# ============================================================================

def send_startup_notification():
    """Run startup self-test and send a startup notification email."""
    try:
        def check_gpio():
            try:
                import RPi.GPIO as _GPIO
                return ("PASS", "RPi.GPIO imported successfully")
            except ImportError as e:
                return ("FAIL", f"RPi.GPIO import failed: {e}")

        def check_ds18b20():
            count = len(_ds18b20_sensor_cache)
            if count > 0:
                return ("PASS", f"{count} sensor(s) found in cache")
            import glob as _glob
            found = _glob.glob('/sys/bus/w1/devices/28-*')
            if found:
                return ("WARN", f"{len(found)} sensor(s) found on bus but cache empty")
            return ("FAIL", "no DS18B20 sensors found in /sys/bus/w1/devices/28-*")

        def check_bme280():
            try:
                import smbus2 as _smbus2
                import bme280 as _bme280
                bus = _smbus2.SMBus(1)
                cal = _bme280.load_calibration_params(bus, BME280_I2C_ADDRESS)
                data = _bme280.sample(bus, BME280_I2C_ADDRESS, cal)
                bus.close()
                return ("PASS", f"temp={data.temperature:.1f}°C humidity={data.humidity:.1f}%")
            except Exception as e:
                return ("WARN", f"BME280 read failed: {e}")

        def check_renogy_prom():
            prom = BATTERY_DATA_FILE
            if os.path.exists(prom):
                import time as _time
                age = _time.time() - os.path.getmtime(prom)
                if age < BATTERY_DATA_TIMEOUT:
                    return ("PASS", f"{prom} exists and is fresh ({age:.0f}s old)")
                else:
                    return ("WARN",
                            f"{prom} exists but is stale ({age:.0f}s old, "
                            f"timeout={BATTERY_DATA_TIMEOUT}s)")
            return ("WARN", f"{prom} not found — is renogy.py running?")

        extra_checks = [
            ("GPIO",              check_gpio),
            ("1-Wire sensors",    check_ds18b20),
            ("I2C/BME280",        check_bme280),
            ("Ramdisk Renogy data", check_renogy_prom),
        ]
        startup_selftest("waterflow", _CONFIG_PATH, LOG_FILE_PATH, extra_checks)
    except Exception as e:
        logging.warning(f"Failed to send startup notification: {e}")

# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    """Main monitoring loop"""
    global consecutive_failures

    print("=" * 70)
    print("   HYDROPONIC MONITORING - ENHANCED VERSION")
    print("=" * 70)
    print(f"Prometheus: {PROM_OUTPUT_FILE}")
    print(f"Main loop: {MAIN_LOOP_INTERVAL}s interval (non-blocking)")
    print(f"Health monitoring: Enabled (max failures: {MAX_CONSECUTIVE_FAILURES})")
    print(f"Error logging: {LOG_FILE_PATH}")
    print(f"New: Watchdog, Daily summary, Flow trend, Daily volume, SIGHUP reload")

    disabled_systems = []
    if DISABLE_EMAILS:       disabled_systems.append("EMAILS")
    if DISABLE_PUMP_TESTING: disabled_systems.append("PUMP TESTING")
    if DISABLE_FLOW_ALERTS:  disabled_systems.append("FLOW ALERTS")
    if DISABLE_AERATOR:      disabled_systems.append("AERATOR")
    if DISABLE_FAN:          disabled_systems.append("FAN")

    if disabled_systems:
        print()
        print("=" * 70)
        print("  MAINTENANCE MODE ACTIVE")
        print(f"  DISABLED: {', '.join(disabled_systems)}")
        print("=" * 70)

    print("=" * 70)

    setup_gpio()
    load_state()

    # Cache DS18B20 sensor paths at startup (#5)
    _discover_and_cache_sensors()
    print(f"Temperature sensors: {len(_ds18b20_sensor_cache)}")
    print(f"Initial battery SOC: {battery_soc:.1f}%")
    print()

    # Load alert state
    alert_manager.load()

    # Start hardware watchdog (#8)
    watchdog = None
    if get_config('watchdog.enabled', True):
        watchdog = Watchdog(
            device=get_config('watchdog.device', '/dev/watchdog'),
            interval=get_config('watchdog.interval_seconds', 30),
        )
        watchdog.start()

    # Startup notification (#4)
    send_startup_notification()

    start_flow_measurement()

    iteration = 0
    last_save_time = time.time()

    # Keep references to conditions/temps for passing to prometheus writer
    _last_conditions = None
    _last_temps = {}

    try:
        while True:
            iteration += 1
            loop_start = time.time()

            # Check if flow measurement complete
            if check_flow_measurement():
                try:
                    monitor_flow()
                    consecutive_failures = 0
                except Exception as e:
                    consecutive_failures += 1
                    logging.error(
                        f"Flow monitoring failed "
                        f"({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}"
                    )

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logging.critical("Multiple consecutive failures! Check sensors!")
                        write_unhealthy_status()

                start_flow_measurement()

            # Run every iteration (1 second)
            try:
                control_aeration()
            except Exception as e:
                logging.error(f"Aeration control failed: {e}")

            # Environmental monitoring every 30 seconds
            if iteration % 30 == 0:
                try:
                    conditions = read_enclosure_conditions()
                    temps = read_all_temperatures()
                    _last_conditions = conditions
                    _last_temps = temps
                    control_ventilation_fan(conditions, temps)
                    if conditions:
                        check_humidity_alerts(conditions)
                    monitor_temperature_differentials(temps)
                    update_daily_summary(temps, conditions,
                                         smoothed_flow_inlet, smoothed_flow_outlet)
                except Exception as e:
                    logging.error(f"Environmental monitoring failed: {e}")

            # Daily summary check every 60 seconds
            if iteration % 60 == 0:
                try:
                    check_daily_summary()
                except Exception as e:
                    logging.error(f"Daily summary check failed: {e}")

            # Write Prometheus metrics every 10 seconds
            if iteration % 10 == 0:
                write_prometheus_metrics(temps=_last_temps, conditions=_last_conditions)

            # Pump test check every 60 seconds
            if iteration % 60 == 0:
                if should_run_pump_test():
                    try:
                        test_backup_pump()
                    except Exception as e:
                        logging.error(f"Pump test failed: {e}")

            # Save state every 10 minutes
            if time.time() - last_save_time > 600:
                save_state()
                alert_manager.save()
                last_save_time = time.time()

            # Sleep remainder of interval
            elapsed = time.time() - loop_start
            if elapsed < MAIN_LOOP_INTERVAL:
                time.sleep(MAIN_LOOP_INTERVAL - elapsed)

    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        logging.critical(f"Unexpected error in main loop: {e}")
        logging.critical(traceback.format_exc())
    finally:
        save_state()
        alert_manager.save()
        if watchdog:
            watchdog.stop()
        GPIO.cleanup()
        print("Hydroponic monitor stopped")


if __name__ == "__main__":
    main()
