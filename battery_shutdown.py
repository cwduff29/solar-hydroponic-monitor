#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
battery_shutdown.py - Standalone battery critical SOC shutdown monitor

Runs as root via its own systemd service. No dependency on monitor_common or
any heavy third-party libraries — stdlib only.

Logic:
- Polls /ramdisk/Renogy.prom every 30 seconds
- Parses battery_soc{source="renogy"} from the prom file
- If SOC <= critical_soc_pct for >= sustained_seconds continuously → shutdown
- If SOC recovers above recovery_soc_pct, reset the sustained timer
- If prom file is missing or stale (>120s old), warns but does NOT shut down
- Before shutting down: logs event, sends email alert, stops services, halts
"""

import os
import sys
import json
import time
import logging
import smtplib
import subprocess
from datetime import datetime
from email.message import EmailMessage

# ============================================================================
# DEFAULTS (used if config.json can't be read)
# ============================================================================

DEFAULT_CRITICAL_SOC_PCT   = 5
DEFAULT_SUSTAINED_SECONDS  = 120
DEFAULT_RECOVERY_SOC_PCT   = 15
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
    return {
        'critical_soc_pct':   sd.get('critical_soc_pct',   DEFAULT_CRITICAL_SOC_PCT),
        'sustained_seconds':  sd.get('sustained_seconds',  DEFAULT_SUSTAINED_SECONDS),
        'recovery_soc_pct':   sd.get('recovery_soc_pct',   DEFAULT_RECOVERY_SOC_PCT),
        'prom_file':          paths.get('renogy_prom',      DEFAULT_PROM_FILE),
        'log_file':           paths.get('renogy_log',       DEFAULT_LOG_FILE),
    }

# ============================================================================
# PROMETHEUS PARSING
# ============================================================================

def parse_battery_soc(prom_file):
    """
    Parse battery_soc{source="renogy"} from a Prometheus text file.

    Returns (soc_value: float, file_age_seconds: float) or (None, None) on failure.
    """
    try:
        stat = os.stat(prom_file)
        file_age = time.time() - stat.st_mtime
    except OSError:
        return None, None

    try:
        with open(prom_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line.startswith('#') or not line:
                    continue
                # Match: battery_soc{source="renogy"} <value>
                if line.startswith('battery_soc{') and 'source="renogy"' in line:
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return float(parts[-1]), file_age
                        except ValueError:
                            pass
    except Exception as e:
        logging.warning(f"battery_shutdown: error reading prom file {prom_file}: {e}")

    return None, file_age

# ============================================================================
# EMAIL ALERT (stdlib only)
# ============================================================================

def send_shutdown_alert(soc, cfg):
    """Send a critical shutdown email using SMTP credentials from env vars."""
    smtp_server     = os.environ.get('SMTP_SERVER',     'smtp.gmail.com')
    smtp_port       = int(os.environ.get('SMTP_PORT',   '587'))
    smtp_user       = os.environ.get('SMTP_USER',       '')
    smtp_password   = os.environ.get('SMTP_PASSWORD',   '')
    smtp_recipients = os.environ.get('SMTP_RECIPIENTS', '')

    if not smtp_user or not smtp_password:
        logging.warning("battery_shutdown: email credentials missing, skipping alert email")
        return False

    subject = f"CRITICAL: Battery SOC {soc:.0f}% — System Shutting Down"
    body = (
        f"CRITICAL: Solar battery SOC has been at or below "
        f"{cfg['critical_soc_pct']}% for {cfg['sustained_seconds']} seconds.\n\n"
        f"Current SOC: {soc:.1f}%\n"
        f"Threshold:   {cfg['critical_soc_pct']}%\n"
        f"Sustained:   {cfg['sustained_seconds']}s\n"
        f"Time:        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"Host:        {os.uname().nodename}\n\n"
        f"The system is shutting down to protect the battery.\n"
        f"It will restart when solar charging restores sufficient charge.\n"
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

    config = load_config(config_path)
    cfg = get_shutdown_config(config)

    # Add file handler now that we have the log path
    try:
        file_handler = logging.FileHandler(cfg['log_file'])
        file_handler.setFormatter(
            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        )
        logging.getLogger().addHandler(file_handler)
    except Exception as e:
        logging.warning(f"battery_shutdown: could not open log file {cfg['log_file']}: {e}")

    logging.warning(
        f"battery_shutdown: started. "
        f"critical={cfg['critical_soc_pct']}% sustained={cfg['sustained_seconds']}s "
        f"recovery={cfg['recovery_soc_pct']}% "
        f"prom_file={cfg['prom_file']} poll=30s"
    )

    # State tracking
    critical_since = None   # timestamp when SOC first went critical

    while True:
        try:
            soc, file_age = parse_battery_soc(cfg['prom_file'])

            # --- Missing or stale prom file: warn, skip, do NOT shut down ---
            if soc is None:
                logging.warning(
                    f"battery_shutdown: prom file '{cfg['prom_file']}' not found or unreadable; "
                    f"skipping cycle"
                )
                critical_since = None
                time.sleep(DEFAULT_POLL_INTERVAL)
                continue

            if file_age is not None and file_age > DEFAULT_PROM_STALE_SECONDS:
                logging.warning(
                    f"battery_shutdown: prom file is stale ({file_age:.0f}s old, "
                    f"limit={DEFAULT_PROM_STALE_SECONDS}s); skipping cycle"
                )
                critical_since = None
                time.sleep(DEFAULT_POLL_INTERVAL)
                continue

            # --- Recovery: reset sustained timer ---
            if soc > cfg['recovery_soc_pct']:
                if critical_since is not None:
                    logging.warning(
                        f"battery_shutdown: SOC recovered to {soc:.1f}% "
                        f"(above recovery threshold {cfg['recovery_soc_pct']}%); "
                        f"resetting sustained timer"
                    )
                    critical_since = None
                # Normal operation; nothing more to do
                time.sleep(DEFAULT_POLL_INTERVAL)
                continue

            # --- SOC at or below critical threshold ---
            if soc <= cfg['critical_soc_pct']:
                now = time.time()
                if critical_since is None:
                    critical_since = now
                    logging.warning(
                        f"battery_shutdown: SOC {soc:.1f}% at or below critical "
                        f"threshold {cfg['critical_soc_pct']}%; starting sustained timer"
                    )
                else:
                    elapsed = now - critical_since
                    logging.warning(
                        f"battery_shutdown: SOC {soc:.1f}% still critical "
                        f"({elapsed:.0f}s / {cfg['sustained_seconds']}s)"
                    )
                    if elapsed >= cfg['sustained_seconds']:
                        perform_shutdown(soc, cfg)
                        # If shutdown command fails, we exit and let systemd restart us
                        sys.exit(0)
            else:
                # SOC is between critical and recovery thresholds
                if critical_since is not None:
                    logging.warning(
                        f"battery_shutdown: SOC {soc:.1f}% between critical "
                        f"({cfg['critical_soc_pct']}%) and recovery "
                        f"({cfg['recovery_soc_pct']}%); sustained timer continues"
                    )

        except Exception as e:
            logging.error(f"battery_shutdown: unexpected error in main loop: {e}")

        time.sleep(DEFAULT_POLL_INTERVAL)


if __name__ == '__main__':
    main()
