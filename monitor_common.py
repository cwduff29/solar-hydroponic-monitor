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
    Send an email using credentials from credentials.py.

    Args:
        subject: Email subject line
        body: Email body text
        disabled: If True, suppress sending and log instead

    Returns:
        True on success, False on failure
    """
    if disabled:
        logging.info(f"[EMAILS DISABLED] Email suppressed: '{subject}'")
        return False

    try:
        import credentials as cr
        msg = EmailMessage()
        msg['From'] = cr.username
        msg['To'] = cr.recipients
        msg['Subject'] = subject
        msg.set_content(body)
        with smtplib.SMTP(cr.server, cr.port) as server:
            server.starttls()
            server.login(cr.username, cr.password)
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
