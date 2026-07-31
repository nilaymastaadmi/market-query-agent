# Next steps — a self-contained prompt for resuming this work

Everything below assumes **no context** from the session that built this. It is
written to be pasted whole into a fresh Claude Code / Claude Desktop session, or
handed to a person.

Two other documents are prerequisites, not alternatives: `HANDOFF.md` holds the
two tasks that need a normal network and an API key (verifying the unexercised
`AnthropicAPIProvider`, and rebuilding the DB from live Yahoo Finance).
`METHODOLOGY.md` states the method the results rest on.

---

## State at the checkpoint

Pushed to `main` at `https://github.com/nilaymastaadmi/market-query-agent`.

**Complete and measured:**

| Thing | Status |
|---|---|
| SQLite DB, 4 tables, 24,243 price rows, 28 NSE tickers | built, committed, integrity-asserted |
| Three tools (`get_schema`, `run_sql`, `compute_metric`) | working |
| Four guardrail layers | 25/25 attacks stopped, 10/10 benign allowed, 58 tests pass |
| 48-question benchmark, 6 tiers + 4 injection probes | committed |
| Independent pandas ground truth | committed, self-checks pass |
| Grader | 19/19 self-tests pass |
| `main` run | **93.8%** overall, **100%** on all 40 answerable, 62.5% correct refusals |
| `abl_schema_in_prompt` run | 91.7% overall, 95.0% answerable, 75.0% correct refusals |
| Failure taxonomy for both | hand-labelled in `eval/failure_labels.json` |
| README with generated tables | rendered from real runs |

**Incomplete:**

1. `abl_no_retry` was still running at the checkpoint (~37/52 done). Its
   summary and its README rows are therefore missing or stale.
2. The serial (`--workers 1`) latency reference run had not started. The README
   currently says so explicitly rather than quoting a contended figure as if it
   were clean.
3. `abl_no_retry`'s failures are not yet hand-labelled.

---

## Copy-paste from here

