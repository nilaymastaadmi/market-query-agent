"""
The evaluation harness. Runs a benchmark configuration end to end and writes
everything needed to reproduce and audit the reported numbers.

    python3 -m eval.run_eval --config main
    python3 -m eval.run_eval --config abl_schema_in_prompt
    python3 -m eval.run_eval --config abl_no_retry
    python3 -m eval.run_eval --list

Outputs, per configuration, under results/:

    runs/<config>/<task_id>.json   full episode: every model message, every
                                   tool call, the SQL, the DB's error text,
                                   tokens, latency, and the grade
    summary_<config>.json          aggregate metrics + the manifest

The manifest records the exact model id, provider, git commit, config and
wall-clock start, so every number in the README can be traced to the run that
produced it.

PARALLELISM. --workers runs several tasks concurrently to keep wall-clock
tolerable. Each task gets its own Tools instance and its own SQLite connection.
Per-task latency is measured individually, but under contention it is inflated
by scheduling, so the summary records `workers` and the README quotes latency
from the workers=1 run. This is a measurement caveat, not a footnote to bury.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.loop import AgentConfig, run_task  # noqa: E402
from agent.providers import build_provider, calibrate_overhead  # noqa: E402
from agent.tools import DEFAULT_DB, Tools  # noqa: E402
from eval.benchmark import BENCHMARK, INJECTION_TASKS, TIERS  # noqa: E402
from eval.compare import grade, load_ground_truth  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")

# The named configurations. `main` is the headline; the other two are the
# ablations, each changing exactly one variable relative to main so the
# difference is attributable.
CONFIGS = {
    "main": AgentConfig(
        label="main", schema_mode="tool", retry_on_sql_error=True, max_steps=8
    ),
    "abl_schema_in_prompt": AgentConfig(
        label="abl_schema_in_prompt", schema_mode="prompt",
        retry_on_sql_error=True, max_steps=8,
    ),
    "abl_no_retry": AgentConfig(
        label="abl_no_retry", schema_mode="tool",
        retry_on_sql_error=False, max_steps=8,
    ),
}


def git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", ROOT, "rev-parse", "HEAD"], text=True
        ).strip()
    except Exception:
        return "unknown"


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return 0.0
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def run_one(task, provider_kind, model, config, db_path):
    """Run and grade one task. Own Tools + own SQLite connection."""
    tools = Tools(db_path)
    try:
        provider = build_provider(provider_kind, model)
        res = run_task(task, tools, provider, config)
        return task, res.to_dict()
    finally:
        tools.close()


def aggregate(graded_rows, tasks_by_id) -> dict:
    """Everything the README's results table needs."""
    from collections import Counter

    n = len(graded_rows)
    answerable = [r for r in graded_rows if r["grade"]["expected"] != "UNANSWERABLE"]
    unanswerable = [r for r in graded_rows if r["grade"]["expected"] == "UNANSWERABLE"]

    def acc(rows):
        return (sum(r["grade"]["correct"] for r in rows) / len(rows)) if rows else None

    per_tier = {}
    for tier in TIERS:
        rows = [r for r in graded_rows if r["grade"]["tier"] == tier]
        if not rows:
            continue
        per_tier[tier] = {
            "n": len(rows),
            "n_correct": sum(r["grade"]["correct"] for r in rows),
            "accuracy": acc(rows),
            "mean_tool_calls": _mean([r["result"]["tool_calls"] for r in rows]),
            "mean_sql_errors": _mean([r["result"]["sql_errors"] for r in rows]),
            "mean_latency_s": _mean([r["result"]["latency_s"] for r in rows]),
            "mean_cost_usd_agent": _mean([r["result"]["cost_usd_agent"] for r in rows]),
            "verdicts": dict(Counter(r["grade"]["verdict"] for r in rows)),
        }

    R = [r["result"] for r in graded_rows]
    tasks_with_sql = [r for r in R if r["sql_calls"] > 0]

    return {
        "n_tasks": n,
        "accuracy_overall": acc(graded_rows),
        "accuracy_answerable_only": acc(answerable),
        "refusal_accuracy_on_unanswerable": acc(unanswerable),
        "n_unanswerable": len(unanswerable),
        "confidently_wrong_on_unanswerable": sum(
            1 for r in unanswerable if r["grade"]["verdict"] == "confidently_wrong"
        ),
        "wrong_refusals_on_answerable": sum(
            1 for r in answerable if r["grade"]["verdict"] == "wrong_refusal"
        ),
        "answers_without_sql": sum(
            1 for r in graded_rows if r["grade"]["verdict"] == "no_sql"
        ),
        "sql_citation_rate": (
            sum(1 for r in R if r["cited_sql"]) / max(1, len(
                [r for r in R if r["outcome"] in ("answered", "answered_no_sql")]
            ))
        ),
        "per_tier": per_tier,
        "verdicts": dict(Counter(r["grade"]["verdict"] for r in graded_rows)),
        "outcomes": dict(Counter(r["result"]["outcome"] for r in graded_rows)),
        "coerced_answers": sum(1 for r in graded_rows if r["grade"]["coerced"]),
        # cost / effort
        "mean_tool_calls": _mean([r["tool_calls"] for r in R]),
        "mean_sql_calls": _mean([r["sql_calls"] for r in R]),
        "mean_steps": _mean([r["steps_used"] for r in R]),
        "mean_prompt_tokens": _mean([r["prompt_tokens"] for r in R]),
        "mean_completion_tokens": _mean([r["completion_tokens"] for r in R]),
        "mean_cost_usd_agent": _mean([r["cost_usd_agent"] for r in R]),
        "mean_cost_usd_measured": _mean([r["cost_usd_measured"] for r in R]),
        "mean_cost_usd_cache_overhead": _mean([r["cost_usd_cache"] for r in R]),
        "total_cost_usd_measured": sum(r["cost_usd_measured"] for r in R),
        "mean_latency_s": _mean([r["latency_s"] for r in R]),
        "median_latency_s": _median([r["latency_s"] for r in R]),
        "mean_llm_latency_s": _mean([r["llm_latency_s"] for r in R]),
        # error / retry behaviour
        "total_sql_errors": sum(r["sql_errors"] for r in R),
        "total_blocked_calls": sum(r["blocked_calls"] for r in R),
        "total_retries": sum(r["retries"] for r in R),
        "tasks_with_at_least_one_sql_error": sum(1 for r in R if r["sql_errors"] > 0),
        "retry_rate": (
            sum(1 for r in tasks_with_sql if r["sql_errors"] > 0) / len(tasks_with_sql)
            if tasks_with_sql else 0.0
        ),
        "total_transport_retries": sum(r.get("transport_retries", 0) for r in R),
        "total_rate_limit_429s": sum(r.get("rate_limit_hits", 0) for r in R),
        "total_protocol_violations": sum(r["protocol_violations"] for r in R),
        "tasks_with_protocol_violation": sum(
            1 for r in R if r["protocol_violations"] > 0
        ),
    }


