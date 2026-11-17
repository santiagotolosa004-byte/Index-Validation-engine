"""Data-validation layer.

Every check returns *exceptions* in one standard shape so they can be stored,
counted, reported and escalated the same way:

    ticker | date | check | severity | value | detail

Severity
    ERROR    the record is wrong and would corrupt the index -> must be remediated
    WARNING  unusual but possibly legitimate -> needs a human review

Most checks are written in SQL (DuckDB) on top of the raw tables; the final
classification of large price moves is done in Python because it needs a
small amount of logic (spike vs. split vs. genuine move).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb
import numpy as np
import pandas as pd

EXCEPTION_COLUMNS = ["ticker", "date", "check", "severity", "value", "detail"]

# Split ratios considered when a large move looks like an unrecorded split.
CANDIDATE_SPLIT_RATIOS = [2, 3, 4, 5, 10, 3 / 2, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 10, 2 / 3]

# What the remediation step does with each type of exception.
REMEDIATION = {
    "duplicate_record": "Kept first record received; later conflicting record discarded",
    "non_positive_price": "Price removed and last valid price carried forward",
    "missing_price": "Last valid price carried forward",
    "price_spike": "Erroneous tick removed and last valid price carried forward",
    "possible_unrecorded_split": "Provisional split added to corporate actions (confirm with vendor)",
    "split_not_reflected": "Split removed from corporate actions (confirm with vendor)",
    "dividend_outlier": "Dividend excluded from total-return calculation (confirm with vendor)",
    "price_jump": "No change - review against news / corporate events",
    "stale_price": "No change - review (possible halt, illiquidity or feed problem)",
    "split_date_without_price": "No change - review",
    "no_data": "Ticker excluded from the index",
    "not_in_universe": "Ignored",
}


@dataclass(frozen=True)
class ValidationConfig:
    jump_threshold: float = 0.20            # |daily move| above 20% is flagged
    spike_reversal_tolerance: float = 0.10  # move + next-day move must cancel (log terms)
    stale_min_obs: int = 4                  # 4+ identical consecutive closes
    split_match_tolerance: float = 0.05     # move within 5% of a split ratio -> split suspect
    split_mismatch_tolerance: float = 0.10  # recorded split vs observed ratio
    dividend_max_yield: float = 0.15        # single dividend > 15% of price is suspicious

    @property
    def jump_log(self) -> float:
        return math.log(1 + self.jump_threshold)


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=EXCEPTION_COLUMNS)


def _register(con: duckdb.DuckDBPyConnection, prices: pd.DataFrame, actions: pd.DataFrame,
              universe: pd.DataFrame) -> None:
    con.register("prices_in", prices)
    con.register("actions_in", actions)
    con.register("universe_in", universe)
    con.execute("""
        CREATE OR REPLACE TEMP VIEW px AS
        SELECT ticker, CAST(date AS DATE) AS date, CAST(close AS DOUBLE) AS close, _row
        FROM prices_in;

        -- first record received wins
        CREATE OR REPLACE TEMP VIEW px_dedup AS
        SELECT * FROM px
        QUALIFY ROW_NUMBER() OVER (PARTITION BY ticker, date ORDER BY _row) = 1;

        CREATE OR REPLACE TEMP VIEW splits AS
        SELECT ticker, CAST(date AS DATE) AS date, CAST(value AS DOUBLE) AS ratio
        FROM actions_in WHERE action_type = 'split';

        CREATE OR REPLACE TEMP VIEW dividends AS
        SELECT ticker, CAST(date AS DATE) AS date, CAST(value AS DOUBLE) AS amount
        FROM actions_in WHERE action_type = 'dividend';

        -- one row per valid observation, with the previous valid close and the
        -- split-adjusted log return (a recorded split on day t is neutralised)
        CREATE OR REPLACE TEMP VIEW obs AS
        WITH pos AS (SELECT ticker, date, close FROM px_dedup WHERE close > 0),
        base AS (
            SELECT p.ticker, p.date, p.close,
                   LAG(p.close) OVER w AS prev_close,
                   s.ratio AS split_ratio
            FROM pos p LEFT JOIN splits s USING (ticker, date)
            WINDOW w AS (PARTITION BY p.ticker ORDER BY p.date)
        )
        SELECT *, LN(close * COALESCE(split_ratio, 1.0) / prev_close) AS lr FROM base;
    """)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_duplicates(con) -> pd.DataFrame:
    return con.execute("""
        SELECT ticker, date, 'duplicate_record' AS "check", 'ERROR' AS severity,
               COUNT(*)::DOUBLE AS value,
               printf('%d records; prices %s', COUNT(*),
                      string_agg(printf('%.2f', close), ' / ' ORDER BY _row)) AS detail
        FROM px GROUP BY ticker, date HAVING COUNT(*) > 1
    """).df()


def check_non_positive(con) -> pd.DataFrame:
    return con.execute("""
        SELECT ticker, date, 'non_positive_price' AS "check", 'ERROR' AS severity,
               close AS value, printf('close = %s', COALESCE(CAST(close AS VARCHAR), 'NULL')) AS detail
        FROM px_dedup WHERE close IS NULL OR close <= 0
    """).df()


def check_missing(con) -> pd.DataFrame:
    """A record is missing if the market traded (some ticker has a price that day)
    but this ticker has no record, inside its own first/last date range."""
    return con.execute("""
        WITH cal AS (SELECT DISTINCT date FROM px),
        span AS (SELECT ticker, MIN(date) AS first_d, MAX(date) AS last_d FROM px GROUP BY ticker),
        expected AS (
            SELECT s.ticker, c.date FROM span s
            JOIN cal c ON c.date BETWEEN s.first_d AND s.last_d
        )
        SELECT e.ticker, e.date, 'missing_price' AS "check", 'WARNING' AS severity,
               NULL::DOUBLE AS value, 'no price record on a trading day' AS detail
        FROM expected e
        ANTI JOIN (SELECT DISTINCT ticker, date FROM px) p USING (ticker, date)
    """).df()


def check_stale(con, cfg: ValidationConfig) -> pd.DataFrame:
    """Runs of identical consecutive closes (gaps-and-islands pattern)."""
    return con.execute(f"""
        WITH d AS (SELECT ticker, date, close FROM px_dedup WHERE close > 0),
        flagged AS (
            SELECT *, CASE WHEN close = LAG(close) OVER w THEN 0 ELSE 1 END AS new_run
            FROM d WINDOW w AS (PARTITION BY ticker ORDER BY date)
        ),
        runs AS (
            SELECT *, SUM(new_run) OVER (PARTITION BY ticker ORDER BY date
                                         ROWS UNBOUNDED PRECEDING) AS run_id
            FROM flagged
        )
        SELECT ticker, MIN(date) AS date, 'stale_price' AS "check", 'WARNING' AS severity,
               COUNT(*)::DOUBLE AS value,
               printf('%d identical closes of %.2f until %s', COUNT(*), ANY_VALUE(close),
                      CAST(MAX(date) AS VARCHAR)) AS detail
        FROM runs GROUP BY ticker, run_id
        HAVING COUNT(*) >= {int(cfg.stale_min_obs)}
    """).df()


def check_price_moves(con, cfg: ValidationConfig) -> pd.DataFrame:
    """Classify every large split-adjusted move as one of:

    price_spike                big move that fully reverses the next day (bad tick)
    possible_unrecorded_split  move matches a typical split ratio, no split recorded
    price_jump                 anything else (could be genuine news) -> review
    """
    jl, rev = cfg.jump_log, cfg.spike_reversal_tolerance
    moves = con.execute(f"""
        WITH o AS (
            SELECT *, LEAD(lr) OVER w AS lr_next
            FROM obs WINDOW w AS (PARTITION BY ticker ORDER BY date)
        ),
        f AS (
            SELECT *,
                   (split_ratio IS NULL AND ABS(lr) > {jl} AND lr_next IS NOT NULL
                    AND ABS(lr_next) > {jl} AND SIGN(lr) <> SIGN(lr_next)
                    AND ABS(lr + lr_next) < {rev}) AS is_spike
            FROM o
        ),
        g AS (
            SELECT *, COALESCE(LAG(is_spike) OVER w, FALSE) AS reverts_spike
            FROM f WINDOW w AS (PARTITION BY ticker ORDER BY date)
        )
        SELECT ticker, date, close, prev_close, lr, is_spike
        FROM g
        WHERE split_ratio IS NULL AND ABS(lr) > {jl} AND NOT reverts_spike
    """).df()
    if moves.empty:
        return _empty()

    rows = []
    log_ratios = np.log(CANDIDATE_SPLIT_RATIOS)
    for m in moves.itertuples(index=False):
        pct = math.expm1(m.lr) * 100
        if m.is_spike:
            rows.append((m.ticker, m.date, "price_spike", "ERROR", m.close,
                         f"{pct:+.1f}% move fully reversed next day (prev close {m.prev_close:.2f})"))
            continue
        observed = -m.lr  # log(prev / close) = log of an implied split ratio
        nearest = int(np.argmin(np.abs(log_ratios - observed)))
        if abs(log_ratios[nearest] - observed) < math.log(1 + cfg.split_match_tolerance):
            ratio = CANDIDATE_SPLIT_RATIOS[nearest]
            rows.append((m.ticker, m.date, "possible_unrecorded_split", "ERROR", float(ratio),
                         f"{pct:+.1f}% move matches a {_fmt_ratio(ratio)} split; none recorded"))
        else:
            rows.append((m.ticker, m.date, "price_jump", "WARNING", m.lr,
                         f"{pct:+.1f}% one-day move (not reversed, no split pattern)"))
    return pd.DataFrame(rows, columns=EXCEPTION_COLUMNS)


def check_recorded_splits(con, cfg: ValidationConfig) -> pd.DataFrame:
    """Every recorded split must be visible in raw prices: prev/close ~= ratio."""
    df = con.execute("""
        SELECT s.ticker, s.date, s.ratio, o.close, o.prev_close
        FROM splits s LEFT JOIN obs o USING (ticker, date)
    """).df()
    rows = []
    for s in df.itertuples(index=False):
        if not s.ratio or s.ratio <= 0:
            rows.append((s.ticker, s.date, "split_not_reflected", "ERROR", s.ratio, "invalid split ratio"))
        elif pd.isna(s.close) or pd.isna(s.prev_close):
            rows.append((s.ticker, s.date, "split_date_without_price", "WARNING", s.ratio,
                         "no valid price on the split date"))
        else:
            observed = s.prev_close / s.close
            if abs(math.log(observed / s.ratio)) > math.log(1 + cfg.split_mismatch_tolerance):
                rows.append((s.ticker, s.date, "split_not_reflected", "ERROR", s.ratio,
                             f"recorded {_fmt_ratio(s.ratio)} split but prices moved {observed:.2f}x"))
    return pd.DataFrame(rows, columns=EXCEPTION_COLUMNS) if rows else _empty()


def check_dividends(con, cfg: ValidationConfig) -> pd.DataFrame:
    return con.execute(f"""
        SELECT d.ticker, d.date, 'dividend_outlier' AS "check", 'ERROR' AS severity,
               d.amount AS value,
               CASE WHEN d.amount <= 0 THEN printf('non-positive dividend %.4f', d.amount)
                    ELSE printf('dividend %.2f = %.1f%% of previous close %.2f',
                                d.amount, 100 * d.amount / o.prev_close, o.prev_close) END AS detail
        FROM dividends d LEFT JOIN obs o USING (ticker, date)
        WHERE d.amount <= 0 OR d.amount / o.prev_close > {cfg.dividend_max_yield}
    """).df()


def check_universe(con) -> pd.DataFrame:
    return con.execute("""
        SELECT u.ticker, NULL::DATE AS date, 'no_data' AS "check", 'ERROR' AS severity,
               NULL::DOUBLE AS value, 'index constituent has no price data' AS detail
        FROM universe_in u ANTI JOIN (SELECT DISTINCT ticker FROM px) p USING (ticker)
        UNION ALL
        SELECT p.ticker, NULL::DATE, 'not_in_universe', 'WARNING', NULL::DOUBLE,
               'prices received for a ticker outside the universe'
        FROM (SELECT DISTINCT ticker FROM px) p ANTI JOIN universe_in u USING (ticker)
    """).df()


def _fmt_ratio(r: float) -> str:
    if r >= 1:
        return f"{r:g}:1" if float(r).is_integer() else f"{r * 2:g}:2"
    inv = 1 / r
    return f"1:{inv:g}" if abs(inv - round(inv)) < 1e-9 else f"2:{2 * inv:g}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_validation(con: duckdb.DuckDBPyConnection, prices: pd.DataFrame, actions: pd.DataFrame,
                   universe: pd.DataFrame, cfg: ValidationConfig | None = None) -> pd.DataFrame:
    cfg = cfg or ValidationConfig()
    _register(con, prices, actions, universe)
    frames = [
        check_universe(con),
        check_duplicates(con),
        check_non_positive(con),
        check_missing(con),
        check_stale(con, cfg),
        check_price_moves(con, cfg),
        check_recorded_splits(con, cfg),
        check_dividends(con, cfg),
    ]
    frames = [f for f in frames if not f.empty]
    out = pd.concat(frames, ignore_index=True) if frames else _empty()
    out["date"] = pd.to_datetime(out["date"])
    out["value"] = pd.to_numeric(out["value"], errors="coerce")
    out["remediation"] = out["check"].map(REMEDIATION)
    sev_order = out["severity"].map({"ERROR": 0, "WARNING": 1})
    return (out.assign(_s=sev_order).sort_values(["_s", "check", "ticker", "date"])
               .drop(columns="_s").reset_index(drop=True))
