#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
battery_shutdown.py - Standalone battery critical-condition monitor

Runs as root via its own systemd service. No dependency on monitor_common or
any heavy third-party libraries — stdlib only.

shutdown.mode in config.json:
- "alert" (default): when the critical condition holds, email once and export
  battery_shutdown_condition=1; never halts.
- "halt": same condition halts the Pi (stops services, shutdown -h now).
- "off": evaluate nothing.

Critical condition (all must hold for sustained_seconds):
- estimated battery_soc <= critical_soc_pct
- battery_soc_confidence >= min_confidence (the old hardware SOC register
  alone can never trigger it)
- rest-equivalent battery voltage <= max_rest_voltage

Why "alert" is the default: everything (pumps, Pi, sensors) runs from the
Renogy load terminal, whose low-voltage disconnect is the real battery
protection. A halted Pi stops the PWM main pump, releases the NC backup pump
(full power, no monitoring), and nothing restarts it unless the LVD cycles.

Polls /ramdisk/Renogy.prom every 30 s. A missing or stale prom file never
triggers anything. SIGHUP reloads config.json.
"""

import os
import sys
import json
import time
import logging
import smtplib
import signal
import subprocess
from datetime import datetime
from email.message import EmailMessage

# ============================================================================
# DEFAULTS (used if config.json can't be read)
# ============================================================================

DEFAULT_MODE               = 'alert'
DEFAULT_CRITICAL_SOC_PCT   = 5
DEFAULT_SUSTAINED_SECONDS  = 600
DEFAULT_RECOVERY_SOC_PCT   = 15
DEFAULT_MIN_CONFIDENCE     = 0.5
DEFAULT_MAX_REST_VOLTAGE   = 12.0
DEFAULT_METRICS_FILE       = '/ramdisk/battery_shutdown.prom'
MODES                      = ('off', 'alert', 'halt')
DEFAULT_POLL_INTERVAL      = 30
DEFAULT_PROM_FILE          = '/ramdisk/Renogy.prom'
DEFAULT_PROM_STALE_SECONDS = 120
DEFAULT_LOG_FILE           = '/var/log/renogy.log'

# ============================================================================
# CONFIG LOADING
# ============================================================================

def load_config(path):
    """Load JSON config. Returns dict with shutdown section, or empty dict on failure."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"battery_shutdown: could not read config from {path}: {e}")
        return {}


def get_shutdown_config(config):
    """Extract shutdown parameters from config dict, falling back to defaults."""
    sd = config.get('shutdown', {})
    paths = config.get('paths', {})
    mode = sd.get('mode', DEFAULT_MODE)
    if mode not in MODES:
        logging.error(f"battery_shutdown: unknown shutdown.mode '{mode}', using 'alert'")
        mode = 'alert'
    return {
        'mode':               mode,
        'critical_soc_pct':   sd.get('critical_soc_pct',   DEFAULT_CRITICAL_SOC_PCT),
        'sustained_seconds':  sd.get('sustained_seconds',  DEFAULT_SUSTAINED_SECONDS),
        'recovery_soc_pct':   sd.get('recovery_soc_pct',   DEFAULT_RECOVERY_SOC_PCT),
        'min_confidence':     sd.get('min_confidence',     DEFAULT_MIN_CONFIDENCE),
        'max_rest_voltage':   sd.get('max_rest_voltage',   DEFAULT_MAX_REST_VOLTAGE),
        'prom_file':          paths.get('renogy_prom',      DEFAULT_PROM_FILE),
        'metrics_file':       paths.get('battery_shutdown_prom', DEFAULT_METRICS_FILE),
        'log_file':           paths.get('renogy_log',       DEFAULT_LOG_FILE),
    }

# ============================================================================
# PROMETHEUS PARSING
# ============================================================================

_WANTED_METRICS = (
    'battery_soc', 'battery_soc_confidence', 'battery_voltage_rest_estimate', 'battery_voltage',
)


