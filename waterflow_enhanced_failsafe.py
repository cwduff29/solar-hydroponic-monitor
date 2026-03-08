#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Hydroponic Water Flow and Equipment Monitor - Enhanced Version
Non-blocking event-driven architecture with reliability improvements

Features:
- Non-blocking flow measurement (background interrupt-driven)
- Dual flow sensor monitoring (inlet/outlet)
- Smart aeration control with battery-based load shedding
- Environmental monitoring (temperature, humidity, pressure)
- Automatic ventilation control
- Email alerts with throttling
- Prometheus metrics export to /ramdisk
- Health monitoring and failure tracking
- Graceful degradation on sensor failures
- Renogy monitor health checking
"""

import RPi.GPIO as GPIO
import time
import os
import json
import glob
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta
import credentials as cr

# ============================================================================
# CONFIGURATION
# ============================================================================

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

# GPIO Pin Assignments
# 
# ============================================================================
# RELAY CONFIGURATION - TERMINAL TYPE & BOARD LOGIC
# ============================================================================
# 
# STEP 1: Configure which terminal each device uses
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAIN_PUMP_TERMINAL = "NC"      # "NC" or "NO"
BACKUP_PUMP_TERMINAL = "NO"    # "NC" or "NO"
AERATION_TERMINAL = "NO"       # "NC" or "NO" ⭐ Changed to NO temporarily
FAN_TERMINAL = "NC"            # "NC" or "NO"

# STEP 2: Configure your relay board type
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RELAY_BOARD_ACTIVE_LOW = True  # True = ACTIVE LOW board (most common)
                                # False = ACTIVE HIGH board (rare)
#
# ACTIVE LOW board:  GPIO LOW  = Relay energized (LED ON)
#                    GPIO HIGH = Relay off (LED OFF)
#
# ACTIVE HIGH board: GPIO HIGH = Relay energized  
#                    GPIO LOW  = Relay off
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#
# UNDERSTANDING THE LOGIC:
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Terminal Type determines when device is ON:
#   NC (Normally Closed): Device ON when relay is DE-ENERGIZED
#   NO (Normally Open):   Device ON when relay is ENERGIZED
#
# Board Type determines GPIO level to energize relay:
#   ACTIVE LOW:  GPIO LOW energizes relay
#   ACTIVE HIGH: GPIO HIGH energizes relay
#
# Examples with ACTIVE LOW board (most common):
#   NC Terminal + Want Device ON  → GPIO HIGH (relay off, NC closes)
#   NC Terminal + Want Device OFF → GPIO LOW  (relay on, NC opens)
#   NO Terminal + Want Device ON  → GPIO LOW  (relay on, NO closes)
#   NO Terminal + Want Device OFF → GPIO HIGH (relay off, NO opens)
#
# FAIL-SAFE BEHAVIOR:
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NC terminals = Device runs if Pi crashes (relay de-energizes)
# NO terminals = Device stops if Pi crashes (relay de-energizes)
#
# Recommended for fail-safe operation:
#   Main Pump:   NC (keeps running if Pi dies)
#   Aeration:    NC (keeps aerating if Pi dies)
#   Fan:         NC (keeps cooling if Pi dies)
#   Backup Pump: NO (doesn't start if Pi dies)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

FLOW_SENSOR_INLET_GPIO = 17
FLOW_SENSOR_OUTLET_GPIO = 27
MAIN_PUMP_RELAY_GPIO = 22
BACKUP_PUMP_RELAY_GPIO = 23
AERATION_PUMP_GPIO = 24
FAN_CONTROL_GPIO = 25

# Flow Sensor Configuration
FLOW_CALIBRATION_FACTOR = 7.5  # Pulses per liter for YF-S201
FLOW_MEASUREMENT_DURATION = 10  # seconds
MIN_FLOW_THRESHOLD = 0.25  # L/min - minimum acceptable flow
FLOW_WARNING_DELAY = 600  # seconds (10 min) before low flow alert
FLOW_IMBALANCE_THRESHOLD = 0.5  # L/min - allows for NFT evaporation (was 0.15)
FLOW_IMBALANCE_DURATION = 600  # seconds (5 min) before leak alert

# Main Loop Timing
MAIN_LOOP_INTERVAL = 1  # seconds (non-blocking architecture)

# Temperature Sensor Paths
DS18B20_BASE_DIR = '/sys/bus/w1/devices/'
DS18B20_DEVICE_PREFIX = '28-'

# DS18B20 Sensor ID Mapping
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Map unique sensor IDs to logical names
# Run identify_ds18b20_sensors.py to discover your sensor IDs
#
# Your sensor configuration:
DS18B20_SENSOR_MAP = {
    '28-000000b18b1c': 'reservoir',   # Main reservoir (buried)
    '28-000000baada8': 'nft drain',   # Water draining from NFT pipes
    '28-000000b26508': 'outdoor',     # Outdoor ambient air
}
#
# Supported sensor names:
#   'reservoir'  - Main water reservoir (used for aeration, freeze warnings)
#   'outdoor'    - Outdoor/ambient air (used for smart fan control, freeze risk)
#   'nft drain' or 'nft_return' - Drain/return water from NFT pipes (thermal tracking)
#   'water'      - Alias for reservoir (backward compatible)
#
# Note: NFT systems normally show "nft drain" > reservoir due to solar
#       heating of exposed PVC pipes. This is NORMAL, not a problem!
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# BME280 I2C Configuration
BME280_I2C_ADDRESS = 0x76

# ============================================================================
# TEMPERATURE MONITORING (3-Sensor NFT System)
# ============================================================================
# With reservoir (buried), outdoor, and nft_return sensors:
# - Track thermal patterns (nft_return typically > reservoir from sun heating)
# - Smart fan control (outdoor vs ENCLOSURE, not reservoir)
# - Freeze risk warnings (outdoor + reservoir temps)
# - NO critical alerts on temperature differential (normal for NFT!)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Temperature Differential Tracking (for metrics/graphing, NOT critical alerts)
# NFT systems NORMALLY show nft_return > reservoir due to solar heating of pipes
TEMP_DIFF_TRACKING_ENABLED = True  # Track in Prometheus for analysis

# Extreme NFT Temperature Alert (optional - for plant stress prevention)
NFT_RETURN_EXTREME_TEMP = 85.0     # °F - Alert if return water this hot (was 90°F, lowered for Vista CA)
ENABLE_NFT_EXTREME_TEMP_ALERT = True  # Enabled for plant protection in Vista CA summer

# Outdoor Temperature Thresholds (for smart fan control)
OUTDOOR_COOLING_DELTA = 5.0        # °F - Outdoor must be this much cooler than ENCLOSURE
OUTDOOR_HEAT_DELTA = 10.0          # °F - Reduce ventilation if outdoor this much hotter than ENCLOSURE

# Freeze Risk Warnings
OUTDOOR_FREEZE_RISK = 35.0         # °F - Alert if outdoor this cold with low reservoir
RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT = 45.0  # °F - Don't alert if reservoir above this

# Battery Data
BATTERY_DATA_FILE = '/ramdisk/Renogy.prom'
BATTERY_DATA_TIMEOUT = 60  # seconds

# Battery Thresholds for Load Shedding
BATTERY_THRESHOLD_DISABLE = 30  # % - Disable aeration completely
BATTERY_THRESHOLD_REDUCE = 50   # % - Reduce aeration duty cycle
BATTERY_THRESHOLD_NORMAL = 70   # % - Normal operation

# Aeration Timing (Normal Mode)
AERATION_ON_DURATION = 360   # 6 minutes
AERATION_OFF_DURATION = 1440 # 24 minutes (20% duty cycle)

# Aeration Timing (Reduced Mode - 40-60% SOC)
AERATION_ON_DURATION_REDUCED = 240   # 4 minutes  
AERATION_OFF_DURATION_REDUCED = 2160 # 36 minutes (10% duty cycle)

# Temperature-based Aeration (°F)
WATER_TEMP_HOT = 78      # >78°F: increase aeration
WATER_TEMP_WARM = 73     # 73-78°F: normal
WATER_TEMP_MODERATE = 68 # 68-73°F: reduce aeration
# <68°F: further reduce

# Fan Control (°F and % RH)
FAN_TEMP_ON = 95
FAN_TEMP_OFF = 85
FAN_TEMP_FORCE_ON = 110
FAN_HUMIDITY_ON = 80   # Raised from 70 for Vista CA coastal climate (normal marine layer)
FAN_HUMIDITY_OFF = 70  # Raised from 60 to match new ON threshold
FAN_MIN_TOGGLE_INTERVAL = 120  # seconds (prevent rapid cycling)

# Humidity Alert Thresholds (% RH)
# Adjusted for Vista CA coastal climate - 70% is normal morning fog
HUMIDITY_WARNING = 80   # Raised from 70 (reduce false alarms from marine layer)
HUMIDITY_CRITICAL = 85  # Raised from 80 (actually unusual for Vista)
HUMIDITY_EMERGENCY = 90

# Pump Testing
PUMP_TEST_INTERVAL = 48 * 3600  # 48 hours
PUMP_TEST_DURATION = 60  # seconds

# Pump Recovery (triggered automatically by sustained low flow)
PUMP_CYCLE_PAUSE = 5        # seconds - off pause between pump cycle
PUMP_RECOVERY_WAIT = 90     # seconds - time to wait after each cycle before checking flow
PUMP_CYCLE_MAX_ATTEMPTS = 2 # main pump cycles to attempt before switching to backup

# File Paths
PROM_OUTPUT_FILE = '/ramdisk/waterflow.prom'
PROM_TEMP_FILE = '/ramdisk/waterflow.prom.tmp'
STATE_FILE = '/ramdisk/waterflow_state.json'  # Use ramdisk instead of /var/lib (no permission issues)
LOG_FILE_PATH = '/var/log/waterflow.log'

# Email Configuration
SMTP_SERVER = cr.server
SENDER_EMAIL = cr.username
RECIPIENT_EMAIL = cr.recipients
EMAIL_PASSWORD = cr.password
SMTP_PORT = cr.port
EMAIL_COOLDOWN_MINUTES = 60
EMAIL_REMINDER_HOURS = 12

# Health Monitoring
MAX_CONSECUTIVE_FAILURES = 5

# Logging (ERROR LEVEL ONLY)
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
    
    Logic:
        1. Determine if relay should be energized:
           - NC terminal: Relay energized when device should be OFF
           - NO terminal: Relay energized when device should be ON
        
        2. Convert to GPIO level based on board type:
           - ACTIVE LOW:  Energize with LOW, de-energize with HIGH
           - ACTIVE HIGH: Energize with HIGH, de-energize with LOW
    """
    # Step 1: Should relay be energized?
    if terminal_type == "NC":
        relay_energized = not device_on  # NC: energize to turn OFF
    else:  # "NO"
        relay_energized = device_on      # NO: energize to turn ON
    
    # Step 2: Convert to GPIO level
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
FLOW_HISTORY_SIZE = 5

