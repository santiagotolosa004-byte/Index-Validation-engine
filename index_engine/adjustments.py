"""Corporate-action adjustments.

split-adjusted price  : every price *before* a split of ratio k is divided by k,
                        so the series is continuous across the split.
total-return series   : daily gross return = (P_t + D_t) / P_{t-1}, i.e. the
                        dividend is reinvested on its ex-date.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class AdjustedPrices:
    adjusted: pd.DataFrame      # split-adjusted closes (used for price return + weights)
    dividends: pd.DataFrame     # split-adjusted dividends on ex-dates (0 elsewhere)
    total_return: pd.DataFrame  # dividend-reinvested series (used for total return)


def _trading_day(index: pd.DatetimeIndex, day: pd.Timestamp) -> pd.Timestamp | None:
    """Map an action date to the first trading day on or after it."""
    pos = index.searchsorted(day)
    return index[pos] if pos < len(index) else None


def adjust(raw: pd.DataFrame, actions: pd.DataFrame) -> AdjustedPrices:
    adj = raw.astype(float).copy()
    divs = pd.DataFrame(0.0, index=raw.index, columns=raw.columns)

    for a in actions[actions["action_type"] == "dividend"].itertuples(index=False):
        day = _trading_day(raw.index, a.date)
        if a.ticker in divs.columns and day is not None:
            divs.at[day, a.ticker] += a.value

    for a in actions[actions["action_type"] == "split"].itertuples(index=False):
        if a.ticker not in adj.columns or a.value <= 0:
            continue
        before = raw.index < a.date
        adj.loc[before, a.ticker] /= a.value
        divs.loc[before, a.ticker] /= a.value

    with np.errstate(divide="ignore", invalid="ignore"):  # a zero price -> inf, kept visible
        gross = (adj + divs) / adj.shift(1)
        first_valid = adj.bfill().iloc[0]
        tr = gross.fillna(1.0).cumprod() * first_valid
    tr = tr.where(adj.notna())
    return AdjustedPrices(adjusted=adj, dividends=divs, total_return=tr)
