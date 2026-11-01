#!/usr/bin/env python3
"""Tests for battery_shutdown decision logic. subprocess.run is patched so
no test can stop services or halt the machine."""

import logging
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import battery_shutdown as bs  # noqa: E402


def cfg(**over):
    c = bs.get_shutdown_config({'shutdown': {}})
    c.update(over)
    return c


def prom(**metrics):
    lines = [f'{k}{{source="renogy"}} {v}' for k, v in metrics.items()]
    f = tempfile.NamedTemporaryFile('w', suffix='.prom', delete=False)
    f.write('# HELP x\n' + '\n'.join(lines) + '\nbattery_soc_hw{source="renogy"} 99\n')
    f.close()
    return f.name


CRITICAL = dict(battery_soc=3.0, battery_soc_confidence=0.9, battery_voltage_rest_estimate=11.8)


class Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(bs.subprocess, 'run',
                              side_effect=AssertionError('subprocess.run must not be called'))
        self.run_mock = p.start()
        self.addCleanup(p.stop)


class TestConfig(Base):
    def test_default_mode_is_alert(self):
        self.assertEqual(bs.get_shutdown_config({})['mode'], 'alert')

    def test_unknown_mode_falls_back_to_alert(self):
        self.assertEqual(bs.get_shutdown_config({'shutdown': {'mode': 'reboot'}})['mode'], 'alert')


class TestParsing(Base):
    def test_parses_needed_metrics_only(self):
        path = prom(**CRITICAL, battery_voltage=11.7)
        try:
            values, age = bs.parse_battery_metrics(path)
        finally:
            os.unlink(path)
        self.assertEqual(values['battery_soc'], 3.0)
        self.assertEqual(values['battery_soc_confidence'], 0.9)
        self.assertEqual(values['battery_voltage_rest_estimate'], 11.8)
        self.assertNotIn('battery_soc_hw', values)
        self.assertLess(age, 5)

    def test_missing_file(self):
        self.assertEqual(bs.parse_battery_metrics('/nonexistent.prom'), ({}, None))


class TestCondition(Base):
    def test_requires_all_three(self):
        c = cfg()
        self.assertTrue(bs.critical_condition(CRITICAL, c))
        self.assertFalse(bs.critical_condition({**CRITICAL, 'battery_soc': 6}, c))
        self.assertFalse(bs.critical_condition({**CRITICAL, 'battery_soc_confidence': 0.2}, c))
        self.assertFalse(bs.critical_condition({**CRITICAL, 'battery_voltage_rest_estimate': 12.6}, c))

    def test_old_renogy_without_confidence_never_triggers(self):
        # Hardware register said 5 % but nothing vouches for it
        self.assertFalse(bs.critical_condition({'battery_soc': 2.0, 'battery_voltage': 11.5}, cfg()))


class TestEvaluate(Base):
    def test_sustained(self):
        c, st = cfg(sustained_seconds=600), {'since': None, 'alerted': False}
        self.assertFalse(bs.evaluate(st, CRITICAL, 1, c, 0))
        self.assertFalse(bs.evaluate(st, CRITICAL, 1, c, 590))
        self.assertTrue(bs.evaluate(st, CRITICAL, 1, c, 600))

    def test_interruption_resets_timer(self):
        c, st = cfg(sustained_seconds=600), {'since': None, 'alerted': False}
        bs.evaluate(st, CRITICAL, 1, c, 0)
        bs.evaluate(st, {**CRITICAL, 'battery_voltage_rest_estimate': 12.3}, 1, c, 300)
        self.assertFalse(bs.evaluate(st, CRITICAL, 1, c, 650))

    def test_stale_or_missing_data_never_triggers(self):
        c, st = cfg(sustained_seconds=0), {'since': None, 'alerted': False}
        self.assertFalse(bs.evaluate(st, CRITICAL, 500, c, 0))
        self.assertFalse(bs.evaluate(st, {}, None, c, 0))

    def test_alert_rearms_above_recovery(self):
        c, st = cfg(), {'since': None, 'alerted': True}
        bs.evaluate(st, {**CRITICAL, 'battery_soc': 10}, 1, c, 0)
        self.assertTrue(st['alerted'])
        bs.evaluate(st, {**CRITICAL, 'battery_soc': 20}, 1, c, 10)
        self.assertFalse(st['alerted'])


class TestMainLoop(Base):
    """Drive main() for a few polls with time/sleep patched."""

    def setUp(self):
        super().setUp()
        root = logging.getLogger()
        handlers = list(root.handlers)
        logging.disable(logging.CRITICAL)

        def restore():
            logging.disable(logging.NOTSET)
            for h in list(root.handlers):
                if h not in handlers:
                    root.removeHandler(h)
                    h.close()
        self.addCleanup(restore)

    def _run(self, mode, metrics, polls=25):
        with tempfile.TemporaryDirectory() as d:
            prom_path = prom(**metrics)
            conf = os.path.join(d, 'config.json')
            with open(conf, 'w') as f:
                f.write('{"shutdown": {"mode": "%s", "sustained_seconds": 600}, '
                        '"paths": {"renogy_prom": "%s", "renogy_log": "%s/l.log", '
                        '"battery_shutdown_prom": "%s/bs.prom"}}' % (mode, prom_path, d, d))
            clock = [time.time()]
            calls = {'n': 0}

            def fake_sleep(s):
                calls['n'] += 1
                clock[0] += s
                os.utime(prom_path, (clock[0], clock[0]))
                if calls['n'] >= polls:
                    raise KeyboardInterrupt

            with mock.patch.object(bs.time, 'sleep', fake_sleep), \
                    mock.patch.object(bs.time, 'time', lambda: clock[0]), \
                    mock.patch.object(bs, 'send_shutdown_alert', return_value=True) as mail, \
                    mock.patch.object(bs, 'perform_shutdown') as halt, \
                    mock.patch.object(sys, 'argv', ['bs', conf]):
                with self.assertRaises(KeyboardInterrupt):
                    bs.main()
                with open(os.path.join(d, 'bs.prom')) as f:
                    metrics_out = f.read()
            os.unlink(prom_path)
            return mail, halt, metrics_out

    def test_alert_mode_emails_once_and_never_halts(self):
        mail, halt, out = self._run('alert', CRITICAL)
        self.assertEqual(mail.call_count, 1)
        self.assertFalse(mail.call_args.kwargs['halting'])
        halt.assert_not_called()
        self.assertIn('battery_shutdown_condition{source="battery_shutdown"} 1', out)
        self.assertIn('battery_shutdown_mode{source="battery_shutdown"} 1', out)

    def test_off_mode_does_nothing(self):
        mail, halt, out = self._run('off', CRITICAL)
        mail.assert_not_called()
        halt.assert_not_called()
        self.assertIn('battery_shutdown_condition{source="battery_shutdown"} 0', out)

    def test_halt_mode_calls_perform_shutdown(self):
        with self.assertRaises(SystemExit):
            self._run('halt', CRITICAL)

    def test_halt_mode_ignores_unconfirmed_low_soc(self):
        mail, halt, _ = self._run('halt', {**CRITICAL, 'battery_soc_confidence': 0.1})
        halt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