# Flow alerts
low_flow_start_time = None
flow_imbalance_start_time = None
low_flow_alert_sent = False
leak_alert_sent = False

# Aeration state (start OFF, let control_aeration() turn it on based on schedule)
aeration_state = False
last_aeration_toggle = time.time()  # Use current time, not 0!
aeration_mode = "normal"

# Fan state (start OFF, let control_ventilation_fan() turn it on based on temperature)
fan_running = False
fan_last_toggle = time.time()  # Use current time, not 0!

# Battery state
battery_soc = 100.0

# Pump testing
last_pump_test = 0

# Pump recovery state
recovery_attempted = False   # True once recovery has been tried for current low flow event
backup_pump_active = False   # True when backup pump is running as failover

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

# Alert state tracking
alert_state = {
    'low_flow': {'last_sent': None, 'first_detected': None, 'active': False},
    'leak_detected': {'last_sent': None, 'first_detected': None, 'active': False},
    'humidity_warning': {'last_sent': None, 'first_detected': None, 'active': False},
    'humidity_critical': {'last_sent': None, 'first_detected': None, 'active': False},
    'load_shed_active': {'last_sent': None, 'first_detected': None, 'active': False},
    'freeze_risk': {'last_sent': None, 'first_detected': None, 'active': False},  # NEW: Freeze risk warnings
    'nft_extreme_temp': {'last_sent': None, 'first_detected': None, 'active': False},  # NEW: Extreme NFT temp
    'backup_pump_failover': {'last_sent': None, 'first_detected': None, 'active': False},
    'pump_recovery_failed': {'last_sent': None, 'first_detected': None, 'active': False},
}

# ============================================================================
# ALERT MANAGEMENT (matching Renogy script)
# ============================================================================

def should_send_alert(alert_type):
    """Determine if alert should be sent based on cooldown
    
    Automatically initializes new alert types if not already present.
    """
    # Initialize alert type if it doesn't exist (defensive programming)
    if alert_type not in alert_state:
        alert_state[alert_type] = {'last_sent': None, 'first_detected': None, 'active': False}
    
    state = alert_state[alert_type]
    now = datetime.now()
    
    if not state['active']:
        state['active'] = True
        state['first_detected'] = now
        return (True, "first_occurrence")
    
    if not state['last_sent']:
        return (True, "never_sent")
    
    time_since_last = now - state['last_sent']
    cooldown = timedelta(minutes=EMAIL_COOLDOWN_MINUTES)
    
    if time_since_last < cooldown:
        return (False, f"cooldown ({(cooldown.total_seconds() - time_since_last.total_seconds()):.0f}s remaining)")
    
    time_since_first = now - state['first_detected']
    reminder_interval = timedelta(hours=EMAIL_REMINDER_HOURS)
    
    if time_since_first >= reminder_interval:
        return (True, f"reminder (problem persisting for {time_since_first.total_seconds()/3600:.1f}h)")
    
    return (True, "cooldown_expired")

def mark_alert_sent(alert_type):
    """Mark that alert was sent
    
    Automatically initializes new alert types if not already present.
    """
    # Initialize alert type if it doesn't exist (defensive programming)
    if alert_type not in alert_state:
        alert_state[alert_type] = {'last_sent': None, 'first_detected': None, 'active': False}
    
    alert_state[alert_type]['last_sent'] = datetime.now()

def clear_alert(alert_type):
    """Clear alert when condition resolves
    
    Automatically initializes new alert types if not already present.
    """
    # Initialize alert type if it doesn't exist (defensive programming)
    if alert_type not in alert_state:
        alert_state[alert_type] = {'last_sent': None, 'first_detected': None, 'active': False}
    
    if alert_state[alert_type]['active']:
        alert_state[alert_type]['active'] = False
        alert_state[alert_type]['first_detected'] = None
        return True
    return False