> I have a Python repository cloned at `<PATH>` from
> `https://github.com/nilaymastaadmi/market-query-agent`. It benchmarks an LLM
> agent that answers questions about a stock-price SQLite database by writing
> SQL, and the point of the project is the *evaluation*, not the agent. Please
> finish the last configuration and re-render the report. Do not change the
> benchmark questions, the ground truth, the grader, or the agent prompt —
> changing any of those invalidates the runs already committed.
>
> **Setup**
>
> ```
> cd <PATH>
> python3 -m pip install -r requirements.txt
> python3 -m pytest tests/ -q          # expect 58 passed
> python3 -m eval.ground_truth         # expect "All self-checks passed"
> python3 -m eval.audit_ties           # expect exit 0, no unhandled ties
> ```
>
> If any of those three fail, stop and report the output — the committed results
> depend on them holding.
>
> **Step 1 — finish the third configuration.**
> Check whether `results/summary_abl_no_retry.json` exists and how many task
> logs are in `results/runs/abl_no_retry/` (should be 52). If it is incomplete,
> run:
>
> ```
> python3 -m eval.run_eval --config abl_no_retry --workers 2
> ```
>
> Use `--workers 2`. Do **not** use 4: it rate-limits with HTTP 429 and the run
> then measures the harness instead of the agent. Every summary reports
> `total_rate_limit_429s`; it must be 0. This takes roughly 20 minutes and needs
> either the authenticated `claude` CLI on PATH or `ANTHROPIC_API_KEY` set.
>
> **Step 2 — the serial latency reference.**
> The headline runs use 2 concurrent workers, so their per-task latency is
> inflated by contention. Produce a clean figure:
>
> ```
> python3 -m eval.run_eval --config main --workers 1 --tier timewindow \
>     --skip-injection --tag serial
> ```
>
> This writes `results/summary_main_serial.json`, which the report picks up
> automatically. Note it deliberately reruns only one tier — that is a sample
> for latency, not a second accuracy measurement, and it should not be presented
> as one.
>
> **Step 3 — hand-label the new failures.** This is the most important step and
> must not be automated. Run:
>
> ```
> python3 -m eval.classify_failures --config abl_no_retry --review
> ```
>
> For each failing task it prints the question, the expected and actual answers,
> the cited SQL, and every tool call with the database's own error text. Read
> each episode and add an entry to `eval/failure_labels.json` keyed
> `"abl_no_retry:<TASK_ID>"`, with a `category` drawn from `TAXONOMY` in
> `eval/classify_failures.py` and a `note` saying what *in the log* decided it.
>
> The script prints a heuristic suggestion next to each case. **It is a
> labelling aid and it is frequently wrong** — it disagreed with the hand label
> on 7 of 7 failures labelled so far. Do not accept it without reading the
> episode. If a failure genuinely does not fit any existing category, add a new
> one to `TAXONOMY` with a description rather than forcing a bad fit; two
> categories in the current taxonomy (`fabricated_after_empty_result`,
> `silent_disambiguation`) were added exactly that way.
>
> Failures already seen in this configuration, for orientation:
> - `J05` terminated on `no such column: p.ticker` with no chance to recover.
>   This is the ablation working as designed and is probably
>   `hallucinated_column`.
> - `J04` returned a volume 4.6x too large.
> - `R04` answered `BSEDATA`, a ticker that does not exist in the universe at
>   all — check whether it appeared in a tool result or was invented outright,
>   because that determines whether it is `hallucinated_column` or something
>   closer to `fabricated_after_empty_result`.
>
> Then verify nothing is unlabelled:
>
> ```
> python3 -m eval.classify_failures --config abl_no_retry --check   # must exit 0
> ```
>
> **Step 4 — re-render and check the ablation reads honestly.**
>
> ```
> python3 -m eval.render_readme
> python3 -m eval.report > results/RESULTS.md
> ```
>
> Now read the "Ablations" section critically. There is a specific trap here.
> The `main` run recorded **zero SQL errors**, so retry never fired in the
> baseline. A retry ablation against a baseline that never errs has almost
> nothing to bite on, which means any accuracy difference between `main` and
> `abl_no_retry` is confounded with ordinary run-to-run sampling variance —
> the same prompt does not produce the same SQL every time. If the numbers
> suggest retry "helped", that conclusion is not supported by one run each.
> Add a sentence to the ablation section of `README.md` saying so plainly, and
> report the retry rate (0% in main) next to it so the reader can see why the
> comparison is weak. Do not delete the ablation — a null or uninterpretable
> result is a finding and should be reported as one.
>
> **Step 5 — commit and push.**
>
> ```
> git add -A
> git commit    # describe what the third configuration measured
> git push -u origin main
> ```
>
> Do not open a pull request.
>
> **Rules that matter for this repository:**
> - Every number in `README.md` must come from a run that actually happened. The
>   results tables are generated between `<!-- X_START -->` / `<!-- X_END -->`
>   markers by `eval/render_readme.py`; never hand-edit inside them.
> - If you fix a scoring or grading bug, use
>   `python3 -m eval.run_eval --config <name> --rescore --note "why"` to rebuild
>   summaries from the saved episode logs instead of spending another run.
> - Report failures as prominently as successes. If a tier is at 40%, that
>   number goes in the headline table with the reason.

## Copy-paste ends

---

## Optional work, in the order I would do it

These are genuine improvements, not chores. None is required for the current
results to stand.

1. **Run each configuration 3–5 times and report a spread.** This is the single
   biggest weakness of the current results: every configuration was run once,
   so a 2-percentage-point difference between configurations is not
   distinguishable from noise, and the README says so. Repeat runs would turn
   the ablation section from suggestive into measured. Cheapest useful version:
   3 repeats of `main` only, reporting min/median/max accuracy.

2. **Root-cause `abl_schema_in_prompt:J06` and `:T07`.** Both produced
   structurally correct SQL that returned slightly wrong values (0.014% and 0.1%
   relative). They are labelled `arithmetic_slip` with an explicit note that
   they were *not* root-caused. Someone should run the cited SQL against
   `db/market.db` by hand and compare against `eval/ground_truth.py`. If the
   divergence is a real semantic difference — a different row set, a NULL
   handling difference — the labels should change and the finding is
   interesting.

3. **Add a second model.** `agent/providers.py` already takes `--model`, so
   `--model claude-sonnet-5` needs no code change. The interesting question is
   whether the three unanswerable-tier failures (fabrication and silent
   disambiguation) are model-specific or general. That is the most valuable
   unanswered question in the project.

4. **Grow the unanswerable tier.** It is where every failure lives and it is
   only 8 questions, so each one is worth 12.5 points of that tier's accuracy.
   Doubling it would sharpen the most informative measurement in the benchmark.

5. **Test the fabrication failure directly.** `main:U04` wrote correct SQL, got
   zero rows twice, and reported an invented price anyway. A targeted set of
   questions about absent tickers and out-of-range dates would establish whether
   that was one unlucky episode or a reliable behaviour. Given what it implies —
   a well-formed citation attached to a fabricated number — this deserves more
   than the single data point it currently has.
