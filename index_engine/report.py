"""Markdown report + charts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import MaxNLocator

if TYPE_CHECKING:
    from .pipeline import RunConfig, RunResult

INK, MUTED, GRID = "#1f2933", "#7b8794", "#e4e7eb"
BLUE, TEAL, ORANGE, RED = "#2f6fdf", "#1a9e8f", "#e8833a", "#d64545"


def _style(ax, title: str) -> None:
    ax.set_title(title, loc="left", fontsize=12, color=INK, fontweight="bold", pad=10)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED, labelsize=9)


def _md_table(df: pd.DataFrame) -> str:
    cols = list(df.columns)
    lines = ["| " + " | ".join(map(str, cols)) + " |", "|" + "---|" * len(cols)]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |")
    return "\n".join(lines)


def _pct(x: float) -> str:
    return f"{x:.2%}"


def plot_performance(res: RunResult, path) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.6))
    tr, pr = res.total_return.levels, res.price_return.levels
    ax.plot(tr.index, tr, color=BLUE, lw=1.8, label="Index - total return")
    ax.plot(pr.index, pr, color=TEAL, lw=1.4, label="Index - price return")
    if res.raw.benchmark is not None:
        b = res.raw.benchmark.reindex(tr.index).ffill().dropna()
        ax.plot(b.index, b / b.iloc[0] * tr.loc[b.index[0]], color=MUTED, lw=1.4, ls="--",
                label=f"Benchmark ({res.raw.benchmark_name}), rebased")
    for d in res.total_return.weights.index[1:]:
        ax.axvline(d, color=GRID, lw=0.8, zorder=0)
    _style(ax, "Index level vs benchmark (vertical lines = quarterly rebalances)")
    ax.legend(fontsize=9, loc="upper left", facecolor="white", edgecolor="none", framealpha=0.92)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_exceptions(res: RunResult, path) -> None:
    exc = res.exceptions
    if exc.empty:
        fig, ax = plt.subplots(figsize=(8, 1.6))
        ax.axis("off")
        ax.text(0.0, 0.5, "No exceptions raised - all validation checks passed", fontsize=12, color=INK)
        fig.savefig(path, dpi=160)
        plt.close(fig)
        return
    counts = exc.groupby(["check", "severity"]).size().unstack(fill_value=0)
    for s in ("ERROR", "WARNING"):
        if s not in counts:
            counts[s] = 0
    counts = counts.loc[counts.sum(axis=1).sort_values().index]
    fig, ax = plt.subplots(figsize=(8, 0.45 * len(counts) + 1.4))
    ax.barh(counts.index, counts["ERROR"], color=RED, label="ERROR")
    ax.barh(counts.index, counts["WARNING"], left=counts["ERROR"], color=ORANGE, label="WARNING")
    for i, total in enumerate(counts.sum(axis=1)):
        ax.text(total + 0.05, i, str(int(total)), va="center", fontsize=9, color=INK)
    _style(ax, "Exceptions raised by the validation layer")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_impact(res: RunResult, path) -> None:
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True,
                                   gridspec_kw={"height_ratios": [2, 1]})
    pr, naive = res.price_return.levels, res.as_received.levels
    ax1.plot(naive.index, naive, color=RED, lw=1.3, label="Calculated on data as received")
    ax1.plot(pr.index, pr, color=BLUE, lw=1.6, label="Calculated on validated data")
    _style(ax1, "What happens if the data is not validated (price-return index)")
    ax1.legend(fontsize=9, loc="upper left", facecolor="white", edgecolor="none", framealpha=0.92)
    ax2.fill_between(res.impact_bps.index, res.impact_bps, 0, color=RED, alpha=0.35, lw=0)
    ax2.plot(res.impact_bps.index, res.impact_bps, color=RED, lw=0.9)
    _style(ax2, "Error in the published level (basis points)")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def plot_weights(res: RunResult, cfg: RunConfig, path) -> None:
    w = res.total_return.weights.iloc[-1].sort_values()
    w = w[w > 0]
    fig, ax = plt.subplots(figsize=(8, 0.32 * len(w) + 1.4))
    ax.barh(w.index, w * 100, color=BLUE)
    if cfg.cap:
        ax.axvline(cfg.cap * 100, color=RED, lw=1, ls="--")
        ax.text(cfg.cap * 100, len(w) - 0.4, f" cap {cfg.cap:.0%}", color=RED, fontsize=9, va="top")
    _style(ax, f"Constituent weights at last rebalance ({w.name:%Y-%m-%d}), %")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def write_report(res: RunResult, cfg: RunConfig) -> None:
    out = res.out_dir
    plot_performance(res, out / "performance.png")
    plot_exceptions(res, out / "exceptions.png")
    plot_impact(res, out / "impact.png")
    plot_weights(res, cfg, out / "weights.png")

    exc = res.exceptions
    n_err = int((exc["severity"] == "ERROR").sum())
    n_warn = int((exc["severity"] == "WARNING").sum())
    worst = res.impact_bps.abs()
    status = "PASSED" if res.passed else "FAILED"

    lev = res.total_return.levels
    cap_txt = f"{cfg.cap:.0%} cap" if cfg.cap else "uncapped"
    lines = [
        "# Index production run report",
        "",
        (f"- **Source:** `{res.raw.source}` | **Period:** {lev.index[0]:%Y-%m-%d} to {lev.index[-1]:%Y-%m-%d}"
         f" | **Constituents:** {len(res.clean.universe)}"),
        (f"- **Methodology:** market-cap weighted, {cap_txt}, quarterly rebalance (Feb/May/Aug/Nov),"
         f" base {cfg.base_value:g}"),
        (f"- **Validation:** {len(exc)} exceptions ({n_err} errors, {n_warn} warnings),"
         f" {len(res.clean.log)} data changes logged"),
        f"- **Calculation checks:** {status}",
        (f"- **Impact of skipping validation:** max error of {worst.max():,.0f} bps"
         f" on {worst.idxmax():%Y-%m-%d}"),
        "",
    ]

    if res.scorecard is not None:
        sc = res.scorecard
        known = {(t, d, c) for t, d, c in sc[["ticker", "date", "expected_check"]].itertuples(index=False)}
        errors = exc[exc["severity"] == "ERROR"]
        false_pos = sum((t, d, c) not in known for t, d, c in
                        errors[["ticker", "date", "check"]].itertuples(index=False))
        lines += [
            "## Known-answer test (synthetic data)",
            "",
            (f"**{int(sc['detected'].sum())} / {len(sc)}** injected issues detected;"
             f" **{false_pos}** false-positive errors."),
            "",
            _md_table(sc.assign(date=sc["date"].dt.strftime("%Y-%m-%d"),
                                detected=sc["detected"].map({True: "yes", False: "**NO**"}))
                      [["ticker", "date", "expected_check", "severity", "detected", "description"]]),
            "",
        ]

    lines += [
        "## Calculation checks",
        "",
        _md_table(res.calc_checks),
        "",
        "## Exceptions",
        "",
        "![exceptions](exceptions.png)",
        "",
        _md_table(exc.assign(date=exc["date"].dt.strftime("%Y-%m-%d"))
                  [["severity", "check", "ticker", "date", "detail", "remediation"]].head(60)),
        "",
    ]
    if len(exc) > 60:
        lines += [f"_Showing 60 of {len(exc)} exceptions. Full list in `exceptions.csv`._", ""]

    if res.metrics:
        m = res.metrics
        perf = pd.DataFrame({
            "metric": ["Annualised return", "Annualised volatility", "Max drawdown"],
            "index (TR)": [_pct(m["index_ann_return"]), _pct(m["index_ann_vol"]), _pct(m["index_max_drawdown"])],
            res.raw.benchmark_name: [_pct(m["bench_ann_return"]), _pct(m["bench_ann_vol"]),
                                     _pct(m["bench_max_drawdown"])],
        })
        lines += [
            "## Performance vs benchmark",
            "",
            "![performance](performance.png)",
            "",
            _md_table(perf),
            "",
            (f"Tracking error **{_pct(m['tracking_error'])}**, correlation **{m['correlation']:.3f}**,"
             f" beta **{m['beta']:.2f}**, information ratio **{m['information_ratio']:.2f}**"
             f" ({m['observations']} daily observations)."),
            "",
        ]

    lines += [
        "## Impact of skipping validation",
        "",
        "The same methodology run on the data exactly as received (no validation, no remediation).",
        "",
        "![impact](impact.png)",
        "",
        "## Weights",
        "",
        "![weights](weights.png)",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
