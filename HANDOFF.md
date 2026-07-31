# Handoff — things that need doing outside the build session

Two things could not be done in the sandbox this project was built in. Neither
is guessed at or silently skipped; each is written below as a self-contained,
copy-pasteable prompt you can hand to someone (or to Claude Desktop / Claude
Code on a normal machine) with **no context from the build session**.

Nothing in the committed results depends on either task. They exist to verify a
code path that is committed but unexercised, and to rebuild the database from
its live upstream source.

---

## Task 1 — verify the Anthropic API provider (the unexercised code path)

**Why this is needed.** `agent/providers.py` ships two model backends behind one
interface. `ClaudeCLIProvider` produced every number in the README. The
`AnthropicAPIProvider` is committed but **has never been executed** — the build
sandbox had no `ANTHROPIC_API_KEY`, and a direct probe of
`https://api.anthropic.com/v1/messages` returned
`401 authentication_error: x-api-key header is required`. It is written from the
documented request shape and is unverified. The README says so. This task closes
that gap.

**Cost.** A 3-question smoke test is a few cents. A full 48-question run on
Claude Haiku 4.5 is well under a dollar.

### Copy-paste from here

> I have a Python repository cloned at `<PATH>` from
> `https://github.com/nilaymastaadmi/market-query-agent`. It is a benchmark
> harness for an LLM agent that answers questions about a stock-price database
> by writing SQL. It contains two model backends behind one interface, in
> `agent/providers.py`: `ClaudeCLIProvider` (already verified) and
> `AnthropicAPIProvider` (written but never executed, because the machine it was
> built on had no API key).
>
> Please verify and, if necessary, fix `AnthropicAPIProvider` so it works. Steps:
>
> 1. `cd <PATH> && python3 -m pip install -r requirements.txt`
> 2. Export a working Anthropic API key: `export ANTHROPIC_API_KEY=sk-ant-...`
> 3. Confirm the database and answer key are present and self-consistent:
>    `python3 -m eval.ground_truth`
>    It should print about 41 answers and end with
>    "All self-checks passed". If it does not, stop and report what it printed.
> 4. Smoke-test the API provider on three questions:
>    `python3 -m eval.run_eval --config main --provider api --limit 3 --skip-injection --workers 1 --tag apitest`
> 5. If any task reports `outcome: provider_error`, open
>    `results/runs/main_apitest/<TASK_ID>.json`, read the `error` field, and fix
>    `AnthropicAPIProvider` in `agent/providers.py`. Likely culprits are the
>    request body shape, a missing header, or the `usage` field names used for
>    token accounting. Do not change `ClaudeCLIProvider`, the benchmark
>    questions, the ground truth, or the grader — only the API provider.
> 6. Once three tasks pass, run the full benchmark on the API:
>    `python3 -m eval.run_eval --config main --provider api --workers 2 --tag api`
> 7. Report back: the accuracy line printed at the end, and a diff of any changes
>    you made to `agent/providers.py`.
>
> Context you need: the agent must reply with exactly one JSON object per turn,
> either `{"thought": ..., "tool": ..., "args": {...}}` or
> `{"thought": ..., "final": {"value": ..., "sql": "..."}}`. The provider's only
> job is to send a system prompt plus a single user message and return the
> model's text along with token counts. It must never raise on a model-side
> failure — it returns an `LLMResponse` with `error` set. Keep that contract.

### Copy-paste ends

---

## Task 2 — rebuild the database from live Yahoo Finance

**Why this is needed.** The committed `db/market.db` was built with
`--source cache` from CSVs that are Yahoo Finance output with
`auto_adjust=True`. The live download path is implemented but was never run,
because the build sandbox's egress proxy blocks Yahoo Finance
(`403 CONNECT` on `query1.finance.yahoo.com:443`, and the same for `query2`,
`stooq.com` and `nseindia.com`). Running it on a normal network confirms the
live path works and that the cached data matches what Yahoo returns today.

**Note on what to expect.** Yahoo revises history occasionally, so small
differences in older bars are normal and are not by themselves a bug. A ticker
returning *no* data, or a row count far from 866, is a real problem.

### Copy-paste from here

> I have a Python repository cloned at `<PATH>` from
> `https://github.com/nilaymastaadmi/market-query-agent`. It builds a SQLite
> database of daily stock prices for 28 Indian (NSE) stocks by downloading from
> Yahoo Finance with the `yfinance` package. The committed database was built
> from cached CSVs because the build machine had no access to Yahoo Finance.
> Please rebuild it from the live source and tell me whether the results match.
>
> 1. `cd <PATH> && python3 -m pip install -r requirements.txt`
> 2. Record the current state so it can be compared afterwards:
>    `python3 -m eval.ground_truth > /tmp/ground_truth_cached.txt`
>    then `cp eval/ground_truth.json /tmp/ground_truth_cached.json`
> 3. Rebuild from the live source (this downloads ~29 tickers and takes a few
>    minutes): `cd data && python3 build_db.py --source yfinance && cd ..`
> 4. Regenerate the answer key from the freshly downloaded CSVs:
>    `python3 -m eval.ground_truth`
> 5. Compare: `diff /tmp/ground_truth_cached.json eval/ground_truth.json`
>
> Report back:
> - whether step 3 completed without a `MISSING:` line or a `SystemExit`;
> - the row counts and date range it printed;
> - whether step 4 ended with "All self-checks passed";
> - the diff from step 5, or "identical" if there is none.
>
> Small numeric differences in the older price history are expected — Yahoo
> revises adjusted prices when corporate actions are restated. What would be a
> real problem is a ticker returning no data at all, a row count far from 866
> per ticker, or a self-check failure. If `git status` shows `data/raw/*.csv`
> changed, that is expected: the live download overwrites the cache. Do not
> commit anything unless I ask.

### Copy-paste ends

---

## Neither task blocks anything

The committed results stand on their own: they were produced by a real model
over a real database with a real answer key. Task 1 would let the same benchmark
run on a second transport; Task 2 would confirm the data pipeline against its
live upstream. Both are verification, not repair.
