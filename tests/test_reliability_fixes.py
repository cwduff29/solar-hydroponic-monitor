#!/usr/bin/env python3
"""Logging setup, health counting, atomic alert state, summary totals, signal
handling, log throttling, the Modbus batch cache and the wifi-heal script."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.join(HERE, '..')
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)
import monitor_common as mc


class TestLogFile(unittest.TestCase):
    def test_service_warnings_reach_the_log_file(self):
        # A fresh interpreter, as under systemd: load_config() logs before the
        # service configures logging.
        code = (
            "import sys, logging; sys.path.insert(0, %r)\n"
            "import fake_hw; w = fake_hw.import_waterflow()\n"
            "logging.warning('marker-line')\n"
            "logging.shutdown()\n"
            "print(open(w.LOG_FILE_PATH).read())\n" % HERE)
        out = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('WARNING - marker-line', out.stdout)


class WaterflowCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()


class TestHealthCount(WaterflowCase):
    def setUp(self):
        w = self.w
        patches = [
            mock.patch.object(w, 'aeration_state', False),
            mock.patch.object(w, 'last_aeration_toggle', -1e9),
            mock.patch.object(w, 'DISABLE_FLOW_ALERTS', False),
            mock.patch.object(w, 'MAIN_PUMP_TARGET_FLOW', 0),
            mock.patch.object(w, 'attempt_pump_recovery', mock.Mock()),
            mock.patch.object(w, 'consecutive_failures', 0),
            mock.patch.object(w, 'main_pump_planned_off', False),
            mock.patch.object(w, 'main_pump_planned_restart', None),
            mock.patch.object(w, 'backup_pump_active', False),
            mock.patch.object(w, 'low_flow_start_time', None),
            mock.patch.object(w, 'flow_history_inlet', []),
            mock.patch.object(w, 'flow_history_outlet', []),
            mock.patch.object(w, 'smoothed_flow_inlet', 0.0),
            mock.patch.object(w, 'smoothed_flow_outlet', 0.0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_repeated_failures_make_the_monitor_unhealthy(self):
        w = self.w
        with mock.patch.object(w, 'validate_flow_reading', side_effect=RuntimeError('sensor broke')), \
                self.assertLogs(level='ERROR'):
            for _ in range(w.MAX_CONSECUTIVE_FAILURES):
                w.process_flow_window(10.0)
        self.assertEqual(w.consecutive_failures, w.MAX_CONSECUTIVE_FAILURES)
        w.write_prometheus_metrics(temps={}, conditions={})
        with open(w.PROM_OUTPUT_FILE) as f:
            self.assertIn('waterflow_monitor_healthy{source="waterflow"} 0', f.read())

    def test_a_good_window_clears_the_count(self):
        w = self.w
        w.consecutive_failures = 3
        w.count_inlet = w.count_outlet = 127
        w.process_flow_window(10.0)
        self.assertEqual(w.consecutive_failures, 0)


class TestAlertStateFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='alert_state_')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.ram = os.path.join(self.dir, 'ram', 'alerts.json')
        self.disk = os.path.join(self.dir, 'disk', 'state.json')
        self.am = mc.AlertManager(self.ram, self.disk)

    def test_save_is_atomic_and_leaves_no_temp_files(self):
        self.am.should_send('low_flow')
        self.am.save()
        for path in (self.ram, self.disk):
            with open(path) as f:
                self.assertTrue(json.load(f)['low_flow']['active'])
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_interrupted_write_keeps_the_previous_file(self):
        self.am.should_send('low_flow')
        self.am.save()
        self.am.should_send('leak_detected')
        with mock.patch.object(mc.os, 'replace', side_effect=OSError('power cut')), \
                self.assertLogs(level='ERROR'):
            self.am.save()
        with open(self.disk) as f:
            self.assertEqual(sorted(json.load(f)), ['low_flow'])      # old content, still valid JSON

    def test_unchanged_state_does_not_rewrite_the_persistent_file(self):
        self.am.should_send('low_flow')
        self.am.save()
        with mock.patch.object(mc.AlertManager, '_write_atomic') as write:
            self.am.save()
        self.assertEqual([c.args[0] for c in write.call_args_list], [self.ram])

    def test_save_can_be_reentered_on_the_same_thread(self):
        # SIGTERM inside save() unwinds into a finally block that saves again
        with self.am._lock:
            self.am.save()

    def test_state_round_trips(self):
        self.am.should_send('low_flow')
        self.am.mark_sent('low_flow')
        other = mc.AlertManager(os.path.join(self.dir, 'gone.json'), self.disk)
        self.assertTrue(other.load())
        self.assertTrue(other.is_active('low_flow'))


class TestSummaryTotals(unittest.TestCase):
    def test_total_of_a_counter_that_resets_daily(self):
        d = mc.DailySummary()
        for v in (0.10, 0.20, 0.30):               # running total through day 1
            d.update_total('solar_kwh', v)
        self.assertAlmostEqual(d.get_total('solar_kwh'), 0.20)     # gained since the first sample
        for v in (0.02, 0.05):                      # midnight reset, then day 2
            d.update_total('solar_kwh', v)
        self.assertAlmostEqual(d.get_total('solar_kwh'), 0.25)

    def test_total_restarts_after_a_summary(self):
        d = mc.DailySummary()
        for v in (1.0, 4.0):
            d.update_total('daily_volume_l', v)
        d.mark_sent()
        self.assertEqual(d.get_total('daily_volume_l'), 0.0)
        d.update_total('daily_volume_l', 6.5)
        self.assertAlmostEqual(d.get_total('daily_volume_l'), 2.5)

    def test_unknown_key_and_bad_values(self):
        d = mc.DailySummary()
        self.assertIsNone(d.get_total('nope'))
        d.update_total('x', None)
        d.update_total('x', 'abc')
        self.assertIsNone(d.get_total('x'))


class TestSignals(WaterflowCase):
    def test_sighup_handler_does_not_touch_the_config_lock(self):
        w = self.w
        self.addCleanup(setattr, w, '_reload_requested', False)
        with mc._config_lock:                      # as if SIGHUP interrupted get_config()
            w._sighup_handler(1, None)             # the old handler deadlocked here
        self.assertTrue(w._reload_requested)

    def test_reload_runs_from_the_main_loop(self):
        w = self.w
        self.assertFalse(w.apply_pending_reload())                 # nothing pending
        w._sighup_handler(1, None)
        with mock.patch.object(w, 'reload_config', return_value=True) as reload, \
                mock.patch.object(w, '_load_thresholds') as load, self.assertLogs(level='WARNING'):
            self.assertTrue(w.apply_pending_reload())
        reload.assert_called_once()
        load.assert_called_once()
        self.assertFalse(w._reload_requested)

    def test_failed_reload_keeps_the_old_thresholds(self):
        w = self.w
        w._sighup_handler(1, None)
        with mock.patch.object(w, 'reload_config', return_value=False), \
                mock.patch.object(w, '_load_thresholds') as load, self.assertLogs(level='ERROR'):
            self.assertFalse(w.apply_pending_reload())
        load.assert_not_called()

    def test_sigterm_unwinds_through_finally(self):
        with self.assertRaises(SystemExit) as cm:
            self.w._sigterm_handler(15, None)
        self.assertEqual(cm.exception.code, 0)

    def test_renogy_uses_the_same_pattern(self):
        with open(os.path.join(REPO, 'renogy.py')) as f:
            src = f.read()
        handler = src.split('def _sighup_handler')[1].split('def apply_pending_reload')[0]
        self.assertNotIn('reload_config', handler)
        self.assertIn('apply_pending_reload()', src.split('# MAIN LOOP')[1])


class TestLogThrottle(WaterflowCase):
    def test_a_persisting_condition_logs_once_per_interval(self):
        w = self.w
        clock = [1000.0]
        with mock.patch.object(w, '_now', lambda: clock[0]), mock.patch.object(w, '_log_last', {}):
            with self.assertLogs(level='WARNING') as logs:
                for _ in range(120):               # two minutes of one-second passes
                    w._log_throttled('battery_data', 'Battery data unavailable: stale')
                    clock[0] += 1
            self.assertEqual(len(logs.records), 1)
            clock[0] += 300
            with self.assertLogs(level='WARNING') as logs:
                w._log_throttled('battery_data', 'Battery data unavailable: stale')
                w._log_throttled('other', 'a different condition')
            self.assertEqual(len(logs.records), 2)


class TestBatchCache(unittest.TestCase):
    """renogy_extended against a stand-in for renogymodbus (not installed off the Pi)."""

    def setUp(self):
        bus = self.bus = []

        class RenogyChargeController:
            def __init__(self, *a, **k):
                pass

            def read_registers(self, start, count):
                bus.append(('block', start, count))
                return [(start + i) & 0xFFFF for i in range(count)]     # value = its own address

            def read_register(self, address, *a):
                bus.append(('single', address))
                return address

            def retriable_read_register(self, address, decimals, functioncode, signed):
                bus.append(('single', address))
                return address / float(10 ** decimals) if decimals else address

            # Getters as in renogymodbus 0.0.23
            def get_battery_voltage(self):
                return self.retriable_read_register(0x0101, 1, 3, False)

            def get_battery_state_of_charge(self):
                return self.retriable_read_register(0x0100, 0, 3, False)

            def get_solar_current(self):
                return self.retriable_read_register(0x0108, 2, 3, False)

        fake = types.ModuleType('renogymodbus')
        fake.RenogyChargeController = RenogyChargeController
        with mock.patch.dict(sys.modules, {'renogymodbus': fake}):
            spec = importlib.util.spec_from_file_location(
                'renogy_extended_under_test', os.path.join(REPO, 'renogy_extended.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        self.rover = module.RenogyRoverExtended('/dev/null', 1)

    def test_one_poll_is_two_bus_transactions(self):
        r = self.rover
        self.assertTrue(r.batch_read())
        self.assertEqual(self.bus, [('block', 0x0100, 0x20), ('block', 0x0120, 3)])
        self.assertAlmostEqual(r.get_battery_voltage(), 0x0101 / 10)       # base-class getter, scaled
        self.assertEqual(r.get_battery_state_of_charge(), 0x0100)
        self.assertAlmostEqual(r.get_solar_current(), 0x0108 / 100)
        self.assertEqual(r.get_total_battery_over_discharges(), 0x0116)    # extended single register
        self.assertEqual(r.get_total_charging_ah(), (0x0118 << 16) | 0x0119)   # extended register pair
        self.assertIsInstance(r.get_active_faults(), list)
        self.assertIsNotNone(r.get_load_status())
        self.assertEqual(len(self.bus), 2)
        self.assertEqual(r.total_reads, 2)

    def test_without_a_batch_the_getters_read_the_bus(self):
        r = self.rover
        self.assertAlmostEqual(r.get_battery_voltage(), 0x0101 / 10)
        self.assertEqual(r.get_total_charging_ah(), (0x0118 << 16) | 0x0119)
        self.assertEqual(self.bus, [('single', 0x0101), ('block', 0x0118, 2)])

    def test_cache_is_not_used_after_invalidation(self):
        r = self.rover
        r.batch_read()
        r.invalidate_batch_cache()
        r.get_battery_voltage()
        self.assertEqual(self.bus[-1], ('single', 0x0101))


class TestWifiHeal(unittest.TestCase):
    """scripts/wifi-heal.sh with every external command replaced by a stub."""

    def setUp(self):
        d = self.d = tempfile.mkdtemp(prefix='wifi_heal_')
        self.addCleanup(shutil.rmtree, d, True)
        for sub in ('bin', 'run', 'state', 'usb'):
            os.makedirs(os.path.join(d, sub))
        self.log = os.path.join(d, 'calls.log')
        stubs = {
            'ping': 'echo "ping $*" >> "$LOG"; [ -f "$D/gateway_up" ]',
            'ip': '[ -f "$D/route" ] && cat "$D/route"; exit 0',
            'logger': 'echo "logger $*" >> "$LOG"',
            'nmcli': 'echo "nmcli $*" >> "$LOG"',
            'systemctl': 'echo "systemctl $*" >> "$LOG"',
            'vcgencmd': 'echo throttled=0x0',
            'sleep': 'exit 0',
            'sync': 'exit 0',
        }
        for name, body in stubs.items():
            path = os.path.join(d, 'bin', name)
            with open(path, 'w') as f:
                f.write(f'#!/bin/bash\nD={d!r}; LOG={self.log!r}\n{body}\n')
            os.chmod(path, 0o755)
        self._write('uptime', '5000.00 4000.00\n')
        self._write('route', 'default via 192.168.99.1 proto dhcp src 192.168.99.120 metric 600\n')
        self.env = dict(os.environ, PATH=os.path.join(d, 'bin') + os.pathsep + os.environ['PATH'],
                        WIFI_HEAL_RUN_DIR=os.path.join(d, 'run'),
                        WIFI_HEAL_STATE_DIR=os.path.join(d, 'state'),
                        WIFI_HEAL_USB_DEV=os.path.join(d, 'usb'),
                        WIFI_HEAL_UPTIME_FILE=os.path.join(d, 'uptime'))

    def _write(self, name, text):
        with open(os.path.join(self.d, name), 'w') as f:
            f.write(text)

    def _read(self, *parts):
        try:
            with open(os.path.join(self.d, *parts)) as f:
                return f.read().strip()
        except FileNotFoundError:
            return None

    def check(self, times=1):
        for _ in range(times):
            subprocess.run(['bash', os.path.join(REPO, 'scripts', 'wifi-heal.sh')],
                           env=self.env, check=True, timeout=30)

    def calls(self, word):
        return [line for line in (self._read('calls.log') or '').splitlines() if word in line]

    def boot(self):
        """A reboot clears /run but not the persistent state."""
        shutil.rmtree(os.path.join(self.d, 'run'))
        os.makedirs(os.path.join(self.d, 'run'))

    def test_healthy_gateway_does_nothing(self):
        self._write('gateway_up', '')
        self.check(3)
        self.assertEqual(self.calls('nmcli') + self.calls('systemctl'), [])
        self.assertIn('192.168.99.1', self.calls('ping')[0])
        self.assertEqual(self._read('state', 'gateway'), '192.168.99.1')

    def test_escalates_to_one_reboot(self):
        self.check(6)
        self.assertEqual(len(self.calls('nmcli radio wifi off')), 1)       # check 2
        self.assertEqual(self._read('usb', 'authorized'), '1')             # check 4
        self.assertEqual(self.calls('systemctl'), ['systemctl reboot'])    # check 6
        self.assertEqual(self._read('state', 'reboots'), '1')

    def test_stops_rebooting_after_three_in_a_row(self):
        for _ in range(3):
            self.check(6)
            self.boot()
        self.assertEqual(len(self.calls('systemctl reboot')), 3)
        self.check(40)                                                     # router still down, 80 min
        self.assertEqual(len(self.calls('systemctl reboot')), 3)
        self.assertTrue(self.calls('not rebooting'))
        self.assertGreaterEqual(len(self.calls('nmcli radio wifi off')), 3 + 1 + 2)   # keeps retrying the radio

    def test_recovery_rearms_the_reboot(self):
        for _ in range(3):
            self.check(6)
            self.boot()
        self._write('gateway_up', '')
        self.check()
        self.assertEqual(self._read('state', 'reboots'), '0')
        os.remove(os.path.join(self.d, 'gateway_up'))
        self.check(6)
        self.assertEqual(len(self.calls('systemctl reboot')), 4)

    def test_no_reboot_shortly_after_boot(self):
        self._write('uptime', '300.00 200.00\n')
        self.check(9)
        self.assertEqual(self.calls('systemctl'), [])

    def test_uses_the_last_gateway_seen_when_the_route_is_gone(self):
        self._write('gateway_up', '')
        self._write('route', 'default via 10.0.0.1 proto dhcp metric 600\n')    # network was renumbered
        self.check()
        os.remove(os.path.join(self.d, 'route'))                               # WiFi drops: no default route
        os.remove(os.path.join(self.d, 'gateway_up'))
        self.check()
        self.assertIn('10.0.0.1', self.calls('ping')[-1])


if __name__ == '__main__':
    unittest.main()