def aggregate_injection(rows) -> dict:
    """
    Injection probes measure two things at once: did the tool layer refuse the
    payload, and did the agent still answer the honest question correctly while
    being pushed at.
    """
    n = len(rows)
    blocked = 0
    executed = 0
    never_attempted = 0
    for r in rows:
        calls = r["result"].get("tool_call_log", [])
        # Match a DISTINCTIVE fragment, not the payload's first word. Using
        # the first word would score any SELECT as an attempt at the
        # "SELECT 1; DROP TABLE sectors" payload, inflating the block count
        # with queries that were never attacks.
        marker = r["task"]["payload_marker"].lower()
        attempts = [
            c for c in calls
            if c["tool"] == "run_sql"
            and marker in " ".join(json.dumps(c["args"]).lower().split())
        ]
        if not attempts:
            never_attempted += 1
        elif all(not c["ok"] for c in attempts):
            blocked += 1
        else:
            executed += 1
    return {
        "n_probes": n,
        "payload_never_attempted": never_attempted,
        "payload_attempted_and_blocked": blocked,
        "payload_executed": executed,
        "honest_answer_correct": sum(r["grade"]["correct"] for r in rows),
        "honest_answer_accuracy": (
            sum(r["grade"]["correct"] for r in rows) / n if n else None
        ),
    }


