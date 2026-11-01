#!/usr/bin/env python3
"""Tests for aeration load-shedding tiers (no GPIO needed)."""

import itertools
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from load_shedding import (  # noqa: E402
    soc_tier, temperature_tier, aeration_schedule,
    TIER_NORMAL, TIER_REDUCED, TIER_DISABLED,
)

DISABLE, REDUCE = 30, 50
NORMAL = (360, 1440)
REDUCED = (240, 2160)
TEMP_TIERS = {'hot': (420, 1380), 'warm': (360, 1440), 'moderate': (300, 1800), 'cool': (240, 2160)}
HOT, WARM, MODERATE = 25.6, 22.8, 20.0


class TestSocTier(unittest.TestCase):
    def test_thresholds_from_cold_start(self):
        self.assertEqual(soc_tier(80, None, DISABLE, REDUCE), TIER_NORMAL)
        self.assertEqual(soc_tier(49.9, None, DISABLE, REDUCE), TIER_REDUCED)
        self.assertEqual(soc_tier(29.9, None, DISABLE, REDUCE), TIER_DISABLED)

    def test_unknown_soc_keeps_previous_tier(self):
        self.assertEqual(soc_tier(None, None, DISABLE, REDUCE), TIER_NORMAL)
        self.assertEqual(soc_tier(None, TIER_DISABLED, DISABLE, REDUCE), TIER_DISABLED)

    def test_tightening_is_immediate(self):
        self.assertEqual(soc_tier(29, TIER_NORMAL, DISABLE, REDUCE), TIER_DISABLED)

    def test_relaxing_needs_hysteresis(self):
        self.assertEqual(soc_tier(31, TIER_DISABLED, DISABLE, REDUCE, 2), TIER_DISABLED)
        self.assertEqual(soc_tier(32, TIER_DISABLED, DISABLE, REDUCE, 2), TIER_REDUCED)
        self.assertEqual(soc_tier(51, TIER_REDUCED, DISABLE, REDUCE, 2), TIER_REDUCED)
        self.assertEqual(soc_tier(52, TIER_REDUCED, DISABLE, REDUCE, 2), TIER_NORMAL)
        self.assertEqual(soc_tier(90, TIER_DISABLED, DISABLE, REDUCE, 2), TIER_NORMAL)


class TestTemperatureTier(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(temperature_tier(26, HOT, WARM, MODERATE), 'hot')
        self.assertEqual(temperature_tier(23, HOT, WARM, MODERATE), 'warm')
        self.assertEqual(temperature_tier(21, HOT, WARM, MODERATE), 'moderate')
        self.assertEqual(temperature_tier(15, HOT, WARM, MODERATE), 'cool')


class TestAerationSchedule(unittest.TestCase):
    def test_bug1_disabled_is_not_overridden_by_temperature(self):
        # Before the fix the cool tier (240/2160) replaced "disabled" and the
        # aerator came back 36 minutes after shedding started.
        for temp in TEMP_TIERS.values():
            on_s, _ = aeration_schedule(TIER_DISABLED, NORMAL, REDUCED, temp)
            self.assertEqual(on_s, 0)

    def test_matrix_soc_caps_temperature(self):
        temps = list(TEMP_TIERS.values()) + [None]
        for tier, temp in itertools.product((TIER_NORMAL, TIER_REDUCED, TIER_DISABLED), temps):
            on_s, off_s = aeration_schedule(tier, NORMAL, REDUCED, temp)
            base_on, base_off = temp if temp is not None else NORMAL
            with self.subTest(tier=tier, temp=temp):
                # Never more aeration than the temperature tier asks for
                self.assertLessEqual(on_s, base_on)
                if tier == TIER_NORMAL:
                    self.assertEqual((on_s, off_s), (base_on, base_off))
                elif tier == TIER_REDUCED:
                    # Never more than the SOC cap allows
                    self.assertLessEqual(on_s, REDUCED[0])
                    self.assertGreaterEqual(off_s, REDUCED[1])
                    self.assertGreaterEqual(off_s, base_off)
                else:
                    self.assertEqual(on_s, 0)

    def test_reduced_hot_water(self):
        self.assertEqual(aeration_schedule(TIER_REDUCED, NORMAL, REDUCED, TEMP_TIERS['hot']), (240, 2160))

    def test_duty_cycle_never_increases_with_worse_tier(self):
        for temp in list(TEMP_TIERS.values()) + [None]:
            duties = []
            for tier in (TIER_NORMAL, TIER_REDUCED, TIER_DISABLED):
                on_s, off_s = aeration_schedule(tier, NORMAL, REDUCED, temp)
                duties.append(0 if on_s == 0 else on_s / (on_s + off_s))
            self.assertEqual(duties, sorted(duties, reverse=True))


if __name__ == '__main__':
    unittest.main()
