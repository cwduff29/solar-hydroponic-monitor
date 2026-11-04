#!/usr/bin/env python3
"""Boot-reason classification and the pi_boot_reason_total counter."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))


class TestClassify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def test_table(self):
        c = self.w.classify_boot
        self.assertEqual(c(0x1000, False), 'power_loss')
        self.assertEqual(c(0x1000, True), 'halt')
        self.assertEqual(c(0x20, True), 'reboot')
        self.assertEqual(c(0x20, False), 'watchdog')
        self.assertEqual(c(None, True), 'unknown')
        self.assertEqual(c(0, False), 'unknown')


class TestRecordBootReason(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fake_hw import import_waterflow
        cls.w = import_waterflow()

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='boot_reason_')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.p = {k: os.path.join(self.d, k) for k in ('state', 'marker', 'rsts', 'boot_id')}
        patches = [
            mock.patch.object(self.w, 'BOOT_REASON_STATE_FILE', self.p['state']),
            mock.patch.object(self.w, 'CLEAN_SHUTDOWN_MARKER', self.p['marker']),
            mock.patch.object(self.w, 'RESET_STATUS_FILE', self.p['rsts']),
            mock.patch.object(self.w, 'BOOT_ID_FILE', self.p['boot_id']),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def boot(self, boot_id, rsts, marker=None):
        """Simulate a boot: new boot_id and reset register; marker = id of the boot that shut down cleanly."""
        with open(self.p['boot_id'], 'w') as f:
            f.write(boot_id + '\n')
        with open(self.p['rsts'], 'wb') as f:
            f.write(rsts.to_bytes(4, 'big'))
        if marker:
            with open(self.p['marker'], 'w') as f:
                f.write(marker + '\n')
        return self.w.record_boot_reason()

    def test_first_run_records_but_does_not_count(self):
        s = self.boot('b0', 0x20)
        self.assertEqual(s['last_reason'], 'unknown')
        self.assertEqual(sum(s['counts'].values()), 0)
        self.assertEqual(json.load(open(self.p['state']))['boot_id'], 'b0')

    def test_sequence(self):
        self.boot('b0', 0x20)
        self.assertEqual(self.boot('b1', 0x20, marker='b0')['last_reason'], 'reboot')
        self.assertFalse(os.path.exists(self.p['marker']))           # consumed
        self.assertEqual(self.boot('b2', 0x20)['last_reason'], 'watchdog')
        self.assertEqual(self.boot('b3', 0x1000)['last_reason'], 'power_loss')
        s = self.boot('b4', 0x1000, marker='b3')
        self.assertEqual(s['last_reason'], 'halt')
        self.assertEqual(s['counts'], {'power_loss': 1, 'halt': 1, 'reboot': 1, 'watchdog': 1, 'unknown': 0})
        self.assertEqual(s['rsts'], '0x1000')

    def test_service_restart_same_boot_not_counted(self):
        self.boot('b0', 0x20)
        self.boot('b1', 0x20)
        s = self.w.record_boot_reason()                                # same boot_id again
        self.assertEqual(s['counts']['watchdog'], 1)

    def test_stale_marker_does_not_hide_a_freeze(self):
        self.boot('b0', 0x20)
        self.boot('b1', 0x20)
        # marker from an older boot (b0) left behind; b1 then froze
        s = self.boot('b2', 0x20, marker='b0')
        self.assertEqual(s['last_reason'], 'watchdog')
        self.assertFalse(os.path.exists(self.p['marker']))

    def test_corrupt_state_starts_again(self):
        with open(self.p['state'], 'w') as f:
            f.write('{not json')
        with self.assertLogs(level='WARNING'):
            s = self.boot('b0', 0x20)
        self.assertEqual(s['boot_id'], 'b0')

    def test_unwritable_state_never_raises(self):
        with mock.patch.object(self.w, 'BOOT_REASON_STATE_FILE', os.path.join(self.d, 'missing', 'x.json')):
            s = self.boot('b0', 0x20)
        self.assertIn('counts', s)

    def test_metrics_written(self):
        w = self.w
        self.boot('b0', 0x20)
        state = self.boot('b1', 0x20)
        with mock.patch.object(w, '_boot_reason_state', state):
            w.write_prometheus_metrics()
        text = open(w.PROM_OUTPUT_FILE).read()
        self.assertIn('# TYPE pi_boot_reason_total counter', text)
        self.assertIn('pi_boot_reason_total{reason="watchdog",source="waterflow"} 1', text)
        self.assertIn('pi_boot_reason_total{reason="power_loss",source="waterflow"} 0', text)
        self.assertIn('pi_last_boot_reason{reason="watchdog",source="waterflow"} 1', text)


if __name__ == '__main__':
    unittest.main()
