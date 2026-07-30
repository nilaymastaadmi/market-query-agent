"""
Builds db/market.db from the pinned universe in data/universe.csv.

Two data sources, both producing an identical schema:

  --source yfinance  Download daily OHLCV per ticker from Yahoo Finance
                     (`<TICKER>.NS`, auto_adjust=True so splits, bonuses and
                     dividends are reflected in the price level), cache each
                     to data/raw/<TICKER>.csv, then load.
  --source cache     Load straight from data/raw/<TICKER>.csv. This is the
                     source used for every number reported in the README,
                     because the sandbox the project was built in blocks
                     egress to Yahoo Finance (see README "Known limits").

The cached CSVs are themselves Yahoo Finance output with auto_adjust=True,
so both paths describe the same series; --source cache just skips the
network. Row counts and date ranges are printed and asserted either way.

Schema (full ER description in README "Database"):

  sectors(sector_id PK, sector_name UNIQUE, sector_group)
      ^
      | sector_id
  instruments(instrument_id PK, ticker UNIQUE, name, sector_id FK,
              cap_tier, exchange, currency)
      ^
      | instrument_id
  prices(instrument_id FK, date, open, high, low, close, volume,
         PK(instrument_id, date))

  index_prices(index_code, date, open, high, low, close, volume,
               PK(index_code, date))   -- NIFTY 50 benchmark, no FK

Deliberate design choice: prices keys on the surrogate instrument_id, not on
the ticker string. Every price question therefore *requires* a join, which is
what the benchmark is meant to exercise. It also means a plausible-looking
`SELECT ... FROM prices WHERE ticker = 'TCS'` fails loudly instead of
silently returning nothing.
"""
import argparse
import csv
import os
import sqlite3
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from universe_pool import SECTOR_GROUP  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW_DIR = os.path.join(HERE, "raw")
UNIVERSE_CSV = os.path.join(HERE, "universe.csv")
DB_PATH = os.path.join(ROOT, "db", "market.db")

START = "2023-01-01"
END = "2026-07-04"
INDEX_CODE = "NIFTY50"
INDEX_YF_SYMBOL = "^NSEI"
MIN_ROWS_PER_TICKER = 500

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE sectors (
    sector_id    INTEGER PRIMARY KEY,
    sector_name  TEXT NOT NULL UNIQUE,
    sector_group TEXT NOT NULL
);

CREATE TABLE instruments (
    instrument_id INTEGER PRIMARY KEY,
    ticker        TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    sector_id     INTEGER NOT NULL REFERENCES sectors(sector_id),
    cap_tier      TEXT NOT NULL CHECK (cap_tier IN ('LARGE','MID','SMALL')),
    exchange      TEXT NOT NULL DEFAULT 'NSE',
    currency      TEXT NOT NULL DEFAULT 'INR'
);

CREATE TABLE prices (
    instrument_id INTEGER NOT NULL REFERENCES instruments(instrument_id),
    date          TEXT NOT NULL,
    open          REAL NOT NULL,
    high          REAL NOT NULL,
    low           REAL NOT NULL,
    close         REAL NOT NULL,
    volume        INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, date)
);

CREATE TABLE index_prices (
    index_code TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL NOT NULL,
    high       REAL NOT NULL,
    low        REAL NOT NULL,
    close      REAL NOT NULL,
    volume     INTEGER NOT NULL,
    PRIMARY KEY (index_code, date)
);

