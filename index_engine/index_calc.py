"""Capped market-cap-weighted index calculation.

Methodology (simplified, but the same mechanics real indices use)
-----------------------------------------------------------------
* Weights      : market cap = split-adjusted price x shares outstanding,
                 capped at `cap` per constituent; excess weight is
                 redistributed pro-rata to the uncapped names.
* Rebalancing  : quarterly, at the close of the last trading day of
                 Feb / May / Aug / Nov (and on the base date).
* Holdings     : at each rebalance the index "buys" units_i = w_i * L / P_i.
                 Between rebalances  L_t = sum_i units_i * P_i,t.
                 Because the new units are valued at the same closing prices,
                 the level is continuous through the rebalance (this is what a
                 divisor does in a classic index formula).
* Price vs total return: the same engine is run on split-adjusted prices
                 (price return) or on dividend-reinvested prices (total return).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

DEFAULT_REBALANCE_MONTHS = (2, 5, 8, 11)


@dataclass
class IndexResult:
    levels: pd.Series
    weights: pd.DataFrame    # rebalance date x ticker (target weights)
    units: pd.DataFrame      # rebalance date x ticker (holdings bought)


def rebalance_dates(dates: pd.DatetimeIndex, months=DEFAULT_REBALANCE_MONTHS) -> list[pd.Timestamp]:
    """Base date + last trading day of each rebalance month."""
    s = pd.Series(dates, index=dates)
    month_ends = s.groupby([dates.year, dates.month]).max()
    picks = [d for d in month_ends if d.month in months and d != dates[0]]
    return [dates[0], *picks]


def apply_cap(weights: pd.Series | np.ndarray, cap: float | None, tol: float = 1e-12) -> np.ndarray:
    """Iteratively cap weights and redistribute the excess pro-rata."""
    w = np.asarray(weights, dtype=float).copy()
    if w.sum() <= 0:
        raise ValueError("weights must have a positive sum")
    w = w / w.sum()
    if cap is None or cap >= 1:
        return w
    n_positive = int((w > 0).sum())
    if cap * n_positive < 1 - tol:
        raise ValueError(f"cap {cap:.2%} is infeasible with {n_positive} constituents")

    capped = np.zeros(len(w), dtype=bool)
    for _ in range(len(w) + 1):
        over = (w > cap + tol) & ~capped
        if not over.any():
            break
        capped |= over
        free = ~capped
        remaining = 1.0 - cap * capped.sum()
        w[capped] = cap
        w[free] = w[free] / w[free].sum() * remaining
    return w


def calculate_index(level_prices: pd.DataFrame, weight_prices: pd.DataFrame, shares: pd.Series,
                    rebal_dates: list[pd.Timestamp], base_value: float = 1000.0,
                    cap: float | None = None) -> IndexResult:
    dates, tickers = level_prices.index, level_prices.columns
    P = level_prices.to_numpy(float)
    W = weight_prices.reindex(index=dates, columns=tickers).to_numpy(float)
    sh = shares.reindex(tickers).to_numpy(float)
    rebal = set(pd.DatetimeIndex(rebal_dates)) | {dates[0]}

    levels = np.empty(len(dates))
    units = None
    w_hist, u_hist, r_dates = [], [], []

    for i, day in enumerate(dates):
        level = base_value if units is None else float(np.nansum(units * P[i]))
        levels[i] = level
        if day in rebal:
            mcap = W[i] * sh
            valid = np.isfinite(mcap) & (mcap > 0) & np.isfinite(P[i]) & (P[i] > 0)
            w = np.zeros(len(tickers))
            w[valid] = apply_cap(mcap[valid], cap)
            units = np.where(valid, w * level / np.where(valid, P[i], 1.0), 0.0)
            w_hist.append(w)
            u_hist.append(units.copy())
            r_dates.append(day)

    idx = pd.DatetimeIndex(r_dates, name="rebalance_date")
    return IndexResult(
        levels=pd.Series(levels, index=dates, name="level"),
        weights=pd.DataFrame(w_hist, index=idx, columns=tickers),
        units=pd.DataFrame(u_hist, index=idx, columns=tickers),
    )


def shadow_calculate(level_prices: pd.DataFrame, weights: pd.DataFrame, base_value: float = 1000.0) -> pd.Series:
    """Independent recalculation (no units): within each period
    L_t = L_r * sum_i w_i * P_i,t / P_i,r. Index providers run a 'shadow'
    calculation like this to catch bugs in the production calculation."""
    dates = level_prices.index
    levels = pd.Series(np.nan, index=dates, name="shadow_level")
    levels.iloc[0] = base_value
    r_dates = list(weights.index)
    for k, r in enumerate(r_dates):
        end = r_dates[k + 1] if k + 1 < len(r_dates) else dates[-1]
        seg = level_prices.loc[r:end]
        rel = seg / seg.iloc[0]
        w = weights.loc[r]
        base = levels.loc[r]
        levels.loc[seg.index[1:]] = base * (rel.iloc[1:] * w).sum(axis=1)
    return levels
