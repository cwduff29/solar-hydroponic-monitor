#!/usr/bin/env python3
"""Tests for the low-SOC main pump tiers: the pure state machine, and the
waterflow integration (planned OFF windows must not trigger failover, but a
pump that fails to restart must). No hardware needed."""

import copy
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import load_shedding as ls  # noqa: E402
from load_shedding import MainPumpLowSoc, PUMP_NORMAL, PUMP_REDUCED, PUMP_CYCLING  # noqa: E402

BOTH = {'reduced_speed': {'enabled': True}, 'cycling': {'enabled': True}}


def drive(sm, cfg, soc, seconds, step=10, t0=0, **kw):
    """Run the state machine; returns list of (t, mode, run, duty)."""
    out = []
    t = t0
    while t < t0 + seconds:
        out.append((t,) + sm.update(t, cfg, soc, kw.get('confidence', 0.9),
                                    water_temp_c=kw.get('water_temp_c', 18.0),
                                    flow_alert_active=kw.get('flow_alert_active', False),
                                    backup_active=kw.get('backup_active', False)))
        t += step
    return out


def longest_off(trace, step=10):
    best = cur = 0
    for _, _, run, _ in trace:
        cur = 0 if run else cur + step
        best = max(best, cur)
    return best


class TestStateMachine(unittest.TestCase):
    def test_disabled_by_default(self):
        trace = drive(MainPumpLowSoc(), {}, 5, 7200)
        self.assertTrue(all(m == PUMP_NORMAL and run and d is None for _, m, run, d in trace))

    def test_reduced_speed_with_hysteresis(self):
        sm, cfg = MainPumpLowSoc(), {'reduced_speed': {'enabled': True}}
        self.assertEqual(sm.update(0, cfg, 41, 0.9), (PUMP_NORMAL, True, None))
        self.assertEqual(sm.update(1, cfg, 39, 0.9), (PUMP_REDUCED, True, 55))
        self.assertEqual(sm.update(2, cfg, 49, 0.9)[0], PUMP_REDUCED)
        self.assertEqual(sm.update(3, cfg, 51, 0.9)[0], PUMP_NORMAL)

    def test_cycling_starts_on_and_respects_windows(self):
        trace = drive(MainPumpLowSoc(), BOTH, 15, 4 * 3600)
        self.assertTrue(trace[0][2])                       # starts ON
        self.assertEqual(longest_off(trace), 15 * 60)
        self.assertTrue(all(d == 55 for _, _, run, d in trace if run))
        on_fraction = sum(1 for _, _, run, _ in trace if run) / len(trace)
        self.assertAlmostEqual(on_fraction, 0.5, delta=0.05)

    def test_off_window_hard_capped(self):
        cfg = {'cycling': {'enabled': True, 'off_minutes': 120, 'max_off_minutes': 90}}
        trace = drive(MainPumpLowSoc(), cfg, 10, 6 * 3600)
        self.assertEqual(longest_off(trace), ls.HARD_MAX_OFF_MINUTES * 60)

    def test_cycling_skipped_when_unsafe(self):
        for kw in ({'water_temp_c': 23.0}, {'water_temp_c': None},
                   {'flow_alert_active': True}, {'confidence': 0.1},
                   {'backup_active': True}):
            with self.subTest(**kw):
                trace = drive(MainPumpLowSoc(), BOTH, 10, 3 * 3600, **kw)
                self.assertTrue(all(run for _, _, run, _ in trace))

    def test_unsafe_condition_ends_off_window_immediately(self):
        sm = MainPumpLowSoc()
        trace = drive(sm, BOTH, 10, 16 * 60)
        self.assertFalse(trace[-1][2])                      # in an OFF window
        mode, run, _ = sm.update(16 * 60, BOTH, 10, 0.9, water_temp_c=18.0, flow_alert_active=True)
        self.assertTrue(run)
        self.assertNotEqual(mode, PUMP_CYCLING)

    def test_soc_recovery_exits_cycling(self):
        sm = MainPumpLowSoc()
        drive(sm, BOTH, 10, 16 * 60)
        self.assertEqual(sm.update(1000, BOTH, 25, 0.9, water_temp_c=18.0)[0], PUMP_CYCLING)
        mode, run, _ = sm.update(1010, BOTH, 31, 0.9, water_temp_c=18.0)
        self.assertEqual((mode, run), (PUMP_REDUCED, True))


