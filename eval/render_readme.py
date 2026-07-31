"""
Splices generated tables into README.md between HTML comment markers.

    python3 -m eval.render_readme

Every results table in the README is written by this script from
results/summary_*.json and the committed failure labels. Nothing between a
<!-- X_START --> / <!-- X_END --> pair is hand-maintained, so a README number
cannot drift away from the run that produced it: rerun and diff.
"""
import io
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval import report  # noqa: E402
from eval.classify_failures import (  # noqa: E402
    TAXONOMY, distribution, failures, load_labels, load_run,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
RESULTS = os.path.join(ROOT, "results")


def splice(text: str, name: str, body: str) -> str:
    pat = re.compile(
        rf"(<!-- {name}_START -->).*?(<!-- {name}_END -->)", re.DOTALL
    )
    if not pat.search(text):
        raise SystemExit(f"marker pair {name}_START/{name}_END not found in README.md")
    return pat.sub(lambda m: f"{m.group(1)}\n{body}\n{m.group(2)}", text)


def taxonomy_section(config="main"):
    rows = load_run(config)
    labels = load_labels()
    counts, unlabelled, divergences, fails = distribution(rows, labels)
    n_graded = len([r for r in rows if r["task"]["tier"] != "injection"])
    n_fail = len(fails)
    out = io.StringIO()

    if not n_fail:
        out.write("No failures in the `main` run — the taxonomy is empty.\n")
        return out.getvalue()

    transport = counts.get("transport_failure", 0)
    agent_fails = n_fail - transport
    out.write(
        f"{n_fail} of {n_graded} tasks failed in the `main` run. Every one was "
        f"classified by hand from its episode log "
        f"(`results/runs/main/<id>.json`); the labels and their justifications "
        f"are committed in `eval/failure_labels.json`.\n\n"
    )
    if transport:
        out.write(
            f"**{transport} of those were transport failures** — provider or "
            f"subprocess errors, not agent mistakes. They are listed below for "
            f"completeness but excluded from the agent-attributable count of "
            f"**{agent_fails}**.\n\n"
        )
    out.write(f"| Category | Count | % of failures | % of all {n_graded} tasks | What it means |\n")
    out.write("|---|---:|---:|---:|---|\n")
    for cat, c in counts.most_common():
        out.write(
            f"| `{cat}` | {c} | {c / n_fail:.0%} | {c / n_graded:.1%} | "
            f"{TAXONOMY.get(cat, '?')} |\n"
        )
    out.write(f"| **total** | **{n_fail}** | **100%** | **{n_fail / n_graded:.1%}** | |\n")

    if divergences:
        n_lab = n_fail - len(unlabelled)
        out.write(
            f"\nA shallow automated heuristic (`suggest()` in "
            f"`eval/classify_failures.py`) disagreed with the hand label on "
            f"**{len(divergences)} of {n_lab}** failures "
            f"({len(divergences) / max(1, n_lab):.0%}). That gap is the argument "
            f"for hand-labelling: an automated taxonomy would have been wrong "
            f"about roughly that share of the cases.\n"
        )
    if unlabelled:
        out.write(
            f"\n**{len(unlabelled)} failure(s) are unlabelled: "
            f"{', '.join(unlabelled)}.** Run "
            f"`python3 -m eval.classify_failures --config main --review`.\n"
        )
    return out.getvalue()


def per_failure_table(config="main"):
    """One row per failing task -- the actual evidence behind the taxonomy."""
    rows = load_run(config)
    labels = load_labels()
    fails = failures(rows)
    if not fails:
        return ""
    out = io.StringIO()
    out.write("\n<details>\n<summary>Every individual failure "
              f"({len(fails)}), with what it got and why</summary>\n\n")
    out.write("| Task | Tier | Label | Expected | Got | What happened |\n")
    out.write("|---|---|---|---|---|---|\n")
    for r in sorted(fails, key=lambda x: x["task"]["id"]):
        t, g = r["task"], r["grade"]
        key = f"{r['result']['config']['label']}:{t['id']}"
        lab = labels.get(key, {})
        exp = str(g["expected"])[:40]
        got = str(g["got"])[:40]
        note = (lab.get("note") or g["detail"] or "")[:150].replace("|", "\\|")
        out.write(
            f"| `{t['id']}` | {t['tier']} | `{lab.get('category', '**unlabelled**')}` "
            f"| `{exp}` | `{got}` | {note} |\n"
        )
    out.write("\n</details>\n")
    return out.getvalue()


def model_section():
    p = os.path.join(RESULTS, "summary_main.json")
    if not os.path.exists(p):
        return "_no main run found_"
    with open(p) as f:
        s = json.load(f)
    man = s["manifest"]
    return (
        f"Every number above was produced by **`{man['model_reported']}`** via "
        f"the `{man['provider']}` provider, on commit `{man['git_commit'][:10]}`, "
        f"run {man['started_utc'][:16].replace('T', ' ')} UTC. "
        f"The model id, provider, git commit and configuration are recorded in "
        f"the manifest of every `results/summary_*.json`, so any figure here can "
        f"be traced to the run that produced it."
    )


def guardrail_section():
    """Run the adversarial suite and embed its actual output."""
    try:
        out = subprocess.check_output(
            [sys.executable, os.path.join(ROOT, "tests", "test_guardrails.py")],
            text=True, timeout=300, cwd=ROOT,
        )
    except Exception as e:
        return f"_could not run tests/test_guardrails.py: {e}_"
    lines = out.splitlines()

    def grab(pred):
        return next((ln for ln in lines if pred(ln)), "")

    attacks = grab(lambda ln: "attacks stopped" in ln)
    benign = grab(lambda ln: "benign queries allowed" in ln)
    inj = grab(lambda ln: "injected payloads blocked" in ln)
    body = io.StringIO()
    body.write(f"**{attacks.strip()}**, **{benign.strip()}**, "
               f"**{inj.strip()}**.\n\n")
    body.write("| Result | Measure |\n|---|---|\n")
    body.write(f"| {attacks.strip()} | each stopped by the layer the test says "
               f"stops it, not merely stopped |\n")
    body.write(f"| {benign.strip()} | false-positive rate on queries that only "
               f"*look* dangerous |\n")
    body.write(f"| {inj.strip()} | payloads embedded in question text, refused "
               f"at the tool layer |\n")
    body.write("\n<details>\n<summary>Full attack table (generated by "
               "`python3 tests/test_guardrails.py`)</summary>\n\n```\n")
    start = next((i for i, ln in enumerate(lines) if ln.startswith("ID ")), 0)
    body.write("\n".join(lines[start:]))
    body.write("\n```\n\n</details>\n")
    return body.getvalue()


def results_section():
    summaries = {c: report.load(c) for c in report.CONFIG_ORDER}
    if not any(summaries.values()):
        return "_no runs found; see Reproduction below_"
    b = io.StringIO()
    b.write("## Results\n\n")
    b.write("_Every number in this section is generated by "
            "`python3 -m eval.render_readme` from `results/summary_*.json`. "
            "None of it is hand-written._\n\n")
    b.write("### Headline: accuracy by tier\n\n")
    b.write(report.headline(summaries) + "\n\n")
    b.write("### Failure taxonomy\n\n")
    b.write("The most valuable output in this repository. Accuracy tells you "
            "*whether* the agent is wrong; only this tells you *how*.\n\n")
    b.write(taxonomy_section() + "\n")
    b.write(per_failure_table() + "\n")
    b.write("### Cost, effort and behaviour, all configurations\n\n")
    b.write(report.cost_table(summaries) + "\n\n")
    b.write("### Ablations\n\n")
    b.write(report.ablation_deltas(summaries) + "\n\n")
    b.write("### Prompt injection\n\n")
    b.write("Four probes carry a real, answerable question with an attack "
            "appended, so both the refusal and the honest answer are measured "
            "at once.\n\n")
    b.write(report.injection_table(summaries) + "\n\n")
    b.write("### Measurement caveats attached to the numbers above\n\n")
    b.write("**Transport overhead.** " + report.overhead_note() + "\n\n")
    b.write("**Latency.** " + report.latency_note(summaries) + "\n")
    return b.getvalue()


def main():
    with open(README) as f:
        text = f.read()
    text = splice(text, "RESULTS", results_section())
    text = splice(text, "GUARDRAILS", guardrail_section())
    text = splice(text, "MODEL", model_section())
    with open(README, "w") as f:
        f.write(text)
    print(f"Rendered {README}")


if __name__ == "__main__":
    main()
