"""Command-line entry point:  python -m index_engine --help"""

from __future__ import annotations

import argparse
import logging
import sys

from .pipeline import RunConfig, run


def parse_args(argv=None) -> RunConfig:
    p = argparse.ArgumentParser(prog="python -m index_engine",
                                description="Calculate and validate a capped market-cap index.")
    p.add_argument("--source", choices=["synthetic", "yahoo"], default="synthetic",
                   help="synthetic = offline demo with injected data errors; yahoo = real market data")
    p.add_argument("--start", default="2024-01-02")
    p.add_argument("--end", default="2025-12-31")
    p.add_argument("--benchmark", default="SPY", help="benchmark ticker for --source yahoo")
    p.add_argument("--universe", default=None, help="CSV with ticker,name,sector,shares_outstanding")
    p.add_argument("--cap", type=float, default=0.15, help="max weight per constituent (0 = uncapped)")
    p.add_argument("--out", default="output", help="output folder")
    p.add_argument("--seed", type=int, default=8, help="random seed for synthetic data")
    p.add_argument("--refresh-shares", action="store_true", help="update share counts from Yahoo")
    p.add_argument("--strict", action="store_true", help="exit with code 1 if any calculation check fails")
    a = p.parse_args(argv)
    return RunConfig(source=a.source, start=a.start, end=a.end, benchmark=a.benchmark,
                     universe_file=a.universe, cap=a.cap or None, out_dir=a.out, seed=a.seed,
                     refresh_shares=a.refresh_shares, strict=a.strict)


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = parse_args(argv)
    res = run(cfg)
    n_err = int((res.exceptions["severity"] == "ERROR").sum())
    print(f"\nExceptions: {len(res.exceptions)} ({n_err} errors) | "
          f"calculation checks: {'PASSED' if res.passed else 'FAILED'}")
    if res.scorecard is not None:
        print(f"Known-answer test: {int(res.scorecard['detected'].sum())}/{len(res.scorecard)} injected issues detected")
    print(f"Report: {res.out_dir / 'report.md'}")
    return 1 if (cfg.strict and not res.passed) else 0


if __name__ == "__main__":
    sys.exit(main())
