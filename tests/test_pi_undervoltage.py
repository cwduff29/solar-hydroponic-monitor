#!/usr/bin/env python3
"""Pi under-voltage alarm export (replaces node_exporter's hwmon collector)."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))


class TestPiUndervoltage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def _sysfs(self, chips):
        """chips: {name: alarm value or None (no alarm file)}"""
        d = tempfile.mkdtemp()
        for i, (name, alarm) in enumerate(chips.items()):
            h = os.path.join(d, f'hwmon{i}')
            os.makedirs(h)
            with open(os.path.join(h, 'name'), 'w') as f:
                f.write(name + '\n')
            if alarm is not None:
                with open(os.path.join(h, 'in0_lcrit_alarm'), 'w') as f:
                    f.write(f'{alarm}\n')
        return d

    def _read(self, base):
        with mock.patch.object(self.w, 'HWMON_BASE_DIR', base), \
             mock.patch.object(self.w, '_rpi_volt_alarm_path', None):
            return self.w.read_pi_undervoltage(), self.w._rpi_volt_alarm_path

    def test_finds_rpi_volt_among_other_chips(self):
        base = self._sysfs({'cpu_thermal': None, 'w1_slave_temp': None, 'rpi_volt': 1})
        value, path = self._read(base)
        self.assertEqual(value, 1)
        self.assertTrue(path.endswith('hwmon2/in0_lcrit_alarm'))

    def test_no_alarm(self):
        self.assertEqual(self._read(self._sysfs({'rpi_volt': 0}))[0], 0)

    def test_missing_chip_returns_none_and_is_remembered(self):
        value, path = self._read(self._sysfs({'cpu_thermal': None}))
        self.assertIsNone(value)
        self.assertEqual(path, '')

    def test_metric_written_to_prom_file(self):
        w = self.w
        base = self._sysfs({'rpi_volt': 1})
        with mock.patch.object(w, 'HWMON_BASE_DIR', base), \
             mock.patch.object(w, '_rpi_volt_alarm_path', None):
            w.write_prometheus_metrics()
        with open(w.PROM_OUTPUT_FILE) as f:
            self.assertIn('pi_undervoltage_alarm{source="waterflow"} 1', f.read())


if __name__ == '__main__':
    unittest.main()
