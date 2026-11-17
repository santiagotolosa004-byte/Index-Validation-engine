"""End-to-end run: load -> validate -> remediate -> adjust -> calculate -> check -> report."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pandas as pd

from . import synthetic
from .adjustments import adjust
from .benchmark import compare, impact
from .calc_checks import run_calc_checks
from .data import RawData, load_universe_csv, load_yahoo
from .index_calc import IndexResult, calculate_index, rebalance_dates
from .remediation import CleanData, remediate, to_wide
from .validation import ValidationConfig, run_validation

log = logging.getLogger(__name__)


@dataclass
class RunConfig:
    source: str = "synthetic"            # "synthetic" | "yahoo"
    start: str = "2024-01-02"
    end: str | None = "2025-12-31"
    benchmark: str = "SPY"
    universe_file: str | None = None
    cap: float | None = 0.15
    base_value: float = 1000.0
    out_dir: str = "output"
    seed: int = 8
    refresh_shares: bool = False
    strict: bool = False
    validation: ValidationConfig = field(default_factory=ValidationConfig)


@dataclass
class RunResult:
    raw: RawData
    exceptions: pd.DataFrame
    clean: CleanData
    price_return: IndexResult
    total_return: IndexResult
    as_received: IndexResult
    calc_checks: pd.DataFrame
    metrics: dict
    impact_bps: pd.Series
    scorecard: pd.DataFrame | None
    out_dir: Path

    @property
    def passed(self) -> bool:
        return not (self.calc_checks["status"] == "FAIL").any()


def load(cfg: RunConfig) -> RawData:
    if cfg.source == "synthetic":
        return synthetic.generate(seed=cfg.seed, start=cfg.start, end=cfg.end or "2025-12-31")
    if cfg.source == "yahoo":
        uni = load_universe_csv(cfg.universe_file) if cfg.universe_file else load_universe_csv()
        return load_yahoo(uni, cfg.start, cfg.end, cfg.benchmark, cfg.refresh_shares)
    raise ValueError(f"unknown source {cfg.source!r}")


def score_against_ground_truth(exceptions: pd.DataFrame, truth: pd.DataFrame) -> pd.DataFrame:
    """For synthetic runs: did validation catch every injected issue?"""
    found = set(zip(exceptions["ticker"], exceptions["date"], exceptions["check"]))
    sev = {(t, d, c): s for t, d, c, s in
           exceptions[["ticker", "date", "check", "severity"]].itertuples(index=False)}
    out = truth.copy()
    out["detected"] = [(t, d, c) in found for t, d, c in
                       truth[["ticker", "date", "expected_check"]].itertuples(index=False)]
    out["severity"] = [sev.get((t, d, c), "-") for t, d, c in
                       truth[["ticker", "date", "expected_check"]].itertuples(index=False)]
    return out


def _calc(raw_wide: pd.DataFrame, actions: pd.DataFrame, universe: pd.DataFrame, cfg: RunConfig):
    adj = adjust(raw_wide, actions)
    shares = universe.set_index("ticker")["shares_outstanding"]
    rdates = rebalance_dates(adj.adjusted.index)
    pr = calculate_index(adj.adjusted, adj.adjusted, shares, rdates, cfg.base_value, cfg.cap)
    tr = calculate_index(adj.total_return, adj.adjusted, shares, rdates, cfg.base_value, cfg.cap)
    return adj, pr, tr


def run(cfg: RunConfig) -> RunResult:
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    raw = load(cfg)
    log.info("Loaded %d price records, %d corporate actions, %d constituents (%s)",
             len(raw.prices), len(raw.actions), len(raw.universe), raw.source)

    con = duckdb.connect(str(out / "index_engine.duckdb"))
    try:
        exceptions = run_validation(con, raw.prices, raw.actions, raw.universe, cfg.validation)
        n_err = int((exceptions["severity"] == "ERROR").sum())
        log.info("Validation: %d exceptions (%d errors)", len(exceptions), n_err)

        clean = remediate(raw.prices, raw.actions, raw.universe, exceptions)
        adj, pr, tr = _calc(clean.prices, clean.actions, clean.universe, cfg)

        # the same calculation on the data exactly as received (no validation):
        naive_wide = to_wide(raw.prices, keep="last").reindex(columns=raw.universe["ticker"]).ffill()
        _, naive_pr, _ = _calc(naive_wide, raw.actions, raw.universe, cfg)

        checks = run_calc_checks(pr, adj.adjusted, cfg.cap, cfg.base_value)
        checks_tr = run_calc_checks(tr, adj.total_return, cfg.cap, cfg.base_value)
        checks_tr["check"] = "TR: " + checks_tr["check"]
        checks = pd.concat([checks.assign(check="PR: " + checks["check"]), checks_tr], ignore_index=True)

        metrics = compare(tr.levels, raw.benchmark) if raw.benchmark is not None else {}
        impact_bps = impact(naive_pr.levels, pr.levels)
        scorecard = (score_against_ground_truth(exceptions, raw.ground_truth)
                     if raw.ground_truth is not None else None)

        levels = pd.DataFrame({"price_return": pr.levels, "total_return": tr.levels,
                               "price_return_as_received": naive_pr.levels})
        tables = {
            "prices_raw": raw.prices, "corporate_actions_raw": raw.actions, "universe": raw.universe,
            "exceptions": exceptions, "remediation_log": clean.log,
            "corporate_actions_clean": clean.actions,
            "index_levels": levels.rename_axis("date").reset_index(),
            "weights": tr.weights.rename_axis("rebalance_date").reset_index(),
            "calc_checks": checks,
        }
        for name, df in tables.items():
            con.register("_tmp", df)
            con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _tmp")
            con.unregister("_tmp")
    finally:
        con.close()

    exceptions.to_csv(out / "exceptions.csv", index=False)
    clean.log.to_csv(out / "remediation_log.csv", index=False)
    levels.to_csv(out / "index_levels.csv", index_label="date")
    tr.weights.to_csv(out / "weights.csv")
    checks.to_csv(out / "calc_checks.csv", index=False)

    result = RunResult(raw=raw, exceptions=exceptions, clean=clean, price_return=pr, total_return=tr,
                       as_received=naive_pr, calc_checks=checks, metrics=metrics,
                       impact_bps=impact_bps, scorecard=scorecard, out_dir=out)

    from .report import write_report  # local import keeps matplotlib optional for library use
    write_report(result, cfg)
    return result
