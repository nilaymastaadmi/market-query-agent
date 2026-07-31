# Methodology

Stated before results, so the results cannot be reverse-engineered into a
method that flatters them.

## 1. What is being measured

A tool-using LLM agent answers analytical questions about Indian equity data by
writing SQL against a real SQLite database. The evaluation asks four questions
about it, in descending order of how much they matter:

1. **How often is it right, and where does it break?** Accuracy overall and per
   difficulty tier, so a headline number cannot hide a tier at 0%.
2. **How does it fail?** Every wrong answer hand-classified into a taxonomy.
   This is the deliverable; accuracy alone tells you nothing actionable.
3. **What does it cost?** Tokens, USD, latency and tool calls per task.
4. **Does it know what it does not know?** Correct-refusal rate on questions the
   schema cannot answer, and the rate at which it answers them anyway.

## 2. Universe selection

The 28-ticker universe comes from a disclosed 104-ticker candidate pool
(`data/universe_pool.py`) reproduced verbatim from the attached `trading-bot`
project. Reusing an already-published pool is the point: the candidate set was
fixed before this project existed, so it cannot have been shaped around this
benchmark.

`data/select_universe.py` applies a data-availability filter — which never looks
at returns, only at whether a usable price history exists — and then a seeded
stratified sample across cap tiers with a per-sector cap.

**The seed did no work here, and the script says so.** The availability filter
left exactly 28 tickers and the targets asked for 28, so the "sample" is the
entire available set, taken with zero discretion. The script prints
`NO SAMPLING DISCRETION` in that case rather than letting a disclosed seed imply
a selection it never made. This is a stronger anti-cherry-picking property than
a real sample would have been, not a weaker one.

`data/universe.csv` is pinned and committed; `build_db.py` reads that file, so a
fresh clone rebuilds the same database.

## 3. Data

Daily OHLCV from Yahoo Finance, `.NS` tickers, `auto_adjust=True` so splits,
bonuses and dividends are reflected in the price level. 2023-01-02 to
2026-07-03, plus the NIFTY 50 index as a benchmark series.

**Provenance caveat, stated plainly.** The sandbox this project was built in
blocks outbound HTTPS to Yahoo Finance (the egress proxy returns
`403 CONNECT` for `query1.finance.yahoo.com:443`, and equally for `query2`,
`stooq.com` and `nseindia.com`). The committed database was therefore built with
`--source cache` from CSVs that are themselves Yahoo Finance output with
`auto_adjust=True`, carried over from the attached `trading-bot` project. The
live path (`--source yfinance`) is implemented and is what a fresh clone with
network access runs; it was **not exercised here**. Both paths produce an
identical schema from the same upstream source, but only the cache path has been
run, and no claim is made beyond that.

## 4. Schema

Four tables. The full ER description is in the README.

The load-bearing design choice: `prices` keys on the surrogate `instrument_id`
and has **no ticker column**. Every price question therefore requires a join,
which is what the benchmark is meant to exercise, and a plausible-looking
`SELECT ... FROM prices WHERE ticker = 'TCS'` fails loudly instead of silently
returning nothing.

## 5. Metric conventions

Stated in the question text wherever a metric admits more than one definition,
and implemented independently on both sides:

| Metric | Definition |
|---|---|
| daily return | `close_t / close_{t-1} - 1` (simple, not log) |
| total return | `close_last / close_first - 1` over rows in window |
| CAGR | `(1 + total_return) ** (252 / n_returns) - 1` |
| annualised volatility | `stdev(r, ddof=1) * sqrt(252)` |
| max drawdown | `min(close / cummax(close) - 1)`, a negative number |
| Sharpe | `(mean(r) * 252 - 0.065) / (stdev(r, ddof=1) * sqrt(252))` |

`TRADING_DAYS = 252`, annual risk-free rate `RF = 0.065`. Both constants are
printed by `get_schema()`, so a model that gets a Sharpe wrong cannot blame an
undisclosed convention.

