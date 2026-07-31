"""
Ground truth for every benchmark question, computed independently in pandas.

INDEPENDENCE — what it means here and what it does not.

This module imports NOTHING from `agent/`. It does not open db/market.db. It
reads data/raw/*.csv and data/universe.csv directly with pandas and computes
each answer from scratch. So the ground truth and the agent reach their answers
by different code over different artefacts, and a bug in the schema build, the
SQL guardrails or the tools shows up as a disagreement instead of cancelling
out. (It has already earned this: the DB row count and the CSV row count are
asserted equal by `self_check()`, which is a real end-to-end check of
build_db.py rather than a restatement of it.)

What independence does NOT mean: the *conventions* are shared. Annualising by
252, sample stdev with ddof=1, simple returns rather than log, a 6.5% risk-free
rate -- these are stated in the question text and implemented separately on both
sides. A benchmark where the two sides disagree about what "volatility" means
measures nothing about the agent. The specification is shared and disclosed; the
implementation is not shared.

Run standalone to (re)generate eval/ground_truth.json:

    python3 -m eval.ground_truth

The generated file is committed so a grader run needs no recomputation, and so
a change in ground truth shows up as a reviewable diff rather than silently
moving the accuracy number.
"""
import json
import math
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RAW = os.path.join(ROOT, "data", "raw")
UNIVERSE = os.path.join(ROOT, "data", "universe.csv")
OUT = os.path.join(HERE, "ground_truth.json")

TRADING_DAYS = 252
RF = 0.065

# Sector groups, restated here rather than imported, so this module does not
# depend on the code that populated the database. A divergence between the two
# is a real inconsistency and should surface as one.
SECTOR_GROUP = {
    "IT_Services": "Technology",
    "Private_Banks": "Financials",
    "PSU_Banks_Financials": "Financials",
    "NBFC_Insurance": "Financials",
    "Diversified_Financials_Other": "Financials",
    "Energy_OilGas": "Energy_Utilities",
    "FMCG": "Consumer_Staples",
    "Auto_AutoAncillary": "Consumer_Discretionary",
    "Consumer_Durables_Retail": "Consumer_Discretionary",
    "Pharma_Healthcare": "Healthcare",
    "Metals_Mining": "Materials",
    "Cement_Building": "Materials",
    "Telecom": "Communication",
    "Capital_Goods_Infra": "Industrials",
    "Realty": "Real_Estate",
}


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------
def load_universe() -> pd.DataFrame:
    u = pd.read_csv(UNIVERSE)
    u["sector_group"] = u["sector"].map(SECTOR_GROUP)
    if u["sector_group"].isna().any():
        missing = sorted(u.loc[u["sector_group"].isna(), "sector"].unique())
        raise SystemExit(f"no SECTOR_GROUP entry for {missing}")
    return u


def load_prices(tickers) -> pd.DataFrame:
    """Long-format frame: ticker, date, open, high, low, close, volume."""
    frames = []
    for t in tickers:
        path = os.path.join(RAW, f"{t}.csv")
        df = pd.read_csv(path)
        df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
        df = df.dropna(subset=["Close"]).sort_values("Date")
        df.insert(0, "ticker", t)
        frames.append(df)
    p = pd.concat(frames, ignore_index=True)
    p.columns = [c.lower() for c in p.columns]
    return p


def load_index() -> pd.DataFrame:
    df = pd.read_csv(os.path.join(RAW, "NIFTY50.csv"))
    df["Date"] = pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["Close"]).sort_values("Date")
    df.columns = [c.lower() for c in df.columns]
    return df


# --------------------------------------------------------------------------
# metric primitives (independent re-implementations)
# --------------------------------------------------------------------------
def daily_returns(close: pd.Series) -> pd.Series:
    return close.astype(float).pct_change().dropna()


def total_return(close: pd.Series) -> float:
    c = close.astype(float)
    return float(c.iloc[-1] / c.iloc[0] - 1.0)


def ann_vol(close: pd.Series) -> float:
    return float(daily_returns(close).std(ddof=1) * math.sqrt(TRADING_DAYS))


def max_drawdown(close: pd.Series) -> float:
    c = close.astype(float)
    return float((c / c.cummax() - 1.0).min())


def sharpe(close: pd.Series) -> float:
    r = daily_returns(close)
    vol = float(r.std(ddof=1) * math.sqrt(TRADING_DAYS))
    return float((r.mean() * TRADING_DAYS - RF) / vol)


def window(p: pd.DataFrame, ticker: str, start: str = None, end: str = None) -> pd.DataFrame:
    d = p[p["ticker"] == ticker]
    if start:
        d = d[d["date"] >= start]
    if end:
        d = d[d["date"] <= end]
    return d.sort_values("date")


