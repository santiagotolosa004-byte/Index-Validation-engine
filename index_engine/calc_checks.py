"""Post-calculation controls: the 'testing campaign' that must pass before an
index level is published."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .index_calc import IndexResult, shadow_calculate


def run_calc_checks(result: IndexResult, level_prices: pd.DataFrame, cap: float | None,
                    base_value: float = 1000.0, max_daily_move: float = 0.15) -> pd.DataFrame:
    rows = []

    def add(name, ok, detail, warn_only=False):
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        rows.append((name, status, detail))

    w = result.weights
    sums = w.sum(axis=1)
    add("weights_sum_to_one", bool(np.allclose(sums, 1.0, atol=1e-9)),
        f"max abs(sum - 1) = {np.abs(sums - 1).max():.2e} across {len(w)} rebalances")

    if cap is not None:
        max_w = w.max(axis=1).max()
        add("cap_respected", bool(max_w <= cap + 1e-9), f"max weight {max_w:.2%} vs cap {cap:.2%}")

    # continuity: new holdings valued at rebalance-day prices == published level
    gaps = []
    for day in result.units.index:
        value = float(np.nansum(result.units.loc[day].to_numpy() * level_prices.loc[day].to_numpy()))
        gaps.append(abs(value / result.levels.loc[day] - 1))
    add("rebalance_continuity", bool(max(gaps) < 1e-10), f"max level gap at rebalance = {max(gaps):.2e}")

    add("no_missing_levels", bool(result.levels.notna().all() and np.isfinite(result.levels).all()),
        f"{int(result.levels.isna().sum())} missing levels")

    shadow = shadow_calculate(level_prices, result.weights, base_value)
    diff = (shadow / result.levels - 1).abs().max()
    add("shadow_calculation_match", bool(diff < 1e-9), f"max relative difference = {diff:.2e}")

    daily = result.levels.pct_change().abs()
    worst = daily.max()
    add("daily_move_within_bounds", bool(worst <= max_daily_move),
        f"largest daily move {worst:.2%} on {daily.idxmax():%Y-%m-%d} (limit {max_daily_move:.0%})",
        warn_only=True)

    return pd.DataFrame(rows, columns=["check", "status", "detail"])
