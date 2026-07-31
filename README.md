# market-query-agent

A tool-using LLM agent that answers analytical questions about Indian equity
market data by writing and executing SQL — and, more to the point, **the
evaluation harness that measures how well it actually works.**

Wiring up tool calls is easy and everyone has done it. Almost nobody reports
task success rate, cost per task, and a taxonomy of how it fails. That is what
this repository is for. Where a choice existed between a more capable agent and
a more rigorous evaluation, the evaluation won.

**Read `METHODOLOGY.md` before the results below.** Method is stated there
before any number appears here, so the results cannot be reverse-engineered into
a method that flatters them.

<!-- RESULTS_START -->
<!-- RESULTS_END -->

## What this is not

Not investment advice. Not a production text-to-SQL system. Not a claim about
any model's general capability — one model, one database, one 48-question
benchmark, run once. See "Known limits" before drawing any conclusion stronger
than "here is what happened on this specific data."

## Database

28 NSE-listed stocks, daily OHLCV, 2023-01-02 to 2026-07-03, plus the NIFTY 50
index. 24,243 price rows. Yahoo Finance via `yfinance`, `auto_adjust=True`.

### ER description

```
  sectors                    instruments                  prices
  -----------                -----------                  ------
  sector_id      PK   <---+  instrument_id  PK   <---+    instrument_id  FK
  sector_name    UNIQUE    |  ticker         UNIQUE   |    date
  sector_group             +--sector_id      FK       +--  open high low close
                              name                          volume
                              cap_tier                   PK (instrument_id, date)
                              exchange
                              currency

  index_prices  (index_code, date, open, high, low, close, volume)
                 PK (index_code, date) -- NIFTY 50; no FK, an index is not
                 an instrument in this schema
```

- `sectors 1 --- N instruments 1 --- N prices`
- **`prices` has no ticker column.** To filter or group by ticker, cap tier or
  sector you must join `prices → instruments` on `instrument_id`, and
  `instruments → sectors` on `sector_id` for sector questions. Every price
  question in the benchmark is therefore at least a two-table join, and the
  join tier is three tables or more. This is deliberate: it is what makes the
  benchmark test joins rather than single-table selects, and it makes a
  plausible-looking `SELECT ... FROM prices WHERE ticker='TCS'` fail loudly
  instead of silently returning nothing.

**Indexes**, on the columns the benchmark actually filters and groups by:
`prices(instrument_id, date)` (PK), `prices(date)`, `instruments(ticker)`
(unique), `instruments(sector_id)`, `instruments(cap_tier)`,
`index_prices(index_code, date)` (PK), `index_prices(date)`.

**Integrity**, asserted at build time and re-asserted by the ground-truth
self-check: 0 orphan price rows, 0 OHLC violations (`high < low`, non-positive
prices), 0 foreign-key violations, and the SQLite row count equals the CSV row
count computed independently.

### What is deliberately absent

No fundamentals, no earnings, no P/E, no shares outstanding (so absolute market
cap cannot be computed — `cap_tier` is a categorical label, not a number), no
dividend rows (prices are already adjusted), no intraday data, no news or
sentiment, and no instruments outside the 28. `get_schema()` states all of this
explicitly, because the unanswerable tier is meant to test whether the agent
reads a schema, not whether it can guess what a database probably contains.

## The three tools

| Tool | What it does |
|---|---|
| `get_schema()` | Returns the schema as text, with live row counts, date coverage, an explicit list of what is *not* in the database, and the metric conventions. |
| `run_sql(query)` | Executes one read-only `SELECT` (or `WITH … SELECT`). Returns columns and rows, capped at 200 rows, with a 5s statement timeout. On error it returns the database's own message so the agent can correct itself. |
| `compute_metric(metric, ticker, start, end)` | Computes `total_return`, `cagr`, `ann_volatility`, `max_drawdown` or `sharpe` in pandas over one ticker's close series. |

Errors are returned to the model, not raised. That retry behaviour is one of the
things the evaluation measures, so swallowing the database's message would
destroy the measurement.

## Guardrails