# --------------------------------------------------------------------------
# the answers
# --------------------------------------------------------------------------
def compute_all() -> dict:
    u = load_universe()
    tickers = sorted(u["ticker"].tolist())
    p = load_prices(tickers)
    idx = load_index()
    tick2sector = dict(zip(u["ticker"], u["sector"]))
    p = p.assign(sector=p["ticker"].map(tick2sector))
    g = {}

    # ---- lookup ----------------------------------------------------------
    g["n_instruments"] = int(len(u))
    g["sector_of_tcs"] = str(tick2sector["TCS"])
    g["n_small_cap"] = int((u["cap_tier"] == "SMALL").sum())
    g["name_of_sobha"] = str(u.loc[u["ticker"] == "SOBHA", "name"].iloc[0])
    g["n_sectors"] = int(u["sector"].nunique())
    g["realty_tickers"] = sorted(u.loc[u["sector"] == "Realty", "ticker"].tolist())
    g["n_price_rows"] = int(len(p))
    g["min_date"] = str(p["date"].min())

    # ---- aggregation -----------------------------------------------------
    g["max_close_tcs"] = float(window(p, "TCS")["close"].max())
    g["avg_close_marico"] = float(window(p, "MARICO")["close"].mean())
    g["n_days_jkpaper"] = int(len(window(p, "JKPAPER")))
    g["total_volume_tatasteel"] = float(window(p, "TATASTEEL")["volume"].sum())
    g["ticker_highest_close_ever"] = str(p.loc[p["close"].idxmax(), "ticker"])
    sc = window(p, "SHREECEM")
    g["max_range_shreecem"] = float((sc["high"] - sc["low"]).max())
    hb = window(p, "HDFCBANK")
    g["n_up_days_hdfcbank"] = int((hb["close"] > hb["open"]).sum())
    bc = window(p, "BAJAJCON")
    g["date_max_close_bajajcon"] = str(bc.loc[bc["close"].idxmax(), "date"])

    # ---- join ------------------------------------------------------------
    sector_avg = p.groupby("sector")["close"].mean()
    g["sector_highest_avg_close"] = str(sector_avg.idxmax())
    large = set(u.loc[u["cap_tier"] == "LARGE", "ticker"])
    g["avg_close_large_cap"] = float(p[p["ticker"].isin(large)]["close"].mean())
    # Explicit tie-break: Materials and Financials both hold 5 instruments, so
    # idxmax() would return whichever pandas happened to see first and the
    # question would have two correct answers. The question states the rule.
    _grp = u.groupby("sector_group")["ticker"].count()
    g["sector_group_most_instruments"] = str(sorted(_grp[_grp == _grp.max()].index)[0])
    g["total_volume_it_services"] = float(
        p[p["sector"] == "IT_Services"]["volume"].sum()
    )
    mid = set(u.loc[u["cap_tier"] == "MID", "ticker"])
    g["mid_cap_highest_avg_volume"] = str(
        p[p["ticker"].isin(mid)].groupby("ticker")["volume"].mean().idxmax()
    )
    d = "2024-06-28"
    nifty_close = float(idx.loc[idx["date"] == d, "close"].iloc[0])
    avg_close_d = float(p.loc[p["date"] == d, "close"].mean())
    g["nifty_to_avg_close_ratio_20240628"] = nifty_close / avg_close_d
    g["n_financials_instruments"] = int((u["sector_group"] == "Financials").sum())
    ph = p[(p["sector"] == "Pharma_Healthcare") & p["date"].str.startswith("2025")]
    g["avg_close_pharma_2025"] = float(ph["close"].mean())

    # ---- timewindow ------------------------------------------------------
    g["total_return_tcs_2024"] = total_return(
        window(p, "TCS", "2024-01-01", "2024-12-31")["close"]
    )
    g["ann_vol_tatasteel_2025"] = ann_vol(
        window(p, "TATASTEEL", "2025-01-01", "2025-12-31")["close"]
    )
    g["max_drawdown_spandana"] = max_drawdown(window(p, "SPANDANA")["close"])
    g["total_return_persistent_2023"] = total_return(
        window(p, "PERSISTENT", "2023-01-01", "2023-12-31")["close"]
    )
    g["sharpe_hdfcbank"] = sharpe(window(p, "HDFCBANK")["close"])
    g["n_days_grasim_h1_2025"] = int(
        len(window(p, "GRASIM", "2025-01-01", "2025-06-30"))
    )
    n25 = idx[idx["date"].str.startswith("2025")].sort_values("date")
    g["nifty_return_2025"] = total_return(n25["close"])
    g["avg_close_alkem_march_2026"] = float(
        window(p, "ALKEM", "2026-03-01", "2026-03-31")["close"].mean()
    )

    # ---- ranking ---------------------------------------------------------
    g["top3_avg_close"] = (
        p.groupby("ticker")["close"].mean().sort_values(ascending=False).head(3)
        .index.tolist()
    )
    p25 = p[p["date"].str.startswith("2025")]
    ret25 = (
        p25.sort_values("date").groupby("ticker")["close"]
        .apply(lambda s: s.iloc[-1] / s.iloc[0] - 1.0)
    )
    g["top5_return_2025"] = ret25.sort_values(ascending=False).head(5).index.tolist()
    g["top3_sectors_by_volume"] = (
        p.groupby("sector")["volume"].sum().sort_values(ascending=False).head(3)
        .index.tolist()
    )
    p24 = p[p["date"].str.startswith("2024")]
    ret24 = (
        p24.sort_values("date").groupby("ticker")["close"]
        .apply(lambda s: s.iloc[-1] / s.iloc[0] - 1.0)
    )
    g["worst_return_2024"] = str(ret24.idxmin())
    vol_all = p.sort_values("date").groupby("ticker")["close"].apply(ann_vol)
    g["top4_volatility_alltime"] = (
        vol_all.sort_values(ascending=False).head(4).index.tolist()
    )
    small = set(u.loc[u["cap_tier"] == "SMALL", "ticker"])
    g["top3_small_cap_avg_close"] = (
        p[p["ticker"].isin(small)].groupby("ticker")["close"].mean()
        .sort_values(ascending=False).head(3).index.tolist()
    )
    ps = p.sort_values(["ticker", "date"]).copy()
    ps["ret"] = ps.groupby("ticker")["close"].pct_change()
    g["largest_one_day_gain_ticker"] = str(ps.loc[ps["ret"].idxmax(), "ticker"])
    dd_all = p.sort_values("date").groupby("ticker")["close"].apply(max_drawdown)
    g["top3_deepest_drawdown"] = dd_all.sort_values().head(3).index.tolist()

    # ---- unanswerable ----------------------------------------------------
    # A sentinel, not a value. compare.py treats a task whose expected answer is
    # UNANSWERABLE as correct only when the agent refused.
    g["unanswerable"] = "UNANSWERABLE"

    return g


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def self_check(g: dict) -> list:
    """
    Sanity checks on the ground truth itself. A benchmark whose answer key is
    wrong is worse than no benchmark, so these run every time the file is
    regenerated and any failure is printed loudly.

    The row-count check is genuinely cross-artefact: it compares the CSV count
    computed here against the SQLite count in the database the agent queries.
    """
    problems = []

    def check(cond, msg):
        if not cond:
            problems.append(msg)

    check(g["n_instruments"] == 28, f"expected 28 instruments, got {g['n_instruments']}")
    check(g["n_sectors"] == 14, f"expected 14 sectors, got {g['n_sectors']}")
    check(g["sector_group_most_instruments"] == "Financials",
          "tie-break for sector_group_most_instruments changed unexpectedly")
    check(g["min_date"] == "2023-01-02", f"unexpected min_date {g['min_date']}")
    check(g["sector_of_tcs"] == "IT_Services", f"TCS sector {g['sector_of_tcs']}")
    check(-1.0 < g["max_drawdown_spandana"] < 0.0, "drawdown must be in (-1, 0)")
    check(-1.0 < g["max_drawdown_spandana"], "drawdown below -100% is impossible")
    check(0.0 < g["ann_vol_tatasteel_2025"] < 3.0, "implausible annualised vol")
    check(g["n_up_days_hdfcbank"] < g["n_price_rows"], "up days exceed total rows")
    check(len(g["top3_avg_close"]) == 3, "top3 must have 3 entries")
    check(len(g["top5_return_2025"]) == 5, "top5 must have 5 entries")
    check(
        len(set(g["top5_return_2025"])) == 5, "top5 contains duplicate tickers"
    )
    check(all(v < 0 for v in [g["max_drawdown_spandana"]]), "drawdown sign")

    # cross-artefact: CSVs (here) vs the SQLite DB (what the agent queries)
    db = os.path.join(ROOT, "db", "market.db")
    if os.path.exists(db):
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        n_db = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
        n_ins = con.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
        con.close()
        check(
            n_db == g["n_price_rows"],
            f"DB has {n_db} price rows but the CSVs have {g['n_price_rows']} -- "
            f"the database build and the ground truth disagree",
        )
        check(
            n_ins == g["n_instruments"],
            f"DB has {n_ins} instruments, CSVs have {g['n_instruments']}",
        )
    else:
        problems.append(f"{db} not found -- cross-artefact check skipped")

    return problems


def main():
    g = compute_all()
    problems = self_check(g)
    with open(OUT, "w") as f:
        json.dump(g, f, indent=2, sort_keys=True)
    print(f"Wrote {OUT} with {len(g)} answers.\n")
    for k in sorted(g):
        v = g[k]
        shown = f"{v:.6f}" if isinstance(v, float) else str(v)
        print(f"  {k:38s} {shown}")
    if problems:
        print(f"\n!! {len(problems)} SELF-CHECK FAILURE(S):")
        for p in problems:
            print(f"   - {p}")
        raise SystemExit(1)
    print("\nAll self-checks passed (including CSV-vs-SQLite row-count agreement).")


if __name__ == "__main__":
    main()
