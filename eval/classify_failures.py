"""
The failure taxonomy: every wrong answer classified by hand, then counted.

    python3 -m eval.classify_failures --config main            # distribution
    python3 -m eval.classify_failures --config main --review   # dump for labelling
    python3 -m eval.classify_failures --config main --check    # CI-style gate

WHY THIS IS NOT AUTOMATED. A regex over the logs can tell you a query mentioned
a column that does not exist. It cannot tell you whether the agent misread the
schema, hallucinated the column, or read the schema correctly and then wrote the
wrong join -- and those are different bugs with different fixes. So the labels in
eval/failure_labels.json are hand-assigned, one per failing task, each with a
one-line justification pointing at what in the log decided it. `--review` prints
the evidence needed to assign a label; `--check` fails if any failure is
unlabelled, so the taxonomy cannot silently drift out of date when a rerun
produces a new failure.

The script does compute a HEURISTIC suggestion, shown next to each case in
--review. It is a labelling aid only. Where the committed label disagrees with
the heuristic, the label wins, and `--check` reports how often they diverged --
which is itself informative about how far a naive automated taxonomy would be
off.
"""
import argparse
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")
LABELS_PATH = os.path.join(HERE, "failure_labels.json")

# The taxonomy. The first seven are the categories named in the project brief;
# the rest were added because real failures landed outside those seven and
# forcing them in would have misrepresented what went wrong.
TAXONOMY = {
    "hallucinated_column": "referenced a column or table that does not exist in the schema",
    "wrong_aggregation": "used the wrong aggregate or grouped at the wrong level",
    "wrong_time_window": "applied the wrong date filter, or none where one was required",
    "arithmetic_slip": "right query, wrong arithmetic on the way to the reported number",
    "schema_misread": "read the schema but misunderstood a relationship or a join key",
    "gave_up": "ran out of steps, or terminated without producing an answer",
    "confidently_wrong_unanswerable": "produced a value for a question the schema cannot answer",
    # --- added during labelling, because failures landed here ---
    "over_refusal": "refused a question that the schema can in fact answer",
    "no_sql_citation": "produced an answer without citing the SQL behind it",
    "unit_error": "correct magnitude in the wrong unit (percentage where a fraction was asked)",
    "wrong_ranking_order": "correct set of items, ordered wrongly",
    "protocol_violation": "could not emit a parseable action within the violation budget",
    "transport_failure": "provider/subprocess failure, NOT an agent error -- excluded from"
                         " agent-attributable failure rates and reported separately",
}


def load_run(config: str) -> list:
    d = os.path.join(RESULTS, "runs", config)
    if not os.path.isdir(d):
        raise SystemExit(f"{d} not found. Run: python3 -m eval.run_eval --config {config}")
    rows = []
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".json"):
            with open(os.path.join(d, fn)) as f:
                rows.append(json.load(f))
    return rows


def load_labels() -> dict:
    if not os.path.exists(LABELS_PATH):
        return {}
    with open(LABELS_PATH) as f:
        return json.load(f)


def failures(rows) -> list:
    """Graded tasks that were not correct. Injection probes excluded."""
    return [
        r for r in rows
        if r["task"]["tier"] != "injection" and not r["grade"]["correct"]
    ]


def suggest(row) -> str:
    """
    Heuristic suggestion, for labelling assistance only.

    Deliberately shallow. If this were good enough to trust, the taxonomy would
    not need hand labels -- and the point of --check reporting the divergence
    rate is to show that it is not.
    """
    g, res = row["grade"], row["result"]
    v = g["verdict"]
    if v == "confidently_wrong":
        return "confidently_wrong_unanswerable"
    if v == "wrong_refusal":
        return "over_refusal"
    if v == "no_sql":
        return "no_sql_citation"
    if v == "unit_error":
        return "unit_error"
    if v == "no_answer":
        out = res.get("outcome")
        if out == "provider_error":
            return "transport_failure"
        if out == "protocol_failure":
            return "protocol_violation"
        return "gave_up"
    if v == "wrong_value" and "WRONG ORDER" in (g.get("detail") or ""):
        return "wrong_ranking_order"
    # look at the database's own complaints
    errs = " ".join(
        str(c.get("note", "")) for c in res.get("tool_call_log", []) if not c.get("ok")
    ).lower()
    if "no such column" in errs or "no such table" in errs:
        return "hallucinated_column"
    if v == "malformed":
        return "protocol_violation"
    return "wrong_aggregation"