-- Indexes on the columns the benchmark actually filters and groups by.
-- prices(instrument_id, date) is already covered by the primary key.
CREATE INDEX idx_prices_date          ON prices(date);
CREATE INDEX idx_instruments_sector   ON instruments(sector_id);
CREATE INDEX idx_instruments_cap_tier ON instruments(cap_tier);
CREATE INDEX idx_index_prices_date    ON index_prices(date);
"""


def load_universe():
    with open(UNIVERSE_CSV) as f:
        return list(csv.DictReader(f))


def fetch_yfinance(symbols):
    """Download and cache OHLCV for `symbols` (list of Yahoo symbols)."""
    import time

    import yfinance as yf

    os.makedirs(RAW_DIR, exist_ok=True)
    for i in range(0, len(symbols), 10):
        batch = symbols[i : i + 10]
        data = yf.download(
            batch,
            start=START,
            end=END,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        for sym in batch:
            try:
                df = data[sym].dropna(how="all").dropna(subset=["Close"])
            except KeyError:
                raise SystemExit(f"Yahoo Finance returned no data for {sym}")
            name = INDEX_CODE if sym == INDEX_YF_SYMBOL else sym.replace(".NS", "")
            df.to_csv(os.path.join(RAW_DIR, f"{name}.csv"))
            print(f"  {name}: {len(df)} rows, {df.index.min().date()} -> {df.index.max().date()}")
        time.sleep(0.5)


def read_series(name):
    path = os.path.join(RAW_DIR, f"{name}.csv")
    if not os.path.exists(path):
        raise SystemExit(
            f"Missing {path}. Run with --source yfinance to download it, or "
            f"check that the cached CSVs are present."
        )
    df = pd.read_csv(path)
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    need = ["Date", "Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SystemExit(f"{path} is missing columns {missing}")
    df = df[need].dropna(subset=["Close"]).sort_values("Date").reset_index(drop=True)
    return df


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["cache", "yfinance"], default="cache")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    universe = load_universe()
    tickers = [r["ticker"] for r in universe]
    print(f"Universe: {len(tickers)} tickers from {UNIVERSE_CSV}")

    if args.source == "yfinance":
        print("Downloading from Yahoo Finance (auto_adjust=True)...")
        fetch_yfinance([f"{t}.NS" for t in tickers] + [INDEX_YF_SYMBOL])

    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    if os.path.exists(args.db):
        os.remove(args.db)
    con = sqlite3.connect(args.db)
    con.executescript(SCHEMA_SQL)

    # --- sectors -----------------------------------------------------------
    sector_names = sorted({r["sector"] for r in universe})
    unknown = [s for s in sector_names if s not in SECTOR_GROUP]
    if unknown:
        raise SystemExit(f"No SECTOR_GROUP entry for {unknown}")
    sector_id = {}
    for i, s in enumerate(sector_names, start=1):
        sector_id[s] = i
        con.execute(
            "INSERT INTO sectors (sector_id, sector_name, sector_group) VALUES (?,?,?)",
            (i, s, SECTOR_GROUP[s]),
        )

    # --- instruments -------------------------------------------------------
    instrument_id = {}
    for i, r in enumerate(sorted(universe, key=lambda x: x["ticker"]), start=1):
        instrument_id[r["ticker"]] = i
        con.execute(
            "INSERT INTO instruments (instrument_id, ticker, name, sector_id, cap_tier) "
            "VALUES (?,?,?,?,?)",
            (i, r["ticker"], r["name"], sector_id[r["sector"]], r["cap_tier"]),
        )

    # --- prices ------------------------------------------------------------
    total_rows = 0
    for t in sorted(tickers):
        df = read_series(t)
        if len(df) < MIN_ROWS_PER_TICKER:
            raise SystemExit(f"{t}: only {len(df)} rows, expected >= {MIN_ROWS_PER_TICKER}")
        rows = [
            (
                instrument_id[t],
                d.Date,
                float(d.Open),
                float(d.High),
                float(d.Low),
                float(d.Close),
                int(d.Volume),
            )
            for d in df.itertuples(index=False)
        ]
        con.executemany(
            "INSERT INTO prices (instrument_id, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        total_rows += len(rows)

    # --- index_prices ------------------------------------------------------
    idx = read_series(INDEX_CODE)
    con.executemany(
        "INSERT INTO index_prices (index_code, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (
                INDEX_CODE,
                d.Date,
                float(d.Open),
                float(d.High),
                float(d.Low),
                float(d.Close),
                int(d.Volume),
            )
            for d in idx.itertuples(index=False)
        ],
    )

    con.commit()

    # --- verification ------------------------------------------------------
    n_sec = con.execute("SELECT COUNT(*) FROM sectors").fetchone()[0]
    n_ins = con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    n_prc = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    n_idx = con.execute("SELECT COUNT(*) FROM index_prices").fetchone()[0]
    dmin, dmax = con.execute("SELECT MIN(date), MAX(date) FROM prices").fetchone()
    orphans = con.execute(
        "SELECT COUNT(*) FROM prices p LEFT JOIN instruments i "
        "USING (instrument_id) WHERE i.instrument_id IS NULL"
    ).fetchone()[0]
    bad_ohlc = con.execute(
        "SELECT COUNT(*) FROM prices WHERE high < low OR close <= 0 OR open <= 0"
    ).fetchone()[0]
    fk_errors = con.execute("PRAGMA foreign_key_check").fetchall()

    assert n_prc == total_rows, (n_prc, total_rows)
    assert orphans == 0, f"{orphans} price rows with no instrument"
    assert bad_ohlc == 0, f"{bad_ohlc} price rows fail OHLC sanity"
    assert not fk_errors, f"foreign key violations: {fk_errors}"

    print(
        f"\nWrote {args.db}\n"
        f"  sectors       {n_sec:>7,}\n"
        f"  instruments   {n_ins:>7,}\n"
        f"  prices        {n_prc:>7,}   {dmin} -> {dmax}\n"
        f"  index_prices  {n_idx:>7,}   ({INDEX_CODE})\n"
        f"  integrity: 0 orphan rows, 0 OHLC violations, 0 FK violations"
    )
    con.close()


if __name__ == "__main__":
    main()
