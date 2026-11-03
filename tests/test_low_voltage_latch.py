#!/usr/bin/env python3
"""Tests for the sustained/hysteresis latch used by the low-voltage alert."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from monitor_common import SustainedLatch  # noqa: E402

THR, HYST, HOLD = 12.8, 0.2, 600


def step(latch, v, now):
    return latch.update(v < THR, v >= THR + HYST, HOLD, now=now)


class TestLowVoltageLatch(unittest.TestCase):
    def test_short_dip_does_not_alert(self):
        latch = SustainedLatch()
        for t in range(0, 590, 10):
            self.assertFalse(step(latch, 12.6, t))
        self.assertFalse(step(latch, 12.9, 600))

    def test_sustained_low_alerts(self):
        latch = SustainedLatch()
        for t in range(0, 610, 10):
            step(latch, 12.6, t)
        self.assertTrue(latch.active)

    def test_charging_bump_inside_hysteresis_does_not_clear(self):
        latch = SustainedLatch()
        for t in range(0, 610, 10):
            step(latch, 12.6, t)
        # Oct 30 pattern: PV pushes the battery to 12.9 V while it is still nearly empty
        for t in range(610, 5000, 10):
            self.assertTrue(step(latch, 12.9, t))

    def test_clears_after_sustained_recovery(self):
        latch = SustainedLatch()
        for t in range(0, 610, 10):
            step(latch, 12.6, t)
        for t in range(610, 1200, 10):
            self.assertTrue(step(latch, 13.1, t))
        self.assertFalse(step(latch, 13.1, 1220))

    def test_flapping_resets_timer(self):
        latch = SustainedLatch()
        t = 0
        for _ in range(20):                      # 5 min low, 1 min OK, repeated
            for _ in range(30):
                step(latch, 12.6, t)
                t += 10
            for _ in range(6):
                step(latch, 12.85, t)
                t += 10
        self.assertFalse(latch.active)


if __name__ == '__main__':
    unittest.main()