def review(rows):
    """Print everything needed to hand-label each failure."""
    fails = failures(rows)
    labels = load_labels()
    print(f"{len(fails)} failing tasks to label.\n")
    for r in fails:
        t, g, res = r["task"], r["grade"], r["result"]
        key = f"{res['config']['label']}:{t['id']}"
        have = labels.get(key)
        print("=" * 78)
        print(f"{t['id']}  tier={t['tier']}  verdict={g['verdict']}")
        print(f"Q: {t['question'][:200]}")
        print(f"expected: {g['expected']!r}")
        print(f"got:      {g['got']!r}")
        print(f"detail:   {g['detail'][:300]}")
        print(f"outcome:  {res['outcome']}  steps={res['steps_used']} "
              f"tool_calls={res['tool_calls']} sql_errors={res['sql_errors']}")
        if res.get("cited_sql"):
            print(f"cited SQL:\n  {res['cited_sql'][:600]}")
        for c in res.get("tool_call_log", []):
            flag = "ok " if c["ok"] else "ERR"
            arg = json.dumps(c["args"])[:220]
            print(f"  [{flag}] {c['tool']} {arg}")
            if not c["ok"]:
                print(f"        -> {str(c['note'])[:220]}")
        print(f"heuristic suggests: {suggest(r)}")
        print(f"committed label:    {have['category'] if have else '** MISSING **'}")
        if have:
            print(f"  because: {have.get('note', '')}")


def distribution(rows, labels):
    fails = failures(rows)
    counts = Counter()
    unlabelled = []
    divergences = []
    for r in fails:
        key = f"{r['result']['config']['label']}:{r['task']['id']}"
        lab = labels.get(key)
        if not lab:
            unlabelled.append(r["task"]["id"])
            continue
        counts[lab["category"]] += 1
        if lab["category"] != suggest(r):
            divergences.append((r["task"]["id"], suggest(r), lab["category"]))
    return counts, unlabelled, divergences, fails


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="main")
    ap.add_argument("--review", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    rows = load_run(args.config)
    if args.review:
        review(rows)
        return

    labels = load_labels()
    counts, unlabelled, divergences, fails = distribution(rows, labels)
    n_graded = len([r for r in rows if r["task"]["tier"] != "injection"])
    n_fail = len(fails)
    agent_fails = n_fail - counts.get("transport_failure", 0)

    if args.markdown:
        print(f"| Category | Count | % of failures | % of all {n_graded} tasks |")
        print("|---|---:|---:|---:|")
        for cat, c in counts.most_common():
            print(f"| `{cat}` | {c} | {c / n_fail:.0%} | {c / n_graded:.1%} |")
        print(f"| **total** | **{n_fail}** | **100%** | **{n_fail / n_graded:.1%}** |")
    else:
        print(f"config={args.config}  graded={n_graded}  failures={n_fail}  "
              f"agent-attributable={agent_fails}\n")
        print(f"{'CATEGORY':<34} {'N':>3}  {'%FAIL':>6}  DESCRIPTION")
        print("-" * 100)
        for cat, c in counts.most_common():
            print(f"{cat:<34} {c:>3}  {c / n_fail:>5.0%}   {TAXONOMY.get(cat, '?')[:44]}")
        print("-" * 100)
        print(f"{'TOTAL':<34} {n_fail:>3}")

    if divergences:
        print(f"\nHeuristic disagreed with the hand label on {len(divergences)}/"
              f"{n_fail - len(unlabelled)} labelled failures "
              f"({len(divergences) / max(1, n_fail - len(unlabelled)):.0%}) -- "
              f"which is why the labels are hand-assigned:")
        for tid, h, lab in divergences:
            print(f"  {tid}: heuristic said {h}, actually {lab}")

    if unlabelled:
        print(f"\n!! {len(unlabelled)} UNLABELLED FAILURE(S): {', '.join(unlabelled)}")
        print(f"   Run: python3 -m eval.classify_failures --config {args.config} --review")
        print(f"   then add entries to {os.path.relpath(LABELS_PATH, ROOT)}")
        if args.check:
            raise SystemExit(1)
    elif args.check:
        print("\nEvery failure has a hand-assigned label.")


if __name__ == "__main__":
    main()
