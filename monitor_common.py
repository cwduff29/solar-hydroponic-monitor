#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
monitor_common.py - Shared utilities for solar hydroponic monitor scripts

Provides:
- AlertManager: thread-safe alert state with cooldown/reminder logic
- send_email(): shared email sender using credentials.py
- Watchdog: hardware watchdog keepalive
- DailySummary: min/max/avg/sum tracking with daily send logic
- load_config / get_config / reload_config: thread-safe JSON config loading
"""

import os
import json
import time
import logging
import smtplib
import threading
from datetime import datetime, timedelta
from email.message import EmailMessage

# ============================================================================
# CONFIG LOADING
# ============================================================================

_config = {}
_config_lock = threading.Lock()
_config_path = None


def load_config(path):
    """Load JSON config from path. Called once at startup."""
    global _config, _config_path
    _config_path = path
    with _config_lock:
        try:
            with open(path, 'r') as f:
                _config = json.load(f)
            logging.info(f"Config loaded from {path}")
        except Exception as e:
            logging.error(f"Failed to load config from {path}: {e}")
            _config = {}
    return _config


def get_config(key, default=None):
    """
    Get a config value by dot-separated key path.
    E.g. get_config('renogy.thresholds.low_battery_voltage', 12.8)
    """
    with _config_lock:
        parts = key.split('.')
        node = _config
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return default
        return node


def reload_config():
    """Reload config from disk (for SIGHUP handler). Thread-safe."""
    global _config
    if _config_path is None:
        logging.warning("reload_config: no config path set, cannot reload")
        return False
    with _config_lock:
        try:
            with open(_config_path, 'r') as f:
                _config = json.load(f)
            logging.info(f"Config reloaded from {_config_path}")
            return True
        except Exception as e:
            logging.error(f"Failed to reload config: {e}")
            return False


# ============================================================================
# EMAIL
# ============================================================================

def send_email(subject, body, disabled=False):
    """
    Send an email using credentials from environment variables.

    Required env vars (set via /etc/solar-hydroponic/credentials.env):
        SMTP_USER        Gmail address
        SMTP_PASSWORD    Gmail App Password (16 chars, not your account password)
        SMTP_RECIPIENTS  Comma-separated recipient addresses

    Optional env vars (defaults work for Gmail):
        SMTP_SERVER      Default: smtp.gmail.com
        SMTP_PORT        Default: 587

    Args:
        subject:  Email subject line
        body:     Email body text
        disabled: If True, suppress sending and log instead

    Returns:
        True on success, False on failure
    """
    if disabled:
        logging.info(f"[EMAILS DISABLED] Email suppressed: '{subject}'")
        return False

    smtp_server     = os.environ.get('SMTP_SERVER',     'smtp.gmail.com')
    smtp_port       = int(os.environ.get('SMTP_PORT',   '587'))
    smtp_user       = os.environ.get('SMTP_USER',       '')
    smtp_password   = os.environ.get('SMTP_PASSWORD',   '')
    smtp_recipients = os.environ.get('SMTP_RECIPIENTS', '')

    if not smtp_user or not smtp_password:
        logging.error(
            "Email credentials missing — set SMTP_USER and SMTP_PASSWORD "
            "in /etc/solar-hydroponic/credentials.env"
        )
        return False

    try:
        msg = EmailMessage()
        msg['From']    = smtp_user
        msg['To']      = smtp_recipients
        msg['Subject'] = subject
        msg.set_content(body)
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:
        logging.error(f"Failed to send email '{subject}': {e}")
        return False


# ============================================================================
# ALERT MANAGER
# ============================================================================

class AlertManager:
    """
    Thread-safe alert state management with cooldown and reminder logic.

    Alert state is stored in two locations:
    - ramdisk_path: fast ramdisk state (primary, in /ramdisk/)
    - persistent_path: persistent fallback (in /var/lib/renogy/)

    On load: tries ramdisk first, then persistent.
    On save: always writes ramdisk; also writes persistent when any alert is active.
    """

    def __init__(self, ramdisk_path, persistent_path,
                 cooldown_minutes=60, reminder_hours=12):
        self._lock = threading.Lock()
        self._ramdisk_path = ramdisk_path
        self._persistent_path = persistent_path
        self._cooldown_minutes = cooldown_minutes
        self._reminder_hours = reminder_hours
        self._state = {}  # {alert_type: {last_sent, first_detected, active}}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_type(self, alert_type):
        """Ensure alert_type entry exists (call under lock)."""
        if alert_type not in self._state:
            self._state[alert_type] = {
                'last_sent': None,
                'first_detected': None,
                'active': False,
            }

    def _serialize(self):
        result = {}
        for atype, s in self._state.items():
            result[atype] = {
                'last_sent': s['last_sent'].isoformat() if s['last_sent'] else None,
                'first_detected': s['first_detected'].isoformat() if s['first_detected'] else None,
                'active': s['active'],
            }
        return result

    def _deserialize(self, data):
        for atype, s in data.items():
            self._init_type(atype)
            self._state[atype]['active'] = s.get('active', False)
            ls = s.get('last_sent')
            fd = s.get('first_detected')
            self._state[atype]['last_sent'] = datetime.fromisoformat(ls) if ls else None
            self._state[atype]['first_detected'] = datetime.fromisoformat(fd) if fd else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load(self):
        """Load state from ramdisk first, then persistent file as fallback."""
        with self._lock:
            for path in (self._ramdisk_path, self._persistent_path):
                if path and os.path.exists(path):
                    try:
                        with open(path, 'r') as f:
                            data = json.load(f)
                        self._deserialize(data)
                        logging.info(f"Alert state loaded from {path}")
                        return True
                    except Exception as e:
                        logging.error(f"Failed to load alert state from {path}: {e}")
            return False

    def save(self):
        """
        Save state to ramdisk. Also write persistent file if any alert is active
        (every time, not rate-limited).
        """
        with self._lock:
            data = self._serialize()
            # Write ramdisk
            try:
                os.makedirs(os.path.dirname(self._ramdisk_path), exist_ok=True) \
                    if os.path.dirname(self._ramdisk_path) else None
                with open(self._ramdisk_path, 'w') as f:
                    json.dump(data, f)
            except Exception as e:
                logging.error(f"Failed to save alert state to ramdisk {self._ramdisk_path}: {e}")

            # Write persistent if any alert is active
            any_active = any(s['active'] for s in self._state.values())
            if any_active and self._persistent_path:
                try:
                    os.makedirs(os.path.dirname(self._persistent_path), exist_ok=True)
                    with open(self._persistent_path, 'w') as f:
                        json.dump(data, f)
                except Exception as e:
                    logging.error(
                        f"Failed to save alert state to persistent {self._persistent_path}: {e}"
                    )

    def should_send(self, alert_type):
        """
        Determine if an alert should be sent.

        Returns (bool, reason_string).
        Marks the alert as active (first_detected) if not already active.
        Does NOT call mark_sent() — caller must do that.
        """
        with self._lock:
            self._init_type(alert_type)
            state = self._state[alert_type]
            now = datetime.now()

            if not state['active']:
                state['active'] = True
                state['first_detected'] = now
                return (True, "first_occurrence")

            if not state['last_sent']:
                return (True, "never_sent")

            time_since_last = now - state['last_sent']
            cooldown = timedelta(minutes=self._cooldown_minutes)
            if time_since_last < cooldown:
                remaining = cooldown.total_seconds() - time_since_last.total_seconds()
                return (False, f"cooldown ({remaining:.0f}s remaining)")

            time_since_first = now - state['first_detected']
            reminder_interval = timedelta(hours=self._reminder_hours)
            if time_since_first >= reminder_interval:
                hours = time_since_first.total_seconds() / 3600
                return (True, f"reminder (problem persisting for {hours:.1f}h)")

            return (True, "cooldown_expired")

    def mark_sent(self, alert_type):
        """Record that an alert email was sent for alert_type."""
        with self._lock:
            self._init_type(alert_type)
            self._state[alert_type]['last_sent'] = datetime.now()
        self.save()

    def clear(self, alert_type):
        """
        Clear an alert when its condition resolves.

        Returns True if the alert was previously active (i.e., was just cleared).
        """
        with self._lock:
            self._init_type(alert_type)
            if self._state[alert_type]['active']:
                self._state[alert_type]['active'] = False
                self._state[alert_type]['first_detected'] = None
                do_save = True
            else:
                do_save = False
        if do_save:
            self.save()
            return True
        return False

    def is_active(self, alert_type):
        """Return True if the alert_type is currently active."""
        with self._lock:
            self._init_type(alert_type)
            return self._state[alert_type]['active']

    def all_states(self):
        """Return a snapshot of all alert states as a dict."""
        with self._lock:
            return {
                atype: {
                    'active': s['active'],
                    'last_sent': s['last_sent'].isoformat() if s['last_sent'] else None,
                    'first_detected': s['first_detected'].isoformat() if s['first_detected'] else None,
                }
                for atype, s in self._state.items()
            }


# ============================================================================
# HARDWARE WATCHDOG
# ============================================================================

class Watchdog:
    """
    Hardware watchdog keepalive.

    Opens /dev/watchdog and writes '1' every `interval` seconds from a
    background thread to prevent the system from rebooting.

    On clean shutdown call stop(), which writes 'V' (magic close) to
    disable the watchdog before closing the fd.

    If the device can't be opened (e.g. not supported), logs a warning
    and runs in no-op mode so callers don't need special handling.
    """

    def __init__(self, device='/dev/watchdog', interval=30):
        self._device = device
        self._interval = interval
        self._fd = None
        self._thread = None
        self._stop_event = threading.Event()
        self._enabled = False

    def start(self):
        """Open watchdog device and start keepalive thread."""
        try:
            self._fd = os.open(self._device, os.O_WRONLY)
            self._enabled = True
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._keepalive_loop,
                name='watchdog-keepalive',
                daemon=True,
            )
            self._thread.start()
            logging.info(f"Hardware watchdog started ({self._device}, {self._interval}s interval)")
        except OSError as e:
            logging.warning(
                f"Could not open watchdog device {self._device}: {e} — "
                f"running without hardware watchdog"
            )
            self._enabled = False

    def _keepalive_loop(self):
        """Background thread: write '1' to watchdog every interval seconds."""
        while not self._stop_event.wait(timeout=self._interval):
            if self._fd is not None:
                try:
                    os.write(self._fd, b'1')
                except OSError as e:
                    logging.error(f"Watchdog write failed: {e}")

    def stop(self):
        """
        Stop the watchdog keepalive and write magic 'V' to disable the watchdog.
        Call this in the finally block for a clean shutdown.
        """
        if not self._enabled:
            return
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
        if self._fd is not None:
            try:
                os.write(self._fd, b'V')  # Magic close: disable watchdog
                os.close(self._fd)
                logging.info("Hardware watchdog stopped cleanly")
            except OSError as e:
                logging.error(f"Watchdog stop error: {e}")
            finally:
                self._fd = None
        self._enabled = False


# ============================================================================
# DAILY SUMMARY
# ============================================================================

class DailySummary:
    """
    Tracks min/max/avg/sum for named statistics and provides once-per-day
    send logic.

    Usage:
        summary = DailySummary()
        summary.update('solar_power_w', 123.4)
        if summary.should_send(hour=7):
            # build and send email
            summary.mark_sent()
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stats = {}  # {key: {'sum': float, 'count': int, 'min': float, 'max': float}}
        self._sent_today = False
        self._last_sent_date = None

    def update(self, key, value):
        """Record a new value for a named stat."""
        if value is None:
            return
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        with self._lock:
            if key not in self._stats:
                self._stats[key] = {'sum': 0.0, 'count': 0, 'min': value, 'max': value}
            s = self._stats[key]
            s['sum'] += value
            s['count'] += 1
            if value < s['min']:
                s['min'] = value
            if value > s['max']:
                s['max'] = value

    def get_avg(self, key):
        with self._lock:
            s = self._stats.get(key)
            if s is None or s['count'] == 0:
                return None
            return s['sum'] / s['count']

    def get_min(self, key):
        with self._lock:
            s = self._stats.get(key)
            return s['min'] if s and s['count'] > 0 else None

    def get_max(self, key):
        with self._lock:
            s = self._stats.get(key)
            return s['max'] if s and s['count'] > 0 else None

    def get_sum(self, key):
        with self._lock:
            s = self._stats.get(key)
            return s['sum'] if s and s['count'] > 0 else None

    def get_count(self, key):
        with self._lock:
            s = self._stats.get(key)
            return s['count'] if s else 0

    def should_send(self, hour=7):
        """
        Return True once per day when the current hour matches `hour`.
        Resets automatically at midnight (i.e., once the date changes,
        a new send becomes eligible at the next matching hour).
        """
        now = datetime.now()
        today = now.date()
        with self._lock:
            if self._last_sent_date == today:
                return False
            if now.hour == hour:
                return True
            return False

    def mark_sent(self):
        """Record that the daily summary was sent today; resets stats."""
        today = datetime.now().date()
        with self._lock:
            self._last_sent_date = today
            self._sent_today = True
            self._stats = {}

    def keys(self):
        """Return list of tracked stat keys."""
        with self._lock:
            return list(self._stats.keys())


