"""
Audits the benchmark for ties and near-ties in the answer key.

    python3 -m eval.audit_ties

WHY THIS EXISTS. A question whose answer is decided by a tie has more than one
correct answer, but `idxmax()` silently returns whichever row pandas saw first.
The agent then gets marked wrong for giving the other equally-valid answer. That
is a defect in the benchmark, not in the agent, and it is exactly the kind of
thing that quietly inflates a reported failure rate.

This was not hypothetical. J03 ("which sector_group has the most instruments?")
was a real 5-5 tie between Materials and Financials, caught only because the
agent answered Materials and the failure looked wrong on inspection. The
question now carries an explicit tie-break rule. This script checks the rest of
the benchmark for the same defect, so the next one is caught before a run rather
than after.

It reports two things:
  TIES       -- an exact tie for the top position. The question is ill-posed
                unless it states a tie-break rule.
  NEAR TIES  -- a top-two gap under a relative threshold. Not a bug, but a
                question whose answer turns on the sixth decimal place is
                testing floating-point luck, so it is worth knowing about.
"""
import sys

import pandas as pd

from eval.ground_truth import (
    ann_vol, load_index, load_prices, load_universe, max_drawdown,
)

NEAR_TIE_REL = 0.01  # 1% relative gap between first and second

# Questions that CONTAIN an explicit tie-break rule in their text. A tie in the
# data is then well-defined rather than ill-posed, so the audit reports it as
# handled instead of as a defect. Adding an id here without also putting the
# rule in the question would be cheating the audit, so the reason is recorded.
TIE_BREAK_DECLARED = {
    "J03 sector_group by count":
        "question says: if tied, return the alphabetically first",
}


def _report(name, series, ascending=False, question=""):
    """series: index -> value, higher (or lower if ascending) is the answer."""
    s = series.sort_values(ascending=ascending)
    if len(s) < 2:
        return None
    top, second = s.iloc[0], s.iloc[1]
    tied = [i for i in s.index if s[i] == top]
    if len(tied) > 1:
        return ("TIE", name, f"{len(tied)}-way tie at {top!r}: {tied}", question)
    gap = abs(top - second) / abs(top) if top else float("inf")
    if gap < NEAR_TIE_REL:
        return ("NEAR", name, f"top two differ by {gap:.3%}: "
                f"{s.index[0]}={top:.6g} vs {s.index[1]}={second:.6g}", question)
    return None


def main():
    u = load_universe()
    tickers = sorted(u["ticker"].tolist())
    p = load_prices(tickers)
    tick2sector = dict(zip(u["ticker"], u["sector"]))
    p = p.assign(sector=p["ticker"].map(tick2sector))
    load_index()

    p25 = p[p["date"].str.startswith("2025")].sort_values("date")
    p24 = p[p["date"].str.startswith("2024")].sort_values("date")
    ret = lambda d: d.groupby("ticker")["close"].apply(lambda s: s.iloc[-1] / s.iloc[0] - 1)
    ps = p.sort_values(["ticker", "date"]).copy()
    ps["r"] = ps.groupby("ticker")["close"].pct_change()

    checks = [
        _report("J01 sector by avg close", p.groupby("sector")["close"].mean(),
                question="highest average closing price by sector"),
        _report("J03 sector_group by count",
                u.groupby("sector_group")["ticker"].count().astype(float),
                question="sector_group with most instruments"),
        _report("J05 mid-cap by avg volume",
                p[p["ticker"].isin(set(u.loc[u.cap_tier == "MID", "ticker"]))]
                .groupby("ticker")["volume"].mean(),
                question="mid-cap ticker with highest average volume"),
        _report("A05 highest close ever", p.groupby("ticker")["close"].max(),
                question="ticker with highest close on any day"),
        _report("R01 top avg close", p.groupby("ticker")["close"].mean(),
                question="top 3 by average close"),
        _report("R02 top return 2025", ret(p25), question="top 5 by 2025 return"),
        _report("R03 sector by volume", p.groupby("sector")["volume"].sum().astype(float),
                question="top 3 sectors by volume"),
        _report("R04 worst return 2024", ret(p24), ascending=True,
                question="worst 2024 return"),
        _report("R05 top volatility",
                p.sort_values("date").groupby("ticker")["close"].apply(ann_vol),
                question="top 4 by annualised volatility"),
        _report("R06 small-cap avg close",
                p[p["ticker"].isin(set(u.loc[u.cap_tier == "SMALL", "ticker"]))]
                .groupby("ticker")["close"].mean(),
                question="top 3 small caps by average close"),
        _report("R07 largest one-day gain", ps.groupby("ticker")["r"].max(),
                question="largest single-day gain"),
        _report("R08 deepest drawdown",
                p.sort_values("date").groupby("ticker")["close"].apply(max_drawdown),
                ascending=True, question="top 3 deepest drawdowns"),
    ]

    all_ties = [c for c in checks if c and c[0] == "TIE"]
    ties = [c for c in all_ties if c[1] not in TIE_BREAK_DECLARED]
    handled = [c for c in all_ties if c[1] in TIE_BREAK_DECLARED]
    near = [c for c in checks if c and c[0] == "NEAR"]

    print(f"Audited {len(checks)} rank-style questions for ambiguity.\n")
    if ties:
        print(f"!! {len(ties)} EXACT TIE(S) -- these questions have more than one "
              f"correct answer and MUST state a tie-break rule:")
        for _, name, detail, q in ties:
            print(f"   {name}: {detail}")
        print()
    else:
        print("No unhandled ties: every rank question has either a unique top "
              "answer or a stated tie-break rule.\n")

    if handled:
        print(f"{len(handled)} tie(s) present in the data but HANDLED by an "
              f"explicit rule in the question text:")
        for _, name, detail, q in handled:
            print(f"   {name}: {detail}")
            print(f"      -> {TIE_BREAK_DECLARED[name]}")
        print()

    if near:
        print(f"{len(near)} near tie(s) (top two within {NEAR_TIE_REL:.0%}). Not "
              f"bugs, but the answer turns on small differences:")
        for _, name, detail, q in near:
            print(f"   {name}: {detail}")
    else:
        print(f"No near ties within {NEAR_TIE_REL:.0%}.")

    return 1 if ties else 0


if __name__ == "__main__":
    sys.exit(main())
