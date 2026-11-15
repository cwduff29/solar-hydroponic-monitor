#!/usr/bin/env python3
"""Backup pump test scheduling and grading (pump_test.py)."""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import pump_test as pt


def local(y, mo, d, h, mi=0):
    """Epoch seconds for a local wall-clock time (independent of the test machine's TZ)."""
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


CFG = pt.config({})
OK = dict(uptime_s=7200, backup_active=False, main_flow_ok=True,
          flow_alert_active=False, soc=90.0, soc_confidence=0.9)


def decide(now, state, **over):
    kw = dict(OK)
    kw.update(over)
    return pt.decide(now, state, CFG, **kw)


class TestSchedule(unittest.TestCase):
    def test_never_tested_runs_at_next_window(self):
        st = pt.new_state(local(2026, 11, 13, 21))
        self.assertEqual(decide(local(2026, 11, 13, 21), st)[0], None)   # evening: outside window
        self.assertEqual(decide(local(2026, 11, 14, 9, 59), st)[0], None)
        self.assertEqual(decide(local(2026, 11, 14, 10, 0), st)[0], pt.ACTION_TEST)
        self.assertEqual(decide(local(2026, 11, 14, 14, 59), st)[0], pt.ACTION_TEST)
        self.assertEqual(decide(local(2026, 11, 14, 15, 0), st)[0], None)

    def test_weekly_counts_calendar_days(self):
        st = pt.new_state(0)
        st['last_test_ts'] = local(2026, 11, 14, 14, 50)
        self.assertEqual(decide(local(2026, 11, 20, 10), st)[0], None)          # 6 days
        self.assertEqual(decide(local(2026, 11, 21, 10), st)[0], pt.ACTION_TEST)  # same weekday, window start

    def test_sub_day_interval_uses_seconds(self):
        cfg = pt.config({'test_interval_seconds': 3600})
        self.assertFalse(pt.interval_due(1000.0 + 3599, 1000.0, cfg['test_interval_seconds']))
        self.assertTrue(pt.interval_due(1000.0 + 3600, 1000.0, cfg['test_interval_seconds']))

    def test_preconditions_block_and_test_stays_due(self):
        st = pt.new_state(0)
        now = local(2026, 11, 14, 11)
        for over, why in [({'uptime_s': 600}, 'uptime'),
                          ({'main_flow_ok': False}, 'flow not healthy'),
                          ({'flow_alert_active': True}, 'flow alert'),
                          ({'soc': None}, 'battery data unavailable'),
                          ({'soc': 49.0}, 'SOC 49%'),
                          ({'soc_confidence': 0.1}, 'not confident'),
                          ({'soc_confidence': None}, 'not confident')]:
            action, reason = decide(now, st, **over)
            self.assertIsNone(action, over)
            self.assertIn(why, reason)
        # Later the same day, with conditions fine, it runs
        self.assertEqual(decide(local(2026, 11, 14, 13), st)[0], pt.ACTION_TEST)

    def test_retest_ignores_window_but_not_preconditions(self):
        st = pt.new_state(0)
        t0 = local(2026, 11, 14, 14, 30)
        self.assertFalse(pt.record_test(st, t0, pt.ACTION_TEST, pt.RESULT_FAIL, 0.0, CFG))
        self.assertEqual(st['retest_due_ts'], t0 + 3600)
        self.assertEqual(decide(t0 + 1800, st)[0], None)
        self.assertEqual(decide(t0 + 3600, st)[0], pt.ACTION_RETEST)      # 15:30, outside window
        self.assertEqual(decide(t0 + 3600, st, soc=20.0)[0], None)

    def test_failover_rechecks_main_daily_in_window(self):
        st = pt.new_state(0)
        t_fail = local(2026, 11, 14, 3)
        pt.set_failover(st, True, t_fail)
        self.assertTrue(st['backup_pump_active'])
        self.assertEqual(decide(local(2026, 11, 14, 11), st, backup_active=True)[0], None)  # same day
        self.assertEqual(decide(local(2026, 11, 15, 9), st, backup_active=True)[0], None)   # before window
        # Recheck needs no battery or flow preconditions (the main pump failed; backup carries flow)
        self.assertEqual(decide(local(2026, 11, 15, 10), st, backup_active=True,
                                main_flow_ok=False, soc=None)[0], pt.ACTION_RECHECK)

    def test_entering_failover_drops_pending_retest(self):
        st = pt.new_state(0)
        st['retest_due_ts'] = 123.0
        pt.set_failover(st, True, 1000.0)
        self.assertIsNone(st['retest_due_ts'])


