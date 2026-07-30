"""
The three tools the agent can call: get_schema, run_sql, compute_metric.

Every tool returns a plain dict that is JSON-serialisable and safe to paste
straight into the model's context. Errors are returned as
{"error": "<message>"} rather than raised, because the agent is supposed to
see the database's own complaint and correct itself — that retry behaviour is
one of the things the evaluation measures, so swallowing the message would
destroy the measurement.

METRIC CONVENTIONS (stated here once; the benchmark question text repeats the
relevant one, and eval/ground_truth.py re-implements each from scratch against
the raw CSVs rather than importing anything from this module):

  daily return        r_t = close_t / close_{t-1} - 1        (simple, not log)
  total_return        close_last / close_first - 1           over rows in window
  cagr                (1 + total_return) ** (252 / n_returns) - 1
  ann_volatility      stdev(r, ddof=1) * sqrt(252)
  max_drawdown        min(close / cummax(close) - 1)         (negative number)
  sharpe              (mean(r) * 252 - RF) / (stdev(r, ddof=1) * sqrt(252))

  TRADING_DAYS = 252, RF = 0.065 (6.5% annual, a stand-in for the Indian
  short-term risk-free rate over 2023-2026). Both are module constants and
  both are printed by get_schema(), so a model that gets a Sharpe wrong
  cannot blame an undisclosed convention.
"""
import math
import os
import sqlite3

import pandas as pd

from .guards import MAX_ROWS, TIMEOUT_S, SQLGuardError, execute_guarded, open_readonly, screen_sql

TRADING_DAYS = 252
RF = 0.065

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(_ROOT, "db", "market.db")

SCHEMA_TEXT = """\
DATABASE: Indian equity daily OHLCV (NSE), SQLite, read-only.

TABLE sectors
    sector_id     INTEGER  PRIMARY KEY
    sector_name   TEXT     NOT NULL UNIQUE   -- e.g. 'IT_Services', 'Private_Banks'
    sector_group  TEXT     NOT NULL          -- coarser grouping, e.g. 'Financials'

TABLE instruments
    instrument_id INTEGER  PRIMARY KEY
    ticker        TEXT     NOT NULL UNIQUE   -- NSE symbol without suffix, e.g. 'TCS'
    name          TEXT     NOT NULL          -- registered company name
    sector_id     INTEGER  NOT NULL          -> REFERENCES sectors(sector_id)
    cap_tier      TEXT     NOT NULL          -- one of 'LARGE', 'MID', 'SMALL'
    exchange      TEXT     NOT NULL          -- always 'NSE'
    currency      TEXT     NOT NULL          -- always 'INR'

TABLE prices
    instrument_id INTEGER  NOT NULL          -> REFERENCES instruments(instrument_id)
    date          TEXT     NOT NULL          -- 'YYYY-MM-DD'
    open          REAL     NOT NULL
    high          REAL     NOT NULL
    low           REAL     NOT NULL
    close         REAL     NOT NULL
    volume        INTEGER  NOT NULL          -- shares traded
    PRIMARY KEY (instrument_id, date)

TABLE index_prices
    index_code    TEXT     NOT NULL          -- only 'NIFTY50'
    date          TEXT     NOT NULL
    open, high, low, close  REAL NOT NULL
    volume        INTEGER  NOT NULL          -- always 0 for the index; do not use
    PRIMARY KEY (index_code, date)
    -- no foreign key: an index is not an instrument in this schema

INDEXES
    prices(instrument_id, date)  [primary key]
    prices(date)
    instruments(ticker)          [unique]
    instruments(sector_id)
    instruments(cap_tier)
    index_prices(index_code, date) [primary key]
    index_prices(date)

RELATIONSHIPS
    sectors 1---N instruments 1---N prices
    prices has NO ticker column. To filter or group by ticker, sector or
    cap tier you MUST join prices to instruments on instrument_id, and
    join instruments to sectors on sector_id for sector_name/sector_group.

WHAT IS *NOT* IN THIS DATABASE
    No fundamentals (no earnings, P/E, revenue, market-cap values, book value).
    No dividends or corporate-action records as separate rows: prices are
      already split/bonus/dividend adjusted (auto_adjust=True at download).
    No intraday or tick data; daily bars only.
    No shares outstanding or free float, so absolute market capitalisation
      cannot be computed -- cap_tier is a categorical label, not a number.
    No analyst estimates, news, sentiment, or index constituent weights.
    No instruments outside the 28 in `instruments`, and no dates outside
      the range reported below.

METRIC CONVENTIONS used by compute_metric (and by the benchmark's ground truth)
    daily return   r_t = close_t / close_{t-1} - 1   (simple returns)
    total_return   close_last / close_first - 1
    cagr           (1 + total_return) ** (252 / n_returns) - 1
    ann_volatility stdev(r, ddof=1) * sqrt(252)
    max_drawdown   min(close / cummax(close) - 1)    (a negative number)
    sharpe         (mean(r) * 252 - 0.065) / (stdev(r, ddof=1) * sqrt(252))
    TRADING_DAYS = 252, annual risk-free rate RF = 0.065
"""