## 6. Ground truth independence

`eval/ground_truth.py` imports nothing from `agent/` and never opens
`db/market.db`. It reads `data/raw/*.csv` with pandas and recomputes every
answer from scratch. The agent and the answer key therefore reach their results
by different code over different artefacts, and a bug in the schema build, the
guardrails or the tools shows up as a disagreement rather than cancelling out.

Its self-check compares the CSV row count against the SQLite row count in the
database the agent queries — a genuine end-to-end check of `build_db.py`, not a
restatement of it.

What independence does **not** mean: the conventions in §5 are shared. A
benchmark whose two sides disagree about what "volatility" means measures
nothing. The specification is shared and disclosed; the implementation is not.

## 7. Grading

Tolerance-based for floats (relative, per-question), exact for counts, strings
and rankings. Rankings are graded in order.

Three things the grader refuses to credit, each of which would inflate accuracy:

- **An answer with no SQL behind it.** Graded wrong even when the number is
  right, and reported as its own category.
- **A percentage where a fraction was asked for.** The question says "return a
  fraction"; `9.4985` for `0.094985` is a failed instruction, labelled
  `unit_error` so it is counted separately from arithmetic mistakes.
- **A refusal on an answerable question, or an answer on an unanswerable one.**

What it does forgive, with every instance counted and reported: a number arriving
as `"1234.5"` instead of `1234.5`, and casing or whitespace on tickers. Those say
nothing about whether the agent understood the database.

## 8. Answer-shape disclosure

Each question tells the agent its expected `answer_type`. A real system
specifies its output contract too, and without one the benchmark would largely
measure formatting luck, flooding the failure taxonomy with noise and hiding the
reasoning errors it exists to surface. This is a deliberate simplification of
the task and it makes the benchmark easier than an unconstrained one.

## 9. Configurations and ablations

| Config | Schema delivery | Retry on SQL error |
|---|---|---|
| `main` | fetched via `get_schema()` | enabled |
| `abl_schema_in_prompt` | pasted into the system prompt | enabled |
| `abl_no_retry` | fetched via `get_schema()` | disabled |

Each ablation changes exactly one variable relative to `main`, so any difference
is attributable. With retry disabled, the first failing query ends the episode —
the agent never sees the database's message.

## 10. Honest measurement caveats

- **Transport overhead.** Reported numbers come from the Claude Code CLI, which
  prepends its own system prompt and tool definitions to every call. That
  overhead is measured (`results/harness_overhead.json`) and reported separately
  from agent-attributable tokens. Both figures appear in the README.
- **Parallelism inflates latency.** Runs use concurrent workers; per-task latency
  under contention is inflated by scheduling. A serial (`--workers 1`) reference
  run is reported alongside, and that is the figure to compare against another
  system.
- **Single run, no repeats.** Each configuration was run once. Differences of a
  few points between configurations are within plausible run-to-run noise for a
  48-question benchmark and should not be over-read. Sampling variability is not
  quantified here.
- **One model.** All reported numbers are from one model version. Nothing here
  generalises to other models without rerunning.
- **A harness artifact was found and fixed mid-build.** An early run showed the
  agent refusing answerable questions saying "the tools are not available" — it
  was generalising from the Claude Code CLI's own permission denials to the
  tools described in its prompt. That is a harness bug, not an agent failure,
  and reporting it as the latter would have been a misattribution. The system
  prompt now states explicitly that tools are executed by the harness reading
  its JSON output. Recorded here because an evaluation harness that can
  manufacture failures is exactly the thing an evaluation project should
  disclose.
- **Rate limiting invalidated a first full run.** Running four concurrent CLI
  processes rate-limited nearly every task with HTTP 429, producing a run that
  reported near-zero accuracy while measuring nothing but the harness's own
  concurrency. That run was discarded, not published; the provider now applies
  429-specific backoff and the reported runs use lower concurrency. The count of
  429s absorbed during the reported runs is in each summary
  (`total_rate_limit_429s`).
