import pandas as pd

from index_engine.__main__ import main
from index_engine.pipeline import RunConfig, run


def test_end_to_end_synthetic(tmp_path):
    res = run(RunConfig(source="synthetic", out_dir=str(tmp_path)))
    assert res.passed
    assert res.scorecard["detected"].all()
    for f in ["report.md", "exceptions.csv", "index_levels.csv", "remediation_log.csv",
              "performance.png", "impact.png", "exceptions.png", "weights.png", "index_engine.duckdb"]:
        assert (tmp_path / f).exists(), f

    # validated index must differ from the as-received one, and must be clean
    assert res.impact_bps.abs().max() > 100
    assert res.total_return.levels.notna().all()
    levels = pd.read_csv(tmp_path / "index_levels.csv")
    assert {"price_return", "total_return", "price_return_as_received"} <= set(levels.columns)


def test_total_return_beats_price_return_when_dividends_exist(tmp_path):
    res = run(RunConfig(source="synthetic", out_dir=str(tmp_path)))
    assert res.total_return.levels.iloc[-1] > res.price_return.levels.iloc[-1]


def test_cli_strict_mode_exit_code(tmp_path):
    assert main(["--source", "synthetic", "--out", str(tmp_path), "--strict"]) == 0
