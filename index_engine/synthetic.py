"""Synthetic market data with *known*, deliberately injected data-quality issues.

Why synthetic data? Because it gives a ground truth. We know exactly which
records are wrong, so we can measure whether the validation layer catches
every one of them (recall) without raising false alarms (precision). This is
the same idea as a "known-answer test" in a QA / testing campaign.

Injected issues (all must be detected):
    ORBT  3 missing price records
    HELX  stale price (5 identical closes)
    KITE  fat-finger tick (price x10 for one day, then back to normal)
    LUMA  3:1 split present in prices but missing from corporate actions
    QUAR  2:1 split recorded in corporate actions but not reflected in prices
    VELA  duplicate record with a conflicting price
    ZEPH  zero price
    TERR  dividend equal to 50% of the share price

Legitimate events (must NOT be treated as errors):
    NOVA  properly recorded 4:1 split
    MIRA  genuine -22% one-day move -> should only raise a WARNING for review
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .data import RawData, normalize_actions, normalize_prices

UNIVERSE = [
    ("NOVA", "Nova Semiconductors", "Information Technology"),
    ("ORBT", "Orbit Telecom", "Communication Services"),
    ("HELX", "Helix Therapeutics", "Health Care"),
    ("KITE", "Kite Logistics", "Industrials"),
    ("LUMA", "Luma Energy", "Utilities"),
    ("QUAR", "Quarry Materials", "Materials"),
    ("VELA", "Vela Retail", "Consumer Discretionary"),
    ("ZEPH", "Zephyr Airlines", "Industrials"),
    ("TERR", "Terra Foods", "Consumer Staples"),
    ("MIRA", "Mira Bank", "Financials"),
    ("SOLA", "Sola Software", "Information Technology"),
    ("BRIX", "Brix Properties", "Real Estate"),
]
DIVIDEND_PAYERS = {"ORBT", "LUMA", "TERR", "MIRA", "BRIX", "VELA"}


def _on_or_after(dates: pd.DatetimeIndex, day: str) -> pd.Timestamp:
    return dates[dates.searchsorted(pd.Timestamp(day))]


def generate(seed: int = 8, start: str = "2024-01-02", end: str = "2025-12-31") -> RawData:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, end)
    n = len(dates)

    # one common market factor drives all stocks (and the benchmark)
    mkt = rng.normal(0.0004, 0.009, n)
    mkt[0] = 0.0
    benchmark = pd.Series(100.0 * np.cumprod(1 + mkt), index=dates, name="SYNTH-MKT")

    d = lambda day: _on_or_after(dates, day)
    nova_split, luma_split = d("2025-06-16"), d("2025-03-03")
    mira_drop = d("2025-10-21")
    orbt_missing = [d("2024-03-12"), d("2024-03-13"), d("2024-03-14")]
    helx_stale = d("2024-08-05")
    kite_spike = d("2024-10-08")
    zeph_zero = d("2025-05-20")
    vela_dup = d("2025-01-15")
    quar_bad_split = d("2025-09-15")
    terr_bad_div = d("2025-07-10")

    price_frames, actions, universe = [], [], []
    raw_by_ticker: dict[str, np.ndarray] = {}

    for ticker, name, sector in UNIVERSE:
        beta = rng.uniform(0.7, 1.3)
        r = beta * mkt + rng.normal(0.0, 0.012, n)
        r[0] = 0.0
        if ticker == "MIRA":
            r[dates.get_loc(mira_drop)] = -0.22

        p0 = round(rng.uniform(20, 300), 2)
        shares = round(rng.uniform(0.4e9, 8e9), -6)

        ex_idx: set[int] = set()
        q_yield = 0.0
        if ticker in DIVIDEND_PAYERS:
            q_yield = rng.uniform(0.004, 0.009)
            ex_idx = set(range(int(rng.integers(10, 60)), n, 63))

        econ = np.empty(n)
        div = np.zeros(n)
        econ[0] = p0
        for i in range(1, n):
            econ[i] = econ[i - 1] * (1 + r[i])
            if i in ex_idx:
                div[i] = round(econ[i - 1] * q_yield, 2)
                econ[i] -= div[i]          # price drops by the dividend on the ex-date

        raw, raw_div = econ.copy(), div.copy()
        if ticker == "NOVA":               # legitimate, recorded 4:1 split
            post = dates >= nova_split
            raw[post] /= 4
            raw_div[post] /= 4
            shares *= 4
            actions.append((ticker, nova_split, "split", 4.0))
        if ticker == "LUMA":               # 3:1 split that the vendor forgot to record
            post = dates >= luma_split
            raw[post] /= 3
            raw_div[post] /= 3
            shares *= 3

        raw = np.round(raw, 2)
        raw_div = np.round(raw_div, 2)

        # --- inject price errors ---------------------------------------
        if ticker == "HELX":
            i0 = dates.get_loc(helx_stale)
            raw[i0:i0 + 5] = raw[i0]
        if ticker == "KITE":
            raw[dates.get_loc(kite_spike)] *= 10
        if ticker == "ZEPH":
            raw[dates.get_loc(zeph_zero)] = 0.0

        frame = pd.DataFrame({"ticker": ticker, "date": dates, "close": raw})
        if ticker == "ORBT":
            frame = frame[~frame["date"].isin(orbt_missing)]
        price_frames.append(frame)
        raw_by_ticker[ticker] = raw

        for i in np.flatnonzero(raw_div > 0):
            actions.append((ticker, dates[i], "dividend", float(raw_div[i])))
        universe.append((ticker, name, sector, shares))

    # --- inject record-level errors (arrive after the original records) ----
    vela_price = raw_by_ticker["VELA"][dates.get_loc(vela_dup)]
    dup = pd.DataFrame({"ticker": ["VELA"], "date": [vela_dup], "close": [round(vela_price * 1.03, 2)]})
    prices = pd.concat(price_frames + [dup], ignore_index=True)
    prices["_row"] = np.arange(len(prices))

    actions.append(("QUAR", quar_bad_split, "split", 2.0))
    terr_prev = raw_by_ticker["TERR"][dates.get_loc(terr_bad_div) - 1]
    actions.append(("TERR", terr_bad_div, "dividend", round(0.5 * terr_prev, 2)))

    actions_df = pd.DataFrame(actions, columns=["ticker", "date", "action_type", "value"])
    universe_df = pd.DataFrame(universe, columns=["ticker", "name", "sector", "shares_outstanding"])

    ground_truth = pd.DataFrame(
        [
            *[("ORBT", day, "missing_price", "Price record missing") for day in orbt_missing],
            ("HELX", helx_stale, "stale_price", "5 identical closes"),
            ("KITE", kite_spike, "price_spike", "Fat-finger tick: price x10 for one day"),
            ("LUMA", luma_split, "possible_unrecorded_split", "3:1 split missing from corporate actions"),
            ("QUAR", quar_bad_split, "split_not_reflected", "Recorded 2:1 split not visible in prices"),
            ("VELA", vela_dup, "duplicate_record", "Second record with a conflicting price"),
            ("ZEPH", zeph_zero, "non_positive_price", "Zero price"),
            ("TERR", terr_bad_div, "dividend_outlier", "Dividend = 50% of share price"),
            ("MIRA", mira_drop, "price_jump", "Legitimate -22% move (should be a WARNING, not corrected)"),
        ],
        columns=["ticker", "date", "expected_check", "description"],
    )

    return RawData(
        prices=normalize_prices(prices),
        actions=normalize_actions(actions_df),
        universe=universe_df,
        benchmark=benchmark,
        benchmark_name="SYNTH-MKT",
        source="synthetic",
        ground_truth=ground_truth,
    )