# ============================================================================
# CONFIG VALIDATION
# ============================================================================

_REQUIRED_TOP_LEVEL_KEYS = ['renogy', 'waterflow', 'alerts', 'paths', 'watchdog', 'shutdown']


def get_missing_keys():
    """
    Check for required top-level config keys.

    Returns a list of missing key names.
    """
    with _config_lock:
        return [k for k in _REQUIRED_TOP_LEVEL_KEYS if k not in _config]


def validate_config():
    """
    Validate the loaded configuration for logical consistency and safety.

    Returns:
        True if no ERRORs were found, False if any ERRORs were found.

    Side effects:
        Logs each issue at the appropriate level (ERROR / WARNING / INFO).
    """
    issues = []  # list of (level, message)

    def _get(key, default=None):
        return get_config(key, default)

    # -----------------------------------------------------------------------
    # Missing top-level keys
    # -----------------------------------------------------------------------
    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if get_config(key) is None:
            issues.append(('WARNING', f"Missing top-level config key: '{key}'"))

    # -----------------------------------------------------------------------
    # Battery load shedding SOC thresholds
    # -----------------------------------------------------------------------
    low_soc    = _get('waterflow.battery_load_shedding.disable_threshold_pct',  30)
    reduce_soc = _get('waterflow.battery_load_shedding.reduce_threshold_pct',   50)
    normal_soc = _get('waterflow.battery_load_shedding.normal_threshold_pct',   70)

    if not (0 <= low_soc <= 100):
        issues.append(('ERROR',
            f"waterflow.battery_load_shedding.disable_threshold_pct={low_soc} "
            f"out of range [0, 100]"))
    if not (0 <= reduce_soc <= 100):
        issues.append(('ERROR',
            f"waterflow.battery_load_shedding.reduce_threshold_pct={reduce_soc} "
            f"out of range [0, 100]"))
    if not (0 <= normal_soc <= 100):
        issues.append(('ERROR',
            f"waterflow.battery_load_shedding.normal_threshold_pct={normal_soc} "
            f"out of range [0, 100]"))
    if low_soc >= normal_soc:
        issues.append(('ERROR',
            f"Battery load shedding: disable_threshold_pct ({low_soc}) "
            f"must be < normal_threshold_pct ({normal_soc})"))
    if not (low_soc <= reduce_soc <= normal_soc):
        issues.append(('ERROR',
            f"Battery load shedding thresholds must satisfy "
            f"disable ({low_soc}) <= reduce ({reduce_soc}) <= normal ({normal_soc})"))

    # -----------------------------------------------------------------------
    # Voltage thresholds
    # -----------------------------------------------------------------------
    for key in [
        'renogy.thresholds.low_battery_voltage',
        'renogy.thresholds.max_voltage',
        'renogy.thresholds.min_voltage',
    ]:
        v = _get(key)
        if v is not None and not (8.0 <= v <= 20.0):
            issues.append(('WARNING',
                f"{key}={v} outside reasonable range [8.0, 20.0]V"))

    # -----------------------------------------------------------------------
    # Temperature thresholds (-50 to 100°C)
    # -----------------------------------------------------------------------
    temp_keys = [
        'renogy.thresholds.high_controller_temp_c',
        'renogy.thresholds.high_battery_temp_c',
        'renogy.thresholds.low_battery_temp_c',
        'renogy.thresholds.max_temp_c',
        'renogy.thresholds.min_temp_c',
        'waterflow.temperature.nft_extreme_temp_c',
        'waterflow.temperature.outdoor_freeze_risk_c',
        'waterflow.temperature.reservoir_min_for_freeze_alert_c',
        'waterflow.temperature.water_temp_hot_c',
        'waterflow.temperature.water_temp_warm_c',
        'waterflow.temperature.water_temp_moderate_c',
        'waterflow.fan.temp_on_c',
        'waterflow.fan.temp_off_c',
        'waterflow.fan.temp_force_on_c',
    ]
    for key in temp_keys:
        v = _get(key)
        if v is not None and not (-50.0 <= v <= 100.0):
            issues.append(('WARNING',
                f"{key}={v} outside reasonable range [-50, 100]°C"))

    # -----------------------------------------------------------------------
    # Flow thresholds must be positive
    # -----------------------------------------------------------------------
    flow_pos_keys = [
        'waterflow.flow.calibration_factor',
        'waterflow.flow.measurement_duration_seconds',
        'waterflow.flow.min_flow_threshold_lpm',
        'waterflow.flow.imbalance_threshold_lpm',
    ]
    for key in flow_pos_keys:
        v = _get(key)
        if v is not None and v <= 0:
            issues.append(('ERROR', f"{key}={v} must be positive"))

    # -----------------------------------------------------------------------
    # GPIO pins: valid BCM numbers (0–27), no duplicates
    # -----------------------------------------------------------------------
    gpio_keys = {
        'flow_sensor_inlet':  'waterflow.gpio.flow_sensor_inlet',
        'flow_sensor_outlet': 'waterflow.gpio.flow_sensor_outlet',
        'main_pump_relay':    'waterflow.gpio.main_pump_relay',
        'backup_pump_relay':  'waterflow.gpio.backup_pump_relay',
        'aeration_pump':      'waterflow.gpio.aeration_pump',
        'fan_control':        'waterflow.gpio.fan_control',
    }
    gpio_values = {}
    for name, key in gpio_keys.items():
        v = _get(key)
        if v is not None:
            if not (0 <= int(v) <= 27):
                issues.append(('ERROR',
                    f"GPIO pin {key}={v} out of valid BCM range [0, 27]"))
            else:
                if v in gpio_values:
                    issues.append(('ERROR',
                        f"Duplicate GPIO pin {v} assigned to both "
                        f"'{gpio_values[v]}' and '{name}'"))
                else:
                    gpio_values[v] = name

    # -----------------------------------------------------------------------
    # Aeration durations must be positive
    # -----------------------------------------------------------------------
    for key in [
        'waterflow.aeration.on_duration_seconds',
        'waterflow.aeration.off_duration_seconds',
        'waterflow.aeration.reduced_on_duration_seconds',
        'waterflow.aeration.reduced_off_duration_seconds',
    ]:
        v = _get(key)
        if v is not None and v <= 0:
            issues.append(('ERROR', f"{key}={v} must be positive"))

    # -----------------------------------------------------------------------
    # Alert email cooldown must be positive
    # -----------------------------------------------------------------------
    cooldown = _get('alerts.email_cooldown_minutes', 60)
    if cooldown <= 0:
        issues.append(('ERROR',
            f"alerts.email_cooldown_minutes={cooldown} must be > 0"))

    # -----------------------------------------------------------------------
    # Battery capacity must be positive
    # -----------------------------------------------------------------------
    capacity = _get('renogy.battery_capacity_ah')
    if capacity is not None and capacity <= 0:
        issues.append(('ERROR',
            f"renogy.battery_capacity_ah={capacity} must be positive"))

    # -----------------------------------------------------------------------
    # Poll interval must be positive
    # -----------------------------------------------------------------------
    poll = _get('renogy.poll_interval_seconds')
    if poll is not None and poll <= 0:
        issues.append(('ERROR',
            f"renogy.poll_interval_seconds={poll} must be positive"))

    # -----------------------------------------------------------------------
    # Sensor map: warn if empty
    # -----------------------------------------------------------------------
    sensor_map = _get('waterflow.temperature.sensor_map', {})
    if not sensor_map:
        issues.append(('WARNING',
            "waterflow.temperature.sensor_map is empty — "
            "will fall back to discovery order"))

    # -----------------------------------------------------------------------
    # Shutdown SOC: critical < recovery, both 0-100
    # -----------------------------------------------------------------------
    critical_soc  = _get('shutdown.critical_soc_pct',  5)
    recovery_soc  = _get('shutdown.recovery_soc_pct', 15)
    if critical_soc is not None:
        if not (0 <= critical_soc <= 100):
            issues.append(('ERROR',
                f"shutdown.critical_soc_pct={critical_soc} out of range [0, 100]"))
    if recovery_soc is not None:
        if not (0 <= recovery_soc <= 100):
            issues.append(('ERROR',
                f"shutdown.recovery_soc_pct={recovery_soc} out of range [0, 100]"))
    if (critical_soc is not None and recovery_soc is not None
            and critical_soc >= recovery_soc):
        issues.append(('ERROR',
            f"shutdown.critical_soc_pct ({critical_soc}) must be < "
            f"shutdown.recovery_soc_pct ({recovery_soc})"))

    # -----------------------------------------------------------------------
    # Log and return result
    # -----------------------------------------------------------------------
    has_errors = False
    for level, message in issues:
        if level == 'ERROR':
            logging.error(f"Config validation ERROR: {message}")
            has_errors = True
        elif level == 'WARNING':
            logging.warning(f"Config validation WARNING: {message}")
        else:
            logging.info(f"Config validation INFO: {message}")

    if not issues:
        logging.info("Config validation passed — no issues found")

    return not has_errors