METRICS = ("total_return", "cagr", "ann_volatility", "max_drawdown", "sharpe")


class Tools:
    """
    Bound set of tools over one database. `calls` accumulates a log entry per
    invocation; the agent loop copies it into the per-task record so the
    evaluation can count tool calls, retries and time spent in the database
    separately from time spent in the model.
    """

    def __init__(self, db_path: str = DEFAULT_DB, max_rows: int = MAX_ROWS,
                 timeout_s: float = TIMEOUT_S):
        if not os.path.exists(db_path):
            raise FileNotFoundError(
                f"{db_path} not found. Build it with: "
                f"cd data && python3 build_db.py --source cache"
            )
        self.db_path = db_path
        self.max_rows = max_rows
        self.timeout_s = timeout_s
        self.con = open_readonly(db_path)
        self.calls = []

    # -- tool 1 ------------------------------------------------------------
    def get_schema(self) -> dict:
        """Return the schema as text, with live row counts and date coverage."""
        n_ins = self.con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        n_prc = self.con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        n_sec = self.con.execute("SELECT COUNT(*) FROM sectors").fetchone()[0]
        dmin, dmax = self.con.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()
        text = (
            SCHEMA_TEXT
            + f"\nCOVERAGE\n    {n_ins} instruments, {n_sec} sectors, "
            f"{n_prc:,} price rows, dates {dmin} to {dmax} inclusive.\n"
            f"    run_sql returns at most {self.max_rows} rows and times out "
            f"after {self.timeout_s:g}s.\n"
        )
        self._log("get_schema", {}, ok=True, note=f"{len(text)} chars")
        return {"schema": text}

    # -- tool 2 ------------------------------------------------------------
    def run_sql(self, query: str) -> dict:
        """Execute a read-only SELECT. Returns columns/rows, or {"error": ...}."""
        try:
            cleaned = screen_sql(query)
        except SQLGuardError as e:
            self._log("run_sql", {"query": query}, ok=False, note=f"blocked: {e}")
            return {"error": f"BLOCKED by SQL guardrail: {e}", "blocked": True}

        try:
            cols, rows, truncated = execute_guarded(
                self.con, cleaned, max_rows=self.max_rows, timeout_s=self.timeout_s
            )
        except SQLGuardError as e:
            self._log("run_sql", {"query": query}, ok=False, note=f"blocked: {e}")
            return {"error": f"BLOCKED by SQL guardrail: {e}", "blocked": True}
        except sqlite3.Error as e:
            self._log("run_sql", {"query": query}, ok=False, note=f"sql_error: {e}")
            return {"error": f"SQL error: {e}", "sql_error": True}

        out = {
            "columns": cols,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
        }
        if truncated:
            out["truncated"] = True
            out["note"] = (
                f"result truncated at {self.max_rows} rows; the real result set "
                f"is larger, so any aggregate you compute yourself from these "
                f"rows will be wrong -- aggregate in SQL instead"
            )
        self._log("run_sql", {"query": cleaned}, ok=True, note=f"{len(rows)} rows")
        return out

    # -- tool 3 ------------------------------------------------------------
    def compute_metric(
        self,
        metric: str,
        ticker: str,
        start: str | None = None,
        end: str | None = None,
    ) -> dict:
        """
        Compute a return/risk metric in pandas over one ticker's close series.

        metric : one of total_return, cagr, ann_volatility, max_drawdown, sharpe
        ticker : NSE symbol, e.g. 'TCS'
        start, end : inclusive 'YYYY-MM-DD' bounds; omit for full history
        """
        args = {"metric": metric, "ticker": ticker, "start": start, "end": end}
        if metric not in METRICS:
            self._log("compute_metric", args, ok=False, note="unknown metric")
            return {"error": f"unknown metric '{metric}'; supported: {', '.join(METRICS)}"}

        sql = (
            "SELECT p.date, p.close FROM prices p "
            "JOIN instruments i ON i.instrument_id = p.instrument_id "
            "WHERE i.ticker = ?"
        )
        params: list = [ticker]
        if start:
            sql += " AND p.date >= ?"
            params.append(start)
        if end:
            sql += " AND p.date <= ?"
            params.append(end)
        sql += " ORDER BY p.date"

        rows = self.con.execute(sql, params).fetchall()
        if not rows:
            self._log("compute_metric", args, ok=False, note="no rows")
            return {
                "error": f"no price rows for ticker '{ticker}' in the requested "
                f"window; check the ticker exists in `instruments` and the "
                f"dates are inside the covered range"
            }
        if len(rows) < 2:
            self._log("compute_metric", args, ok=False, note="need >= 2 rows")
            return {"error": f"only {len(rows)} price row(s) in window; need at least 2"}

        df = pd.DataFrame(rows, columns=["date", "close"])
        close = df["close"].astype(float)
        ret = close.pct_change().dropna()

        if metric == "total_return":
            value = float(close.iloc[-1] / close.iloc[0] - 1.0)
        elif metric == "cagr":
            total = float(close.iloc[-1] / close.iloc[0])
            value = float(total ** (TRADING_DAYS / len(ret)) - 1.0)
        elif metric == "ann_volatility":
            value = float(ret.std(ddof=1) * math.sqrt(TRADING_DAYS))
        elif metric == "max_drawdown":
            value = float((close / close.cummax() - 1.0).min())
        else:  # sharpe
            vol = float(ret.std(ddof=1) * math.sqrt(TRADING_DAYS))
            if vol == 0:
                self._log("compute_metric", args, ok=False, note="zero vol")
                return {"error": "volatility is zero over this window; Sharpe undefined"}
            value = float((ret.mean() * TRADING_DAYS - RF) / vol)

        result = {
            "metric": metric,
            "ticker": ticker,
            "value": value,
            "observations": int(len(close)),
            "first_date": df["date"].iloc[0],
            "last_date": df["date"].iloc[-1],
        }
        if metric == "sharpe":
            result["risk_free_annual"] = RF
        self._log("compute_metric", args, ok=True, note=f"{value:.6g}")
        return result

    # -- logging -----------------------------------------------------------
    def _log(self, name, args, ok, note):
        self.calls.append({"tool": name, "args": args, "ok": ok, "note": note})

    def reset_log(self):
        self.calls = []

    def close(self):
        self.con.close()


