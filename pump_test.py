#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Backup pump test scheduling and grading (standard library only, no hardware).

waterflow_enhanced_failsafe.py runs the relays and takes the flow readings;
this module decides when a test is due, whether it may run now, and how to
grade the result. Its state lives in one small JSON file under
/var/lib/renogy, written only when something changes (about once a week).

Schedule:
- A routine test is due once test_interval_seconds have passed. Intervals of
  a day or more count local calendar days, so a weekly test lands on the same
  weekday instead of drifting later each week.
- It runs only inside test_window_hours (local time), at least
  min_uptime_seconds after boot, with healthy main-pump flow, no flow alert
  active and enough battery (SOC and estimator confidence). Otherwise it
  waits and tries again; a skipped test stays due, so it runs later that day
  or the next day.
- A first FAIL schedules one retest after retest_delay_seconds (an air lock
  can clear); only a second FAIL is final. Retests ignore the window.
- While the system runs on the backup pump (failover), routine tests stop and
  the main pump is rechecked every failover_recheck_interval_seconds instead.

Grading: FAIL below the low-flow threshold; WARN when the backup delivers
less than backup_degraded_ratio of its flow in the last passing test; PASS
otherwise. Only PASS updates the baseline, so a slowly weakening pump does
not drag its own reference down.
"""

import datetime
import json
import os
import time

DEFAULTS = {
    'test_interval_seconds': 604800,             # 7 days
    'test_window_hours': [10, 15],               # local time, start inclusive, end exclusive
    'failover_recheck_interval_seconds': 86400,  # recheck the main pump daily while on backup
    'retest_delay_seconds': 3600,
    'backup_degraded_ratio': 0.5,
    'min_uptime_seconds': 3600,
    'min_soc_pct': 50,
    'min_soc_confidence': 0.3,
}

RESULT_PASS = 'pass'
RESULT_WARN = 'warn'
RESULT_FAIL = 'fail'
RESULTS = (RESULT_PASS, RESULT_WARN, RESULT_FAIL)

ACTION_TEST = 'test'            # routine backup pump test
ACTION_RETEST = 'retest'        # retest after a first FAIL
ACTION_RECHECK = 'recheck_main' # failover: try the main pump again


def new_state(now):
    return {
        'created_ts': now,              # when tracking started (overdue clock before the first test)
        'last_test_ts': None,
        'last_result': None,
        'last_backup_flow_lpm': None,
        'good_backup_flow_lpm': None,   # backup flow in the last PASS (degradation baseline)
        'retest_due_ts': None,
        'last_recheck_ts': None,
        'backup_pump_active': False,    # failover state, restored after a reboot
    }


def load_state(path, now):
    """Return the saved state, or a fresh one if the file is missing or unreadable."""
    state = new_state(now)
    try:
        with open(path) as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            state.update({k: saved[k] for k in state if k in saved})
    except (OSError, ValueError):
        pass
    return state


def save_state(path, state):
    """Atomic write (tmp + fsync + rename). Raises OSError on failure."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def config(raw):
    """Merge raw config values (None = use default) over DEFAULTS."""
    cfg = dict(DEFAULTS)
    for k, v in (raw or {}).items():
        if k in cfg and v is not None:
            cfg[k] = v
    return cfg


def _local_date(ts):
    return datetime.date.fromtimestamp(ts)


def interval_due(now, last_ts, interval_s):
    """True if interval_s has passed since last_ts (None = never ran = due).

    Intervals of a day or more compare local calendar dates.
    """
    if last_ts is None:
        return True
    if interval_s < 86400:
        return now - last_ts >= interval_s
    days = max(1, int(round(interval_s / 86400.0)))
    return (_local_date(now) - _local_date(last_ts)).days >= days


def in_window(now, window):
    start, end = window
    return start <= time.localtime(now).tm_hour < end


def decide(now, state, cfg, uptime_s, backup_active, main_flow_ok,
           flow_alert_active, soc, soc_confidence):
    """Return (action, reason). action is None when nothing should run now."""
    if uptime_s < cfg['min_uptime_seconds']:
        return None, f"uptime {uptime_s / 60:.0f} min < {cfg['min_uptime_seconds'] / 60:.0f} min"

    if backup_active:
        if not interval_due(now, state.get('last_recheck_ts'),
                            cfg['failover_recheck_interval_seconds']):
            return None, 'main pump recheck not due'
        if not in_window(now, cfg['test_window_hours']):
            return None, 'outside test window'
        return ACTION_RECHECK, 'main pump recheck due (running on backup)'

    if state.get('retest_due_ts') is not None:
        if now < state['retest_due_ts']:
            return None, 'retest not due yet'
        action = ACTION_RETEST
    else:
        if not interval_due(now, state.get('last_test_ts'), cfg['test_interval_seconds']):
            return None, 'test not due'
        if not in_window(now, cfg['test_window_hours']):
            return None, 'outside test window'
        action = ACTION_TEST

    if not main_flow_ok:
        return None, 'main pump flow not healthy'
    if flow_alert_active:
        return None, 'a flow alert is active'
    if soc is None:
        return None, 'battery data unavailable'
    if soc < cfg['min_soc_pct']:
        return None, f"battery SOC {soc:.0f}% < {cfg['min_soc_pct']}%"
    if soc_confidence is None or soc_confidence < cfg['min_soc_confidence']:
        return None, 'battery SOC estimate not confident'
    return action, 'due'


def grade(backup_flow, state, cfg, min_flow):
    if backup_flow < min_flow:
        return RESULT_FAIL
    good = state.get('good_backup_flow_lpm')
    if good and backup_flow < cfg['backup_degraded_ratio'] * good:
        return RESULT_WARN
    return RESULT_PASS


def record_test(state, now, action, result, backup_flow, cfg):
    """Update state after a test or retest. Returns True when the result is final
    (a PASS/WARN, or a FAIL on the retest); a first FAIL schedules a retest."""
    state['last_test_ts'] = now
    state['last_result'] = result
    state['last_backup_flow_lpm'] = round(backup_flow, 3)
    if result == RESULT_PASS:
        state['good_backup_flow_lpm'] = round(backup_flow, 3)
    if result == RESULT_FAIL and action == ACTION_TEST:
        state['retest_due_ts'] = now + cfg['retest_delay_seconds']
        return False
    state['retest_due_ts'] = None
    return True


def set_failover(state, active, now):
    """Record entering or leaving failover. Entering starts the recheck clock,
    so the first main-pump recheck comes a full interval later."""
    state['backup_pump_active'] = bool(active)
    if active:
        state['last_recheck_ts'] = now
        state['retest_due_ts'] = None
