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
- Alert state tracking
- Prometheus metrics export to /ramdisk
"""

import time
import logging
import smtplib
import os
import json
import traceback
from datetime import datetime, timedelta
from email.message import EmailMessage
from renogy_extended import RenogyRoverExtended
import credentials as cr

# ============================================================================
# CONFIGURATION
# ============================================================================

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
DEV_NAME = '/dev/serial0'
SLEEP_TIME = 10

# Battery Configuration
BATTERY_CAPACITY_AH = 50  # 1x 50Ah LiFePo4
BATTERY_NOMINAL_VOLTAGE = 12.8  # 12V system

# Charging state codes (for Prometheus export as numeric values)
# 0 = deactivated, 1 = activated, 2 = mppt, 3 = equalizing
# 4 = boost, 5 = floating, 6 = current_limiting, -1 = unknown

# File paths (all in ramdisk to minimize SD writes)
TEMP_FILE_PATH = '/ramdisk/Renogy.prom.tmp'
FINAL_FILE_PATH = '/ramdisk/Renogy.prom'
LOG_FILE_PATH = '/var/log/renogy.log'  # Error-only log
STATE_FILE_PATH = '/ramdisk/renogy_alerts.json'  # Alert state in RAM

# Persistent state file (only for critical data, written rarely)
PERSISTENT_STATE_FILE = '/ramdisk/renogy_state.json'  # Use ramdisk instead of /var/lib (no permission issues)

# Critical Thresholds
LOW_BATTERY_VOLTAGE_THRESHOLD = 12.8
HIGH_CONTROLLER_TEMPERATURE_THRESHOLD = 45.0
HIGH_BATTERY_TEMPERATURE_THRESHOLD = 50.0
LOW_BATTERY_TEMPERATURE_THRESHOLD = 0.0
LOW_BATTERY_SOC_THRESHOLD = 20.0
LOW_BATTERY_CAPACITY_AH_THRESHOLD = 10.0  # 20% of 50Ah

# Data validation thresholds (sanity checks)
MAX_REASONABLE_VOLTAGE = 20.0  # Any voltage above this is suspicious
MIN_REASONABLE_VOLTAGE = 8.0   # Any voltage below this is suspicious
MAX_REASONABLE_TEMPERATURE = 80.0  # Degrees Celsius
MIN_REASONABLE_TEMPERATURE = -40.0  # Degrees Celsius
MAX_REASONABLE_CURRENT = 30.0  # Amps
MAX_REASONABLE_POWER = 500.0   # Watts

# Email throttling configuration
EMAIL_COOLDOWN_MINUTES = 60  # Don't send same alert more than once per hour
EMAIL_REMINDER_HOURS = 12    # Send reminder if problem persists for 12 hours

# Email Configuration
SMTP_SERVER = cr.server
SENDER_EMAIL = cr.username
RECIPIENT_EMAIL = cr.recipients
EMAIL_PASSWORD = cr.password
SMTP_PORT = cr.port

# Connection retry configuration
MAX_CONNECTION_RETRIES = 3
RETRY_DELAY_SECONDS = 5
CONNECTION_TIMEOUT_MINUTES = 10

# Logging configuration - ERROR LEVEL ONLY (no data logging)
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE_PATH),
        logging.StreamHandler()
    ]
)

# ============================================================================
# ALERT STATE MANAGEMENT
# ============================================================================

alert_state = {
    'low_battery_voltage': {'last_sent': None, 'first_detected': None, 'active': False},
    'high_controller_temp': {'last_sent': None, 'first_detected': None, 'active': False},
    'high_battery_temp': {'last_sent': None, 'first_detected': None, 'active': False},
    'low_battery_temp': {'last_sent': None, 'first_detected': None, 'active': False},
    'low_battery_soc': {'last_sent': None, 'first_detected': None, 'active': False},
    'low_battery_capacity': {'last_sent': None, 'first_detected': None, 'active': False},
    'hardware_fault': {'last_sent': None, 'first_detected': None, 'active': False},
    'critical_hardware_fault': {'last_sent': None, 'first_detected': None, 'active': False},
    'data_quality_issue': {'last_sent': None, 'first_detected': None, 'active': False},
    'over_discharge_event': {'last_sent': None, 'first_detected': None, 'active': False},
}

def load_alert_state():
    """Load alert state from ramdisk"""
    global alert_state
    try:
        if os.path.exists(STATE_FILE_PATH):
            with open(STATE_FILE_PATH, 'r') as f:
                loaded_state = json.load(f)
                for alert_type in alert_state:
                    if alert_type in loaded_state:
                        if loaded_state[alert_type]['last_sent']:
                            alert_state[alert_type]['last_sent'] = datetime.fromisoformat(
                                loaded_state[alert_type]['last_sent'])
                        if loaded_state[alert_type]['first_detected']:
                            alert_state[alert_type]['first_detected'] = datetime.fromisoformat(
                                loaded_state[alert_type]['first_detected'])
                        alert_state[alert_type]['active'] = loaded_state[alert_type]['active']
    except Exception as e:
        logging.error(f"Failed to load alert state: {e}")

def save_alert_state():
    """Save alert state to ramdisk (and occasionally to persistent storage)"""
    try:
        serializable_state = {}
        for alert_type, state in alert_state.items():
            serializable_state[alert_type] = {
                'last_sent': state['last_sent'].isoformat() if state['last_sent'] else None,
                'first_detected': state['first_detected'].isoformat() if state['first_detected'] else None,
                'active': state['active']
            }
        
        with open(STATE_FILE_PATH, 'w') as f:
            json.dump(serializable_state, f)
        
        any_active = any(state['active'] for state in alert_state.values())
        if any_active:
            write_persistent = True
            if os.path.exists(PERSISTENT_STATE_FILE):
                age = time.time() - os.path.getmtime(PERSISTENT_STATE_FILE)
                if age < 3600:
                    write_persistent = False
            
            if write_persistent:
                os.makedirs(os.path.dirname(PERSISTENT_STATE_FILE), exist_ok=True)
                with open(PERSISTENT_STATE_FILE, 'w') as f:
                    json.dump(serializable_state, f)
        
    except Exception as e:
        logging.error(f"Failed to save alert state: {e}")

def should_send_alert(alert_type):
    """Determine if an alert should be sent based on cooldown period
    
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
        return (False, f"cooldown ({cooldown.total_seconds() - time_since_last.total_seconds():.0f}s remaining)")
    time_since_first = now - state['first_detected']
    reminder_interval = timedelta(hours=EMAIL_REMINDER_HOURS)
    if time_since_first >= reminder_interval:
        return (True, f"reminder (problem persisting for {time_since_first.total_seconds()/3600:.1f}h)")
    return (True, "cooldown_expired")

