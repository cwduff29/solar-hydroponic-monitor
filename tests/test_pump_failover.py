#!/usr/bin/env python3
"""Pump recovery, backup pump test and pump-state persistence
(waterflow_enhanced_failsafe.py), driven through the fake GPIO."""

import json
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))


class PumpTestCase(unittest.TestCase):
    """Fake pumps: a pump delivers pulses during a measurement only if it is
    switched on and listed in self.working."""

    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow, FakePWM
        cls.w = import_waterflow()
        cls.FakePWM = FakePWM
        cls.gpio = sys.modules['RPi.GPIO']

    def setUp(self):
        w = self.w
        self.working = {'main', 'backup'}
        self.emails = []
        self.main_on_during_failover = 0
        real_set_main = w.set_main_pump

        def set_main(state, duty_cycle=None):
            if state and w.backup_pump_active:
                self.main_on_during_failover += 1
            real_set_main(state, duty_cycle)

        def fake_sleep(seconds):
            # The measurement sleep: deliver ~1.7 L/min from whichever working pump is on
            if seconds == w.FLOW_MEASUREMENT_DURATION + 1:
                flowing = ((self.main_on() and 'main' in self.working)
                           or (self.backup_on() and 'backup' in self.working))
                w.count_inlet = w.count_outlet = 127 if flowing else 0

        patches = [
            mock.patch.object(w.time, 'sleep', fake_sleep),
            mock.patch.object(w, 'send_email_alert', lambda subj, body: self.emails.append(subj) or True),
            mock.patch.object(w, 'set_main_pump', set_main),
            mock.patch.object(w, 'MAIN_PUMP_PWM_ENABLED', True),
            mock.patch.object(w, '_main_pump_pwm', self.FakePWM(12, 1000)),
            mock.patch.object(w, 'DISABLE_FLOW_ALERTS', False),
            mock.patch.object(w, 'DISABLE_PUMP_TESTING', False),
            mock.patch.object(w, 'MAIN_PUMP_TARGET_FLOW', 0),
            mock.patch.object(w, 'aeration_state', False),
            mock.patch.object(w, 'last_aeration_toggle', -1e9),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._reset_state()
        self.addCleanup(self._reset_state)
        real_set_main(True)
        w.set_backup_pump(False)

    def _reset_state(self):
        w = self.w
        for name, value in (('recovery_attempted', False), ('backup_pump_active', False),
                            ('low_flow_start_time', None), ('low_flow_alert_sent', False),
                            ('leak_alert_sent', False), ('flow_imbalance_start_time', None),
                            ('main_pump_planned_off', False), ('main_pump_planned_restart', None),
                            ('smoothed_flow_inlet', 0.0), ('smoothed_flow_outlet', 0.0),
                            ('flow_history_inlet', []), ('flow_history_outlet', []),
                            ('_saved_pump_state', None)):
            setattr(w, name, value)
        w.pump_state = w.pump_test.new_state(0)
        w.alert_manager.forget('backup_pump_test_failed')
        for a in w._FLOW_ALERT_TYPES:
            w.alert_manager.forget(a)       # also drops the email cooldown of earlier tests
        if os.path.exists(w.PUMP_STATE_FILE):
            os.remove(w.PUMP_STATE_FILE)

    def main_on(self):
        return self.w.main_pump_duty_cycle > 0

    def backup_on(self):
        w = self.w
        return self.gpio.outputs.get(w.BACKUP_PUMP_RELAY_GPIO) == w.get_relay_state(True, w.BACKUP_PUMP_TERMINAL)

    def flow_cycle(self):
        """One main-loop flow window with whatever the pumps deliver right now."""
        w = self.w
        flowing = ((self.main_on() and 'main' in self.working)
                   or (self.backup_on() and 'backup' in self.working))
        w.count_inlet = w.count_outlet = 127 if flowing else 0
        w.monitor_flow()


class TestRecovery(PumpTestCase):
    def _sustained_low_flow(self):
        self.w.low_flow_start_time = self.w._now() - self.w.FLOW_WARNING_DELAY - 60

    def test_feed_sensor_stall_with_drain_flowing_is_not_low_flow(self):
        w = self.w
        self.working = {'main', 'backup'}
        self._sustained_low_flow()
        with mock.patch.object(w, 'attempt_pump_recovery') as recovery, \
                self.assertLogs(level='WARNING') as logs:
            for _ in range(8):
                w.count_inlet, w.count_outlet = 0, 127   # feed rotor stuck, water still draining
                w.monitor_flow()
        recovery.assert_not_called()
        self.assertTrue(self.main_on())
        self.assertFalse(w.backup_pump_active)
        self.assertIsNone(w.low_flow_start_time)        # the old episode was cleared
        self.assertNotIn('Warning: Low Water Flow', self.emails)
        self.assertTrue(any('feed sensor stall' in m for m in logs.output))

    def test_drain_sensor_dead_leaves_the_feed_sensor_in_charge(self):
        w = self.w
        self.working = {'backup'}
        self._sustained_low_flow()
        w.count_inlet = w.count_outlet = 0               # main dead, drain sensor also reads 0
        w.monitor_flow()
        self.assertTrue(w.backup_pump_active)

    def test_both_pumps_dead_recovers_once_then_alerts(self):
        w = self.w
        self.working = set()                      # e.g. empty reservoir
        self._sustained_low_flow()
        with mock.patch.object(w, 'attempt_pump_recovery', wraps=w.attempt_pump_recovery) as recovery:
            for _ in range(8):
                self.flow_cycle()
            cycling_runs = sum(1 for e in self.emails if 'Both Pumps Failed' in e)
        self.assertEqual(cycling_runs, 1)
        self.assertLessEqual(recovery.call_count, 2)         # the second call returns at once
        self.assertEqual(self.main_on_during_failover, 0)
        self.assertTrue(w.backup_pump_active)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())
        self.assertTrue(w.alert_manager.is_active('pump_recovery_failed'))
        self.assertTrue(w.alert_manager.is_active('low_flow'))
        self.assertIn('Warning: Low Water Flow', self.emails)

    def test_main_dead_fails_over_to_backup(self):
        w = self.w
        self.working = {'backup'}
        self._sustained_low_flow()
        self.flow_cycle()
        self.assertTrue(w.backup_pump_active)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())
        self.assertIn('Warning: Running on Backup Pump - Main Pump Failed', self.emails)
        with open(w.PUMP_STATE_FILE) as f:
            saved = json.load(f)
        self.assertTrue(saved['backup_pump_active'])              # failover is persisted at once
        self.assertIsNotNone(saved['last_recheck_ts'])            # and the recheck clock started

    def test_backup_failing_later_does_not_switch_main_on(self):
        w = self.w
        self.working = {'backup'}
        self._sustained_low_flow()
        self.flow_cycle()                         # fail over
        for _ in range(6):
            self.flow_cycle()                     # healthy on backup
        self.assertFalse(w.recovery_attempted)
        self.working = set()                      # backup dies too
        for _ in range(6):
            self.flow_cycle()
        self._sustained_low_flow()
        for _ in range(3):
            self.flow_cycle()
        self.assertEqual(self.main_on_during_failover, 0)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())
        self.assertTrue(w.alert_manager.is_active('low_flow'))

    def test_main_pump_cycle_that_restores_flow_stays_on_main(self):
        w = self.w
        self._sustained_low_flow()
        w.count_inlet = w.count_outlet = 0        # low reading triggers recovery; cycling fixes it
        w.monitor_flow()
        self.assertFalse(w.backup_pump_active)
        self.assertTrue(self.main_on())
        self.assertIn('Water Flow Restored - Main Pump Cycled', self.emails)


