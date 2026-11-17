"""Risk / return comparison of the index against a benchmark."""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def compare(index_levels: pd.Series, benchmark: pd.Series) -> dict[str, float]:
    df = pd.concat({"index": index_levels, "bench": benchmark}, axis=1).dropna()
    if len(df) < 3:
        raise ValueError("not enough overlapping dates with the benchmark")
    r = df.pct_change().dropna()
    years = len(r) / TRADING_DAYS

    def ann_return(s: pd.Series) -> float:
        return (s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1

    active = r["index"] - r["bench"]
    te = active.std() * np.sqrt(TRADING_DAYS)
    ret_i, ret_b = ann_return(df["index"]), ann_return(df["bench"])
    return {
        "start": df.index[0],
        "end": df.index[-1],
        "observations": len(r),
        "index_ann_return": ret_i,
        "bench_ann_return": ret_b,
        "index_ann_vol": r["index"].std() * np.sqrt(TRADING_DAYS),
        "bench_ann_vol": r["bench"].std() * np.sqrt(TRADING_DAYS),
        "tracking_error": te,
        "information_ratio": (ret_i - ret_b) / te if te > 0 else np.nan,
        "correlation": r["index"].corr(r["bench"]),
        "beta": r["index"].cov(r["bench"]) / r["bench"].var(),
        "index_max_drawdown": (df["index"] / df["index"].cummax() - 1).min(),
        "bench_max_drawdown": (df["bench"] / df["bench"].cummax() - 1).min(),
    }


def impact(as_received: pd.Series, remediated: pd.Series) -> pd.Series:
    """Difference between the two index versions, in basis points."""
    return ((as_received / remediated - 1) * 1e4).rename("impact_bps")