def mark_alert_sent(alert_type):
    """Mark that an alert was sent
    
    Automatically initializes new alert types if not already present.
    """
    # Initialize alert type if it doesn't exist (defensive programming)
    if alert_type not in alert_state:
        alert_state[alert_type] = {'last_sent': None, 'first_detected': None, 'active': False}
    
    alert_state[alert_type]['last_sent'] = datetime.now()
    save_alert_state()

def clear_alert(alert_type):
    """Clear an alert when condition resolves
    
    Automatically initializes new alert types if not already present.
    """
    # Initialize alert type if it doesn't exist (defensive programming)
    if alert_type not in alert_state:
        alert_state[alert_type] = {'last_sent': None, 'first_detected': None, 'active': False}
    
    if alert_state[alert_type]['active']:
        alert_state[alert_type]['active'] = False
        alert_state[alert_type]['first_detected'] = None
        save_alert_state()
        return True
    return False

# ============================================================================
# CONNECTION MANAGEMENT
# ============================================================================

rover = None
last_successful_connection = None
connection_failures = 0

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
                logging.warning(f"Failed to connect to Renogy Rover (attempt {attempt+1}/{MAX_CONNECTION_RETRIES}): {e}")
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
            else:
                logging.critical(f"Failed to initialize Renogy Rover after {MAX_CONNECTION_RETRIES} attempts: {e}")
                return False
    return False

def check_connection_health():
    """Check if connection needs to be re-established"""
    global last_successful_connection
    
    if last_successful_connection is None:
        return False
    
    time_since_connection = datetime.now() - last_successful_connection
    if time_since_connection > timedelta(minutes=CONNECTION_TIMEOUT_MINUTES):
        logging.warning(f"No successful reads for {CONNECTION_TIMEOUT_MINUTES} minutes, reinitializing connection")
        return initialize_rover()
    
    return True

