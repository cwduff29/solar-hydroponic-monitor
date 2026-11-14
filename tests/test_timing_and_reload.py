#!/usr/bin/env python3
"""Measured flow windows, cached temperature reads, clock-step-proof battery
data age, SIGHUP-reloadable settings and fail-fast config loading."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import monitor_common as mc


class WaterflowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def setUp(self):
        w = self.w
        self.clock = [50_000.0]
        patches = [
            mock.patch.object(w, '_now', lambda: self.clock[0]),
            mock.patch.object(w, 'aeration_state', False),
            mock.patch.object(w, 'last_aeration_toggle', -1e9),
            mock.patch.object(w, 'DISABLE_FLOW_ALERTS', False),
            mock.patch.object(w, 'MAIN_PUMP_TARGET_FLOW', 0),
            mock.patch.object(w, 'attempt_pump_recovery', mock.Mock()),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for name, value in (('low_flow_start_time', None), ('flow_imbalance_start_time', None),
                            ('main_pump_planned_off', False), ('main_pump_planned_restart', None),
                            ('backup_pump_active', False), ('smoothed_flow_inlet', 0.0),
                            ('smoothed_flow_outlet', 0.0), ('flow_history_inlet', []),
                            ('flow_history_outlet', [])):
            setattr(w, name, value)


class TestFlowWindow(WaterflowCase):
    def test_window_is_not_reported_before_the_duration(self):
        w = self.w
        w.start_flow_measurement()
        self.clock[0] += w.FLOW_MEASUREMENT_DURATION - 0.5
        self.assertIsNone(w.check_flow_measurement())
        self.assertTrue(w.flow_measurement_active)

    def test_flow_uses_the_measured_window(self):
        w = self.w
        w.start_flow_measurement()
        w.count_inlet = w.count_outlet = 127
        self.clock[0] += 12.7                      # the loop woke 2.7 s late
        elapsed = w.check_flow_measurement()
        self.assertAlmostEqual(elapsed, 12.7)
        self.assertFalse(w.flow_measurement_active)
        w.monitor_flow(elapsed)
        expected = 127 / (w.FLOW_CALIBRATION_FACTOR * 12.7)
        self.assertAlmostEqual(w.flow_history_inlet[-1], expected, places=6)
        # Dividing by the nominal 10 s would have over-read by 27 %
        self.assertLess(w.flow_history_inlet[-1], 127 / (w.FLOW_CALIBRATION_FACTOR * 10) * 0.8)

    def test_no_window_given_assumes_the_nominal_duration(self):
        w = self.w
        w.count_inlet = w.count_outlet = 75
        w.monitor_flow()
        self.assertAlmostEqual(w.flow_history_inlet[-1],
                               75 / (w.FLOW_CALIBRATION_FACTOR * w.FLOW_MEASUREMENT_DURATION))

    def test_fresh_reading_uses_its_own_window_and_stops_counting(self):
        w = self.w

        def sleep(seconds):
            self.clock[0] += seconds
            w.count_inlet = 165                    # delivered over the 11 s window

        with mock.patch.object(w.time, 'sleep', sleep):
            flow = w._take_fresh_flow_reading()
        self.assertAlmostEqual(flow, 165 / (w.FLOW_CALIBRATION_FACTOR * 11))   # 2.0 L/min
        self.assertFalse(w.flow_measurement_active)


class TestTemperatureCache(WaterflowCase):
    def test_sensors_are_read_once_per_interval(self):
        w = self.w
        reads = []
        with mock.patch.object(w, 'read_all_temperatures', lambda: reads.append(1) or {'reservoir': 20.0}), \
                mock.patch.object(w, '_temps_cache_time', None), \
                mock.patch.object(w, '_temps_cache', {}):
            for _ in range(30):                    # 30 one-second loop passes
                self.assertEqual(w.get_temperatures(max_age=60), {'reservoir': 20.0})
                self.clock[0] += 1
            self.assertEqual(len(reads), 1)
            self.clock[0] += 60
            w.get_temperatures(max_age=60)
            self.assertEqual(len(reads), 2)
            w.get_temperatures()                   # max_age=0: always reads
            self.assertEqual(len(reads), 3)


class TestBatteryDataAge(WaterflowCase):
    def setUp(self):
        super().setUp()
        w = self.w
        self.wall = [1_000_000.0]
        patches = [
            mock.patch.object(w.time, 'time', lambda: self.wall[0]),
            mock.patch.object(w, '_system_uptime_seconds', lambda: 1e6),
            mock.patch.object(w, '_battery_file_mtime', None),
            mock.patch.object(w, '_battery_file_seen', None),
            mock.patch.object(w, 'send_email_alert', lambda *a: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(w.BATTERY_DATA_FILE) and os.remove(w.BATTERY_DATA_FILE))
        self.addCleanup(w.alert_manager.forget, 'battery_data_unavailable')

    def _renogy_writes(self, soc=80.0):
        with open(self.w.BATTERY_DATA_FILE, 'w') as f:
            f.write(f'battery_soc{{source="renogy"}} {soc}\n')
        os.utime(self.w.BATTERY_DATA_FILE, (self.wall[0], self.wall[0]))

    def _advance(self, seconds):
        self.clock[0] += seconds
        self.wall[0] += seconds

    def test_wall_clock_step_does_not_make_fresh_data_stale(self):
        w = self.w
        self._renogy_writes()
        self._advance(5)
        self.assertEqual(w.read_battery_soc(), 80.0)
        self.wall[0] += 3600                       # NTP sync after boot
        self._advance(2)
        self.assertEqual(w.read_battery_soc(), 80.0)
        self._renogy_writes(79.0)                  # next poll, written with the new clock
        self._advance(3)
        self.assertEqual(w.read_battery_soc(), 79.0)

    def test_data_that_stops_updating_goes_stale(self):
        w = self.w
        self._renogy_writes()
        self._advance(5)
        self.assertEqual(w.read_battery_soc(), 80.0)
        self._advance(w.BATTERY_DATA_TIMEOUT + 10)
        self.assertIsNone(w.read_battery_soc())
        self._renogy_writes(77.0)                  # renogy.py comes back
        self._advance(4)
        self.assertEqual(w.read_battery_soc(), 77.0)

    def test_file_already_stale_at_startup_is_stale(self):
        w = self.w
        self._renogy_writes()
        self._advance(w.BATTERY_DATA_TIMEOUT + 30)
        self.assertIsNone(w.read_battery_soc())


class TestReloadableSettings(WaterflowCase):
    def _reload_with(self, overrides):
        w = self.w
        real = w.get_config
        with mock.patch.object(w, 'get_config', lambda k, d=None: overrides.get(k, real(k, d))):
            w._load_thresholds()                   # what the SIGHUP handler calls

    def setUp(self):
        super().setUp()
        self.addCleanup(self.w._load_thresholds)   # back to config.json values

    def test_maintenance_flags_follow_a_reload(self):
        w = self.w
        self._reload_with({'waterflow.maintenance.disable_flow_alerts': True,
                           'waterflow.maintenance.disable_pump_testing': True})
        self.assertTrue(w.DISABLE_FLOW_ALERTS)
        self.assertTrue(w.DISABLE_PUMP_TESTING)
        self.assertFalse(w.DISABLE_AERATOR)
        self.assertEqual(w._maintenance_flags(), ['PUMP TESTING', 'FLOW ALERTS'])
        self._reload_with({})
        self.assertEqual(w._maintenance_flags(), [])

    def test_flow_thresholds_follow_a_reload(self):
        w = self.w
        self._reload_with({'waterflow.flow.min_flow_threshold_lpm': 0.4,
                           'waterflow.flow.warning_delay_seconds': 300,
                           'waterflow.flow.calibration_factor': 8.1})
        self.assertEqual((w.MIN_FLOW_THRESHOLD, w.FLOW_WARNING_DELAY, w.FLOW_CALIBRATION_FACTOR),
                         (0.4, 300, 8.1))

    def test_renogy_maintenance_flags_are_in_config(self):
        with open(os.path.join(os.path.dirname(__file__), '..', 'config.json')) as f:
            cfg = json.load(f)
        self.assertEqual(sorted(cfg['renogy']['maintenance']),
                         ['disable_capacity_alerts', 'disable_emails', 'disable_fault_alerts',
                          'disable_low_battery_alerts', 'disable_temperature_alerts'])
        with open(os.path.join(os.path.dirname(__file__), '..', 'renogy.py')) as f:
            src = f.read()
        for key in cfg['renogy']['maintenance']:
            self.assertIn(f"'renogy.maintenance.{key}'", src)


class TestRequiredConfig(unittest.TestCase):
    def setUp(self):
        saved = (mc._config, mc._config_path)
        self.addCleanup(lambda: (setattr(mc, '_config', saved[0]), setattr(mc, '_config_path', saved[1])))
        self.dir = tempfile.mkdtemp(prefix='cfg_test_')
        self.path = os.path.join(self.dir, 'config.json')

    def _write(self, text):
        with open(self.path, 'w') as f:
            f.write(text)

    def test_invalid_json_stops_a_service(self):
        self._write('{"waterflow": {"flow": ')
        with self.assertLogs(level='ERROR'), self.assertRaises(SystemExit) as cm:
            mc.load_config(self.path, required=True)
        self.assertIn(self.path, str(cm.exception))
        self.assertNotEqual(cm.exception.code, 0)

    def test_missing_file_stops_a_service(self):
        with self.assertLogs(level='ERROR'), self.assertRaises(SystemExit):
            mc.load_config(os.path.join(self.dir, 'nope.json'), required=True)

    def test_non_object_config_is_rejected(self):
        self._write('[1, 2, 3]')
        with self.assertLogs(level='ERROR'), self.assertRaises(SystemExit):
            mc.load_config(self.path, required=True)

    def test_optional_load_still_falls_back_to_empty(self):
        self._write('not json')
        with self.assertLogs(level='ERROR'):
            self.assertEqual(mc.load_config(self.path), {})

    def test_valid_config_loads(self):
        self._write('{"alerts": {"email_cooldown_minutes": 5}}')
        mc.load_config(self.path, required=True)
        self.assertEqual(mc.get_config('alerts.email_cooldown_minutes'), 5)

    def test_both_services_require_their_config(self):
        root = os.path.join(os.path.dirname(__file__), '..')
        for name in ('renogy.py', 'waterflow_enhanced_failsafe.py'):
            with open(os.path.join(root, name)) as f:
                self.assertIn('load_config(_CONFIG_PATH, required=True)', f.read(), name)


class TestBatteryShutdownTls(unittest.TestCase):
    def test_starttls_verifies_the_server_certificate(self):
        import ssl
        import battery_shutdown as bs
        contexts = []

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def starttls(self, context=None): contexts.append(context)
            def login(self, *a): pass
            def send_message(self, msg): pass

        cfg = bs.get_shutdown_config({})
        env = {'SMTP_USER': 'u@example.com', 'SMTP_PASSWORD': 'pw', 'SMTP_RECIPIENTS': 'r@example.com'}
        with mock.patch.object(bs.smtplib, 'SMTP', FakeSMTP), mock.patch.dict(os.environ, env), \
                self.assertLogs(level='WARNING'):
            self.assertTrue(bs.send_shutdown_alert(4.0, cfg, halting=False))
        (ctx,) = contexts
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


if __name__ == '__main__':
    unittest.main()