class TestBackupPumpTest(PumpTestCase):
    def setUp(self):
        super().setUp()
        self.w.smoothed_flow_inlet = 1.7          # main pump healthy before the test
        self.pt = self.w.pump_test

    def test_first_failure_schedules_a_retest(self):
        w = self.w
        self.working = {'main'}                   # backup pump dead
        w.run_pump_test(self.pt.ACTION_TEST)
        self.assertEqual(self.emails, ['Backup Pump Test FAILED - Retest in 60 min'])
        self.assertIsNotNone(w.pump_state['retest_due_ts'])
        self.assertFalse(w.alert_manager.is_active('backup_pump_test_failed'))
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())

    def test_failed_retest_is_final_and_raises_the_alert(self):
        w = self.w
        self.working = {'main'}
        w.run_pump_test(self.pt.ACTION_TEST)
        self.emails.clear()
        w.run_pump_test(self.pt.ACTION_RETEST)
        self.assertEqual(self.emails, ['FAIL Backup Pump Test FAILED'])
        self.assertIsNone(w.pump_state['retest_due_ts'])
        self.assertEqual(w.pump_state['last_result'], 'fail')
        self.assertTrue(w.alert_manager.is_active('backup_pump_test_failed'))
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())

    def test_passing_retest_clears_the_failure(self):
        w = self.w
        self.working = {'main'}
        w.run_pump_test(self.pt.ACTION_TEST)
        self.working = {'main', 'backup'}         # the air lock cleared
        self.emails.clear()
        w.run_pump_test(self.pt.ACTION_RETEST)
        self.assertEqual(self.emails, ['OK Backup Pump Test PASSED'])
        self.assertIsNone(w.pump_state['retest_due_ts'])
        self.assertFalse(w.alert_manager.is_active('backup_pump_test_failed'))

    def test_working_backup_pump_passes_and_sets_the_baseline(self):
        w = self.w
        w.run_pump_test(self.pt.ACTION_TEST)
        self.assertEqual(self.emails, ['OK Backup Pump Test PASSED'])
        self.assertEqual(w.pump_state['last_result'], 'pass')
        self.assertAlmostEqual(w.pump_state['good_backup_flow_lpm'],
                               127 / (w.FLOW_CALIBRATION_FACTOR_INLET * 10), places=2)
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())
        with open(w.PUMP_STATE_FILE) as f:
            self.assertEqual(json.load(f)['last_result'], 'pass')

    def test_much_weaker_backup_pump_warns(self):
        w = self.w
        w.pump_state['good_backup_flow_lpm'] = 5.0          # it used to deliver 5 L/min
        w.run_pump_test(self.pt.ACTION_TEST)                # now 1.7: under half
        self.assertEqual(self.emails, ['WARN Backup Pump Test: Flow Lower Than Before'])
        self.assertEqual(w.pump_state['last_result'], 'warn')
        self.assertEqual(w.pump_state['good_backup_flow_lpm'], 5.0)   # a WARN does not lower the baseline

    def test_result_is_exported(self):
        w = self.w
        w.run_pump_test(self.pt.ACTION_TEST)
        w.write_prometheus_metrics(temps={}, conditions={})
        with open(w.PROM_OUTPUT_FILE) as f:
            text = f.read()
        self.assertIn('waterflow_backup_pump_test_result{source="waterflow"} 0', text)
        expected = 127 / (w.FLOW_CALIBRATION_FACTOR_INLET * 10)
        self.assertIn('waterflow_backup_pump_test_flow_lpm{source="waterflow"} %.3f' % expected, text)

    def test_recheck_with_dead_main_stays_on_backup(self):
        w = self.w
        self.working = {'backup'}
        w.backup_pump_active = True
        w.set_main_pump(False)
        w.set_backup_pump(True)
        self.main_on_during_failover = 0
        w.run_pump_test(self.pt.ACTION_RECHECK)
        self.assertEqual(self.emails, ['Main Pump Still Failing - Remaining on Backup'])
        self.assertTrue(w.backup_pump_active)
        self.assertIsNotNone(w.pump_state['last_recheck_ts'])
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())

    def test_recheck_with_working_main_switches_back(self):
        w = self.w
        w.backup_pump_active = True
        w.set_main_pump(False)
        w.set_backup_pump(True)
        w.run_pump_test(self.pt.ACTION_RECHECK)
        self.assertEqual(self.emails, ['Main Pump Recovered - Failover Cleared'])
        self.assertFalse(w.backup_pump_active)
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())
        with open(w.PUMP_STATE_FILE) as f:
            self.assertFalse(json.load(f)['backup_pump_active'])