# ============================================================================
# INITIALIZATION
# ============================================================================

if not initialize_rover():
    logging.critical("Cannot start monitoring without controller connection")
    exit(1)

load_alert_state()

# ============================================================================
# EMAIL FUNCTIONS
# ============================================================================

def send_email_alert(subject, content):
    """Send an email alert"""
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
# DATA VALIDATION
# ============================================================================

def validate_metrics(metrics):
    """
    Validate sensor readings are within reasonable ranges.
    Returns (is_valid, list_of_issues)
    """
    issues = []
    
    # Voltage checks
    if metrics["battery_voltage"] is not None:
        if metrics["battery_voltage"] > MAX_REASONABLE_VOLTAGE or metrics["battery_voltage"] < MIN_REASONABLE_VOLTAGE:
            issues.append(f"Battery voltage out of range: {metrics['battery_voltage']:.2f}V")
    
    if metrics["solar_input_voltage"] is not None:
        if metrics["solar_input_voltage"] > MAX_REASONABLE_VOLTAGE and metrics["solar_input_voltage"] > 0:
            issues.append(f"Solar voltage out of range: {metrics['solar_input_voltage']:.2f}V")
    
    # Temperature checks
    if metrics["controller_temperature"] is not None:
        if metrics["controller_temperature"] > MAX_REASONABLE_TEMPERATURE or metrics["controller_temperature"] < MIN_REASONABLE_TEMPERATURE:
            issues.append(f"Controller temp out of range: {metrics['controller_temperature']:.1f}°C")
    
    if metrics["battery_temperature"] is not None:
        if metrics["battery_temperature"] > MAX_REASONABLE_TEMPERATURE or metrics["battery_temperature"] < MIN_REASONABLE_TEMPERATURE:
            issues.append(f"Battery temp out of range: {metrics['battery_temperature']:.1f}°C")
    
    # Current checks
    if metrics["solar_input_current"] is not None:
        if metrics["solar_input_current"] > MAX_REASONABLE_CURRENT:
            issues.append(f"Solar current out of range: {metrics['solar_input_current']:.2f}A")
    
    # Power checks
    if metrics["solar_input_power"] is not None:
        if metrics["solar_input_power"] > MAX_REASONABLE_POWER:
            issues.append(f"Solar power out of range: {metrics['solar_input_power']:.0f}W")
    
    # Check for None values in critical metrics
    critical_metrics = ["battery_voltage", "battery_soc", "controller_temperature"]
    for metric in critical_metrics:
        if metrics.get(metric) is None:
            issues.append(f"Critical metric '{metric}' is None")
    
    return (len(issues) == 0, issues)

# ============================================================================
# DATA COLLECTION
# ============================================================================

# Track previous over-discharge count
previous_over_discharge_count = None

