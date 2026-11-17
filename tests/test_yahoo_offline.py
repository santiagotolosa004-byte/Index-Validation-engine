"""Runs the full Yahoo pipeline offline by replacing `yfinance` with a fake module
that returns frames in the same shape as `yf.download(..., auto_adjust=False, actions=True)`."""

import sys
import types

import numpy as np
import pandas as pd

from index_engine.pipeline import RunConfig, run


def _fake_download(tickers, start=None, end=None, **kwargs):
    tickers = [tickers] if isinstance(tickers, str) else list(tickers)
    idx = pd.bdate_range("2024-01-02", "2024-12-31")
    rng = np.random.default_rng(len(tickers))
    fields = {}
    for f in ["Adj Close", "Close", "Dividends", "Stock Splits"]:
        fields[f] = pd.DataFrame(0.0, index=idx, columns=tickers)
    for t in tickers:
        px = 100 * np.cumprod(1 + rng.normal(0.0004, 0.012, len(idx)))
        fields["Close"][t] = px
        fields["Adj Close"][t] = px
    if "AAA" in tickers:  # a 2:1 split, already smoothed out in Yahoo's Close
        fields["Stock Splits"].loc[idx[100], "AAA"] = 2.0
        fields["Dividends"].loc[idx[50], "AAA"] = 0.5
    return pd.concat(fields, axis=1)


def test_yahoo_pipeline_offline(tmp_path, monkeypatch):
    fake = types.ModuleType("yfinance")
    fake.download = _fake_download
    monkeypatch.setitem(sys.modules, "yfinance", fake)

    uni = pd.DataFrame({"ticker": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"],
                        "name": list("abcdefgh"), "sector": "x",
                        "shares_outstanding": [1e9, 2e9, 3e9, 1e9, 2e9, 3e9, 1e9, 2e9]})
    uni_file = tmp_path / "uni.csv"
    uni.to_csv(uni_file, index=False)

    res = run(RunConfig(source="yahoo", start="2024-01-02", end="2024-12-31", benchmark="SPY",
                        universe_file=str(uni_file), out_dir=str(tmp_path / "out")))
    assert res.passed
    # the de-adjusted split must be recognised as a properly recorded split -> no errors
    assert not (res.exceptions["severity"] == "ERROR").any()
    assert res.metrics["observations"] > 200
    assert (tmp_path / "out" / "report.md").exists()