# Machine-readable tool specs, injected into the system prompt so the schema of
# the tool interface is stated in exactly one place.
TOOL_SPECS = [
    {
        "name": "get_schema",
        "description": "Return the full database schema as text, including which "
        "tables exist, how they join, what is NOT in the database, and the "
        "metric conventions. Takes no arguments.",
        "args": {},
    },
    {
        "name": "run_sql",
        "description": "Execute one read-only SELECT (or WITH ... SELECT) against "
        f"the database. Returns columns and rows, at most {MAX_ROWS} rows, "
        f"{TIMEOUT_S:g}s timeout. Anything that is not a single SELECT is "
        "refused. On a SQL error the database's own message is returned so you "
        "can fix the query and try again.",
        "args": {"query": "string - the SQL to execute"},
    },
    {
        "name": "compute_metric",
        "description": "Compute a return/risk metric in pandas for one ticker "
        f"over an optional date window. metric must be one of: {', '.join(METRICS)}. "
        "Use this instead of reimplementing the maths in SQL.",
        "args": {
            "metric": f"string - one of {', '.join(METRICS)}",
            "ticker": "string - NSE symbol, e.g. 'TCS'",
            "start": "string or null - inclusive 'YYYY-MM-DD' lower bound",
            "end": "string or null - inclusive 'YYYY-MM-DD' upper bound",
        },
    },
]
