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
