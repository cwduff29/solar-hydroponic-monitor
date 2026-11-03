#!/usr/bin/env python3
"""renogy.py startup restore of the SOC estimator, against a fake controller:
a bad first counter read must not discard (or overwrite) the saved estimate."""

import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')


class FakeRover:
    counters = [(8064, 7551)]          # one entry consumed per poll; last one repeats

    def __init__(self, *a, **k):
        pass

    def batch_read(self):
        return True

    def __getattr__(self, name):
        return lambda *a, **k: 1.0

    def get_load_status(self):
        return {'is_on': True, 'brightness': 0, 'charging_state': 'deactivated'}

    def get_active_faults(self):
        return []

    def get_battery_state_of_charge(self):
        return 100

    def get_battery_voltage(self):
        return 13.2

    def get_solar_power(self):
        return 0

    def get_load_current(self):
        return 1.1

    def get_total_battery_over_discharges(self):
        return 6

    def _counter(self, i):
        c = FakeRover.counters[0] if len(FakeRover.counters) == 1 else FakeRover.counters.pop(0)
        FakeRover._last = c
        return c[i]

    def get_total_charging_ah(self):
        return self._counter(0)

    def get_total_discharging_ah(self):
        return FakeRover._last[1]


def load_renogy(tmp):
    """Execute renogy.py up to its main loop with all paths in tmp."""
    sys.path.insert(0, REPO)
    fake = types.ModuleType('renogy_extended')
    fake.RenogyRoverExtended = FakeRover
    sys.modules['renogy_extended'] = fake
    import monitor_common
    real_get = monitor_common.get_config
    overrides = {
        'paths.renogy_log': f'{tmp}/renogy.log',
        'paths.renogy_prom': f'{tmp}/R.prom',
        'paths.renogy_prom_tmp': f'{tmp}/R.tmp',
        'paths.renogy_alert_state': f'{tmp}/alerts.json',
        'paths.renogy_persistent_state': f'{tmp}/p/state.json',
        'renogy.soc_estimator.state_file': f'{tmp}/p/soc.json',
        'watchdog.enabled': False,
    }
    saved = (monitor_common.get_config, monitor_common.send_email, monitor_common.startup_selftest)
    monitor_common.get_config = lambda k, d=None: overrides.get(k, real_get(k, d))
    monitor_common.send_email = lambda *a, **k: True
    monitor_common.startup_selftest = lambda *a, **k: None
    try:
        src = open(os.path.join(REPO, 'renogy.py')).read()
        src = src.replace("DailySummary(state_file='/var/lib/renogy/renogy_summary_state.json')",
                          f"DailySummary(state_file='{tmp}/summary.json')")
        head = src.split('# MAIN LOOP')[0]
        g = {'__name__': 'renogy_under_test', '__file__': os.path.join(REPO, 'renogy.py')}
        exec(compile(head, 'renogy.py', 'exec'), g)
    finally:
        monitor_common.get_config, monitor_common.send_email, monitor_common.startup_selftest = saved
        logging.disable(logging.NOTSET)
    return g


class TestRenogySocRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='renogy_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(f'{self.tmp}/p')
        self.saved = {'soc_ah': 12.0, 'err_ah': 1.0, 'wall_t': time.time() - 120, 'counters': [8064, 7551]}
        with open(f'{self.tmp}/p/soc.json', 'w') as f:
            json.dump(self.saved, f)
        self.r = load_renogy(self.tmp)
        root = logging.getLogger()
        self.addCleanup(lambda: [h.close() for h in root.handlers[:]
                                 if getattr(h, 'baseFilename', '').startswith(self.tmp)])

    def poll(self):
        m = self.r['read_rover_metrics']()
        return m

    def test_bad_first_read_then_restore(self):
        FakeRover.counters = [(0, 0), (8064, 7551)]
        with self.assertLogs(level='WARNING') as logs:
            m1 = self.poll()
            # Retrying: register exported, no estimator diagnostics, saved file untouched
            self.assertEqual(m1['battery_soc_source'], 1)
            self.assertNotIn('battery_soc_confidence', m1)
            with open(f'{self.tmp}/p/soc.json') as f:
                self.assertEqual(json.load(f), self.saved)
            m2 = self.poll()
        self.assertEqual(m2['battery_soc_source'], 2)
        self.assertAlmostEqual(m2['battery_soc'], 24.0, delta=0.2)     # 12 Ah of 50
        text = '\n'.join(logs.output)
        self.assertIn('restore deferred', text)
        self.assertIn('bridged restart', text)
        self.assertNotIn('reseeding', text)

    def test_persistent_reset_reseeds(self):
        FakeRover.counters = [(5, 3)]
        with self.assertLogs(level='WARNING') as logs:
            for _ in range(3):
                m = self.poll()
        self.assertEqual(m['battery_soc_source'], 2)
        self.assertIn('reseeding', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
