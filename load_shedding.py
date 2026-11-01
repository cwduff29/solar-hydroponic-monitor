#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Battery load-shedding decisions (pure functions, no GPIO; unit-testable).

The SOC tier is a CAP on the temperature-based aeration schedule: the
temperature tier picks the schedule, and a reduced/disabled SOC tier can
only shorten the on-time and lengthen the off-time, never the reverse.
"""

TIER_NORMAL = 0
TIER_REDUCED = 1
TIER_DISABLED = 2
TIER_NAMES = {TIER_NORMAL: 'normal', TIER_REDUCED: 'reduced', TIER_DISABLED: 'disabled'}


def soc_tier(soc, prev_tier, disable_pct, reduce_pct, hysteresis_pct=2.0):
    """
    Load-shedding tier for a battery SOC (%).

    Shedding gets stricter as soon as SOC drops below a threshold; it relaxes
    only once SOC is hysteresis_pct above that threshold. Unknown SOC (None)
    keeps the previous tier, or normal if there is none.
    """
    if soc is None:
        return TIER_NORMAL if prev_tier is None else prev_tier

    if soc < disable_pct:
        target = TIER_DISABLED
    elif soc < reduce_pct:
        target = TIER_REDUCED
    else:
        target = TIER_NORMAL

    if prev_tier is None or target >= prev_tier:
        return target

    # Relaxing: require the hysteresis margin above the threshold being crossed
    if prev_tier == TIER_DISABLED and soc < disable_pct + hysteresis_pct:
        return TIER_DISABLED
    if target == TIER_NORMAL and soc < reduce_pct + hysteresis_pct:
        return TIER_REDUCED
    return target


def temperature_tier(water_temp_c, hot_c, warm_c, moderate_c):
    """Name of the aeration temperature tier for a water temperature (C)."""
    if water_temp_c > hot_c:
        return 'hot'
    if water_temp_c > warm_c:
        return 'warm'
    if water_temp_c > moderate_c:
        return 'moderate'
    return 'cool'


def aeration_schedule(tier, normal, reduced, temp_schedule=None):
    """
    (on_seconds, off_seconds) for the aerator.

    tier:          SOC tier (TIER_*)
    normal:        (on, off) used when no water temperature is available
    reduced:       (on, off) cap applied in the reduced tier
    temp_schedule: (on, off) from the water temperature tier, or None

    Disabled returns on=0 (never switch on). Reduced returns
    on = min(base_on, reduced_on), off = max(base_off, reduced_off).
    """
    if tier == TIER_DISABLED:
        return 0, None
    on_s, off_s = temp_schedule if temp_schedule is not None else normal
    if tier == TIER_REDUCED:
        on_s, off_s = min(on_s, reduced[0]), max(off_s, reduced[1])
    return on_s, off_s


# ----------------------------------------------------------------------
# Main pump low-SOC power saving (both tiers disabled by default)
# ----------------------------------------------------------------------

PUMP_NORMAL = 0
PUMP_REDUCED = 1
PUMP_CYCLING = 2
PUMP_MODE_NAMES = {PUMP_NORMAL: 'normal', PUMP_REDUCED: 'reduced', PUMP_CYCLING: 'cycling'}

# NFT roots must never go longer than this without flow, whatever the config says.
HARD_MAX_OFF_MINUTES = 30

PUMP_DEFAULTS = {
    'reduced_speed': {
        'enabled': False,
        'enter_below_soc_pct': 40,
        'exit_above_soc_pct': 50,
        'duty_cycle_pct': 55,
    },
    'cycling': {
        'enabled': False,
        'enter_below_soc_pct': 20,
        'exit_above_soc_pct': 30,
        'on_minutes': 15,
        'off_minutes': 15,
        'max_off_minutes': 30,
        'skip_at_or_above_water_temp_c': 22.8,
        'restart_grace_seconds': 60,
    },
    'min_confidence': 0.3,
}


def pump_config(cfg):
    """Merge a (possibly partial) config dict over PUMP_DEFAULTS."""
    cfg = cfg or {}
    out = {'min_confidence': cfg.get('min_confidence', PUMP_DEFAULTS['min_confidence'])}
    for section in ('reduced_speed', 'cycling'):
        merged = dict(PUMP_DEFAULTS[section])
        merged.update(cfg.get(section) or {})
        out[section] = merged
    return out


class MainPumpLowSoc:
    """
    Decides how the main pump should run at low battery SOC.

    Tier A (reduced_speed): lower PWM duty while SOC is low.
    Tier B (cycling): alternate on/off windows; the pump always starts a
    cycling episode in an ON window, and an OFF window never exceeds
    min(off_minutes, max_off_minutes, HARD_MAX_OFF_MINUTES).

    Everything falls back to normal running when: SOC is unknown, the SOC
    estimate's confidence is below min_confidence, the backup pump is
    active, a flow/leak alert is active (cycling only), or the water
    temperature is unknown or warm (cycling only).
    """

    def __init__(self):
        self.mode = PUMP_NORMAL
        self.phase_on = True
        self.phase_since = None
        self.reason = 'normal'

    def update(self, now, cfg, soc, confidence, water_temp_c=None,
               flow_alert_active=False, backup_active=False):
        """
        Returns (mode, run, duty_pct): run=False means a planned OFF window;
        duty_pct=None means "default duty".
        """
        cfg = pump_config(cfg)
        red, cyc = cfg['reduced_speed'], cfg['cycling']

        blocked = None
        if backup_active:
            blocked = 'backup pump active'
        elif soc is None:
            blocked = 'SOC unknown'
        elif confidence is None or confidence < cfg['min_confidence']:
            blocked = 'SOC confidence low'
        if blocked:
            self._set(PUMP_NORMAL, now, blocked)
            return PUMP_NORMAL, True, None

        # Tier A with hysteresis
        reduced = False
        if red['enabled']:
            if self.mode >= PUMP_REDUCED:
                reduced = soc <= red['exit_above_soc_pct']
            else:
                reduced = soc < red['enter_below_soc_pct']

        # Tier B with hysteresis and safety skips
        cycling = False
        if cyc['enabled']:
            if self.mode == PUMP_CYCLING:
                cycling = soc <= cyc['exit_above_soc_pct']
            else:
                cycling = soc < cyc['enter_below_soc_pct']
            if cycling:
                if flow_alert_active:
                    cycling, why = False, 'flow alert active'
                elif water_temp_c is None:
                    cycling, why = False, 'water temperature unknown'
                elif water_temp_c >= cyc['skip_at_or_above_water_temp_c']:
                    cycling, why = False, f'water {water_temp_c:.1f}C too warm'
                else:
                    why = f'SOC {soc:.0f}%'
            else:
                why = f'SOC {soc:.0f}%'
        duty = red['duty_cycle_pct'] if reduced else None

        if cycling:
            self._set(PUMP_CYCLING, now, why)
            on_s = max(1.0, float(cyc['on_minutes'])) * 60
            off_s = min(float(cyc['off_minutes']), float(cyc['max_off_minutes']),
                        HARD_MAX_OFF_MINUTES) * 60
            if self.phase_on and now - self.phase_since >= on_s and off_s > 0:
                self.phase_on, self.phase_since = False, now
            elif not self.phase_on and now - self.phase_since >= off_s:
                self.phase_on, self.phase_since = True, now
            return PUMP_CYCLING, self.phase_on, duty

        if reduced:
            self._set(PUMP_REDUCED, now, f'SOC {soc:.0f}%')
            return PUMP_REDUCED, True, duty

        self._set(PUMP_NORMAL, now, 'normal' if not cyc['enabled'] else why)
        return PUMP_NORMAL, True, None

    def _set(self, mode, now, reason):
        if mode != self.mode or self.phase_since is None:
            # Every new mode (in particular a new cycling episode) starts ON
            self.phase_on, self.phase_since = True, now
        self.mode = mode
        self.reason = reason
