"""
Selects the benchmark universe from the disclosed candidate pool and writes
data/universe.csv.

Methodology (documented so the selection can't be accused of cherry-picking,
same shape as trading-bot/data/select_universe.py):
  1. Flatten POOL into (sector, cap_tier, ticker) tuples.
  2. Apply a DATA-AVAILABILITY filter. This filter never looks at returns —
     only at whether a usable price history exists — so it cannot select for
     performance. Two sources:
       --source yfinance : bulk-download each candidate and drop any ticker
                           with fewer than MIN_TRADING_DAYS closes.
       --source cache    : keep candidates that have a data/raw/<T>.csv with
                           at least MIN_TRADING_DAYS rows.
  3. random.seed(SEED) — fixed and disclosed — then sample TARGET_PER_TIER
     tickers per cap tier, walking sectors in shuffled order so no single
     sector dominates a tier.
  4. Write data/universe.csv.

IMPORTANT — read this before assuming the seed did any work. If the
availability filter leaves exactly as many tickers as the targets ask for,
the "sample" is the entire available set and the seed has no discretion at
all. That is what happens in the shipped run (see README "Universe"): the
28 tickers in data/universe.csv are every ticker with a usable local
history, taken without choice. The script prints NO SAMPLING DISCRETION in
that case rather than letting a disclosed seed imply a selection it never
made.

data/universe.csv is committed and PINNED. build_db.py reads that file, not
this script, so a fresh clone rebuilds the same database. Re-running this
script with --source yfinance over the full pool will draw a *different*
universe and overwrite the pin; that is a deliberate, separate action.
"""
import argparse
import csv
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from universe_pool import POOL, COMPANY_NAME  # noqa: E402

SEED = 42
START = "2023-01-01"
END = "2026-07-04"
MIN_TRADING_DAYS = 500  # ~2 years of ~250 trading days/yr
TARGET_PER_TIER = {"LARGE": 10, "MID": 9, "SMALL": 9}
MAX_PER_SECTOR_PER_TIER = 2

HERE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(HERE, "raw")
OUT_CSV = os.path.join(HERE, "universe.csv")


def flatten(pool):
    rows = []
    for sector, tiers in pool.items():
        for tier, tickers in tiers.items():
            for t in tickers:
                rows.append((sector, tier, t))
    return rows


def availability_from_cache(tickers):
    """Row count per ticker from the local data/raw cache. 0 if absent."""
    counts = {}
    for t in tickers:
        path = os.path.join(RAW_DIR, f"{t}.csv")
        if not os.path.exists(path):
            counts[t] = 0
            continue
        with open(path) as f:
            counts[t] = max(0, sum(1 for _ in f) - 1)  # minus header
    return counts


def availability_from_yfinance(tickers):
    """Row count per ticker from a live Yahoo Finance download."""
    import time

    import pandas as pd
    import yfinance as yf

    counts = {}
    print(f"Validating {len(tickers)} unique tickers against Yahoo Finance...")
    for i in range(0, len(tickers), 15):
        batch = tickers[i : i + 15]
        data = yf.download(
            [f"{t}.NS" for t in batch],
            start=START,
            end=END,
            group_by="ticker",
            progress=False,
            auto_adjust=True,
            threads=True,
        )
        for t in batch:
            try:
                df = data[f"{t}.NS"].dropna(how="all")
            except KeyError:
                df = pd.DataFrame()
            counts[t] = len(df.dropna(subset=["Close"])) if not df.empty else 0
        time.sleep(0.5)
    return counts


def stratified_sample(valid_rows, targets, seed):
    random.seed(seed)
    selected = []
    for tier, target in targets.items():
        tier_rows = [(s, t) for (s, b, t) in valid_rows if b == tier]
        sectors = sorted({s for s, _ in tier_rows})
        if not sectors:
            continue
        random.shuffle(sectors)
        per_sector = {s: 0 for s in sectors}
        by_sector = {s: [t for (ss, t) in tier_rows if ss == s] for s in sectors}
        for s in by_sector:
            random.shuffle(by_sector[s])

        picked = []
        idx = 0
        guard = 0
        while len(picked) < target and guard < 10000:
            guard += 1
            s = sectors[idx % len(sectors)]
            idx += 1
            if per_sector[s] < MAX_PER_SECTOR_PER_TIER and by_sector[s]:
                cand = by_sector[s].pop()
                if cand not in [p[2] for p in picked]:
                    picked.append((s, tier, cand))
                    per_sector[s] += 1
            if all(not v for v in by_sector.values()):
                break
        selected.extend(picked)
    return selected


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", choices=["cache", "yfinance"], default="cache")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", default=OUT_CSV)
    args = ap.parse_args()

    rows = flatten(POOL)
    unique = sorted({t for _, _, t in rows})

    if args.source == "yfinance":
        counts = availability_from_yfinance(unique)
    else:
        counts = availability_from_cache(unique)

    ok = {t: n for t, n in counts.items() if n >= MIN_TRADING_DAYS}
    dropped = {t: n for t, n in counts.items() if n < MIN_TRADING_DAYS}
    print(
        f"{len(ok)}/{len(unique)} candidates pass the availability filter "
        f"(>= {MIN_TRADING_DAYS} trading days, source={args.source})."
    )
    if dropped and args.source == "yfinance":
        print(f"Dropped for insufficient data: {sorted(dropped)}")

    valid_rows = [(s, b, t) for (s, b, t) in rows if t in ok]
    # De-duplicate tickers that appear under more than one sector in the pool
    # (RATNAMANI is in both Metals_Mining and Auto_AutoAncillary). Keep the
    # first occurrence in POOL iteration order so the result is deterministic.
    seen = set()
    deduped = []
    for s, b, t in valid_rows:
        if t in seen:
            continue
        seen.add(t)
        deduped.append((s, b, t))

    target_total = sum(TARGET_PER_TIER.values())
    available_total = len({t for _, _, t in deduped})
    selected = stratified_sample(deduped, TARGET_PER_TIER, args.seed)

    if available_total <= target_total:
        print(
            f"\nNO SAMPLING DISCRETION: {available_total} tickers available, "
            f"{target_total} requested -> the selection is the full available "
            f"set. seed={args.seed} changed nothing."
        )
    else:
        print(f"\nSampled {len(selected)} of {available_total} available (seed={args.seed}).")

    missing_names = sorted({t for _, _, t in selected if t not in COMPANY_NAME})
    if missing_names:
        raise SystemExit(
            f"No COMPANY_NAME entry for {missing_names}. Add them to "
            f"universe_pool.py before pinning this universe."
        )

    print(f"\nUniverse: {len(selected)} stocks\n")
    for s, b, t in sorted(selected, key=lambda r: (r[1], r[0], r[2])):
        print(f"  {b:6s} {s:30s} {t:12s} {counts[t]:4d} days")

    with open(args.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "sector", "cap_tier", "trading_days"])
        for s, b, t in sorted(selected, key=lambda r: (r[1], r[0], r[2])):
            w.writerow([t, COMPANY_NAME[t], s, b, counts[t]])
    print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