Four independent layers on `run_sql`, because any single one is bypassable and a
guardrail tested one way is a guardrail whose shape you do not know:

1. **Textual screen** — strips comments and string literals, then requires a
   single statement beginning with `SELECT`/`WITH` and rejects a keyword
   denylist. Stripping literals first matters in both directions:
   `SELECT 'DROP TABLE x'` is legitimate and must pass, while
   `SELECT 1 /* */; DROP TABLE prices` must not.
2. **Read-only connection** — opened `mode=ro`, so SQLite itself refuses writes.
3. **SQLite authorizer** — a callback consulted at parse time for every action;
   only `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION` and
   `SQLITE_RECURSIVE` are permitted. This layer does not care how cleverly the
   SQL was written.
4. **Budgets** — a wall-clock timeout enforced through SQLite's progress handler
   (so a runaway cross-join is interrupted mid-execution, not merely reported
   afterwards) and a hard row cap applied at fetch time.

`python3 tests/test_guardrails.py` prints the attack table below.

<!-- GUARDRAILS_START -->
<!-- GUARDRAILS_END -->

## The benchmark

48 graded questions, 8 in each of six tiers, plus 4 prompt-injection probes
scored separately.

| Tier | What it tests |
|---|---|
| `lookup` | single-table reads from `instruments` / `sectors` |
| `aggregation` | aggregates over `prices` (still requires the instruments join) |
| `join` | three-table paths, or the no-FK join across to `index_prices` |
| `timewindow` | date-bounded returns, volatility, drawdown, Sharpe |
| `ranking` | top-N and ordering, graded exactly and in order |
| `unanswerable` | 5 needing data the schema lacks, 3 genuinely ambiguous |

The unanswerable tier is split because the two halves fail differently. A
missing-data question should be refused cleanly by anything that reads the
schema. An ambiguous one — *"which is the best performing stock?"* — has data
behind it but no single defined computation, and silently picking one reading is
the failure mode most likely to look like a success.

Ground truth for every question is computed independently in pandas from the raw
CSVs, importing nothing from `agent/` and never opening the database. See
`METHODOLOGY.md` §6.

## Reproduction

Works from a fresh clone. The database and the answer key are committed, so
steps 3–4 need no network.

```bash
git clone https://github.com/nilaymastaadmi/market-query-agent
cd market-query-agent
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 1. Verify the guardrails (no model, no network, ~10s)
python3 -m pytest tests/ -q
python3 tests/test_guardrails.py          # prints the attack table

# 2. Verify the grader against its own fixtures (~1s)
python3 -m eval.compare

# 3. Regenerate the answer key from the raw CSVs and self-check it (~5s)
python3 -m eval.ground_truth

# 4. Rebuild the database from the committed CSVs (~5s)
cd data && python3 build_db.py --source cache && cd ..

# --- everything below needs a model ---

# 5. Measure the transport's fixed prompt overhead
python3 -m eval.run_eval --calibrate

# 6. Run the benchmark and the two ablations
python3 -m eval.run_eval --config main                 --workers 2
python3 -m eval.run_eval --config abl_schema_in_prompt --workers 2
python3 -m eval.run_eval --config abl_no_retry         --workers 2

# 7. Hand-classify any failures, then print the taxonomy
python3 -m eval.classify_failures --config main --review   # evidence per failure
python3 -m eval.classify_failures --config main --check    # fails if unlabelled

# 8. Regenerate every results table in this README
python3 -m eval.report > results/RESULTS.md
```

Steps 1–4 are fully deterministic and need no API access. Step 6 needs either
the authenticated `claude` CLI on `PATH` (the default, `--provider cli`) or
`ANTHROPIC_API_KEY` set (`--provider api`).

To rebuild the database from live Yahoo Finance instead of the cache:
`cd data && python3 build_db.py --source yfinance`. See `METHODOLOGY.md` §3 for
why the committed build used the cache.

## Repository layout

