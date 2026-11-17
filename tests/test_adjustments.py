import numpy as np
import pandas as pd
import pytest

from index_engine.adjustments import adjust
from index_engine.data import from_yahoo_frame, normalize_actions

IDX = pd.bdate_range("2024-01-02", periods=5)


def test_split_makes_adjusted_series_continuous():
    raw = pd.DataFrame({"A": [100.0, 102.0, 25.5, 26.0, 26.5]}, index=IDX)  # 4:1 split on day 3
    acts = normalize_actions(pd.DataFrame([("A", IDX[2], "split", 4.0)],
                                          columns=["ticker", "date", "action_type", "value"]))
    adj = adjust(raw, acts).adjusted["A"]
    assert adj.tolist() == pytest.approx([25.0, 25.5, 25.5, 26.0, 26.5])


def test_dividend_is_reinvested_in_total_return():
    raw = pd.DataFrame({"A": [100.0, 100.0, 98.0, 98.0, 98.0]}, index=IDX)  # $2 dividend on day 3
    acts = normalize_actions(pd.DataFrame([("A", IDX[2], "dividend", 2.0)],
                                          columns=["ticker", "date", "action_type", "value"]))
    out = adjust(raw, acts)
    # price falls by the dividend, but the total-return series does not
    assert out.adjusted["A"].iloc[-1] == pytest.approx(98.0)
    assert out.total_return["A"].iloc[-1] == pytest.approx(100.0)


def test_dividend_before_split_is_split_adjusted():
    raw = pd.DataFrame({"A": [100.0, 98.0, 98.0, 49.0, 49.0]}, index=IDX)
    acts = normalize_actions(pd.DataFrame(
        [("A", IDX[1], "dividend", 2.0), ("A", IDX[3], "split", 2.0)],
        columns=["ticker", "date", "action_type", "value"]))
    out = adjust(raw, acts)
    assert out.dividends.at[IDX[1], "A"] == pytest.approx(1.0)
    assert out.total_return["A"].iloc[-1] == pytest.approx(50.0)


def test_yahoo_frame_is_converted_back_to_raw_prices():
    # Yahoo reports split-adjusted closes: a 2:1 split on day 3 is already smoothed out.
    cols = pd.MultiIndex.from_product([["Close", "Dividends", "Stock Splits"], ["A"]])
    df = pd.DataFrame(
        np.column_stack([[50.0, 51.0, 52.0, 53.0, 54.0],
                         [0.0, 0.5, 0.0, 0.0, 0.0],
                         [0.0, 0.0, 2.0, 0.0, 0.0]]),
        index=IDX, columns=cols)
    prices, actions = from_yahoo_frame(df)
    assert prices["close"].tolist() == pytest.approx([100.0, 102.0, 52.0, 53.0, 54.0])
    split = actions[actions["action_type"] == "split"]
    div = actions[actions["action_type"] == "dividend"]
    assert split["value"].tolist() == [2.0]
    assert div["value"].tolist() == pytest.approx([1.0])  # raw (pre-split) dividend

    # and the engine's own adjustment recovers Yahoo's series
    wide = prices.pivot(index="date", columns="ticker", values="close")
    assert adjust(wide, actions).adjusted["A"].tolist() == pytest.approx(df["Close"]["A"].tolist())