# ============================================================================
# STARTUP SELF-TEST
# ============================================================================

def startup_selftest(script_name, config_path, log_path, extra_checks=None):
    """
    Run startup self-test checks and send a notification email with results.

    Args:
        script_name:   Name of the calling script (e.g. "renogy", "waterflow")
        config_path:   Path to config.json
        log_path:      Path to the script's log file
        extra_checks:  Optional list of (check_name, callable) where callable
                       returns (status, detail) with status "PASS"/"WARN"/"FAIL"

    Returns:
        (passed: bool, results: list of (check_name, status, detail))
    """
    results = []

    def record(name, status, detail=""):
        results.append((name, status, detail))
        if status == 'PASS':
            logging.info(f"Startup self-test [{name}]: PASS — {detail}")
        elif status == 'WARN':
            logging.warning(f"Startup self-test [{name}]: WARN — {detail}")
        else:
            logging.error(f"Startup self-test [{name}]: FAIL — {detail}")

    # -----------------------------------------------------------------------
    # 1. Config file readable and valid JSON
    # -----------------------------------------------------------------------
    try:
        with open(config_path, 'r') as f:
            json.load(f)
        record("Config file", "PASS", config_path)
    except FileNotFoundError:
        record("Config file", "FAIL", f"not found: {config_path}")
    except json.JSONDecodeError as e:
        record("Config file", "FAIL", f"invalid JSON: {e}")
    except Exception as e:
        record("Config file", "FAIL", str(e))

    # -----------------------------------------------------------------------
    # 2. Config validation
    # -----------------------------------------------------------------------
    missing = get_missing_keys()
    if missing:
        record("Config keys", "WARN",
               f"missing top-level keys: {', '.join(missing)}")
    else:
        record("Config keys", "PASS", "all required keys present")

    valid = validate_config()
    if valid:
        record("Config validation", "PASS", "no errors")
    else:
        record("Config validation", "FAIL", "one or more config ERRORs (see log)")

    # -----------------------------------------------------------------------
    # 3. Credentials (SMTP env vars)
    # -----------------------------------------------------------------------
    smtp_user = os.environ.get('SMTP_USER', '')
    smtp_pass = os.environ.get('SMTP_PASSWORD', '')
    if smtp_user and smtp_pass:
        record("Credentials", "PASS", f"SMTP_USER={smtp_user}")
    elif smtp_user and not smtp_pass:
        record("Credentials", "WARN", "SMTP_USER set but SMTP_PASSWORD is empty")
    else:
        record("Credentials", "WARN",
               "SMTP_USER and SMTP_PASSWORD not set — email alerts disabled")

    # -----------------------------------------------------------------------
    # 4. Ramdisk mounted
    # -----------------------------------------------------------------------
    if os.path.ismount('/ramdisk'):
        record("Ramdisk", "PASS", "/ramdisk is mounted")
    else:
        record("Ramdisk", "FAIL", "/ramdisk is not mounted")

    # -----------------------------------------------------------------------
    # 5. Persistent state directory
    # -----------------------------------------------------------------------
    state_dir = '/var/lib/renogy'
    if os.path.isdir(state_dir) and os.access(state_dir, os.W_OK):
        record("Persistent state dir", "PASS", state_dir)
    elif os.path.isdir(state_dir):
        record("Persistent state dir", "WARN", f"{state_dir} exists but not writable")
    else:
        record("Persistent state dir", "FAIL", f"{state_dir} does not exist")

    # -----------------------------------------------------------------------
    # 6. Log file writable
    # -----------------------------------------------------------------------
    try:
        log_dir = os.path.dirname(log_path)
        if log_dir and not os.path.isdir(log_dir):
            record("Log file", "FAIL", f"parent directory missing: {log_dir}")
        elif os.path.exists(log_path) and not os.access(log_path, os.W_OK):
            record("Log file", "WARN", f"not writable: {log_path}")
        else:
            record("Log file", "PASS", log_path)
    except Exception as e:
        record("Log file", "WARN", str(e))

    # -----------------------------------------------------------------------
    # 7. Disk space (root filesystem)
    # -----------------------------------------------------------------------
    try:
        st = os.statvfs('/')
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        free_pct = (avail / total * 100.0) if total > 0 else 0
        avail_gb = avail / 1024 ** 3
        if free_pct < 5:
            record("Disk space", "FAIL",
                   f"root fs only {free_pct:.1f}% free ({avail_gb:.2f} GB)")
        elif free_pct < 20:
            record("Disk space", "WARN",
                   f"root fs {free_pct:.1f}% free ({avail_gb:.2f} GB) — consider cleanup")
        else:
            record("Disk space", "PASS",
                   f"root fs {free_pct:.1f}% free ({avail_gb:.2f} GB)")
    except Exception as e:
        record("Disk space", "WARN", f"could not check disk space: {e}")

    # -----------------------------------------------------------------------
    # 8. Previous alert state (from AlertManager if available)
    # -----------------------------------------------------------------------
    # We check the global module-level _config for alert state paths, but
    # we don't have a reference to the script's AlertManager here. Instead,
    # we try to read the persistent state file and count active alerts.
    persistent_state = get_config('paths.renogy_persistent_state')
    if 'waterflow' in script_name.lower():
        persistent_state = get_config('paths.waterflow_persistent_state',
                                      persistent_state)
    if persistent_state and os.path.exists(persistent_state):
        try:
            with open(persistent_state, 'r') as f:
                state_data = json.load(f)
            active = [k for k, v in state_data.items() if v.get('active', False)]
            if active:
                record("Previous alerts", "WARN",
                       f"Restored {len(active)} active alert(s) from previous session: "
                       f"{', '.join(active)}")
            else:
                record("Previous alerts", "PASS", "no active alerts from previous session")
        except Exception as e:
            record("Previous alerts", "WARN", f"could not read previous state: {e}")
    else:
        record("Previous alerts", "INFO", "no persistent state file found (first run)")

    # -----------------------------------------------------------------------
    # 9. Script-specific checks
    # -----------------------------------------------------------------------
    if extra_checks:
        for check_name, check_fn in extra_checks:
            try:
                status, detail = check_fn()
                record(check_name, status, detail)
            except Exception as e:
                record(check_name, "FAIL", f"check raised exception: {e}")

    # -----------------------------------------------------------------------
    # Build and send startup email
    # -----------------------------------------------------------------------
    n_pass  = sum(1 for _, s, _ in results if s == 'PASS')
    n_warn  = sum(1 for _, s, _ in results if s == 'WARN')
    n_fail  = sum(1 for _, s, _ in results if s == 'FAIL')

    if n_fail > 0:
        subject = f"System DEGRADED: {script_name} — {n_fail} failure(s)"
    elif n_warn > 0:
        subject = f"System Online: {script_name} ⚠ {n_warn} warning(s)"
    else:
        subject = f"System Online: {script_name}"

    lines = [
        f"Startup self-test for {script_name}",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Host: {os.uname().nodename}",
        f"",
        f"Results: {n_pass} PASS  {n_warn} WARN  {n_fail} FAIL",
        f"",
    ]
    for name, status, detail in results:
        line = f"  [{status:<4}] {name}"
        if detail:
            line += f" — {detail}"
        lines.append(line)

    body = "\n".join(lines)
    send_email(subject, body)

    passed = (n_fail == 0)
    return passed, results