def read_rover_metrics():
    """Read metrics from Renogy Rover with enhanced error handling"""
    global last_successful_connection, previous_over_discharge_count
    
    try:
        # Get active faults and load status first
        active_faults = rover.get_active_faults() or []
        load_status = rover.get_load_status()
        
        # Get current over-discharge count for event detection
        current_over_discharge_count = rover.get_total_battery_over_discharges()
        new_over_discharge_event = False
        if previous_over_discharge_count is not None and current_over_discharge_count is not None:
            if current_over_discharge_count > previous_over_discharge_count:
                new_over_discharge_event = True
        if current_over_discharge_count is not None:
            previous_over_discharge_count = current_over_discharge_count
        
        # Calculate battery capacity (SOC is needed first)
        battery_soc = rover.get_battery_state_of_charge()
        battery_capacity_ah_remaining = (battery_soc / 100.0) * BATTERY_CAPACITY_AH if battery_soc is not None else None
        
        # Read all metrics in a single dictionary
        metrics = {
            # Basic metrics
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
            
            # Calculated battery capacity
            "battery_capacity_ah_remaining": battery_capacity_ah_remaining,
            
            # Extended metrics - Daily statistics
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
            
            # Extended metrics - Historical data
            "total_operating_days": rover.get_total_operating_days(),
            "total_battery_over_discharges": current_over_discharge_count,
            "total_battery_full_charges": rover.get_total_battery_full_charges(),
            "new_over_discharge_event": new_over_discharge_event,
            
            # Extended metrics - Cumulative totals
            "total_charging_ah": rover.get_total_charging_ah(),
            "total_discharging_ah": rover.get_total_discharging_ah(),
            "cumulative_power_generation_kwh": rover.get_cumulative_power_generation(),
            "cumulative_power_consumption_kwh": rover.get_cumulative_power_consumption(),
            
            # Load status
            "load_is_on": 1 if (load_status and load_status['is_on']) else 0,
            "load_brightness": load_status['brightness'] if load_status else None,
            # Convert charging state to numeric code (0=deactivated, 1=activated, 2=mppt, 3=equalizing, 4=boost, 5=floating, 6=current_limiting, -1=unknown)
            "charging_state_code": {
                'deactivated': 0, 'activated': 1, 'mppt': 2, 'equalizing': 3,
                'boost': 4, 'floating': 5, 'current_limiting': 6
            }.get(load_status['charging_state'] if load_status else 'unknown', -1),
            
            # Fault information
            "active_faults": active_faults,
            "active_faults_count": len(active_faults),
            
            # Communication health
            "modbus_error_rate": rover.get_error_rate(),
            "modbus_total_reads": rover.total_reads,
            "modbus_failed_reads": rover.read_errors,
        }
        
        # Mark successful read
        last_successful_connection = datetime.now()
        
        return metrics
        
    except Exception as e:
        logging.error(f"Failed to read metrics from Renogy Rover: {e}")
        logging.error(traceback.format_exc())
        return None

# ============================================================================
# ALERT CHECKING
# ============================================================================

