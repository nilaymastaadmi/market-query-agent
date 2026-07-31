"""
Renders the committed run summaries into the markdown tables the README quotes.

    python3 -m eval.report                 > results/RESULTS.md
    python3 -m eval.report --section main

Every table here is generated from results/summary_*.json, which is written by
eval/run_eval.py from an actual run. Nothing in this file computes a metric or
carries a hardcoded number, so a README table cannot drift away from the run
that produced it: regenerate and diff.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RESULTS = os.path.join(ROOT, "results")

CONFIG_ORDER = ["main", "abl_schema_in_prompt", "abl_no_retry"]
CONFIG_TITLE = {
    "main": "main (schema via tool, retry on)",
    "abl_schema_in_prompt": "ablation A: schema in prompt",
    "abl_no_retry": "ablation B: retry disabled",
}
TIER_ORDER = ["lookup", "aggregation", "join", "timewindow", "ranking", "unanswerable"]


def load(config):
    p = os.path.join(RESULTS, f"summary_{config}.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def pct(x, nd=0):
    return "n/a" if x is None else f"{100 * x:.{nd}f}%"


def headline(summaries):
    """Accuracy per tier for the main run, with the failure reason inline."""
    s = summaries.get("main")
    if not s:
        return "_no main run found_"
    m, man = s["metrics"], s["manifest"]
    out = []
    out.append(f"Model: `{man['model_reported']}` via `{man['provider']}`. "
               f"{m['n_tasks']} graded questions. "
               f"Commit `{man['git_commit'][:10]}`.\n")
    out.append("| Tier | n | Correct | Accuracy | Mean tool calls | Mean $/task | Dominant failure |")
    out.append("|---|---:|---:|---:|---:|---:|---|")
    for tier in TIER_ORDER:
        t = m["per_tier"].get(tier)
        if not t:
            continue
        bad = {k: v for k, v in t["verdicts"].items()
               if k not in ("correct", "correct_refusal")}
        dom = max(bad, key=bad.get) if bad else "-"
        dom_s = f"`{dom}` ({bad[dom]})" if bad else "none"
        out.append(
            f"| {tier} | {t['n']} | {t['n_correct']} | **{pct(t['accuracy'])}** | "
            f"{t['mean_tool_calls']:.1f} | ${t['mean_cost_usd_agent']:.4f} | {dom_s} |"
        )
    out.append(
        f"| **overall** | **{m['n_tasks']}** | "
        f"**{sum(t['n_correct'] for t in m['per_tier'].values())}** | "
        f"**{pct(m['accuracy_overall'], 1)}** | {m['mean_tool_calls']:.1f} | "
        f"${m['mean_cost_usd_agent']:.4f} | |"
    )
    return "\n".join(out)


def cost_table(summaries):
    out = ["| Metric | " + " | ".join(
        CONFIG_TITLE[c] for c in CONFIG_ORDER if summaries.get(c)) + " |"]
    out.append("|---" * (1 + sum(1 for c in CONFIG_ORDER if summaries.get(c))) + "|")
    rows = [
        ("Accuracy, all tasks", lambda m: pct(m["accuracy_overall"], 1)),
        ("Accuracy, answerable only", lambda m: pct(m["accuracy_answerable_only"], 1)),
        ("Correct refusal rate (unanswerable)",
         lambda m: pct(m["refusal_accuracy_on_unanswerable"], 1)),
        ("Confidently wrong on unanswerable",
         lambda m: f"{m['confidently_wrong_on_unanswerable']}/{m['n_unanswerable']}"),
        ("Wrong refusals on answerable",
         lambda m: str(m["wrong_refusals_on_answerable"])),
        ("Answers with no SQL cited", lambda m: str(m["answers_without_sql"])),
        ("SQL citation rate", lambda m: pct(m["sql_citation_rate"], 1)),
        ("Mean tool calls / task", lambda m: f"{m['mean_tool_calls']:.2f}"),
        ("Mean run_sql calls / task", lambda m: f"{m['mean_sql_calls']:.2f}"),
        ("Mean model steps / task", lambda m: f"{m['mean_steps']:.2f}"),
        ("Mean agent tokens in / task", lambda m: f"{m['mean_prompt_tokens']:.0f}"),
        ("Mean agent tokens out / task", lambda m: f"{m['mean_completion_tokens']:.0f}"),
        ("Mean agent cost / task", lambda m: f"${m['mean_cost_usd_agent']:.5f}"),
        ("Mean measured cost / task (incl. harness)",
         lambda m: f"${m['mean_cost_usd_measured']:.5f}"),
        ("Mean latency / task", lambda m: f"{m['mean_latency_s']:.1f}s"),
        ("Median latency / task", lambda m: f"{m['median_latency_s']:.1f}s"),
        ("Retry rate (tasks hitting >=1 SQL error)", lambda m: pct(m["retry_rate"], 1)),
        ("Total SQL errors", lambda m: str(m["total_sql_errors"])),
        ("Total guardrail blocks", lambda m: str(m["total_blocked_calls"])),
        ("Total protocol violations", lambda m: str(m["total_protocol_violations"])),
        ("Transport retries (provider flakes)",
         lambda m: str(m.get("total_transport_retries", 0))),
        ("Answers needing type coercion", lambda m: str(m["coerced_answers"])),
    ]
    for label, fn in rows:
        cells = []
        for c in CONFIG_ORDER:
            s = summaries.get(c)
            if not s:
                continue
            try:
                cells.append(fn(s["metrics"]))
            except (KeyError, TypeError):
                cells.append("n/a")
        out.append(f"| {label} | " + " | ".join(cells) + " |")
    return "\n".join(out)


def ablation_deltas(summaries):
    base = summaries.get("main")
    if not base:
        return "_no main run to compare against_"
    b = base["metrics"]
    out = ["| Ablation | Change vs main | Accuracy | Δ accuracy | Mean tool calls | "
           "Mean $/task | Retry rate |", "|---|---|---:|---:|---:|---:|---:|"]
    out.append(
        f"| main | baseline | {pct(b['accuracy_overall'], 1)} | - | "
        f"{b['mean_tool_calls']:.2f} | ${b['mean_cost_usd_agent']:.5f} | "
        f"{pct(b['retry_rate'], 1)} |"
    )
    what = {
        "abl_schema_in_prompt": "schema pasted into the system prompt "
                                "instead of fetched with get_schema",
        "abl_no_retry": "first SQL error ends the episode; the agent never "
                        "sees the database's message",
    }
    for c in ["abl_schema_in_prompt", "abl_no_retry"]:
        s = summaries.get(c)
        if not s:
            continue
        m = s["metrics"]
        d = m["accuracy_overall"] - b["accuracy_overall"]
        out.append(
            f"| {c} | {what[c]} | {pct(m['accuracy_overall'], 1)} | "
            f"{'+' if d >= 0 else ''}{100 * d:.1f} pp | "
            f"{m['mean_tool_calls']:.2f} | ${m['mean_cost_usd_agent']:.5f} | "
            f"{pct(m['retry_rate'], 1)} |"
        )
    return "\n".join(out)


def injection_table(summaries):
    out = ["| Config | Probes | Payload never attempted | Attempted, blocked | "
           "Payload EXECUTED | Honest answer correct |",
           "|---|---:|---:|---:|---:|---:|"]
    for c in CONFIG_ORDER:
        s = summaries.get(c)
        if not s or "injection" not in s:
            continue
        i = s["injection"]
        out.append(
            f"| {c} | {i['n_probes']} | {i['payload_never_attempted']} | "
            f"{i['payload_attempted_and_blocked']} | **{i['payload_executed']}** | "
            f"{i['honest_answer_correct']}/{i['n_probes']} |"
        )
    return "\n".join(out)


def overhead_note():
    p = os.path.join(RESULTS, "harness_overhead.json")
    if not os.path.exists(p):
        return "_no calibration run found_"
    with open(p) as f:
        o = json.load(f)
    mean = o.get("mean_overhead_tokens")
    return (
        f"Measured with {o['repeats']} near-empty calls to `{o['model']}` via "
        f"`{o['provider']}`: mean **{mean:,.0f} tokens** of transport system "
        f"prompt per model call, costing a mean of "
        f"${o['mean_cost_usd_measured']:.5f} per call, at a mean latency of "
        f"{o['mean_latency_s']:.1f}s. That is the floor under every measured "
        f"cost and latency figure in this project, and it belongs to the Claude "
        f"Code CLI, not to the agent."
    )


def latency_note():
    p = os.path.join(RESULTS, "summary_main_serial.json")
    if not os.path.exists(p):
        return ("_no serial run found; latency figures above are from the "
                "parallel run and are inflated by worker contention_")
    with open(p) as f:
        s = json.load(f)
    m = s["metrics"]
    return (
        f"Serial reference (`--workers 1`, {m['n_tasks']} tasks from the "
        f"timewindow tier): mean **{m['mean_latency_s']:.1f}s** per task, median "
        f"{m['median_latency_s']:.1f}s, mean {m['mean_steps']:.2f} model steps. "
        f"Use these rather than the parallel-run latencies when comparing "
        f"against another system."
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--section", default="all")
    args = ap.parse_args()

    summaries = {c: load(c) for c in CONFIG_ORDER}
    have = [c for c in CONFIG_ORDER if summaries.get(c)]
    if not have:
        raise SystemExit(
            "No summaries in results/. Run: python3 -m eval.run_eval --config all"
        )

    sections = {
        "headline": ("Headline: accuracy per tier", headline(summaries)),
        "main": ("Full metrics, all configurations", cost_table(summaries)),
        "ablations": ("Ablations", ablation_deltas(summaries)),
        "injection": ("Prompt injection probes", injection_table(summaries)),
        "overhead": ("Transport overhead", overhead_note()),
        "latency": ("Latency reference", latency_note()),
    }
    if args.section != "all":
        print(sections[args.section][1])
        return

    print("# Results\n")
    print("_Generated by `python3 -m eval.report` from `results/summary_*.json`. "
          "Do not hand-edit._\n")
    for key in ["headline", "main", "ablations", "injection", "overhead", "latency"]:
        title, body = sections[key]
        print(f"## {title}\n")
        print(body)
        print()


if __name__ == "__main__":
    main()