class TestWaterflowIntegration(unittest.TestCase):
    """Drive the real monitor_flow()/control_main_pump_low_soc() with fake GPIO."""

    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow, FakePWM
        cls.w = import_waterflow()
        cls.FakePWM = FakePWM

    def setUp(self):
        w = self.w
        self.clock = [1_000_000.0]
        patches = [
            mock.patch.object(w.time, 'time', lambda: self.clock[0]),
            mock.patch.object(w, 'read_battery_soc', lambda: 12.0),
            mock.patch.object(w, 'battery_soc_confidence', 0.9),
            mock.patch.object(w, 'last_water_temp_c', 18.0),
            mock.patch.object(w, 'MAIN_PUMP_PWM_ENABLED', True),
            mock.patch.object(w, '_main_pump_pwm', self.FakePWM(12, 1000)),
            mock.patch.object(w, 'DISABLE_FLOW_ALERTS', False),
            mock.patch.object(w, 'MAIN_PUMP_TARGET_FLOW', 0),
            mock.patch.object(w, 'MAIN_PUMP_LOW_SOC_CFG', ls.pump_config(copy.deepcopy(BOTH))),
            mock.patch.object(w, 'pump_low_soc', MainPumpLowSoc()),
            mock.patch.object(w, 'attempt_pump_recovery', mock.Mock()),
            mock.patch.object(w, 'aeration_state', False),
            mock.patch.object(w, 'last_aeration_toggle', 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('main_pump_planned_off', False), ('main_pump_planned_restart', None),
                            ('low_flow_start_time', None), ('recovery_attempted', False),
                            ('backup_pump_active', False), ('smoothed_flow_inlet', 0.0),
                            ('smoothed_flow_outlet', 0.0), ('flow_history_inlet', []),
                            ('flow_history_outlet', [])):
            setattr(w, name, value)
        for a in w._FLOW_ALERT_TYPES:
            w.alert_manager.clear(a)
        w.set_main_pump(True)

    def _simulate(self, seconds, pump_works_after=None):
        """10 s ticks. Flow follows the pump unless the clock passes pump_works_after."""
        w = self.w
        planned_off_seen = 0
        end = self.clock[0] + seconds
        while self.clock[0] < end:
            self.clock[0] += 10
            w.control_main_pump_low_soc()
            planned_off_seen += w.main_pump_planned_off
            broken = pump_works_after is not None and self.clock[0] > pump_works_after
            flowing = w.main_pump_duty_cycle > 0 and not broken
            w.count_inlet = 127 if flowing else 0          # ~1.7 L/min
            w.count_outlet = 120 if flowing else 0
            w.monitor_flow()
            if w.attempt_pump_recovery.called:
                break
        return planned_off_seen

    def test_planned_off_windows_never_trigger_recovery(self):
        off_ticks = self._simulate(4 * 3600)
        self.assertGreater(off_ticks, 0)
        self.w.attempt_pump_recovery.assert_not_called()
        self.assertIn(self._pwm_duty(), (0, 55))            # OFF window or reduced speed

    def test_pump_that_fails_to_restart_is_detected(self):
        start = self.clock[0]
        self._simulate(4 * 3600, pump_works_after=start + 10 * 60)
        self.w.attempt_pump_recovery.assert_called_once()
        elapsed = self.clock[0] - start
        # Worst case: one ON window + one OFF window + restart grace + warning delay + smoothing
        bound = (15 + 15) * 60 + 60 + self.w.FLOW_WARNING_DELAY + 120
        self.assertLessEqual(elapsed, bound)

    def test_backup_active_is_never_touched(self):
        w = self.w
        w.backup_pump_active = True
        duty_before = w.main_pump_duty_cycle
        for _ in range(200):
            self.clock[0] += 10
            w.control_main_pump_low_soc()
        self.assertEqual(w.main_pump_duty_cycle, duty_before)
        self.assertFalse(w.main_pump_planned_off)

    def _pwm_duty(self):
        return self.w._main_pump_pwm.duty


if __name__ == '__main__':
    unittest.main()
