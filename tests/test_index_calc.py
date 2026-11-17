import numpy as np
import pandas as pd
import pytest

from index_engine.index_calc import apply_cap, calculate_index, rebalance_dates, shadow_calculate


def _prices(data: dict, start="2024-01-02") -> pd.DataFrame:
    n = len(next(iter(data.values())))
    return pd.DataFrame(data, index=pd.bdate_range(start, periods=n), dtype=float)


# --- capping ---------------------------------------------------------------

def test_cap_redistributes_excess_pro_rata():
    w = apply_cap([0.6, 0.2, 0.1, 0.1], cap=0.4)
    assert w.sum() == pytest.approx(1.0)
    assert w[0] == pytest.approx(0.4)
    # remaining 0.6 split 2:1:1 among the uncapped names
    assert w[1:] == pytest.approx([0.3, 0.15, 0.15])


def test_cap_cascades_when_redistribution_breaches_cap():
    w = apply_cap([0.5, 0.3, 0.1, 0.1], cap=0.3)
    assert w.max() <= 0.3 + 1e-12
    assert w.sum() == pytest.approx(1.0)
    assert w[:2] == pytest.approx([0.3, 0.3])


def test_cap_infeasible_raises():
    with pytest.raises(ValueError):
        apply_cap([1, 1, 1], cap=0.2)  # 3 x 20% < 100%


def test_no_cap_just_normalises():
    assert apply_cap([2, 1, 1], cap=None) == pytest.approx([0.5, 0.25, 0.25])


# --- index level -----------------------------------------------------------

def test_constant_prices_give_constant_level():
    px = _prices({"A": [10] * 30, "B": [50] * 30})
    res = calculate_index(px, px, pd.Series({"A": 100, "B": 10}), [px.index[0]])
    assert np.allclose(res.levels, 1000.0)


def test_level_follows_weighted_return():
    # A (75% weight) +10%, B (25% weight) -20%  ->  index +2.5%
    px = _prices({"A": [10, 11], "B": [10, 8]})
    res = calculate_index(px, px, pd.Series({"A": 300, "B": 100}), [px.index[0]])
    assert res.weights.iloc[0].tolist() == pytest.approx([0.75, 0.25])
    assert res.levels.iloc[-1] == pytest.approx(1025.0)


def test_level_is_continuous_through_rebalance():
    rng = np.random.default_rng(0)
    px = _prices({t: 100 * np.cumprod(1 + rng.normal(0, 0.02, 120)) for t in "ABCD"})
    shares = pd.Series({"A": 1, "B": 2, "C": 3, "D": 4}, dtype=float)
    rd = [px.index[0], px.index[40], px.index[80]]
    res = calculate_index(px, px, shares, rd, cap=0.35)
    for d in rd[1:]:
        value_new_holdings = (res.units.loc[d] * px.loc[d]).sum()
        assert value_new_holdings == pytest.approx(res.levels.loc[d], rel=1e-12)


def test_shadow_calculation_matches_production():
    rng = np.random.default_rng(1)
    px = _prices({t: 50 * np.cumprod(1 + rng.normal(0, 0.015, 250)) for t in "ABCDEF"})
    shares = pd.Series(rng.uniform(1, 10, 6), index=list("ABCDEF"))
    res = calculate_index(px, px, shares, rebalance_dates(px.index), cap=0.25)
    shadow = shadow_calculate(px, res.weights)
    assert np.allclose(shadow, res.levels, rtol=1e-12)


def test_rebalance_dates_are_last_trading_day_of_quarterly_months():
    idx = pd.bdate_range("2024-01-02", "2024-12-31")
    rd = rebalance_dates(idx)
    assert rd[0] == idx[0]
    assert [d.strftime("%Y-%m-%d") for d in rd[1:]] == [
        "2024-02-29", "2024-05-31", "2024-08-30", "2024-11-29"]