```
data/
  universe_pool.py      disclosed 104-ticker candidate pool + sector groups
  select_universe.py    availability filter + seeded stratified sample
  universe.csv          the pinned 28-ticker universe (committed)
  build_db.py           schema, load, integrity assertions
  raw/*.csv             cached Yahoo Finance OHLCV (committed)
db/market.db            the database (committed, ~2.5 MB)
agent/
  guards.py             four guardrail layers
  tools.py              get_schema / run_sql / compute_metric
  prompts.py            system prompt + JSON action protocol
  providers.py          LLMProvider interface, CLI and API backends
  loop.py               step budget, retry, per-task logging
eval/
  benchmark.py          48 questions + 4 injection probes
  ground_truth.py       independent pandas answer key
  ground_truth.json     the committed answer key
  compare.py            grader (+ 19 self-tests)
  run_eval.py           the harness
  classify_failures.py  taxonomy tooling
  failure_labels.json   hand-assigned failure labels
  report.py             renders the tables in this README
results/
  runs/<config>/*.json  full per-task episodes
  summary_*.json        aggregate metrics + manifest
  harness_overhead.json measured transport overhead
tests/
  test_guardrails.py    25 adversarial attacks + 10 benign + injection
  test_tools.py         tools, and tool-vs-ground-truth metric agreement
METHODOLOGY.md          method, stated before results
HANDOFF.md              the two tasks that need doing outside the sandbox
```

## Model

<!-- MODEL_START -->
<!-- MODEL_END -->

The model sits behind one interface (`agent/providers.py::LLMProvider`) with two
backends. `ClaudeCLIProvider` shells out to the authenticated `claude` CLI and
produced every number in this README. `AnthropicAPIProvider` calls
`POST /v1/messages` directly; it is committed but **has never been executed**,
because the build sandbox had no `ANTHROPIC_API_KEY` (a direct probe returned
`401 authentication_error`). It is written from the documented request shape and
is unverified — `HANDOFF.md` Task 1 is a copy-pasteable prompt to verify it.

## Known limits

Read these before quoting any number above.

1. **One run per configuration, no repeats.** Differences of a few points
   between configurations are within plausible run-to-run noise on a
   48-question benchmark. Sampling variability is not quantified.
2. **One model.** Nothing here generalises to another model without rerunning.
3. **48 questions is small.** A single question is worth ~2 percentage points,
   and a tier is 8 questions, so per-tier accuracy moves in 12.5-point steps.
   Per-tier numbers are directional, not precise.
4. **The benchmark author also wrote the agent.** The questions were written
   against a schema I designed, which is a real bias toward questions the schema
   answers cleanly. The unanswerable tier is a partial counterweight, not a
   cure.
5. **The answer shape is given to the agent.** Each question states its
   `answer_type`. This makes the task easier than an unconstrained one, and is
   done so the taxonomy surfaces reasoning errors rather than formatting luck.
6. **Data is cached, not live.** The committed database was built from cached
   Yahoo Finance CSVs because the build sandbox blocks Yahoo Finance. The live
   path is implemented but was not exercised. See `METHODOLOGY.md` §3.
7. **Sector tags are coarse and inherited.** They come verbatim from the
   attached `trading-bot` pool. Two are known to be imprecise and were kept
   rather than silently improved, because editing an inherited pool mid-project
   is how a "disclosed" pool quietly stops being the disclosed pool: SRF is a
   chemicals business tagged `Diversified_Financials_Other`, and RATNAMANI
   (steel tubes) is tagged `Auto_AutoAncillary`.
8. **Cost figures carry transport overhead.** The Claude Code CLI prepends its
   own system prompt to every call. Agent-attributable cost and measured cost
   are reported separately and both appear above; neither is hidden.
9. **Latency under parallelism is inflated.** The headline runs use concurrent
   workers. A serial reference run is reported separately and is the figure to
   compare against another system.
10. **The guardrail result is a floor, not a proof.** 25 attacks blocked is
    evidence the four layers work against the attacks I thought of. It is not a
    security audit, and the injection probes test whether the *tool layer*
    refuses payloads — not whether the model resists persuasion, which is not a
    guarantee a guardrail can make.