def parse_battery_metrics(prom_file):
    """
    Parse the battery metrics this monitor needs from a Prometheus text file.

    Returns (values: dict, file_age_seconds: float). values maps each name in
    _WANTED_METRICS that was found to a float; ({}, None) if the file is missing.
    """
    try:
        stat = os.stat(prom_file)
        file_age = time.time() - stat.st_mtime
    except OSError:
        return {}, None

    values = {}
    try:
        with open(prom_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                name = line.split('{', 1)[0]
                if name in _WANTED_METRICS and 'source="renogy"' in line:
                    try:
                        values[name] = float(line.split()[-1])
                    except ValueError:
                        pass
    except Exception as e:
        logging.warning(f"battery_shutdown: error reading prom file {prom_file}: {e}")

    return values, file_age


def critical_condition(values, cfg):
    """True if the instantaneous critical condition holds (see module docstring)."""
    soc = values.get('battery_soc')
    conf = values.get('battery_soc_confidence', 0.0)
    v_rest = values.get('battery_voltage_rest_estimate', values.get('battery_voltage'))
    return (soc is not None and soc <= cfg['critical_soc_pct']
            and conf >= cfg['min_confidence']
            and v_rest is not None and v_rest <= cfg['max_rest_voltage'])


def evaluate(state, values, file_age, cfg, now):
    """
    Advance the sustained-condition state machine for one poll.

    state: {'since': float|None, 'alerted': bool}, updated in place.
    Returns True when the condition has held for sustained_seconds.
    Missing/stale data resets the timer and never triggers.
    """
    if not values or file_age is None or file_age > DEFAULT_PROM_STALE_SECONDS:
        state['since'] = None
        return False
    if critical_condition(values, cfg):
        if state['since'] is None:
            state['since'] = now
        return now - state['since'] >= cfg['sustained_seconds']
    state['since'] = None
    soc = values.get('battery_soc')
    if soc is not None and soc > cfg['recovery_soc_pct']:
        state['alerted'] = False   # re-arm the alert email
    return False


def write_metrics(cfg, condition_active):
    """Export this monitor's state for the node_exporter textfile collector."""
    path = cfg['metrics_file']
    tmp = path + '.tmp'
    try:
        with open(tmp, 'w') as f:
            f.write('# HELP battery_shutdown_condition Critical battery condition held for sustained_seconds\n')
            f.write('# TYPE battery_shutdown_condition gauge\n')
            f.write(f'battery_shutdown_condition{{source="battery_shutdown"}} {1 if condition_active else 0}\n')
            f.write('# HELP battery_shutdown_mode Shutdown mode (0=off, 1=alert, 2=halt)\n')
            f.write('# TYPE battery_shutdown_mode gauge\n')
            f.write(f'battery_shutdown_mode{{source="battery_shutdown"}} {MODES.index(cfg["mode"])}\n')
        os.replace(tmp, path)
    except OSError as e:
        logging.warning(f"battery_shutdown: could not write {path}: {e}")

# ============================================================================
# EMAIL ALERT (stdlib only)
# ============================================================================

def send_shutdown_alert(soc, cfg, halting=True):
    """Send a critical battery email using SMTP credentials from env vars."""
    smtp_server     = os.environ.get('SMTP_SERVER',     'smtp.gmail.com')
    smtp_port       = int(os.environ.get('SMTP_PORT',   '587'))
    smtp_user       = os.environ.get('SMTP_USER',       '')
    smtp_password   = os.environ.get('SMTP_PASSWORD',   '')
    smtp_recipients = os.environ.get('SMTP_RECIPIENTS', '')

    if not smtp_user or not smtp_password:
        logging.warning("battery_shutdown: email credentials missing, skipping alert email")
        return False

    if halting:
        subject = f"CRITICAL: Battery SOC {soc:.0f}% — System Shutting Down"
        action = (
            "The Pi is halting (shutdown.mode = \"halt\"). The main pump stops and the\n"
            "NC backup pump and fan run unmonitored until the Pi is power-cycled\n"
            "(e.g. by the Renogy low-voltage disconnect/reconnect).\n"
        )
    else:
        subject = f"CRITICAL: Battery SOC {soc:.0f}% — Near Low-Voltage Disconnect"
        action = (
            "No action taken (shutdown.mode = \"alert\"). The Renogy load-terminal\n"
            "low-voltage disconnect will cut all loads, including the backup pump,\n"
            "if the battery keeps falling. Reduce load or charge the battery now.\n"
        )
    body = (
        f"CRITICAL: estimated battery SOC has been at or below "
        f"{cfg['critical_soc_pct']}% (with rest voltage <= {cfg['max_rest_voltage']}V and "
        f"estimator confidence >= {cfg['min_confidence']}) for {cfg['sustained_seconds']} seconds.\n\n"
        f"Current SOC: {soc:.1f}%\n"
        f"Time:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Host:        {os.uname().nodename}\n\n"
        + action
    )

    try:
        msg = EmailMessage()
        msg['From']    = smtp_user
        msg['To']      = smtp_recipients
        msg['Subject'] = subject
        msg.set_content(body)
        with smtplib.SMTP(smtp_server, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logging.warning(f"battery_shutdown: shutdown alert email sent to {smtp_recipients}")
        return True
    except Exception as e:
        logging.error(f"battery_shutdown: failed to send alert email: {e}")
        return False

# ============================================================================
# SHUTDOWN SEQUENCE
# ============================================================================

def perform_shutdown(soc, cfg):
    """
    Execute graceful shutdown:
    1. Log shutdown event
    2. Send email alert
    3. Stop renogy and waterflow services
    4. Halt the system
    """
    msg = (
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} CRITICAL: "
        f"battery_shutdown: Battery SOC {soc:.1f}% at or below "
        f"{cfg['critical_soc_pct']}% for {cfg['sustained_seconds']}s — "
        f"initiating graceful shutdown"
    )
    logging.critical(msg)

    # Also write directly to renogy log for persistence
    try:
        with open(cfg['log_file'], 'a') as f:
            f.write(msg + '\n')
    except Exception as e:
        logging.error(f"battery_shutdown: could not write to log file: {e}")

    # Try email alert (best effort)
    send_shutdown_alert(soc, cfg)

    # Stop dependent services gracefully
    for service in ('renogy', 'waterflow'):
        try:
            subprocess.run(
                ['systemctl', 'stop', service],
                timeout=15,
                check=False,
            )
            logging.warning(f"battery_shutdown: stopped service '{service}'")
        except Exception as e:
            logging.error(f"battery_shutdown: could not stop '{service}': {e}")

    # Brief pause to allow services to save state
    time.sleep(3)

    # Halt the system
    logging.warning("battery_shutdown: calling 'shutdown -h now'")
    try:
        subprocess.run(['shutdown', '-h', 'now'], check=False)
    except Exception as e:
        logging.critical(f"battery_shutdown: shutdown command failed: {e}")
        # Last resort
        try:
            subprocess.run(['halt', '-p'], check=False)
        except Exception:
            pass

# ============================================================================
# MAIN LOOP
# ============================================================================

def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else './config.json'

    # Set up logging early (to stderr / journal and log file)
    logging.basicConfig(
        level=logging.WARNING,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler()],
    )

    cfg = get_shutdown_config(load_config(config_path))

    # Add file handler now that we have the log path
    try:
        file_handler = logging.FileHandler(cfg['log_file'])
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logging.getLogger().addHandler(file_handler)
    except Exception as e:
        logging.warning(f"battery_shutdown: could not open log file {cfg['log_file']}: {e}")

    def log_settings(prefix):
        logging.warning(
            f"battery_shutdown: {prefix}. mode={cfg['mode']} "
            f"critical={cfg['critical_soc_pct']}% confidence>={cfg['min_confidence']} "
            f"rest_v<={cfg['max_rest_voltage']}V sustained={cfg['sustained_seconds']}s "
            f"recovery={cfg['recovery_soc_pct']}% prom_file={cfg['prom_file']} poll=30s"
        )

    reload_requested = []
    signal.signal(signal.SIGHUP, lambda signum, frame: reload_requested.append(True))
    log_settings("started")

    state = {'since': None, 'alerted': False}

    while True:
        try:
            if reload_requested:
                reload_requested.clear()
                cfg = get_shutdown_config(load_config(config_path))
                state['since'] = None
                log_settings("config reloaded")

            if cfg['mode'] == 'off':
                write_metrics(cfg, False)
                time.sleep(DEFAULT_POLL_INTERVAL)
                continue

            values, file_age = parse_battery_metrics(cfg['prom_file'])
            if not values:
                logging.warning(
                    f"battery_shutdown: prom file '{cfg['prom_file']}' not found or unreadable; "
                    f"skipping cycle"
                )
            elif file_age is not None and file_age > DEFAULT_PROM_STALE_SECONDS:
                logging.warning(
                    f"battery_shutdown: prom file is stale ({file_age:.0f}s old, "
                    f"limit={DEFAULT_PROM_STALE_SECONDS}s); skipping cycle"
                )

            was_timing = state['since'] is not None
            triggered = evaluate(state, values, file_age, cfg, time.time())
            if state['since'] is not None and not was_timing:
                logging.warning(
                    f"battery_shutdown: critical condition started (SOC {values.get('battery_soc')}%, "
                    f"confidence {values.get('battery_soc_confidence')}, "
                    f"rest V {values.get('battery_voltage_rest_estimate')}); sustained timer running"
                )
            elif was_timing and state['since'] is None:
                logging.warning("battery_shutdown: critical condition cleared; timer reset")
            write_metrics(cfg, triggered)

            if triggered:
                soc = values['battery_soc']
                if cfg['mode'] == 'halt':
                    perform_shutdown(soc, cfg)
                    # If shutdown command fails, we exit and let systemd restart us
                    sys.exit(0)
                if not state['alerted']:
                    logging.critical(
                        f"battery_shutdown: critical battery condition (SOC {soc:.1f}%) held for "
                        f"{cfg['sustained_seconds']}s; mode=alert, not halting"
                    )
                    send_shutdown_alert(soc, cfg, halting=False)
                    state['alerted'] = True   # once per episode, even if the email failed

        except Exception as e:
            logging.error(f"battery_shutdown: unexpected error in main loop: {e}")

        time.sleep(DEFAULT_POLL_INTERVAL)


if __name__ == '__main__':
    main()
