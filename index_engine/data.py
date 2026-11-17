"""Data contracts and loaders.

The engine works the way index vendors do: from *unadjusted* (raw) closing
prices plus a separate corporate-actions table (splits and dividends).
Adjustments are applied by the engine itself, so they can be validated.

Tables
------
prices   : ticker, date, close, _row      (_row = order in which records arrived)
actions  : ticker, date, action_type, value
           action_type = "split"    -> value = new shares per old share (4.0 for a 4:1 split)
           action_type = "dividend" -> value = cash per share, in raw (unadjusted) terms
universe : ticker, name, sector, shares_outstanding  (current, split-adjusted share count)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PRICE_COLUMNS = ["ticker", "date", "close", "_row"]
ACTION_COLUMNS = ["ticker", "date", "action_type", "value"]
UNIVERSE_COLUMNS = ["ticker", "name", "sector", "shares_outstanding"]

DEFAULT_UNIVERSE = Path(__file__).resolve().parent.parent / "config" / "universe.csv"


@dataclass
class RawData:
    prices: pd.DataFrame
    actions: pd.DataFrame
    universe: pd.DataFrame
    benchmark: pd.Series | None
    benchmark_name: str
    source: str
    ground_truth: pd.DataFrame | None = None  # only for synthetic data


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Return prices with the standard columns/dtypes and an arrival-order `_row`."""
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["close"] = out["close"].astype(float)
    out["ticker"] = out["ticker"].astype(str)
    if "_row" not in out.columns:
        out["_row"] = np.arange(len(out))
    return out[PRICE_COLUMNS].reset_index(drop=True)


def normalize_actions(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(
            {"ticker": pd.Series(dtype=str), "date": pd.Series(dtype="datetime64[ns]"),
             "action_type": pd.Series(dtype=str), "value": pd.Series(dtype=float)}
        )
    out = df.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.normalize()
    out["value"] = out["value"].astype(float)
    out["ticker"] = out["ticker"].astype(str)
    out["action_type"] = out["action_type"].astype(str).str.lower()
    return out[ACTION_COLUMNS].sort_values(["ticker", "date"]).reset_index(drop=True)


def load_universe_csv(path: str | Path = DEFAULT_UNIVERSE) -> pd.DataFrame:
    uni = pd.read_csv(path)
    missing = set(UNIVERSE_COLUMNS) - set(uni.columns)
    if missing:
        raise ValueError(f"universe file is missing columns: {sorted(missing)}")
    uni["shares_outstanding"] = uni["shares_outstanding"].astype(float)
    return uni[UNIVERSE_COLUMNS]


# ---------------------------------------------------------------------------
# Yahoo Finance
# ---------------------------------------------------------------------------

def from_yahoo_frame(df: pd.DataFrame, tickers: list[str] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert a `yfinance.download(..., auto_adjust=False, actions=True)` frame
    into the engine's raw prices + actions tables.

    Yahoo's `Close` (and its dividends) are already *split-adjusted*. The engine
    expects raw prices, so we undo the split adjustment: every price/dividend
    before a split is multiplied by the ratios of all splits that happen after it.
    """
    if not isinstance(df.columns, pd.MultiIndex):
        if not tickers or len(tickers) != 1:
            raise ValueError("single-level columns require exactly one ticker")
        df = pd.concat({tickers[0]: df}, axis=1).swaplevel(0, 1, axis=1)

    close = df["Close"].astype(float)
    splits = df["Stock Splits"].astype(float) if "Stock Splits" in df.columns.get_level_values(0) \
        else pd.DataFrame(0.0, index=close.index, columns=close.columns)
    divs = df["Dividends"].astype(float) if "Dividends" in df.columns.get_level_values(0) \
        else pd.DataFrame(0.0, index=close.index, columns=close.columns)

    splits = splits.fillna(0.0)
    divs = divs.fillna(0.0)
    ratios = splits.where(splits > 0, 1.0)
    # product of split ratios on or after each date, then remove the current day
    on_or_after = ratios.iloc[::-1].cumprod().iloc[::-1]
    strictly_after = on_or_after / ratios

    raw_close = close * strictly_after
    raw_divs = divs * strictly_after

    prices = (
        raw_close.rename_axis(index="date", columns="ticker")
        .stack()
        .rename("close")
        .reset_index()
        .dropna(subset=["close"])
    )
    prices = prices.sort_values(["ticker", "date"]).reset_index(drop=True)
    prices["_row"] = np.arange(len(prices))

    split_rows = (
        splits.rename_axis(index="date", columns="ticker").stack().rename("value").reset_index()
    )
    split_rows = split_rows[split_rows["value"] > 0].assign(action_type="split")
    div_rows = (
        raw_divs.rename_axis(index="date", columns="ticker").stack().rename("value").reset_index()
    )
    div_rows = div_rows[div_rows["value"] > 0].assign(action_type="dividend")
    actions = pd.concat([split_rows, div_rows], ignore_index=True)

    return normalize_prices(prices), normalize_actions(actions)


def load_yahoo(
    universe: pd.DataFrame,
    start: str,
    end: str | None,
    benchmark: str = "SPY",
    refresh_shares: bool = False,
) -> RawData:
    """Download raw data from Yahoo Finance (requires internet + `yfinance`)."""
    import yfinance as yf  # imported lazily so the rest of the engine works offline

    tickers = universe["ticker"].tolist()
    log.info("Downloading %d tickers from Yahoo Finance (%s -> %s)", len(tickers), start, end)
    df = yf.download(
        tickers, start=start, end=end, auto_adjust=False, actions=True,
        progress=False, group_by="column", threads=True,
    )
    if df is None or df.empty:
        raise RuntimeError("Yahoo Finance returned no data (check internet connection / tickers)")
    prices, actions = from_yahoo_frame(df, tickers)

    bench_df = yf.download(benchmark, start=start, end=end, auto_adjust=False, progress=False)
    bench = bench_df["Adj Close"]
    if isinstance(bench, pd.DataFrame):
        bench = bench.iloc[:, 0]
    bench = bench.dropna().rename(benchmark)
    bench.index = pd.to_datetime(bench.index).normalize()

    universe = universe.copy()
    if refresh_shares:
        for i, t in enumerate(tickers):
            try:
                shares = yf.Ticker(t).fast_info["shares"]
                if shares:
                    universe.loc[i, "shares_outstanding"] = float(shares)
            except Exception as exc:  # noqa: BLE001 - keep the configured value
                log.warning("Could not refresh shares for %s (%s); using config value", t, exc)

    return RawData(prices=prices, actions=actions, universe=universe, benchmark=bench,
                   benchmark_name=benchmark, source="yahoo")