class TestGrade(unittest.TestCase):
    def test_fail_warn_pass(self):
        st = pt.new_state(0)
        self.assertEqual(pt.grade(0.1, st, CFG, 0.25), pt.RESULT_FAIL)
        self.assertEqual(pt.grade(1.0, st, CFG, 0.25), pt.RESULT_PASS)   # no baseline yet
        st['good_backup_flow_lpm'] = 2.0
        self.assertEqual(pt.grade(0.9, st, CFG, 0.25), pt.RESULT_WARN)
        self.assertEqual(pt.grade(1.0, st, CFG, 0.25), pt.RESULT_PASS)

    def test_only_pass_moves_baseline(self):
        st = pt.new_state(0)
        self.assertTrue(pt.record_test(st, 10.0, pt.ACTION_TEST, pt.RESULT_PASS, 2.0, CFG))
        self.assertEqual(st['good_backup_flow_lpm'], 2.0)
        self.assertTrue(pt.record_test(st, 20.0, pt.ACTION_TEST, pt.RESULT_WARN, 0.8, CFG))
        self.assertEqual(st['good_backup_flow_lpm'], 2.0)
        self.assertEqual(st['last_backup_flow_lpm'], 0.8)
        self.assertEqual(st['last_result'], pt.RESULT_WARN)

    def test_second_fail_is_final(self):
        st = pt.new_state(0)
        self.assertFalse(pt.record_test(st, 10.0, pt.ACTION_TEST, pt.RESULT_FAIL, 0.0, CFG))
        self.assertTrue(pt.record_test(st, 3610.0, pt.ACTION_RETEST, pt.RESULT_FAIL, 0.0, CFG))
        self.assertIsNone(st['retest_due_ts'])
        self.assertEqual(st['last_test_ts'], 3610.0)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='pump_test_')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = os.path.join(self.d, 'sub', 'pump_test_state.json')

    def test_missing_file_gives_fresh_state(self):
        st = pt.load_state(self.p, 1234.0)
        self.assertEqual(st['created_ts'], 1234.0)
        self.assertIsNone(st['last_test_ts'])
        self.assertFalse(st['backup_pump_active'])

    def test_round_trip(self):
        st = pt.new_state(5.0)
        pt.set_failover(st, True, 50.0)
        pt.save_state(self.p, st)
        back = pt.load_state(self.p, 999.0)
        self.assertEqual(back, st)
        self.assertFalse(os.path.exists(self.p + '.tmp'))

    def test_corrupt_or_foreign_file(self):
        os.makedirs(os.path.dirname(self.p))
        with open(self.p, 'w') as f:
            f.write('{not json')
        self.assertIsNone(pt.load_state(self.p, 1.0)['last_test_ts'])
        # An AlertManager file (the old shared path) has none of our keys
        with open(self.p, 'w') as f:
            json.dump({'low_flow': {'active': False}}, f)
        st = pt.load_state(self.p, 1.0)
        self.assertEqual(st, pt.new_state(1.0))

    def test_config_merge(self):
        cfg = pt.config({'test_interval_seconds': 100, 'test_window_hours': None, 'bogus': 1})
        self.assertEqual(cfg['test_interval_seconds'], 100)
        self.assertEqual(cfg['test_window_hours'], [10, 15])
        self.assertNotIn('bogus', cfg)


if __name__ == '__main__':
    unittest.main()
