"""Turn validated raw data into clean inputs for the calculation.

The rule of thumb: fix only what is clearly wrong and can be fixed
mechanically, log every change, and leave judgement calls (genuine jumps,
stale prices) untouched but flagged for review.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class CleanData:
    prices: pd.DataFrame      # wide: date x ticker, raw (unadjusted) closes, gaps filled
    actions: pd.DataFrame     # corrected corporate actions
    universe: pd.DataFrame
    log: pd.DataFrame         # one row per change made


def to_wide(prices: pd.DataFrame, keep: str = "first") -> pd.DataFrame:
    """Pivot long prices to date x ticker. `keep` decides which duplicate wins."""
    px = prices.sort_values("_row").drop_duplicates(["ticker", "date"], keep=keep)
    return px.pivot(index="date", columns="ticker", values="close").sort_index()


def remediate(prices: pd.DataFrame, actions: pd.DataFrame, universe: pd.DataFrame,
              exceptions: pd.DataFrame) -> CleanData:
    changes: list[tuple] = []
    exc = exceptions.copy()
    keys = lambda check: set(
        zip(exc.loc[exc["check"] == check, "ticker"], exc.loc[exc["check"] == check, "date"])
    )

    # 1) duplicates: first record received wins (to_wide keep="first")
    wide = to_wide(prices, keep="first")
    for t, d in keys("duplicate_record"):
        changes.append((t, d, "duplicate_record", "dropped later duplicate record(s)"))

    # 2) non-positive prices and bad ticks -> treat as missing
    for check in ("non_positive_price", "price_spike"):
        for t, d in keys(check):
            if t in wide.columns and d in wide.index:
                changes.append((t, d, check, f"removed price {wide.at[d, t]:.2f}"))
                wide.at[d, t] = np.nan

    # 3) constituents without data are dropped
    no_data = set(exc.loc[exc["check"] == "no_data", "ticker"])
    universe = universe[~universe["ticker"].isin(no_data)].reset_index(drop=True)
    wide = wide.reindex(columns=universe["ticker"])

    # 4) fill gaps on the full trading calendar with the last valid price
    filled_mask = wide.isna() & wide.ffill().notna()
    wide = wide.ffill()
    for t in wide.columns:
        for d in wide.index[filled_mask[t].to_numpy()]:
            changes.append((t, d, "fill", "last valid price carried forward"))

    # 5) corporate actions
    acts = actions.copy()
    bad_splits = keys("split_not_reflected")
    drop = acts.apply(lambda a: a["action_type"] == "split" and (a["ticker"], a["date"]) in bad_splits, axis=1)
    for a in acts[drop].itertuples(index=False):
        changes.append((a.ticker, a.date, "split_not_reflected", f"removed recorded split {a.value:g}"))
    acts = acts[~drop]

    bad_divs = keys("dividend_outlier")
    drop = acts.apply(lambda a: a["action_type"] == "dividend" and (a["ticker"], a["date"]) in bad_divs, axis=1)
    for a in acts[drop].itertuples(index=False):
        changes.append((a.ticker, a.date, "dividend_outlier", f"excluded dividend {a.value:.2f}"))
    acts = acts[~drop]

    unrec = exc[exc["check"] == "possible_unrecorded_split"]
    if not unrec.empty:
        provisional = pd.DataFrame({"ticker": unrec["ticker"], "date": unrec["date"],
                                    "action_type": "split", "value": unrec["value"].astype(float)})
        for a in provisional.itertuples(index=False):
            changes.append((a.ticker, a.date, "possible_unrecorded_split", f"added provisional split {a.value:g}"))
        acts = pd.concat([acts, provisional], ignore_index=True)

    acts = acts.sort_values(["ticker", "date"]).reset_index(drop=True)
    log = pd.DataFrame(changes, columns=["ticker", "date", "reason", "change"])
    return CleanData(prices=wide, actions=acts, universe=universe, log=log)