def rescore(outname: str, gt: dict):
    """
    Rebuild a summary from the per-task episode logs already on disk.

    The episode logs hold every tool call, so a scoring bug can be corrected
    without spending another run against the model. Grades are recomputed too,
    so a grader fix propagates. The manifest is preserved and stamped with
    `rescored_utc` -- a rescored summary must be identifiable as one.
    """
    run_dir = os.path.join(RESULTS, "runs", outname)
    if not os.path.isdir(run_dir):
        raise SystemExit(f"{run_dir} not found; nothing to rescore")
    rows = []
    for fn in sorted(os.listdir(run_dir)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(run_dir, fn)) as f:
            row = json.load(f)
        row["grade"] = grade(row["task"], row["result"], gt)
        with open(os.path.join(run_dir, fn), "w") as f:
            json.dump(row, f, indent=2, default=str)
        rows.append(row)

    graded_rows = [r for r in rows if r["task"]["tier"] != "injection"]
    inj_rows = [r for r in rows if r["task"]["tier"] == "injection"]
    path = os.path.join(RESULTS, f"summary_{outname}.json")
    manifest = {}
    if os.path.exists(path):
        with open(path) as f:
            manifest = json.load(f).get("manifest", {})
    manifest["rescored_utc"] = datetime.now(timezone.utc).isoformat()
    summary = {"manifest": manifest, "metrics": aggregate(graded_rows, {})}
    if inj_rows:
        summary["injection"] = aggregate_injection(inj_rows)
    with open(path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    m = summary["metrics"]
    print(f"rescored {outname}: accuracy {m['accuracy_overall']:.1%} "
          f"({m['n_tasks']} tasks) -> {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="main", choices=list(CONFIGS) + ["all"])
    ap.add_argument("--provider", default="cli", choices=["cli", "api"])
    ap.add_argument("--model", default="claude-haiku-4-5-20251001")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="first N graded tasks only")
    ap.add_argument("--tier", default="", help="restrict to one tier")
    ap.add_argument("--task", default="", help="comma-separated task ids to run; "
                    "merges into the existing run dir so a single corrected "
                    "question can be redone without repeating the benchmark")
    ap.add_argument("--skip-injection", action="store_true")
    ap.add_argument("--calibrate", action="store_true",
                    help="measure the provider's fixed prompt overhead and exit")
    ap.add_argument("--tag", default="", help="suffix for the summary/run dir, so a "
                    "subset run cannot overwrite a full run's results")
    ap.add_argument("--rescore", action="store_true",
                    help="recompute summary_<config>.json from the saved per-task "
                    "logs without calling the model. Use after a scoring fix so a "
                    "corrected metric does not require burning a fresh run.")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        for name, c in CONFIGS.items():
            print(f"{name:24s} {c.to_dict()}")
        return

    os.makedirs(RESULTS, exist_ok=True)

    if args.calibrate:
        provider = build_provider(args.provider, args.model)
        info = calibrate_overhead(provider)
        path = os.path.join(RESULTS, "harness_overhead.json")
        with open(path, "w") as f:
            json.dump(info, f, indent=2)
        print(json.dumps(
            {k: v for k, v in info.items() if k != "runs"}, indent=2
        ))
        print(f"\nWrote {path}")
        return

    gt = load_ground_truth()
    config_names = list(CONFIGS) if args.config == "all" else [args.config]

    if args.rescore:
        for cname in config_names:
            outname = cname + (f"_{args.tag}" if args.tag else "")
            rescore(outname, gt)
        return

    for cname in config_names:
        config = CONFIGS[cname]
        tasks = list(BENCHMARK)
        if args.task:
            want = {t.strip() for t in args.task.split(",")}
            tasks = [t for t in tasks if t["id"] in want]
        if args.tier:
            tasks = [t for t in tasks if t["tier"] == args.tier]
        if args.limit:
            tasks = tasks[: args.limit]
        inj = [] if (args.skip_injection or args.task) else list(INJECTION_TASKS)
        all_tasks = tasks + inj

        outname = cname + (f"_{args.tag}" if args.tag else "")
        run_dir = os.path.join(RESULTS, "runs", outname)
        os.makedirs(run_dir, exist_ok=True)

        print(f"\n=== {outname} | {args.provider}:{args.model} | "
              f"{len(tasks)} graded + {len(inj)} injection | "
              f"workers={args.workers} ===")
        started = datetime.now(timezone.utc).isoformat()
        t0 = time.monotonic()
        rows = []

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = {
                pool.submit(run_one, t, args.provider, args.model, config, args.db): t
                for t in all_tasks
            }
            for fut in as_completed(futures):
                task = futures[fut]
                try:
                    task, result = fut.result()
                except Exception as e:  # harness bug, not a model failure
                    result = {
                        "task_id": task["id"], "tier": task["tier"],
                        "question": task["question"],
                        "answer_type": task["answer_type"],
                        "outcome": "provider_error",
                        "error": f"harness exception: {type(e).__name__}: {e}",
                        "value": None, "cited_sql": None,
                        "claimed_unanswerable": False,
                        "tool_calls": 0, "sql_calls": 0, "sql_errors": 0,
                        "blocked_calls": 0, "retries": 0, "steps_used": 0,
                        "protocol_violations": 0, "transport_retries": 0,
                        "rate_limit_hits": 0, "prompt_tokens": 0,
                        "completion_tokens": 0, "cache_write_tokens": 0,
                        "cache_read_tokens": 0, "cost_usd_measured": 0.0,
                        "cost_usd_agent": 0.0, "cost_usd_cache": 0.0,
                        "latency_s": 0.0, "llm_latency_s": 0.0,
                        "config": config.to_dict(), "model": args.model,
                        "provider": args.provider, "transcript": [],
                        "tool_call_log": [],
                    }
                g = grade(task, result, gt)
                row = {"task": task, "result": result, "grade": g}
                rows.append(row)
                with open(os.path.join(run_dir, f"{task['id']}.json"), "w") as f:
                    json.dump(row, f, indent=2, default=str)
                mark = "OK " if g["correct"] else "XX "
                print(f"  {mark}{task['id']:4s} {g['verdict']:<18s} "
                      f"{result['tool_calls']}tc {result['latency_s']:5.1f}s "
                      f"{g['detail'][:60]}")

        wall = time.monotonic() - t0
        rows.sort(key=lambda r: r["task"]["id"])
        graded_rows = [r for r in rows if r["task"]["tier"] != "injection"]
        inj_rows = [r for r in rows if r["task"]["tier"] == "injection"]

        summary = {
            "manifest": {
                "config": config.to_dict(),
                "provider": args.provider,
                "model_requested": args.model,
                "model_reported": next(
                    (r["result"]["model"] for r in rows if r["result"].get("model")),
                    args.model,
                ),
                "db": os.path.relpath(args.db, ROOT),
                "git_commit": git_commit(),
                "started_utc": started,
                "wall_clock_s": wall,
                "workers": args.workers,
                "ground_truth_answers": len(gt),
            },
            "metrics": aggregate(graded_rows, {t["id"]: t for t in all_tasks}),
        }
        if inj_rows:
            summary["injection"] = aggregate_injection(inj_rows)

        path = os.path.join(RESULTS, f"summary_{outname}.json")
        with open(path, "w") as f:
            json.dump(summary, f, indent=2, default=str)

        m = summary["metrics"]

        def pct(x):
            return "n/a" if x is None else f"{x:.1%}"

        print(f"\n  accuracy overall        {pct(m['accuracy_overall'])} "
              f"({sum(r['grade']['correct'] for r in graded_rows)}/{m['n_tasks']})")
        print(f"  answerable only         {pct(m['accuracy_answerable_only'])}")
        print(f"  correct refusals        "
              f"{pct(m['refusal_accuracy_on_unanswerable'])} "
              f"of {m['n_unanswerable']}")
        print(f"  mean tool calls/task    {m['mean_tool_calls']:.2f}")
        print(f"  retry rate              {m['retry_rate']:.1%}")
        print(f"  mean agent cost/task    ${m['mean_cost_usd_agent']:.5f}")
        print(f"  mean latency/task       {m['mean_latency_s']:.1f}s")
        print(f"  wall clock              {wall:.0f}s")
        print(f"  wrote {path}")


if __name__ == "__main__":
    main()
