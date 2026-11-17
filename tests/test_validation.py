import duckdb
import numpy as np
import pandas as pd
import pytest

from index_engine import synthetic
from index_engine.data import normalize_actions, normalize_prices
from index_engine.pipeline import score_against_ground_truth
from index_engine.validation import run_validation

IDX = pd.bdate_range("2024-01-02", periods=12)
UNI = pd.DataFrame({"ticker": ["A", "B"], "name": ["A", "B"], "sector": ["x", "x"],
                    "shares_outstanding": [1.0, 1.0]})


def _validate(a_prices, b_prices=None, actions=None):
    b_prices = b_prices if b_prices is not None else 100 + np.arange(len(IDX), dtype=float)
    rows = [("A", d, p) for d, p in zip(IDX, a_prices) if p is not None]
    rows += [("B", d, p) for d, p in zip(IDX, b_prices)]
    prices = normalize_prices(pd.DataFrame(rows, columns=["ticker", "date", "close"]))
    acts = normalize_actions(pd.DataFrame(actions or [], columns=["ticker", "date", "action_type", "value"]))
    return run_validation(duckdb.connect(), prices, acts, UNI)


def _base():
    return [50.0 + 0.3 * i for i in range(len(IDX))]


def test_clean_data_has_no_exceptions():
    assert _validate(_base()).empty


def test_fat_finger_is_a_spike_not_a_split():
    p = _base()
    p[5] *= 10
    exc = _validate(p)
    assert exc["check"].tolist() == ["price_spike"]
    assert exc["date"].iloc[0] == IDX[5]


def test_unrecorded_split_detected_with_ratio():
    p = _base()
    p[6:] = [x / 2 for x in p[6:]]
    exc = _validate(p)
    row = exc.iloc[0]
    assert (row["check"], row["date"], row["value"]) == ("possible_unrecorded_split", IDX[6], 2.0)


def test_recorded_split_is_accepted():
    p = _base()
    p[6:] = [x / 2 for x in p[6:]]
    assert _validate(p, actions=[("A", IDX[6], "split", 2.0)]).empty


def test_recorded_split_not_in_prices_is_flagged():
    exc = _validate(_base(), actions=[("A", IDX[6], "split", 2.0)])
    assert exc["check"].tolist() == ["split_not_reflected"]


def test_genuine_large_move_is_only_a_warning():
    p = _base()
    p[7:] = [x * 0.7 for x in p[7:]]
    exc = _validate(p)
    assert exc[["check", "severity"]].values.tolist() == [["price_jump", "WARNING"]]


def test_missing_stale_zero_and_duplicates():
    p = _base()
    p[2] = None            # missing
    p[4:9] = [p[4]] * 5    # stale run of 5
    p[10] = 0.0            # zero price
    rows = [("A", d, x) for d, x in zip(IDX, p) if x is not None]
    rows += [("B", d, 100.0 + i) for i, d in enumerate(IDX)]
    rows.append(("B", IDX[3], 999.0))  # duplicate arriving later
    prices = normalize_prices(pd.DataFrame(rows, columns=["ticker", "date", "close"]))
    exc = run_validation(duckdb.connect(), prices, normalize_actions(None), UNI)
    found = set(zip(exc["ticker"], exc["date"], exc["check"]))
    assert ("A", IDX[2], "missing_price") in found
    assert ("A", IDX[4], "stale_price") in found
    assert ("A", IDX[10], "non_positive_price") in found
    assert ("B", IDX[3], "duplicate_record") in found
    # the duplicate is resolved first-come, so it must not also look like a spike
    assert not ((exc["ticker"] == "B") & (exc["check"] == "price_spike")).any()


def test_dividend_outlier():
    exc = _validate(_base(), actions=[("A", IDX[5], "dividend", 30.0)])
    assert exc["check"].tolist() == ["dividend_outlier"]


def test_constituent_without_data():
    prices = normalize_prices(pd.DataFrame({"ticker": "A", "date": IDX, "close": _base()}))
    exc = run_validation(duckdb.connect(), prices, normalize_actions(None), UNI)
    assert ("B", "no_data") in set(zip(exc["ticker"], exc["check"]))


# --- known-answer test on the full synthetic dataset -------------------------

@pytest.fixture(scope="module")
def synthetic_run():
    raw = synthetic.generate()
    exc = run_validation(duckdb.connect(), raw.prices, raw.actions, raw.universe)
    return raw, exc


def test_every_injected_issue_is_detected(synthetic_run):
    raw, exc = synthetic_run
    score = score_against_ground_truth(exc, raw.ground_truth)
    missed = score[~score["detected"]]
    assert missed.empty, f"missed: {missed[['ticker', 'expected_check']].values.tolist()}"


def test_no_false_positive_errors(synthetic_run):
    raw, exc = synthetic_run
    truth = set(zip(raw.ground_truth["ticker"], raw.ground_truth["date"], raw.ground_truth["expected_check"]))
    errors = exc[exc["severity"] == "ERROR"]
    unexpected = [r for r in zip(errors["ticker"], errors["date"], errors["check"]) if r not in truth]
    assert unexpected == []


def test_legitimate_split_is_not_flagged(synthetic_run):
    _, exc = synthetic_run
    assert not (exc["ticker"] == "NOVA").any()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_detection_is_robust_across_random_seeds(seed):
    raw = synthetic.generate(seed=seed)
    exc = run_validation(duckdb.connect(), raw.prices, raw.actions, raw.universe)
    assert score_against_ground_truth(exc, raw.ground_truth)["detected"].all()