class TestPumpTestSchedule(PumpTestCase):
    """next_pump_test_action(): the controller's inputs to pump_test.decide()."""

    def setUp(self):
        super().setUp()
        w = self.w
        self.pt = w.pump_test
        self.wall = [time.mktime((2026, 11, 16, 11, 0, 0, 0, 0, -1))]      # 11:00 local, inside the window
        self.soc = [90.0]
        patches = [
            mock.patch.object(w.time, 'time', lambda: self.wall[0]),
            mock.patch.object(w, '_system_uptime_seconds', lambda: 7200.0),
            mock.patch.object(w, 'read_battery_soc', lambda: self.soc[0]),
            mock.patch.object(w, 'battery_soc_confidence', 0.9),
            mock.patch.object(w, 'PUMP_TEST_CFG', self.pt.config({})),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        w.smoothed_flow_inlet = 1.7

    def test_never_tested_runs_in_the_window(self):
        self.assertEqual(self.w.next_pump_test_action(), self.pt.ACTION_TEST)

    def test_not_outside_the_window(self):
        self.wall[0] = time.mktime((2026, 11, 16, 20, 0, 0, 0, 0, -1))
        self.assertIsNone(self.w.next_pump_test_action())

    def test_not_right_after_boot(self):
        with mock.patch.object(self.w, '_system_uptime_seconds', lambda: 120.0):
            self.assertIsNone(self.w.next_pump_test_action())

    def test_blocked_by_low_flow_planned_off_alert_or_battery(self):
        w = self.w
        w.low_flow_start_time = w._now()
        self.assertIsNone(w.next_pump_test_action())
        w.low_flow_start_time = None
        w.main_pump_planned_off = True
        self.assertIsNone(w.next_pump_test_action())
        w.main_pump_planned_off = False
        w.smoothed_flow_inlet = 0.1
        self.assertIsNone(w.next_pump_test_action())
        w.smoothed_flow_inlet = 1.7
        w.alert_manager.should_send('leak_detected')
        self.assertIsNone(w.next_pump_test_action())
        w.alert_manager.forget('leak_detected')
        for soc in (None, 30.0):
            self.soc[0] = soc
            self.assertIsNone(w.next_pump_test_action())
        self.soc[0] = 90.0
        self.assertEqual(w.next_pump_test_action(), self.pt.ACTION_TEST)     # still due once clear

    def test_maintenance_flag_stops_tests(self):
        with mock.patch.object(self.w, 'DISABLE_PUMP_TESTING', True):
            self.assertIsNone(self.w.next_pump_test_action())

    def test_weekly_after_a_pass_and_retest_after_a_fail(self):
        w = self.w
        w.run_pump_test(self.pt.ACTION_TEST)                    # passes at 11:00
        self.wall[0] += 3 * 86400
        self.assertIsNone(w.next_pump_test_action())
        self.wall[0] += 4 * 86400
        self.assertEqual(w.next_pump_test_action(), self.pt.ACTION_TEST)
        self.working = {'main'}
        w.run_pump_test(self.pt.ACTION_TEST)                    # fails
        self.assertIsNone(w.next_pump_test_action())
        self.wall[0] += 3600
        self.assertEqual(w.next_pump_test_action(), self.pt.ACTION_RETEST)

    def test_failover_rechecks_the_main_pump_a_day_later(self):
        w = self.w
        w.backup_pump_active = True
        self.pt.set_failover(w.pump_state, True, self.wall[0])
        self.assertIsNone(w.next_pump_test_action())            # recheck clock just started
        self.wall[0] += 86400
        self.assertEqual(w.next_pump_test_action(), self.pt.ACTION_RECHECK)

    def test_config_section_defaults_and_bad_window(self):
        w = self.w
        real = w.get_config

        def cfg_with(section, old_soc=None):
            values = {'waterflow.pump_test': section,
                      'waterflow.battery_load_shedding.skip_pump_test_below_pct': old_soc}
            return mock.patch.object(w, 'get_config', lambda k, d=None: values[k] if k in values else real(k, d))

        with cfg_with({}):                                      # the Pi's config before this change
            cfg = w._load_pump_test_config()
        self.assertEqual(cfg['test_interval_seconds'], 604800)
        self.assertEqual(cfg['test_window_hours'], [10, 15])
        with cfg_with({}, old_soc=40):                          # old SOC limit is still honoured
            self.assertEqual(w._load_pump_test_config()['min_soc_pct'], 40)
        with cfg_with({'min_soc_pct': 60, 'test_window_hours': [9, 17]}, old_soc=40):
            cfg = w._load_pump_test_config()
        self.assertEqual((cfg['min_soc_pct'], cfg['test_window_hours']), (60, [9, 17]))
        for bad in ([15, 10], [10], 'noon', [10, 30]):
            with cfg_with({'test_window_hours': bad}), self.assertLogs(level='ERROR'):
                self.assertEqual(w._load_pump_test_config()['test_window_hours'], [10, 15])


class TestPumpStatePersistence(PumpTestCase):
    def test_pump_state_has_its_own_file(self):
        w = self.w
        self.assertNotIn(w.PUMP_STATE_FILE, (w.STATE_FILE, w.PERSISTENT_STATE_FILE))

    def _restart(self):
        w = self.w
        w.backup_pump_active, w._saved_pump_state = False, None
        w.pump_state = w.pump_test.new_state(0)
        w.set_main_pump(True)
        w.set_backup_pump(False)
        w.load_state()

    def test_failover_survives_a_restart_and_alert_saves(self):
        w = self.w
        w.backup_pump_active = True
        w.pump_test.set_failover(w.pump_state, True, 12345.0)
        w.save_state()
        w.alert_manager.save()                    # used to overwrite the pump state
        self._restart()
        self.assertTrue(w.backup_pump_active)
        self.assertEqual(w.pump_state['last_recheck_ts'], 12345.0)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())

    def test_test_history_survives_a_restart(self):
        w = self.w
        w.smoothed_flow_inlet = 1.7
        w.run_pump_test(w.pump_test.ACTION_TEST)
        before = dict(w.pump_state)
        self._restart()
        self.assertEqual(w.pump_state, before)
        self.assertEqual(w.pump_state['last_result'], 'pass')

    def test_first_run_is_untested_on_the_main_pump(self):
        w = self.w
        self._restart()                           # no file yet
        self.assertFalse(w.backup_pump_active)
        self.assertIsNone(w.pump_state['last_test_ts'])
        self.assertTrue(os.path.exists(w.PUMP_STATE_FILE))

    def test_older_state_file_keeps_the_failover_flag(self):
        w = self.w
        os.makedirs(os.path.dirname(w.PUMP_STATE_FILE), exist_ok=True)
        with open(w.PUMP_STATE_FILE, 'w') as f:
            json.dump({'last_pump_test': 1794694851.3, 'backup_pump_active': True, 'last_save': 1.0}, f)
        self._restart()
        self.assertTrue(w.backup_pump_active)
        self.assertTrue(self.backup_on())
        self.assertIsNone(w.pump_state['last_test_ts'])       # that timestamp was not a real test
        with open(w.PUMP_STATE_FILE) as f:
            saved = json.load(f)
        self.assertIn('created_ts', saved)                    # rewritten in the new format
        self.assertNotIn('last_pump_test', saved)

    def test_unchanged_state_is_not_rewritten(self):
        w = self.w
        w.save_state()
        with mock.patch.object(w.pump_test, 'save_state', side_effect=AssertionError('rewrote an unchanged file')):
            w.save_state()

    def test_corrupt_file_falls_back_to_main_pump(self):
        w = self.w
        os.makedirs(os.path.dirname(w.PUMP_STATE_FILE), exist_ok=True)
        with open(w.PUMP_STATE_FILE, 'w') as f:
            f.write('{"backup_pump_act')
        with self.assertLogs(level='ERROR'):
            self._restart()
        self.assertFalse(w.backup_pump_active)
        self.assertTrue(self.main_on())


if __name__ == '__main__':
    unittest.main()
