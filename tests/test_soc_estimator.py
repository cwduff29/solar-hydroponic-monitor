#!/usr/bin/env python3
"""Unit and replay tests for soc_estimator (no hardware needed).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""

import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import soc_estimator  # noqa: E402
from soc_estimator import (  # noqa: E402
    SocEstimator, rest_voltage_to_soc, plausible_band, CHG_FLOAT,
)

CAP = 50.0
FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'oct30_deep_discharge.csv')


def run(est, t0, seconds, step=10, **kw):
    """Feed constant readings for `seconds`; returns the final t."""
    t = t0
    while t < t0 + seconds:
        t += step
        est.update(t, **kw)
    return t


class TestTable(unittest.TestCase):
    def test_alert_threshold_consistency(self):
        # 12.8 V at rest == 20 %, matching low_battery_voltage / low_battery_soc_pct
        self.assertAlmostEqual(rest_voltage_to_soc(12.8), 20.0)

    def test_monotonic_and_bounded(self):
        prev = -1
        for mv in range(9500, 14001, 10):
            soc = rest_voltage_to_soc(mv / 1000.0)
            self.assertGreaterEqual(soc, prev)
            self.assertTrue(0 <= soc <= 100)
            prev = soc

    def test_flat_region_band_is_wide(self):
        lo, hi = plausible_band(13.2)
        self.assertLessEqual(lo, 20)
        self.assertGreaterEqual(hi, 95)

    def test_low_voltage_band_is_narrow(self):
        lo, hi = plausible_band(12.3)
        self.assertLess(hi - lo, 10)


class TestSeeding(unittest.TestCase):
    def test_hw_register_rejected_when_inconsistent_with_voltage(self):
        # Oct 30: register said 61 % at 12.3 V under 2.5 A load
        est = SocEstimator(CAP)
        soc = est.update(0, 12.3, 0.8, 2.5, hw_soc=61)
        self.assertLess(soc, 12)
        self.assertTrue(est.hw_rejected)
        self.assertIn('rejected', est.pop_events()[0])

    def test_hw_register_used_when_plausible(self):
        est = SocEstimator(CAP)
        soc = est.update(0, 13.2, 0.0, 1.0, hw_soc=60)
        self.assertAlmostEqual(soc, 60, delta=0.1)
        self.assertFalse(est.hw_rejected)
        self.assertLess(est.confidence, 0.5)   # flat region: low confidence

    def test_voltage_seed_without_register(self):
        est = SocEstimator(CAP)
        soc = est.update(0, 12.8, 0.0, 0.0)
        self.assertAlmostEqual(soc, 20, delta=0.5)


class TestIntegration(unittest.TestCase):
    def test_net_current_includes_charge(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=50)
        # 1 h at +5 A net -> +5 Ah * 0.99 = +9.9 %
        run(est, 0, 3600, step=10, voltage=13.3, i_charge=6.0, i_load=1.0)
        self.assertAlmostEqual(est.soc_pct, 59.9, delta=0.2)

    def test_discharge(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=60)
        run(est, 0, 7200, voltage=13.15, i_charge=0.0, i_load=2.5)
        self.assertAlmostEqual(est.soc_pct, 50.0, delta=0.2)

    def test_uncertainty_grows_without_anchor(self):
        est = SocEstimator(CAP)
        est.update(0, 12.8, 0.0, 0.0)               # narrow band -> confident seed
        c0 = est.confidence
        self.assertGreater(c0, 0.5)
        run(est, 0, 6 * 3600, step=60, voltage=13.2, i_charge=1.0, i_load=1.0)
        self.assertLess(est.confidence, c0)


class TestAnchors(unittest.TestCase):
    def test_full_on_charge_completion(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=40)
        t = run(est, 0, 300, voltage=14.2, i_charge=2.0, i_load=1.0, charging_state=CHG_FLOAT)
        self.assertLess(est.soc_pct, 99)            # not held long enough yet
        run(est, t, 400, voltage=14.2, i_charge=2.0, i_load=1.0, charging_state=CHG_FLOAT)
        self.assertEqual(est.soc_pct, 100.0)
        self.assertGreater(est.confidence, 0.9)

    def test_full_on_sustained_rest_voltage(self):
        est = SocEstimator(CAP)
        est.update(0, 13.45, 0.0, 0.2, hw_soc=80)
        run(est, 0, 1900, voltage=13.45, i_charge=0.0, i_load=0.2)
        self.assertEqual(est.soc_pct, 100.0)

    def test_no_rest_anchor_while_voltage_rising(self):
        est = SocEstimator(CAP)
        est.update(0, 13.1, 0.0, 0.2, hw_soc=40)
        t, v = 0, 13.1
        while t < 2400:                             # +0.6 V/h: external charger
            t += 10
            v += 0.6 / 360
            est.update(t, v, 0.0, 0.2)
        self.assertLess(est.soc_pct, 99)

    def test_low_clamp(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=60)
        # rest-compensated: 12.4 + 1.0 * 0.026 ~= 12.43 V -> ~10 %
        t = run(est, 0, 200, voltage=12.4, i_charge=0.0, i_load=1.0)
        self.assertGreater(est.soc_pct, 40)          # hold time not reached
        run(est, t, 200, voltage=12.4, i_charge=0.0, i_load=1.0)
        self.assertLess(est.soc_pct, 12)
        self.assertTrue(any('clamped low' in e for e in est.pop_events()))

    def test_raise_to_voltage_floor_on_external_charge(self):
        est = SocEstimator(CAP)
        est.update(0, 12.2, 0.0, 1.0)              # seeded ~6 %
        est.update(10, 13.35, 0.0, 1.0)            # external charger lifts voltage
        self.assertTrue(est.external_charge_suspected)
        self.assertGreaterEqual(est.soc_pct, plausible_band(13.35 + 0.026)[0] - 0.01)
        self.assertLessEqual(est.confidence, 0.21)


class TestPersistenceAndBridging(unittest.TestCase):
    def test_round_trip_and_bridge(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=60, counters=(1000, 900))
        est.err_ah = 1.0
        state = est.to_state(wall_t=1_000_000)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'sub', 'soc.json')
            soc_estimator.save_state(path, state)
            loaded = soc_estimator.load_state(path)
        est2 = SocEstimator(CAP)
        # 24 h later: +30 Ah in, -40 Ah out
        self.assertTrue(est2.restore(loaded, 1_000_000 + 86400, (1030, 940)))
        self.assertAlmostEqual(est2.soc_pct, 60 + (30 * 0.99 - 40) / CAP * 100, delta=0.1)
        self.assertLess(est2.confidence, est.confidence)
        self.assertIn('bridged', est2.pop_events()[0])

    def test_bridge_refused_on_counter_reset(self):
        est = SocEstimator(CAP)
        state = {'soc_ah': 25.0, 'err_ah': 1.0, 'wall_t': 0, 'counters': [1000, 900]}
        self.assertFalse(est.restore(state, 3600, (5, 3)))
        self.assertIsNone(est.soc_ah)

    def test_bridge_refused_after_max_days(self):
        est = SocEstimator(CAP)
        state = {'soc_ah': 25.0, 'err_ah': 1.0, 'wall_t': 0, 'counters': [1000, 900]}
        self.assertFalse(est.restore(state, 8 * 86400, (1100, 1000)))

    def test_missing_or_corrupt_state(self):
        self.assertIsNone(soc_estimator.load_state('/nonexistent/x.json'))
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            f.write('{not json')
        try:
            self.assertIsNone(soc_estimator.load_state(f.name))
        finally:
            os.unlink(f.name)
        self.assertFalse(SocEstimator(CAP).restore(None, 0, (1, 1)))

    def test_long_poll_gap_uses_counters(self):
        est = SocEstimator(CAP)
        est.update(0, 13.2, 0.0, 0.0, hw_soc=60, counters=(100, 100))
        est.update(7200, 13.2, 0.0, 0.0, counters=(100, 110))   # 2 h stall, 10 Ah out
        self.assertAlmostEqual(est.soc_pct, 40.0, delta=0.1)

    def test_configure_ignores_unknown_keys(self):
        est = SocEstimator(CAP, config={'r_internal_ohm': 0.05, 'state_file': '/x', 'bogus': 1})
        self.assertEqual(est.cfg['r_internal_ohm'], 0.05)
        self.assertNotIn('bogus', est.cfg)


class TestOct30Replay(unittest.TestCase):
    """Regression replay of the 2026-10-30 deep discharge (battery truly 0-8 %)."""

    @classmethod
    def setUpClass(cls):
        with open(FIXTURE) as f:
            lines = f.readlines()
        pre = [l for l in lines if l.startswith('# pre_outage:')][0]
        cls.pre = json.loads(pre.split(':', 1)[1])
        cls.rows = list(csv.DictReader(l for l in lines if not l.startswith('#')))

    def _replay(self, est):
        out = []
        for r in self.rows:
            v = float(r['battery_voltage'])
            soc = est.update(
                int(r['t']), v, float(r['solar_input_power']) * 0.967 / v,
                float(r['load_current']), charging_state=int(r['charging_state_code']),
                counters=(int(r['total_charging_ah']), int(r['total_discharging_ah'])),
            )
            out.append((soc, float(r['recorded_soc']), float(r['truth_soc'])))
        return out

    def test_cold_start_tracks_truth(self):
        est = SocEstimator(CAP)
        out = self._replay(est)
        recorded = [o[1] for o in out]
        new_err = max(abs(o[0] - o[2]) for o in out)
        self.assertGreater(max(recorded), 50)        # what the old code reported
        self.assertLess(max(o[0] for o in out), 20)  # never claims > 20 % that day
        self.assertLess(new_err, 12)

    def test_bridged_across_outage(self):
        # State saved at the Oct 26 outage start (truth 74 %), restored 4 days later
        est = SocEstimator(CAP)
        pre = self.pre
        est.soc_ah = pre['truth_soc'] / 100 * CAP
        est.err_ah = 1.0
        est.last_counters = tuple(pre['counters'])
        state = est.to_state(pre['t'])
        first = self.rows[0]
        est2 = SocEstimator(CAP)
        self.assertTrue(est2.restore(state, int(first['t']),
                                     (int(first['total_charging_ah']), int(first['total_discharging_ah']))))
        out = self._replay(est2)
        self.assertLess(max(abs(o[0] - o[2]) for o in out), 15)
        self.assertLess(out[-1][0], 10)


if __name__ == '__main__':
    unittest.main()