def check_critical_conditions(metrics):
    """Check for critical conditions and send alerts (with throttling)"""
    active_alerts = []
    
    # First, validate data quality
    is_valid, validation_issues = validate_metrics(metrics)
    if not is_valid:
        alert_type = 'data_quality_issue'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            issues_text = "\n".join([f"  - {issue}" for issue in validation_issues])
            subject = "⚠ Data Quality Issue Detected"
            content = (
                f"Invalid sensor readings detected:\n\n{issues_text}\n\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Alert reason: {reason}\n"
                f"This may indicate sensor failure or communication errors."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.warning(f"Data quality alert sent: {len(validation_issues)} issues")
        active_alerts.append(alert_type)
    else:
        clear_alert('data_quality_issue')
    
    # Check for new over-discharge events
    if metrics.get("new_over_discharge_event"):
        alert_type = 'over_discharge_event'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "⚠ Battery Over-Discharge Event Detected"
            content = (
                f"A new over-discharge event has been recorded!\n\n"
                f"Total over-discharge events: {metrics['total_battery_over_discharges']}\n"
                f"Current battery SOC: {metrics['battery_soc']}%\n"
                f"Current battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Over-discharging reduces battery lifespan. Consider increasing low-voltage disconnect threshold."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.warning(f"Over-discharge event alert sent")
        active_alerts.append(alert_type)
    else:
        clear_alert('over_discharge_event')
    
    # Check for hardware faults
    active_faults = metrics.get("active_faults", [])
    
    # Critical faults that require immediate attention
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
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            faults_text = "\n".join([f"  - {fault}" for fault in critical_faults_present])
            subject = "🚨 CRITICAL Hardware Fault Detected"
            content = (
                f"CRITICAL HARDWARE FAULT(S) DETECTED:\n\n{faults_text}\n\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Solar power: {metrics['solar_input_power']}W\n"
                f"Controller temp: {metrics['controller_temperature']:.1f}°C\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"IMMEDIATE ACTION REQUIRED! System may be damaged or unsafe."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.error(f"Critical hardware fault alert sent: {critical_faults_present}")
        active_alerts.append(alert_type)
    else:
        if clear_alert('critical_hardware_fault'):
            send_email_alert(
                "✓ Critical Hardware Faults Cleared",
                f"All critical hardware faults have been resolved.\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
    
    # Non-critical faults (warnings)
    warning_faults = [f for f in active_faults if f not in critical_faults]
    
    if warning_faults and not DISABLE_FAULT_ALERTS:
        alert_type = 'hardware_fault'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            faults_text = "\n".join([f"  - {fault}" for fault in warning_faults])
            subject = "⚠ Hardware Warning Detected"
            content = (
                f"Hardware warning(s) detected:\n\n{faults_text}\n\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Solar power: {metrics['solar_input_power']}W\n"
                f"Controller temp: {metrics['controller_temperature']:.1f}°C\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Monitor the situation and investigate if warnings persist."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.warning(f"Hardware warning alert sent: {warning_faults}")
        active_alerts.append(alert_type)
    else:
        if clear_alert('hardware_fault') and not critical_faults_present:
            send_email_alert(
                "✓ Hardware Warnings Cleared",
                f"All hardware warnings have been resolved.\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
    
    # Low battery voltage
    if metrics["battery_voltage"] < LOW_BATTERY_VOLTAGE_THRESHOLD and not DISABLE_LOW_BATTERY_ALERTS:
        alert_type = 'low_battery_voltage'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "⚠ Low Battery Voltage Alert"
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
                mark_alert_sent(alert_type)
                logging.warning(f"Low battery voltage alert sent: {metrics['battery_voltage']:.2f}V")
        active_alerts.append(alert_type)
    else:
        if clear_alert('low_battery_voltage'):
            send_email_alert(
                "✓ Battery Voltage Recovered",
                f"Battery voltage has recovered to {metrics['battery_voltage']:.2f}V"
            )
    
    # Low battery SOC
    if metrics["battery_soc"] < LOW_BATTERY_SOC_THRESHOLD and not DISABLE_LOW_BATTERY_ALERTS:
        alert_type = 'low_battery_soc'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "⚠ Low Battery State of Charge"
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
                mark_alert_sent(alert_type)
                logging.warning(f"Low battery SOC alert sent: {metrics['battery_soc']}%")
        active_alerts.append(alert_type)
    else:
        if clear_alert('low_battery_soc'):
            send_email_alert(
                "✓ Battery SOC Recovered",
                f"Battery SOC has recovered to {metrics['battery_soc']}%"
            )
    
    # Low battery capacity (Ah remaining)
    if (metrics["battery_capacity_ah_remaining"] is not None and 
        metrics["battery_capacity_ah_remaining"] < LOW_BATTERY_CAPACITY_AH_THRESHOLD and 
        not DISABLE_CAPACITY_ALERTS):
        alert_type = 'low_battery_capacity'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "⚠ Low Battery Capacity Alert"
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
                mark_alert_sent(alert_type)
                logging.warning(f"Low battery capacity alert sent: {metrics['battery_capacity_ah_remaining']:.1f}Ah")
        active_alerts.append(alert_type)
    else:
        if clear_alert('low_battery_capacity'):
            send_email_alert(
                "✓ Battery Capacity Recovered",
                f"Battery capacity has recovered to {metrics['battery_capacity_ah_remaining']:.1f}Ah ({metrics['battery_soc']}%)"
            )
    
    # High controller temperature
    if metrics["controller_temperature"] > HIGH_CONTROLLER_TEMPERATURE_THRESHOLD and not DISABLE_TEMPERATURE_ALERTS:
        alert_type = 'high_controller_temp'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "🔥 High Controller Temperature Alert"
            content = (
                f"Controller temperature is dangerously high: {metrics['controller_temperature']:.1f}°C\n"
                f"Threshold: {HIGH_CONTROLLER_TEMPERATURE_THRESHOLD}°C\n"
                f"Solar input: {metrics['solar_input_power']}W\n"
                f"Charging current: {metrics['solar_input_current']:.1f}A\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Immediate action required! Improve ventilation or reduce load."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.warning(f"High controller temp alert sent: {metrics['controller_temperature']:.1f}°C")
        active_alerts.append(alert_type)
    else:
        if clear_alert('high_controller_temp'):
            send_email_alert(
                "✓ Controller Temperature Normal",
                f"Controller temperature has returned to normal: {metrics['controller_temperature']:.1f}°C"
            )
    
    # High battery temperature
    if metrics["battery_temperature"] > HIGH_BATTERY_TEMPERATURE_THRESHOLD and not DISABLE_TEMPERATURE_ALERTS:
        alert_type = 'high_battery_temp'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "🔥 High Battery Temperature Alert"
            content = (
                f"Battery temperature is dangerously high: {metrics['battery_temperature']:.1f}°C\n"
                f"Threshold: {HIGH_BATTERY_TEMPERATURE_THRESHOLD}°C\n"
                f"Charging current: {metrics['solar_input_current']:.1f}A\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"DANGER! Battery may be damaged. Improve cooling immediately!"
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.error(f"High battery temp alert sent: {metrics['battery_temperature']:.1f}°C")
        active_alerts.append(alert_type)
    else:
        if clear_alert('high_battery_temp'):
            send_email_alert(
                "✓ Battery Temperature Normal",
                f"Battery temperature has returned to safe levels: {metrics['battery_temperature']:.1f}°C"
            )
    
    # Low battery temperature
    if metrics["battery_temperature"] < LOW_BATTERY_TEMPERATURE_THRESHOLD and not DISABLE_TEMPERATURE_ALERTS:
        alert_type = 'low_battery_temp'
        should_send, reason = should_send_alert(alert_type)
        if should_send:
            subject = "❄ Low Battery Temperature Alert"
            content = (
                f"Battery temperature is critically low: {metrics['battery_temperature']:.1f}°C\n"
                f"Threshold: {LOW_BATTERY_TEMPERATURE_THRESHOLD}°C\n"
                f"Battery SOC: {metrics['battery_soc']}%\n"
                f"Battery voltage: {metrics['battery_voltage']:.2f}V\n"
                f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"Alert reason: {reason}\n"
                f"Battery performance reduced. Consider insulation or heating."
            )
            if send_email_alert(subject, content):
                mark_alert_sent(alert_type)
                logging.warning(f"Low battery temp alert sent: {metrics['battery_temperature']:.1f}°C")
        active_alerts.append(alert_type)
    else:
        if clear_alert('low_battery_temp'):
            send_email_alert(
                "✓ Battery Temperature Normal",
                f"Battery temperature has risen to safe levels: {metrics['battery_temperature']:.1f}°C"
            )
    
    return active_alerts

# ============================================================================
# PROMETHEUS METRICS
# ============================================================================

def write_metrics_to_file(metrics, active_alerts, temp_file_path, final_file_path):
    """Write metrics to file in Prometheus format with atomic rename"""
    try:
        with open(temp_file_path, 'w') as temp_file:
            # Write all numeric metrics
            for key, value in metrics.items():
                # Skip non-numeric values and special keys
                if key in ['active_faults', 'charging_state', 'new_over_discharge_event']:
                    continue
                
                # Skip None values - they can't be represented in Prometheus
                if value is None:
                    continue
                
                # Validate value is actually a number
                try:
                    float(value)
                except (ValueError, TypeError):
                    logging.warning(f"Skipping non-numeric metric {key}={value}")
                    continue
                
                temp_file.write(f'# HELP {key} Renogy solar charge controller metric\n')
                temp_file.write(f'# TYPE {key} gauge\n')
                temp_file.write(f'{key}{{source="renogy"}} {value}\n')
            
            # Write battery capacity configuration
            temp_file.write(f'# HELP battery_capacity_total Total battery capacity in Ah\n')
            temp_file.write(f'# TYPE battery_capacity_total gauge\n')
            temp_file.write(f'battery_capacity_total{{source="renogy"}} {BATTERY_CAPACITY_AH}\n')
            
            # Write fault flags as individual metrics
            for fault in metrics.get('active_faults', []):
                safe_fault_name = fault.replace('-', '_')
                temp_file.write(f'# HELP renogy_fault_{safe_fault_name} Fault status\n')
                temp_file.write(f'# TYPE renogy_fault_{safe_fault_name} gauge\n')
                temp_file.write(f'renogy_fault_{safe_fault_name}{{source="renogy"}} 1\n')
            
            # Write alert status
            temp_file.write('# HELP renogy_alerts_active Active alert count\n')
            temp_file.write('# TYPE renogy_alerts_active gauge\n')
            temp_file.write(f'renogy_alerts_active{{source="renogy"}} {len(active_alerts)}\n')
            
            for alert_type in alert_state:
                status = 1 if alert_state[alert_type]['active'] else 0
                temp_file.write(f'# HELP renogy_alert_{alert_type} Alert status\n')
                temp_file.write(f'# TYPE renogy_alert_{alert_type} gauge\n')
                temp_file.write(f'renogy_alert_{alert_type}{{source="renogy"}} {status}\n')
            
            # Maintenance mode flags
            temp_file.write('# HELP renogy_emails_disabled Email alerts disabled (1=disabled, 0=enabled)\n')
            temp_file.write('# TYPE renogy_emails_disabled gauge\n')
            temp_file.write(f'renogy_emails_disabled{{source="renogy"}} {1 if DISABLE_EMAILS else 0}\n')
            
            temp_file.write('# HELP renogy_low_battery_alerts_disabled Low battery alerts disabled (1=disabled, 0=enabled)\n')
            temp_file.write('# TYPE renogy_low_battery_alerts_disabled gauge\n')
            temp_file.write(f'renogy_low_battery_alerts_disabled{{source="renogy"}} {1 if DISABLE_LOW_BATTERY_ALERTS else 0}\n')
            
            temp_file.write('# HELP renogy_fault_alerts_disabled Fault alerts disabled (1=disabled, 0=enabled)\n')
            temp_file.write('# TYPE renogy_fault_alerts_disabled gauge\n')
            temp_file.write(f'renogy_fault_alerts_disabled{{source="renogy"}} {1 if DISABLE_FAULT_ALERTS else 0}\n')
            
            temp_file.write('# HELP renogy_temperature_alerts_disabled Temperature alerts disabled (1=disabled, 0=enabled)\n')
            temp_file.write('# TYPE renogy_temperature_alerts_disabled gauge\n')
            temp_file.write(f'renogy_temperature_alerts_disabled{{source="renogy"}} {1 if DISABLE_TEMPERATURE_ALERTS else 0}\n')
            
            temp_file.write('# HELP renogy_capacity_alerts_disabled Capacity alerts disabled (1=disabled, 0=enabled)\n')
            temp_file.write('# TYPE renogy_capacity_alerts_disabled gauge\n')
            temp_file.write(f'renogy_capacity_alerts_disabled{{source="renogy"}} {1 if DISABLE_CAPACITY_ALERTS else 0}\n')
            
            # Write health metric
            temp_file.write('# HELP renogy_monitor_healthy Monitor health status\n')
            temp_file.write('# TYPE renogy_monitor_healthy gauge\n')
            temp_file.write(f'renogy_monitor_healthy{{source="renogy"}} 1\n')
            
            # Write last update timestamp
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

# Show disabled subsystems
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
    print("🔧" * 35)
    print("   ⚠️  MAINTENANCE MODE ACTIVE ⚠️")
    print(f"   DISABLED: {', '.join(disabled_systems)}")
    print("🔧" * 35)
print()

consecutive_failures = 0
max_consecutive_failures = 5

try:
    while True:
        metrics = read_rover_metrics()
        
        if metrics:
            consecutive_failures = 0
            active_alerts = check_critical_conditions(metrics)
            write_metrics_to_file(metrics, active_alerts, TEMP_FILE_PATH, FINAL_FILE_PATH)
        else:
            consecutive_failures += 1
            logging.error(f"Failed to read metrics ({consecutive_failures}/{max_consecutive_failures})")
            
            if consecutive_failures >= max_consecutive_failures:
                logging.critical(f"Failed to read metrics {consecutive_failures} times in a row, attempting reconnection")
                if not initialize_rover():
                    logging.critical("Reconnection failed, will retry on next cycle")
                    # Write unhealthy status
                    try:
                        with open(TEMP_FILE_PATH, 'w') as f:
                            f.write('# HELP renogy_monitor_healthy Monitor health status\n')
                            f.write('# TYPE renogy_monitor_healthy gauge\n')
                            f.write('renogy_monitor_healthy{source="renogy"} 0\n')
                        os.rename(TEMP_FILE_PATH, FINAL_FILE_PATH)
                    except:
                        pass
                consecutive_failures = 0
        
        # Check connection health periodically
        check_connection_health()
        
        time.sleep(SLEEP_TIME)

except KeyboardInterrupt:
    print("\nShutdown requested")
except Exception as e:
    logging.critical(f"Unexpected error in main loop: {e}")
    logging.critical(traceback.format_exc())
finally:
    save_alert_state()
    print("Renogy monitor stopped")
