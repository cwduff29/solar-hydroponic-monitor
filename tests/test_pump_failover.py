#!/usr/bin/env python3
"""Pump recovery, backup pump test and pump-state persistence
(waterflow_enhanced_failsafe.py), driven through the fake GPIO."""

import json
import os
import sys
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
                            ('last_pump_test', 0), ('_saved_pump_state', None)):
            setattr(w, name, value)
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
            self.assertTrue(json.load(f)['backup_pump_active'])   # failover is persisted at once

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

    def test_dead_backup_pump_fails_the_test(self):
        self.working = {'main'}
        self.w.test_backup_pump()
        self.assertEqual(self.emails, ['FAIL Backup Pump Test FAILED'])
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())

    def test_working_backup_pump_passes(self):
        self.w.test_backup_pump()
        self.assertEqual(self.emails, ['OK Backup Pump Test PASSED'])
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())

    def test_recheck_with_dead_main_stays_on_backup(self):
        w = self.w
        self.working = {'backup'}
        w.backup_pump_active = True
        w.set_main_pump(False)
        w.set_backup_pump(True)
        self.main_on_during_failover = 0
        w.test_backup_pump()
        self.assertEqual(self.emails, ['Main Pump Still Failing - Remaining on Backup'])
        self.assertTrue(w.backup_pump_active)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())

    def test_recheck_with_working_main_switches_back(self):
        w = self.w
        w.backup_pump_active = True
        w.set_main_pump(False)
        w.set_backup_pump(True)
        w.test_backup_pump()
        self.assertEqual(self.emails, ['Main Pump Recovered - Failover Cleared'])
        self.assertFalse(w.backup_pump_active)
        self.assertTrue(self.main_on())
        self.assertFalse(self.backup_on())
        with open(w.PUMP_STATE_FILE) as f:
            self.assertFalse(json.load(f)['backup_pump_active'])

    def test_test_is_not_started_during_a_low_flow_episode_or_without_soc(self):
        w = self.w
        w.last_pump_test = 1.0                    # long overdue
        with mock.patch.object(w, 'read_battery_soc', lambda: 90.0):
            self.assertTrue(w.should_run_pump_test())
            w.low_flow_start_time = w._now()
            self.assertFalse(w.should_run_pump_test())
            w.low_flow_start_time = None
            w.main_pump_planned_off = True
            self.assertFalse(w.should_run_pump_test())
            w.main_pump_planned_off = False
        with mock.patch.object(w, 'read_battery_soc', lambda: None):
            self.assertFalse(w.should_run_pump_test())


class TestPumpStatePersistence(PumpTestCase):
    def test_pump_state_has_its_own_file(self):
        w = self.w
        self.assertNotIn(w.PUMP_STATE_FILE, (w.STATE_FILE, w.PERSISTENT_STATE_FILE))

    def test_failover_survives_a_restart_and_alert_saves(self):
        w = self.w
        w.backup_pump_active, w.last_pump_test = True, 12345.0
        w.save_state()
        w.alert_manager.save()                    # used to overwrite the pump state
        w.backup_pump_active, w.last_pump_test, w._saved_pump_state = False, 0, None
        w.set_main_pump(True)
        w.set_backup_pump(False)
        w.load_state()                            # restart or reboot: same persistent file
        self.assertTrue(w.backup_pump_active)
        self.assertEqual(w.last_pump_test, 12345.0)
        self.assertFalse(self.main_on())
        self.assertTrue(self.backup_on())

    def test_first_run_starts_the_test_clock_and_keeps_it(self):
        w = self.w
        w.load_state()                            # no file yet
        first = w.last_pump_test
        self.assertGreater(first, 0)
        self.assertFalse(w.backup_pump_active)
        w.last_pump_test, w._saved_pump_state = 0, None
        w.load_state()                            # next boot
        self.assertEqual(w.last_pump_test, first)

    def test_unchanged_state_is_not_rewritten(self):
        w = self.w
        w.last_pump_test = 500.0
        w.save_state()
        with mock.patch.object(w.os, 'replace', side_effect=AssertionError('rewrote an unchanged file')):
            w.save_state()

    def test_corrupt_file_falls_back_to_main_pump(self):
        w = self.w
        os.makedirs(os.path.dirname(w.PUMP_STATE_FILE), exist_ok=True)
        with open(w.PUMP_STATE_FILE, 'w') as f:
            f.write('{"last_pump_te')
        w.load_state()
        self.assertFalse(w.backup_pump_active)
        self.assertGreater(w.last_pump_test, 0)


if __name__ == '__main__':
    unittest.main()