# ============================================================================
# GPIO SETUP
# ============================================================================

def setup_gpio():
    """Initialize GPIO pins"""
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
    
    # Flow sensors (input with pull-up)
    GPIO.setup(FLOW_SENSOR_INLET_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(FLOW_SENSOR_OUTLET_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    # Relays (output)
    GPIO.setup(MAIN_PUMP_RELAY_GPIO, GPIO.OUT)
    GPIO.setup(BACKUP_PUMP_RELAY_GPIO, GPIO.OUT)
    GPIO.setup(AERATION_PUMP_GPIO, GPIO.OUT)
    GPIO.setup(FAN_CONTROL_GPIO, GPIO.OUT)
    
    # Initialize relays with safe defaults:
    # Start main pump ON, others OFF - let control functions manage them
    set_main_pump(True)      # Main pump ON (always running)
    set_backup_pump(False)   # Backup pump OFF (only during tests)
    set_aeration(False)      # Aeration OFF (control_aeration will turn ON when ready)
    set_fan(False)           # Fan OFF (control_ventilation_fan will turn ON if needed)
    
    logging.info(f"GPIO initialized - Relay config: Main={MAIN_PUMP_TERMINAL}, Backup={BACKUP_PUMP_TERMINAL}, Aeration={AERATION_TERMINAL}, Fan={FAN_TERMINAL}, Board={'ACTIVE_LOW' if RELAY_BOARD_ACTIVE_LOW else 'ACTIVE_HIGH'}")
    
    # Setup interrupt handlers
    GPIO.add_event_detect(FLOW_SENSOR_INLET_GPIO, GPIO.FALLING, callback=countPulse_inlet)
    GPIO.add_event_detect(FLOW_SENSOR_OUTLET_GPIO, GPIO.FALLING, callback=countPulse_outlet)

# ============================================================================
# FLOW SENSOR INTERRUPTS
# ============================================================================

def countPulse_inlet(channel):
    """Interrupt handler for inlet flow sensor"""
    global count_inlet
    if flow_measurement_active:
        count_inlet += 1

def countPulse_outlet(channel):
    """Interrupt handler for outlet flow sensor"""
    global count_outlet
    if flow_measurement_active:
        count_outlet += 1

# ============================================================================
# FLOW MEASUREMENT (Non-blocking)
# ============================================================================

def start_flow_measurement():
    """Start a new flow measurement period"""
    global flow_measurement_active, flow_measurement_start_time, count_inlet, count_outlet
    
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
        flow_measurement_active = False
        return True
    
    return False

def monitor_flow():
    """Process completed flow measurement and check for issues"""
    global smoothed_flow_inlet, smoothed_flow_outlet
    global flow_history_inlet, flow_history_outlet
    global low_flow_start_time, flow_imbalance_start_time
    global low_flow_alert_sent, leak_alert_sent
    global recovery_attempted, backup_pump_active
    global consecutive_failures
    
    try:
        # Calculate flow rates (L/min)
        duration = FLOW_MEASUREMENT_DURATION
        flow_inlet = count_inlet / (FLOW_CALIBRATION_FACTOR * duration)
        flow_outlet = count_outlet / (FLOW_CALIBRATION_FACTOR * duration)
        
        # Update flow history
        flow_history_inlet.append(flow_inlet)
        flow_history_outlet.append(flow_outlet)
        
        if len(flow_history_inlet) > FLOW_HISTORY_SIZE:
            flow_history_inlet.pop(0)
        if len(flow_history_outlet) > FLOW_HISTORY_SIZE:
            flow_history_outlet.pop(0)
        
        # Calculate weighted moving average (more weight to recent)
        weights = [1, 2, 3, 4, 5]
        weights = weights[-len(flow_history_inlet):]
        
        smoothed_flow_inlet = sum(f * w for f, w in zip(flow_history_inlet, weights)) / sum(weights)
        smoothed_flow_outlet = sum(f * w for f, w in zip(flow_history_outlet, weights)) / sum(weights)
        
        # Skip flow alert logic if disabled
        if DISABLE_FLOW_ALERTS:
            logging.debug(f"[FLOW ALERTS DISABLED] Monitoring disabled - Flow: {smoothed_flow_inlet:.3f}/{smoothed_flow_outlet:.3f} L/min")
            consecutive_failures = 0
            sensors_available['flow_inlet'] = True
            sensors_available['flow_outlet'] = True
            return
        
        # Check for low flow
        if smoothed_flow_inlet < MIN_FLOW_THRESHOLD:
            if low_flow_start_time is None:
                low_flow_start_time = time.time()
            elif time.time() - low_flow_start_time > FLOW_WARNING_DELAY:
                # Attempt automatic recovery before sending a low flow alert
                if not recovery_attempted:
                    attempt_pump_recovery()
                elif not low_flow_alert_sent:
                    # Recovery was attempted but flow is still low - alert the operator
                    alert_type = 'low_flow'
                    should_send, reason = should_send_alert(alert_type)
                    if should_send:
                        recovery_note = (
                            "Backup pump is active." if backup_pump_active
                            else f"Main pump was cycled {PUMP_CYCLE_MAX_ATTEMPTS}x and backup pump was tried."
                        )
                        send_email_alert(
                            "⚠️ Low Water Flow Warning",
                            f"Water flow has been low for {FLOW_WARNING_DELAY/60:.0f} minutes.\n"
                            f"Automatic recovery was attempted but flow remains low.\n\n"
                            f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                            f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                            f"Recovery status: {recovery_note}\n"
                            f"Alert reason: {reason}\n"
                            f"Check pump and filters."
                        )
                        mark_alert_sent(alert_type)
                        low_flow_alert_sent = True
        else:
            if low_flow_start_time is not None:
                # Flow has returned to normal
                if backup_pump_active:
                    # Main pump previously failed - stay on backup, just clear the low
                    # flow alert state. Operator must manually inspect main pump and
                    # restart the script to restore normal operation.
                    logging.warning(
                        f"Flow restored on backup pump ({smoothed_flow_inlet:.3f} L/min) "
                        f"- remaining on backup until manual intervention"
                    )
                    if low_flow_alert_sent and clear_alert('low_flow'):
                        send_email_alert(
                            "✓ Flow Restored on Backup Pump",
                            f"Water flow has returned to normal, but the system is still running "
                            f"on the backup pump.\n\n"
                            f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                            f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                            f"The main pump failed to recover automatically and requires manual "
                            f"inspection.\nRestart the script after servicing the main pump to "
                            f"restore normal operation."
                        )
                elif low_flow_alert_sent and clear_alert('low_flow'):
                    send_email_alert(
                        "✓ Water Flow Restored",
                        f"Water flow has returned to normal.\n"
                        f"Current flow: {smoothed_flow_inlet:.3f} L/min"
                    )
                clear_alert('pump_recovery_failed')
                low_flow_start_time = None
                low_flow_alert_sent = False
                recovery_attempted = False
        
        # Check for flow imbalance (leak detection)
        flow_diff = abs(smoothed_flow_inlet - smoothed_flow_outlet)
        
        if flow_diff > FLOW_IMBALANCE_THRESHOLD:
            if flow_imbalance_start_time is None:
                flow_imbalance_start_time = time.time()
            elif time.time() - flow_imbalance_start_time > FLOW_IMBALANCE_DURATION and not leak_alert_sent:
                alert_type = 'leak_detected'
                should_send, reason = should_send_alert(alert_type)
                
                if should_send:
                    send_email_alert(
                        "🚨 Possible Leak Detected",
                        f"Flow imbalance detected for {FLOW_IMBALANCE_DURATION/60:.0f} minutes.\n"
                        f"Inlet flow: {smoothed_flow_inlet:.3f} L/min\n"
                        f"Outlet flow: {smoothed_flow_outlet:.3f} L/min\n"
                        f"Difference: {flow_diff:.3f} L/min\n"
                        f"Threshold: {FLOW_IMBALANCE_THRESHOLD} L/min\n"
                        f"Alert reason: {reason}\n"
                        f"Check system for leaks!"
                    )
                    mark_alert_sent(alert_type)
                    leak_alert_sent = True
        else:
            if flow_imbalance_start_time is not None:
                if leak_alert_sent and clear_alert('leak_detected'):
                    send_email_alert(
                        "✓ Flow Balance Restored",
                        f"Flow rates have returned to normal.\n"
                        f"Inlet: {smoothed_flow_inlet:.3f} L/min\n"
                        f"Outlet: {smoothed_flow_outlet:.3f} L/min"
                    )
                flow_imbalance_start_time = None
                leak_alert_sent = False
        
        # Reset consecutive failures on success
        consecutive_failures = 0
        sensors_available['flow_inlet'] = True
        sensors_available['flow_outlet'] = True
        
    except Exception as e:
        consecutive_failures += 1
        logging.error(f"Flow monitoring failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
        sensors_available['flow_inlet'] = False
        sensors_available['flow_outlet'] = False
        
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            logging.critical("Multiple consecutive flow measurement failures!")

# ============================================================================
# TEMPERATURE SENSORS
# ============================================================================

def discover_temp_sensors():
    """Discover all DS18B20 temperature sensors"""
    try:
        device_folders = glob.glob(DS18B20_BASE_DIR + DS18B20_DEVICE_PREFIX + '*')
        return [folder + '/w1_slave' for folder in device_folders]
    except Exception as e:
        logging.error(f"Failed to discover temperature sensors: {e}")
        return []

def read_temp_sensor(device_file):
    """Read temperature from DS18B20 sensor"""
    try:
        with open(device_file, 'r') as f:
            lines = f.readlines()
        
        if lines[0].strip()[-3:] != 'YES':
            return None
        
        equals_pos = lines[1].find('t=')
        if equals_pos != -1:
            temp_c = float(lines[1][equals_pos+2:]) / 1000.0
            temp_f = temp_c * 9.0 / 5.0 + 32.0
            return temp_f
    except Exception as e:
        logging.error(f"Failed to read temperature sensor {device_file}: {e}")
    
    return None

def read_all_temperatures():
    """Read all temperature sensors using ID mapping if available"""
    sensors = discover_temp_sensors()
    temps = {}
    
    if DS18B20_SENSOR_MAP:
        # Use sensor ID mapping (RECOMMENDED)
        for sensor_path in sensors:
            # Extract sensor ID from path (e.g., '28-000000b18b1c')
            sensor_id = sensor_path.split('/')[-2]
            
            # Check if this sensor is mapped
            if sensor_id in DS18B20_SENSOR_MAP:
                logical_name = DS18B20_SENSOR_MAP[sensor_id]
                temp = read_temp_sensor(sensor_path)
                
                if temp is not None:
                    temps[logical_name] = temp
                    
                    # Update sensor availability and create aliases
                    if logical_name == 'reservoir':
                        temps['water'] = temp  # Alias for backward compatibility
                        sensors_available['water_temp'] = True
                    elif logical_name == 'water':
                        temps['reservoir'] = temp  # Alias
                        sensors_available['water_temp'] = True
                    elif logical_name == 'enclosure':
                        sensors_available['enclosure_temp'] = True
    else:
        # Fall back to discovery order (NOT RECOMMENDED - order can change!)
        logging.warning("DS18B20_SENSOR_MAP not configured - using discovery order (unreliable!)")
        
        for i, sensor in enumerate(sensors):
            temp = read_temp_sensor(sensor)
            if temp is not None:
                if i == 0:
                    temps['water'] = temp
                    temps['reservoir'] = temp  # Alias
                    sensors_available['water_temp'] = True
                elif i == 1:
                    temps['enclosure'] = temp
                    sensors_available['enclosure_temp'] = True
    
    # Update availability flags
    if 'water' not in temps and 'reservoir' not in temps:
        sensors_available['water_temp'] = False
    if 'enclosure' not in temps:
        sensors_available['enclosure_temp'] = False
    
    return temps

# ============================================================================
# BME280 ENVIRONMENTAL SENSOR
# ============================================================================

def read_enclosure_conditions():
    """Read BME280 sensor data"""
    try:
        import smbus2
        import bme280
        
        # Use context manager to ensure I2C bus is always closed
        with smbus2.SMBus(1) as bus:
            calibration_params = bme280.load_calibration_params(bus, BME280_I2C_ADDRESS)
            data = bme280.sample(bus, BME280_I2C_ADDRESS, calibration_params)
            
            temp_f = data.temperature * 9.0 / 5.0 + 32.0
            humidity = data.humidity
            pressure = data.pressure
            
            # Calculate dew point
            a = 17.27
            b = 237.7
            alpha = ((a * data.temperature) / (b + data.temperature)) + (humidity / 100.0)
            dew_point_c = (b * alpha) / (a - alpha)
            dew_point_f = dew_point_c * 9.0 / 5.0 + 32.0
            
            sensors_available['bme280'] = True
            
            return {
                'temp_f': temp_f,
                'humidity': humidity,
                'pressure': pressure,
                'dewpoint_f': dew_point_f
            }
    except Exception as e:
        logging.error(f"BME280 read failed: {e}")
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
                # Check Renogy monitor health
                if 'renogy_monitor_healthy' in line:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            renogy_monitor_healthy = float(parts[-1]) == 1.0
                            renogy_healthy_found = True
                        except ValueError:
                            pass
                
                # Get battery SOC (be specific - don't match renogy_alert_low_battery_soc!)
                if line.strip().startswith('battery_soc{'):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            battery_soc = float(parts[-1])
                            soc_found = True
                        except ValueError:
                            continue
        
        # Warn if Renogy monitor is unhealthy
        if renogy_healthy_found and not renogy_monitor_healthy:
            logging.warning("Renogy monitor reports unhealthy status - battery data may be unreliable")
        
        return battery_soc if soc_found else None
        
    except Exception as e:
        logging.error(f"Failed to read battery SOC: {e}")
        return None

# ============================================================================
# AERATION CONTROL
# ============================================================================

def get_aeration_by_temperature(water_temp_f):
    """Get aeration timing based on water temperature"""
    if water_temp_f > WATER_TEMP_HOT:
        # Hot water (>78°F): 23.3% duty cycle
        return 420, 1380
    elif water_temp_f > WATER_TEMP_WARM:
        # Warm water (73-78°F): 20% duty cycle
        return 360, 1440
    elif water_temp_f > WATER_TEMP_MODERATE:
        # Moderate water (68-73°F): 14.3% duty cycle
        return 300, 1800
    else:
        # Cool water (<68°F): 10% duty cycle
        return 240, 2160

def control_aeration():
    """Control aeration pump with battery-based load shedding"""
    global aeration_state, last_aeration_toggle, aeration_mode
    
    # If aerator disabled, turn it off
    if DISABLE_AERATOR:
        set_aeration(False)
        if aeration_state:
            logging.info("[AERATOR DISABLED] Aerator turned off")
        aeration_state = False
        aeration_mode = "disabled-manual"
        return
    
    soc = read_battery_soc()
    temps = read_all_temperatures()
    
    # Determine mode based on battery SOC
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
    
    # Temperature-based adjustment (if water temp available)
    if sensors_available['water_temp'] and 'water' in temps:
        on_duration, off_duration = get_aeration_by_temperature(temps['water'])
        mode = f"{mode}-{temps['water']:.0f}F"
    
    # Alert if load shedding active
    if mode.startswith("disabled") or mode.startswith("reduced"):
        alert_type = 'load_shed_active'
        should_send, reason = should_send_alert(alert_type)
        
        if should_send:
            send_email_alert(
                "⚠️ Aeration Load Shedding Active",
                f"Aeration reduced due to low battery.\n"
                f"Battery SOC: {soc}%\n"
                f"Mode: {mode}\n"
                f"Alert reason: {reason}"
            )
            mark_alert_sent(alert_type)
    else:
        clear_alert('load_shed_active')
    
    # Toggle logic
    current_time = time.time()
    time_in_state = current_time - last_aeration_toggle
    
    if aeration_state:
        # Currently ON
        if time_in_state >= on_duration:
            set_aeration(False)
            aeration_state = False
            last_aeration_toggle = current_time
    else:
        # Currently OFF
        if time_in_state >= off_duration:
            if on_duration > 0:  # Don't turn on if disabled
                set_aeration(True)
                aeration_state = True
                last_aeration_toggle = current_time
    
    aeration_mode = mode

# ============================================================================
# FAN CONTROL
# ============================================================================

def control_ventilation_fan(conditions, temps):
    """Control ventilation fan based on temperature and humidity
    
    Enhanced with outdoor temperature awareness for smart enclosure cooling
    
    Note: Fan cools the ENCLOSURE (electronics on post), NOT the reservoir
          Enclosure is exposed to outdoor air, reservoir is buried
    """
    global fan_running, fan_last_toggle
    
    # If fan disabled, turn it off
    if DISABLE_FAN:
        set_fan(False)
        if fan_running:
            logging.info("[FAN DISABLED] Ventilation fan turned off")
        fan_running = False
        return
    
    # Try to get ENCLOSURE temperature from available sensors
    enclosure_temp_f = None
    humidity = None
    
    # Priority 1: BME280 (inside enclosure - this is what fan cools!)
    if sensors_available['bme280'] and conditions and 'temp_f' in conditions:
        enclosure_temp_f = conditions['temp_f']
        humidity = conditions.get('humidity')
    # Priority 2: DS18B20 enclosure sensor (if no BME280)
    elif sensors_available['enclosure_temp'] and 'enclosure' in temps:
        enclosure_temp_f = temps['enclosure']
    
    # Can't control fan without enclosure temperature
    if enclosure_temp_f is None:
        if fan_running:  # Turn off fan if we can't monitor
            set_fan(False)
            fan_running = False
        return
    
    # Prevent rapid cycling
    if time.time() - fan_last_toggle < FAN_MIN_TOGGLE_INTERVAL:
        return
    
    # Fan control logic (based on ENCLOSURE temp)
    should_run = False
    
    # Emergency: Force on at extreme temperature
    if enclosure_temp_f >= FAN_TEMP_FORCE_ON:
        should_run = True
    # Temperature-based with hysteresis
    elif enclosure_temp_f >= FAN_TEMP_ON:
        should_run = True
    elif enclosure_temp_f <= FAN_TEMP_OFF:
        should_run = False
    else:
        should_run = fan_running  # Maintain current state in hysteresis zone
    
    # Humidity override (if available)
    if humidity is not None:
        if humidity >= FAN_HUMIDITY_ON:
            should_run = True
        elif humidity <= FAN_HUMIDITY_OFF and enclosure_temp_f < FAN_TEMP_ON:
            should_run = False
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # SMART COOLING: Use outdoor temperature vs ENCLOSURE (not reservoir!)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Enclosure is on post (exposed to outdoor air)
    # Reservoir is buried (different thermal environment)
    # Fan cools ENCLOSURE, so compare outdoor to ENCLOSURE temp!
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if 'outdoor' in temps:
        outdoor = temps['outdoor']
        
        # Free cooling opportunity: outdoor is significantly cooler than ENCLOSURE
        if outdoor < enclosure_temp_f - OUTDOOR_COOLING_DELTA and enclosure_temp_f > 70.0:
            # Maximize ventilation for free cooling of electronics!
            should_run = True
            if not fan_running:
                logging.info(f"Smart enclosure cooling: outdoor {outdoor:.1f}°F < enclosure {enclosure_temp_f:.1f}°F")
        
        # Prevent heat gain: outdoor is significantly hotter than ENCLOSURE
        elif outdoor > enclosure_temp_f + OUTDOOR_HEAT_DELTA and enclosure_temp_f < 80.0:
            # Reduce ventilation to prevent heating electronics (unless critically hot inside)
            if enclosure_temp_f < FAN_TEMP_FORCE_ON:
                should_run = False
                if fan_running:
                    logging.info(f"Reducing ventilation: outdoor {outdoor:.1f}°F > enclosure {enclosure_temp_f:.1f}°F")
    
    # Apply state change
    if should_run != fan_running:
        set_fan(should_run)
        fan_running = should_run
        fan_last_toggle = time.time()

# ============================================================================
# TEMPERATURE MONITORING (NFT System)
# ============================================================================

def monitor_temperature_differentials(temps):
    """Monitor temperature relationships for NFT hydroponic system
    
    NFT System Characteristics:
    - Water flows through exposed PVC pipes
    - Solar heating causes nft_return > reservoir (NORMAL!)
    - Reservoir is partially buried (thermally buffered)
    - Enclosure is on post (exposed to outdoor air)
    
    This function:
    ✓ Tracks thermal patterns (metrics only, no critical alerts on differential)
    ✓ Warns on freeze risks
    ✓ Optional: Warns on extreme NFT temps (plant stress)
    ✗ Does NOT alert on heat buildup (normal for NFT)
    ✗ Does NOT verify flow (you have actual flow sensors)
    """
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 1. FREEZE RISK WARNING (Useful for NFT systems)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if 'outdoor' in temps and 'reservoir' in temps:
        outdoor = temps['outdoor']
        reservoir = temps['reservoir']
        
        if outdoor < OUTDOOR_FREEZE_RISK and reservoir < RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT:
            alert_type = 'freeze_risk'
            should_send, reason = should_send_alert(alert_type)
            if should_send:
                send_email_alert(
                    "❄️ Freeze Risk Warning",
                    f"Cold outdoor temperature with low reservoir temperature!\n"
                    f"\n"
                    f"Temperature Readings:\n"
                    f"  Outdoor:    {outdoor:.1f}°F (Freeze risk: <{OUTDOOR_FREEZE_RISK}°F)\n"
                    f"  Reservoir:  {reservoir:.1f}°F (Target: >{RESERVOIR_TEMP_MIN_FOR_FREEZE_ALERT}°F)\n"
                    f"\n"
                    f"Freeze Risks:\n"
                    f"  • Exposed NFT pipes may freeze\n"
                    f"  • Reservoir heat loss accelerating\n"
                    f"  • Plant stress from cold\n"
                    f"  • Pump may be affected\n"
                    f"\n"
                    f"Recommended Actions:\n"
                    f"  • Monitor temperatures closely\n"
                    f"  • Consider adding heat source to reservoir\n"
                    f"  • Insulate exposed NFT pipes if possible\n"
                    f"  • Protect pump from freezing\n"
                    f"  • Ensure water circulation continues\n"
                    f"\n"
                    f"Note: Ground burial provides some insulation but extended\n"
                    f"      cold periods can still cause freezing.\n"
                    f"\n"
                    f"Alert reason: {reason}"
                )
                mark_alert_sent(alert_type)
        else:
            clear_alert('freeze_risk')
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 2. EXTREME NFT TEMPERATURE WARNING (Optional - Plant Stress)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Check for NFT sensor (supports multiple naming conventions)
    nft_temp_key = None
    for key in ['nft drain', 'nft_drain', 'nft_return']:
        if key in temps:
            nft_temp_key = key
            break
    
    if ENABLE_NFT_EXTREME_TEMP_ALERT and nft_temp_key:
        nft_return = temps[nft_temp_key]
        
        if nft_return > NFT_RETURN_EXTREME_TEMP:
            alert_type = 'nft_extreme_temp'
            should_send, reason = should_send_alert(alert_type)
            if should_send:
                reservoir = temps.get('reservoir', 'N/A')
                send_email_alert(
                    "🌡️ Extreme NFT Temperature Warning",
                    f"NFT drain/return water temperature is very high!\n"
                    f"\n"
                    f"Temperature Readings:\n"
                    f"  NFT Drain:   {nft_return:.1f}°F (Limit: <{NFT_RETURN_EXTREME_TEMP}°F)\n"
                    f"  Reservoir:   {reservoir if isinstance(reservoir, str) else f'{reservoir:.1f}°F'}\n"
                    f"\n"
                    f"Plant Stress Risks:\n"
                    f"  • Root zone too warm (reduces oxygen)\n"
                    f"  • Increased pathogen growth risk\n"
                    f"  • Nutrient uptake affected\n"
                    f"  • Plant wilting possible\n"
                    f"\n"
                    f"Recommended Actions:\n"
                    f"  • Add shade to NFT pipes\n"
                    f"  • Increase water flow rate\n"
                    f"  • Consider chilling reservoir\n"
                    f"  • Check for adequate aeration\n"
                    f"  • Monitor plants for stress signs\n"
                    f"\n"
                    f"Alert reason: {reason}"
                )
                mark_alert_sent(alert_type)
        else:
            clear_alert('nft_extreme_temp')
    
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 3. INFORMATIONAL LOGGING (No alerts, just data)
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    if TEMP_DIFF_TRACKING_ENABLED and 'reservoir' in temps:
        # Check for NFT sensor (supports multiple naming conventions)
        nft_temp_key = None
        for key in ['nft drain', 'nft_drain', 'nft_return']:
            if key in temps:
                nft_temp_key = key
                break
        
        if nft_temp_key:
            differential = temps[nft_temp_key] - temps['reservoir']
            # Just log, don't alert - this differential is normal for NFT!
            logging.debug(f"NFT thermal load: {nft_temp_key} {temps[nft_temp_key]:.1f}°F - reservoir {temps['reservoir']:.1f}°F = {differential:+.1f}°F")

# ============================================================================
# HUMIDITY ALERTS
# ============================================================================

def check_humidity_alerts(conditions):
    """Check humidity levels and send alerts"""
    if conditions is None or 'humidity' not in conditions:
        return
    
    humidity = conditions['humidity']
    
    # Critical humidity
    if humidity >= HUMIDITY_EMERGENCY:
        alert_type = 'humidity_critical'
        should_send, reason = should_send_alert(alert_type)
        
        if should_send:
            send_email_alert(
                "🚨 EMERGENCY: Critical Humidity Level",
                f"Humidity has reached emergency levels!\n"
                f"Current: {humidity:.1f}%\n"
                f"Threshold: {HUMIDITY_EMERGENCY}%\n"
                f"Dew point: {conditions.get('dewpoint_f', 'N/A')}°F\n"
                f"Alert reason: {reason}\n"
                f"High risk of condensation damage!"
            )
            mark_alert_sent(alert_type)
    elif humidity >= HUMIDITY_WARNING:
        alert_type = 'humidity_warning'
        should_send, reason = should_send_alert(alert_type)
        
        if should_send:
            send_email_alert(
                "⚠️ High Humidity Warning",
                f"Humidity is elevated.\n"
                f"Current: {humidity:.1f}%\n"
                f"Threshold: {HUMIDITY_WARNING}%\n"
                f"Dew point: {conditions.get('dewpoint_f', 'N/A')}°F\n"
                f"Alert reason: {reason}\n"
                f"Monitor for condensation."
            )
            mark_alert_sent(alert_type)
    else:
        # Clear alerts
        if clear_alert('humidity_critical'):
            send_email_alert(
                "✓ Humidity Normalized",
                f"Humidity has returned to safe levels.\n"
                f"Current: {humidity:.1f}%"
            )
        if clear_alert('humidity_warning'):
            send_email_alert(
                "✓ Humidity Normal",
                f"Humidity has decreased.\n"
                f"Current: {humidity:.1f}%"
            )

# ============================================================================
# PUMP TESTING
# ============================================================================

def should_run_pump_test():
    """Check if backup pump test is due"""
    global last_pump_test
    
    # Skip pump tests if disabled
    if DISABLE_PUMP_TESTING:
        return False
    
    if last_pump_test == 0:
        return False  # Will be set from loaded state
    
    return time.time() - last_pump_test > PUMP_TEST_INTERVAL

def test_backup_pump():
    """Test backup pump functionality, or recheck main pump if currently in failover.

    Normal mode (backup_pump_active=False):
        Switches briefly to backup pump, checks flow, returns to main.

    Failover mode (backup_pump_active=True):
        The 48-hour interval is used to recheck the main pump instead.
        If main pump now has flow, switch back and clear the failover state.
        If main still fails, return to backup and report it.
    """
    global last_pump_test, backup_pump_active

    if backup_pump_active:
        # ── Failover recheck: try main pump ──────────────────────────────
        logging.info("Pump test: in failover mode - rechecking main pump")

        set_backup_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_main_pump(True)

        time.sleep(PUMP_TEST_DURATION)
        test_flow = smoothed_flow_inlet

        if test_flow >= MIN_FLOW_THRESHOLD:
            # Main pump has recovered - switch back
            backup_pump_active = False
            clear_alert('backup_pump_failover')
            clear_alert('pump_recovery_failed')
            logging.warning(
                f"Pump test: main pump recovered ({test_flow:.3f} L/min) - "
                f"switched back from backup"
            )
            send_email_alert(
                "✓ Main Pump Recovered - Failover Cleared",
                f"The scheduled pump test found the main pump is working again.\n"
                f"System has switched back to the main pump.\n\n"
                f"Flow during test: {test_flow:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"No further action required. Next pump test in "
                f"{PUMP_TEST_INTERVAL/3600:.0f} hours."
            )
        else:
            # Main pump still failing - return to backup
            set_main_pump(False)
            time.sleep(PUMP_CYCLE_PAUSE)
            set_backup_pump(True)
            logging.warning(
                f"Pump test: main pump still failing ({test_flow:.3f} L/min) - "
                f"returned to backup"
            )
            send_email_alert(
                "✗ Main Pump Still Failing - Remaining on Backup",
                f"The scheduled pump recheck found the main pump is still not working.\n"
                f"System has returned to the backup pump.\n\n"
                f"Flow during test: {test_flow:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Main pump requires manual inspection.\n"
                f"Next recheck in {PUMP_TEST_INTERVAL/3600:.0f} hours."
            )

    else:
        # ── Normal mode: test backup pump ────────────────────────────────
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
            status = "✓"
        else:
            result = "FAILED"
            status = "✗"

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
      2. If flow is still low after all cycles, switch to backup pump
         and wait PUMP_RECOVERY_WAIT seconds.
      3. Send an appropriate alert regardless of outcome so the operator
         knows what the system did.

    Sets backup_pump_active=True if failover occurred so monitor_flow()
    knows to restore the main pump once flow returns to normal.
    """
    global recovery_attempted, backup_pump_active

    recovery_attempted = True
    logging.warning("Low flow recovery initiated - cycling main pump")

    # ── Step 1: Main pump cycles ──────────────────────────────────────────
    for attempt in range(1, PUMP_CYCLE_MAX_ATTEMPTS + 1):
        logging.warning(f"Pump recovery: main pump cycle {attempt}/{PUMP_CYCLE_MAX_ATTEMPTS}")

        set_main_pump(False)
        time.sleep(PUMP_CYCLE_PAUSE)
        set_main_pump(True)

        # Wait for flow to stabilise, then check
        time.sleep(PUMP_RECOVERY_WAIT)

        if smoothed_flow_inlet >= MIN_FLOW_THRESHOLD:
            logging.warning(
                f"Pump recovery: flow restored after cycle {attempt} "
                f"({smoothed_flow_inlet:.3f} L/min)"
            )
            send_email_alert(
                "✓ Water Flow Restored - Main Pump Cycled",
                f"Low flow was resolved by cycling the main pump.\n\n"
                f"Cycles attempted: {attempt}\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return  # Flow restored — nothing else to do

    # ── Step 2: Failover to backup pump ──────────────────────────────────
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
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            send_email_alert(
                "⚠️ Running on Backup Pump - Main Pump Failed",
                f"Main pump failed to restore flow after {PUMP_CYCLE_MAX_ATTEMPTS} cycles.\n"
                f"System has switched to the backup pump.\n\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Action required: inspect main pump and filters.\n"
                f"The main pump will be restored automatically once flow is confirmed stable."
            )
            mark_alert_sent(alert_type)
    else:
        # Both pumps failed
        logging.error(
            f"Pump recovery: backup pump also failed ({smoothed_flow_inlet:.3f} L/min) "
            f"- manual intervention required"
        )
        alert_type = 'pump_recovery_failed'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            send_email_alert(
                "🚨 CRITICAL: Both Pumps Failed - Manual Intervention Required",
                f"Automatic recovery failed. Neither main nor backup pump restored flow.\n\n"
                f"Current flow: {smoothed_flow_inlet:.3f} L/min\n"
                f"Threshold: {MIN_FLOW_THRESHOLD} L/min\n"
                f"Main pump cycles attempted: {PUMP_CYCLE_MAX_ATTEMPTS}\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Immediate manual inspection required!\n"
                f"Check: power supply, tubing, reservoir level, blockages."
            )
            mark_alert_sent(alert_type)

def send_email_alert(subject, content):
    """Send email alert"""
    # Skip emails if disabled
    if DISABLE_EMAILS:
        logging.info(f"[EMAILS DISABLED] Email suppressed: '{subject}'")
        return False
    
    try:
        msg = EmailMessage()
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECIPIENT_EMAIL
        msg['Subject'] = subject
        msg.set_content(content)
        
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, EMAIL_PASSWORD)
            server.send_message(msg)
        
        return True
    except Exception as e:
        logging.error(f"Failed to send email '{subject}': {e}")
        return False

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
                last_pump_test = state.get('last_pump_test', time.time())
                backup_pump_active = state.get('backup_pump_active', False)
                if backup_pump_active:
                    # Reboot while on backup pump - keep backup running, main stays off
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
    """Save persistent state to file"""
    try:
        state = {
            'last_pump_test': last_pump_test,
            'backup_pump_active': backup_pump_active,
            'last_save': time.time()
        }
        
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logging.error(f"Failed to save state: {e}")

# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

def write_prometheus_metrics():
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
            
            # Raw flow (most recent measurement)
            if len(flow_history_inlet) > 0:
                f.write("# HELP waterflow_inlet_raw_lpm Raw inlet water flow\n")
                f.write("# TYPE waterflow_inlet_raw_lpm gauge\n")
                f.write(f"waterflow_inlet_raw_lpm{{source=\"waterflow\"}} {flow_history_inlet[-1]:.3f}\n")
            
            if len(flow_history_outlet) > 0:
                f.write("# HELP waterflow_outlet_raw_lpm Raw outlet water flow\n")
                f.write("# TYPE waterflow_outlet_raw_lpm gauge\n")
                f.write(f"waterflow_outlet_raw_lpm{{source=\"waterflow\"}} {flow_history_outlet[-1]:.3f}\n")
            
            # Flow imbalance
            flow_diff = abs(smoothed_flow_inlet - smoothed_flow_outlet)
            f.write("# HELP waterflow_imbalance_lpm Flow difference between inlet and outlet\n")
            f.write("# TYPE waterflow_imbalance_lpm gauge\n")
            f.write(f"waterflow_imbalance_lpm{{source=\"waterflow\"}} {flow_diff:.3f}\n")
            
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
            f.write("# HELP waterflow_emails_disabled Email alerts disabled (1=disabled, 0=enabled)\n")
            f.write("# TYPE waterflow_emails_disabled gauge\n")
            f.write(f"waterflow_emails_disabled{{source=\"waterflow\"}} {1 if DISABLE_EMAILS else 0}\n")
            
            f.write("# HELP waterflow_pump_testing_disabled Pump testing disabled (1=disabled, 0=enabled)\n")
            f.write("# TYPE waterflow_pump_testing_disabled gauge\n")
            f.write(f"waterflow_pump_testing_disabled{{source=\"waterflow\"}} {1 if DISABLE_PUMP_TESTING else 0}\n")
            
            f.write("# HELP waterflow_flow_alerts_disabled Flow alerts disabled (1=disabled, 0=enabled)\n")
            f.write("# TYPE waterflow_flow_alerts_disabled gauge\n")
            f.write(f"waterflow_flow_alerts_disabled{{source=\"waterflow\"}} {1 if DISABLE_FLOW_ALERTS else 0}\n")
            
            f.write("# HELP waterflow_aerator_disabled Aerator disabled (1=disabled, 0=enabled)\n")
            f.write("# TYPE waterflow_aerator_disabled gauge\n")
            f.write(f"waterflow_aerator_disabled{{source=\"waterflow\"}} {1 if DISABLE_AERATOR else 0}\n")
            
            f.write("# HELP waterflow_fan_disabled Fan disabled (1=disabled, 0=enabled)\n")
            f.write("# TYPE waterflow_fan_disabled gauge\n")
            f.write(f"waterflow_fan_disabled{{source=\"waterflow\"}} {1 if DISABLE_FAN else 0}\n")
            
            # Aeration
            f.write("# HELP aeration_state Aeration pump state\n")
            f.write("# TYPE aeration_state gauge\n")
            f.write(f"aeration_state{{source=\"waterflow\"}} {1 if aeration_state else 0}\n")
            
            f.write("# HELP aeration_mode Aeration mode\n")
            f.write("# TYPE aeration_mode gauge\n")
            f.write(f'aeration_mode{{mode=\"{aeration_mode}\",source=\"waterflow\"}} 1\n')
            
            # Fan
            f.write("# HELP fan_state Ventilation fan state\n")
            f.write("# TYPE fan_state gauge\n")
            f.write(f"fan_state{{source=\"waterflow\"}} {1 if fan_running else 0}\n")
            
            # Temperatures - Individual sensors
            temps = read_all_temperatures()
            
            # Temperature metrics - write HELP/TYPE once, then all sensor values
            f.write("# HELP waterflow_temperature_fahrenheit Temperature reading\n")
            f.write("# TYPE waterflow_temperature_fahrenheit gauge\n")
            for sensor_name in ['water', 'reservoir', 'nft drain', 'nft_drain', 'nft_return', 'outdoor', 'enclosure']:
                if sensor_name in temps:
                    # Replace spaces with underscores for Prometheus metric names
                    metric_name = sensor_name.replace(' ', '_')
                    f.write(f"waterflow_temperature_fahrenheit{{sensor=\"{metric_name}\",source=\"waterflow\"}} {temps[sensor_name]:.1f}\n")
            
            # Temperature differentials (for tracking/graphing, NOT critical alerts)
            # NFT systems normally show nft_drain > reservoir due to solar heating
            nft_temp_key = None
            for key in ['nft drain', 'nft_drain', 'nft_return']:
                if key in temps:
                    nft_temp_key = key
                    break
            
            if nft_temp_key and 'reservoir' in temps:
                # NFT thermal load (nft_drain typically warmer from sun heating pipes)
                temp_diff_nft = temps[nft_temp_key] - temps['reservoir']
                f.write("# HELP waterflow_temperature_differential_fahrenheit Temperature differential (tracking only)\n")
                f.write("# TYPE waterflow_temperature_differential_fahrenheit gauge\n")
                f.write(f"waterflow_temperature_differential_fahrenheit{{diff_type=\"nft_solar_heating\",source=\"waterflow\"}} {temp_diff_nft:.2f}\n")
            
            if 'outdoor' in temps and 'reservoir' in temps:
                # Outdoor vs reservoir (ground buffering analysis)
                temp_diff_outdoor = temps['outdoor'] - temps['reservoir']
                f.write(f"waterflow_temperature_differential_fahrenheit{{diff_type=\"outdoor_vs_reservoir\",source=\"waterflow\"}} {temp_diff_outdoor:.2f}\n")
            
            # Environmental conditions
            conditions = read_enclosure_conditions()
            if conditions:
                # BME280 temperature is available - set flag
                if 'temp_f' in conditions:
                    sensors_available['enclosure_temp'] = True
                
                f.write("# HELP enclosure_temperature_fahrenheit Enclosure air temperature from BME280\n")
                f.write("# TYPE enclosure_temperature_fahrenheit gauge\n")
                f.write(f"enclosure_temperature_fahrenheit{{source=\"waterflow\"}} {conditions['temp_f']:.1f}\n")
                
                f.write("# HELP enclosure_humidity_percent Relative humidity\n")
                f.write("# TYPE enclosure_humidity_percent gauge\n")
                f.write(f"enclosure_humidity_percent{{source=\"waterflow\"}} {conditions['humidity']:.1f}\n")
                
                f.write("# HELP enclosure_pressure_hpa Barometric pressure\n")
                f.write("# TYPE enclosure_pressure_hpa gauge\n")
                f.write(f"enclosure_pressure_hpa{{source=\"waterflow\"}} {conditions['pressure']:.1f}\n")
                
                f.write("# HELP enclosure_dewpoint_f Dew point temperature\n")
                f.write("# TYPE enclosure_dewpoint_f gauge\n")
                f.write(f"enclosure_dewpoint_f{{source=\"waterflow\"}} {conditions['dewpoint_f']:.1f}\n")
            else:
                # BME280 failed - enclosure temp not available
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
            f.write(f"renogy_monitor_healthy_from_waterflow{{source=\"waterflow\"}} {1 if renogy_monitor_healthy else 0}\n")
            
            # Sensor availability
            for sensor_name, available in sensors_available.items():
                f.write(f"# HELP waterflow_sensor_available_{sensor_name} Sensor availability\n")
                f.write(f"# TYPE waterflow_sensor_available_{sensor_name} gauge\n")
                f.write(f"waterflow_sensor_available_{sensor_name}{{source=\"waterflow\"}} {1 if available else 0}\n")
            
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
    
    # Show disabled subsystems
    disabled_systems = []
    if DISABLE_EMAILS:
        disabled_systems.append("EMAILS")
    if DISABLE_PUMP_TESTING:
        disabled_systems.append("PUMP TESTING")
    if DISABLE_FLOW_ALERTS:
        disabled_systems.append("FLOW ALERTS")
    if DISABLE_AERATOR:
        disabled_systems.append("AERATOR")
    if DISABLE_FAN:
        disabled_systems.append("FAN")
    
    if disabled_systems:
        print()
        print("🔧" * 35)
        print("   ⚠️  MAINTENANCE MODE ACTIVE ⚠️")
        print(f"   DISABLED: {', '.join(disabled_systems)}")
        print("🔧" * 35)
    
    print("=" * 70)
    
    setup_gpio()
    load_state()
    
    sensors = discover_temp_sensors()
    print(f"Temperature sensors: {len(sensors)}")
    print(f"Initial battery SOC: {battery_soc:.1f}%")
    print()
    
    start_flow_measurement()
    
    iteration = 0
    last_save_time = time.time()
    
    try:
        while True:
            iteration += 1
            loop_start = time.time()
            
            # Check if flow measurement complete
            if check_flow_measurement():
                try:
                    monitor_flow()
                    consecutive_failures = 0  # Reset on success
                except Exception as e:
                    consecutive_failures += 1
                    logging.error(f"Flow monitoring failed ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
                    
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
                    control_ventilation_fan(conditions, temps)
                    if conditions:
                        check_humidity_alerts(conditions)
                    # Temperature differential monitoring (requires 3 sensors)
                    monitor_temperature_differentials(temps)
                except Exception as e:
                    logging.error(f"Environmental monitoring failed: {e}")
            
            # Write Prometheus metrics every 10 seconds
            if iteration % 10 == 0:
                write_prometheus_metrics()
            
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
                last_save_time = time.time()
            
            # Sleep remainder of interval
            elapsed = time.time() - loop_start
            if elapsed < MAIN_LOOP_INTERVAL:
                time.sleep(MAIN_LOOP_INTERVAL - elapsed)
    
    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        logging.critical(f"Unexpected error in main loop: {e}")
        import traceback
        logging.critical(traceback.format_exc())
    finally:
        save_state()
        GPIO.cleanup()
        print("Hydroponic monitor stopped")

if __name__ == "__main__":
    main()
