#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Battery state-of-charge estimator (standard library only, no hardware access).

The Renogy SOC register is voltage-driven and badly wrong on LiFePO4 (it read
57-92 % on 2026-10-30 while the battery was at 0-8 %). This estimator:

- Integrates NET battery current (charge current in minus load current out).
- Anchors to 100 % on charge completion (boost/float/equalize at >= 14.0 V,
  sustained) or a sustained high rest-equivalent voltage (>= 13.4 V, not
  charging, not rising).
- Clamps down when the rest-compensated voltage implies <= 20 % SOC.
- Raises to the voltage floor when voltage is far above what the integrated
  SOC allows while metered current says "discharging" (external charger on
  the battery terminals, invisible to the controller).
- Sanity-checks the hardware SOC register against voltage; it is exported but
  never trusted after the initial seed.
- Bridges restarts and Pi outages with the controller's cumulative charging /
  discharging Ah counters, which keep counting while the Pi is off.
- Tracks an uncertainty (Ah) that grows with throughput and time and resets
  on anchors; confidence = 1 - err / (25 % of capacity).

All parameters come from config.json (renogy.soc_estimator) via DEFAULTS.
"""

import json
import os

# At-rest voltage-to-SOC tables for a 12 V pack (>= 1 h with ~no current).
# Under load or charge, compensate first: V_rest ~= V - I_net * R_internal.
# LiFePO4 12.80 V = 20 % matches the 12.8 V / 20 % low-battery alert thresholds.
SOC_TABLES = {
    'lifepo4': [
        (13.60, 100), (13.40, 98), (13.30, 90), (13.25, 80), (13.20, 70),
        (13.15, 55), (13.10, 40), (13.00, 30), (12.90, 25), (12.80, 20),
        (12.60, 14), (12.40,  9), (12.20,  6), (12.00,  4), (11.50,  2),
        (10.00,  0),
    ],
    'lead_acid': [
        (12.70, 100), (12.50, 90), (12.42, 75), (12.32, 50),
        (12.20, 25), (12.06, 10), (11.90,  0),
    ],
}

# Voltage range where the curve is too flat to locate SOC (V_lo, V_hi, SOC_lo, SOC_hi):
# inside it, any SOC between SOC_lo and SOC_hi is considered plausible.
FLAT_REGIONS = {
    'lifepo4': (12.95, 13.45, 20.0, 95.0),
}

# Renogy charging_state_code values that indicate end-of-charge stages
CHG_EQUALIZING, CHG_BOOST, CHG_FLOAT = 3, 4, 5

DEFAULTS = {
    'r_internal_ohm': 0.026,          # measured from 112 aerator on/off steps
    'charge_efficiency': 0.99,        # coulombic efficiency of LiFePO4
    'unmetered_load_a': 0.0,          # everything is on the load terminal
    'full_charge_voltage': 14.0,
    'full_charge_hold_seconds': 600,
    'rest_full_voltage': 13.4,
    'rest_full_hold_seconds': 1800,
    'rest_full_max_charge_a': 0.3,
    'rest_rising_v_per_hour': 0.15,
    'low_clamp_soc_pct': 20.0,
    'low_clamp_hold_seconds': 300,
    'voltage_margin_v': 0.10,         # battery_voltage register resolution is 0.1 V
    'hw_tolerance_pct': 5.0,
    'max_integration_gap_seconds': 900,
    'max_bridge_days': 7,
}


def rest_voltage_to_soc(voltage, chemistry='lifepo4'):
    """Linearly interpolate SOC (%) from an at-rest voltage."""
    table = SOC_TABLES.get(chemistry, SOC_TABLES['lifepo4'])
    if voltage >= table[0][0]:
        return float(table[0][1])
    if voltage <= table[-1][0]:
        return float(table[-1][1])
    for (v_hi, soc_hi), (v_lo, soc_lo) in zip(table, table[1:]):
        if v_lo <= voltage <= v_hi:
            return soc_lo + (voltage - v_lo) / (v_hi - v_lo) * (soc_hi - soc_lo)
    return None


def plausible_band(v_rest, chemistry='lifepo4', margin_v=0.10):
    """SOC range (lo, hi) consistent with a rest-equivalent voltage."""
    lo = rest_voltage_to_soc(v_rest - margin_v, chemistry)
    hi = rest_voltage_to_soc(v_rest + margin_v, chemistry)
    flat = FLAT_REGIONS.get(chemistry)
    if flat and flat[0] <= v_rest <= flat[1]:
        lo, hi = min(lo, flat[2]), max(hi, flat[3])
    return lo, hi


class SocEstimator:
    def __init__(self, capacity_ah, chemistry='lifepo4', config=None):
        self.cap = float(capacity_ah)
        self.chemistry = chemistry
        self.configure(config)
        self.soc_ah = None
        self.err_ah = None
        self.last_t = None
        self.last_counters = None      # (total_charging_ah, total_discharging_ah)
        self.v_rest = None
        self.i_net = None
        self.hw_rejected = False
        self.external_charge_suspected = False
        self.last_event = 'init'
        self._events = []
        self._full_since = None
        self._rest_full_since = None
        self._low_since = None
        self._vhist = []               # (t, v_rest) over the last hour

    def configure(self, config=None):
        """(Re)load parameters; unknown keys are ignored, missing keys use DEFAULTS."""
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in (config or {}).items() if k in DEFAULTS})
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    @property
    def soc_pct(self):
        return None if self.soc_ah is None else 100.0 * self.soc_ah / self.cap

    @property
    def remaining_ah(self):
        return self.soc_ah

    @property
    def confidence(self):
        """1.0 right after an anchor; falls to 0 as uncertainty reaches 25 % of capacity."""
        if self.err_ah is None:
            return 0.0
        return max(0.0, min(1.0, 1.0 - self.err_ah / (0.25 * self.cap)))

    def pop_events(self):
        """Return and clear notable events (seed, anchor, clamp, bridge) for logging."""
        events, self._events = self._events, []
        return events

    def _event(self, text):
        self.last_event = text
        self._events.append(text)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_state(self, wall_t):
        return {
            'soc_ah': self.soc_ah,
            'err_ah': self.err_ah,
            'wall_t': wall_t,
            'counters': list(self.last_counters) if self.last_counters else None,
        }

    def restore(self, state, wall_t, counters):
        """
        Resume from a saved state, adding the charge that moved while this
        process was not running (from the controller's cumulative counters).
        Returns True if restored; False leaves the estimator unseeded.
        """
        if not state or state.get('soc_ah') is None or state.get('wall_t') is None:
            return False
        c0 = state.get('counters')
        gap_s = wall_t - state['wall_t']
        why = None
        if not c0 or not counters:
            why = 'controller counters missing'
        elif counters[0] < c0[0] or counters[1] < c0[1]:
            why = 'controller counters reset'
        elif gap_s > self.cfg['max_bridge_days'] * 86400:
            why = f'gap {gap_s / 86400:.1f} days too long'
        if why:
            self._event(f'bridge failed ({why}); reseeding')
            return False
        self.soc_ah = float(state['soc_ah'])
        self.err_ah = float(state.get('err_ah') or 0.0)
        if gap_s < 0:
            # No RTC: after a power loss the clock restarts from fake-hwclock and
            # can be behind the saved timestamp until NTP syncs. The counters are
            # still exact, so bridge with them and treat the duration as unknown.
            self._event(f'clock is {-gap_s:.0f}s behind the saved state (no RTC/NTP yet); '
                        f'bridging with counters only')
            gap_s = 0.0
        self._bridge(c0, counters, gap_s / 3600.0, 'restart')
        return True

    def _bridge(self, c0, c1, hours, why):
        d_in, d_out = c1[0] - c0[0], c1[1] - c0[1]
        self.soc_ah += d_in * self.cfg['charge_efficiency'] - d_out
        self.soc_ah -= self.cfg['unmetered_load_a'] * hours
        self.soc_ah = max(0.0, min(self.cap, self.soc_ah))
        # Counter registers are whole Ah (+-1 each), plus 2 % of throughput
        self.err_ah += 2.0 + 0.02 * (d_in + d_out) + 0.05 * hours
        self.last_counters = tuple(c1)
        self._event(f'bridged {why} gap {hours:.1f}h: +{d_in:.0f}Ah in, -{d_out:.0f}Ah out '
                    f'-> {self.soc_pct:.1f}%')

    def _seed(self, v_rest, hw_soc):
        lo, hi = plausible_band(v_rest, self.chemistry, self.cfg['voltage_margin_v'])
        tol = self.cfg['hw_tolerance_pct']
        if hw_soc is not None and lo - tol <= hw_soc <= hi + tol:
            soc = float(hw_soc)
            src = f'hardware register {hw_soc:.0f}%'
        else:
            soc = rest_voltage_to_soc(v_rest, self.chemistry)
            src = f'rest voltage {v_rest:.2f}V'
            if hw_soc is not None:
                src += f' (hardware {hw_soc:.0f}% rejected)'
        self.soc_ah = soc / 100.0 * self.cap
        self.err_ah = max(2.5, (hi - lo) / 100.0 * self.cap / 2)
        self._event(f'seeded {soc:.1f}% from {src}')

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    def update(self, t, voltage, i_charge, i_load, charging_state=None,
               hw_soc=None, counters=None):
        """
        One poll. t: monotonic seconds; voltage: battery V; i_charge: A into
        the battery from the controller; i_load: A out of the load terminal;
        counters: (total_charging_ah, total_discharging_ah) or None.
        Returns SOC in percent.
        """
        cfg = self.cfg
        i_net = (i_charge or 0.0) - (i_load or 0.0) - cfg['unmetered_load_a']
        v_rest = voltage - i_net * cfg['r_internal_ohm']
        self.i_net, self.v_rest = i_net, v_rest
        counters = tuple(counters) if counters else None

        if self.soc_ah is None:
            self._seed(v_rest, hw_soc)
            self.last_t = t
            self.last_counters = counters

        dt_s = 0.0 if self.last_t is None else max(0.0, t - self.last_t)
        self.last_t = t

        if dt_s > cfg['max_integration_gap_seconds']:
            # Long stall inside one process: use the controller counters rather
            # than extrapolating a stale current reading.
            if (self.last_counters and counters and counters[0] >= self.last_counters[0]
                    and counters[1] >= self.last_counters[1]):
                self._bridge(self.last_counters, counters, dt_s / 3600.0, 'poll')
            else:
                self.err_ah += 0.1 * self.cap
                self._event(f'poll gap {dt_s / 3600.0:.1f}h without usable counters')
        else:
            dt_h = dt_s / 3600.0
            d_ah = (i_net * cfg['charge_efficiency'] if i_net > 0 else i_net) * dt_h
            self.soc_ah = max(0.0, min(self.cap, self.soc_ah + d_ah))
            self.err_ah += 0.02 * abs(d_ah) + 0.05 * dt_h
        if counters:
            self.last_counters = counters

        # "Not rising" guard for the rest anchor (an external charger raises voltage slowly)
        self._vhist.append((t, v_rest))
        while self._vhist and t - self._vhist[0][0] > 3600:
            self._vhist.pop(0)
        v_rising = (len(self._vhist) > 5 and
                    v_rest - min(v for _, v in self._vhist) > cfg['rest_rising_v_per_hour'])

        # Anchor: charge completion
        if (charging_state in (CHG_BOOST, CHG_FLOAT, CHG_EQUALIZING)
                and voltage >= cfg['full_charge_voltage']):
            self._full_since = self._full_since if self._full_since is not None else t
            if t - self._full_since >= cfg['full_charge_hold_seconds']:
                self._anchor_full('charge completion')
        else:
            self._full_since = None

        # Anchor: sustained high rest-equivalent voltage without PV charging
        if (v_rest >= cfg['rest_full_voltage'] and (i_charge or 0.0) < cfg['rest_full_max_charge_a']
                and not v_rising):
            self._rest_full_since = self._rest_full_since if self._rest_full_since is not None else t
            if t - self._rest_full_since >= cfg['rest_full_hold_seconds']:
                self._anchor_full(f'sustained rest voltage {v_rest:.2f}V')
        else:
            self._rest_full_since = None

        # Clamp low: voltage says <= low_clamp_soc while discharging
        v_soc = rest_voltage_to_soc(v_rest, self.chemistry)
        if i_net <= 0 and v_soc <= cfg['low_clamp_soc_pct']:
            self._low_since = self._low_since if self._low_since is not None else t
            if t - self._low_since >= cfg['low_clamp_hold_seconds'] and self.soc_pct > v_soc + 3:
                self.soc_ah = v_soc / 100.0 * self.cap
                self.err_ah = min(self.err_ah, 2.5)
                self._event(f'clamped low to {v_soc:.0f}% at rest voltage {v_rest:.2f}V')
        else:
            self._low_since = None

        # Unmetered charge: metered current says discharging, voltage says higher SOC
        lo, hi = plausible_band(v_rest, self.chemistry, cfg['voltage_margin_v'])
        tol = cfg['hw_tolerance_pct']
        suspected = i_net <= 0 and self.soc_pct < lo - tol
        if suspected:
            self.soc_ah = lo / 100.0 * self.cap
            self.err_ah = max(self.err_ah, 10.0)
            if not self.external_charge_suspected:
                self._event(f'raised to voltage floor {lo:.0f}% at {v_rest:.2f}V '
                            f'(external charger or unmetered charge?)')
        self.external_charge_suspected = suspected

        # Hardware register sanity check (reported only)
        self.hw_rejected = hw_soc is not None and not (lo - tol <= hw_soc <= hi + tol)
        return self.soc_pct

    def _anchor_full(self, why):
        if self.soc_pct < 99.5 or self.err_ah > 1.0:
            self._event(f'anchored 100% on {why} (was {self.soc_pct:.1f}%)')
        self.soc_ah = self.cap
        self.err_ah = 0.5


# ----------------------------------------------------------------------
# State file helpers
# ----------------------------------------------------------------------

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_state(path, state):
    """Atomic write (tmp + rename)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, path)
