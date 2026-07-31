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

Everything required to read and audit this project is committed, and every
number in `README.md` is rendered from a run that actually happened. What
follows is optional, with an honest note on what each item would and would not
buy.

### Status of the steps this file previously listed

| Step | Status |
|---|---|
| 1. Run `abl_no_retry` | **Not run, deliberately.** See below |
| 2. Serial latency baseline | **Dropped as unnecessary.** `main` already ran at `--workers 1`, so its latency is already uncontended. The premise that the figures were inflated by 2-worker concurrency was false for `main` |
| 3. Hand-label `abl_no_retry` failures | Moot while step 1 is unrun |
| 4. Re-render report, document ablation validity | **Done.** `eval/report.py` now states the finding, and `results/RESULTS.md` is generated |
| 5. Commit and push | **Done** |

---

## Why `abl_no_retry` was not run

Verified from `results/summary_main.json`, not assumed:

```
manifest.config.retry_on_sql_error        = True
metrics.total_sql_errors                  = 0
metrics.tasks_with_at_least_one_sql_error = 0
metrics.retry_rate                        = 0.0
```

`main` ran with retry **enabled** and the agent produced zero invalid SQL across
48 tasks, so the retry path never executed once. Removing an inactive code path
cannot change behaviour. Any accuracy delta from that ablation would be model
nondeterminism reported as an ablation effect, which is exactly the kind of
claim this project refuses to make.

The row is kept in the ablation table marked `not run`, with the reason,
because *the baseline never wrote invalid SQL across 48 tasks* is itself a
result worth stating.

**This does mean the project ships one measured ablation (schema-in-prompt)
rather than two.** That is a real gap against the original brief and it is not
hidden.

---

## Optional work, in descending value

### 1. Make the retry ablation measurable

The useful version is not "run it anyway". It is to construct conditions where
retry can actually fire, then measure whether it helps:

- Add questions whose natural phrasing invites a schema mistake: a column that
  sounds plausible but does not exist, or an ambiguous join key.
- Or run against a deliberately mutated schema, so the first attempt fails and
  retry has something to recover from.

Report the retry rate first. If it is still zero, the ablation is still
unmeasurable, and that should be said again rather than papered over.

### 2. Repeat runs for a variance estimate

Every reported number is a single run. The measured `-2.1 pp` gap between
`main` and `abl_schema_in_prompt` is currently indistinguishable from noise,
because there is no spread to compare it against. Three to five repeats per
configuration would either confirm or dissolve the one comparative claim the
project currently makes.

```bash
python3 -m eval.run_eval --config main --workers 1 --tag run2
python3 -m eval.run_eval --config main --workers 1 --tag run3
```

### 3. Validate the Anthropic API provider

`AnthropicAPIProvider` in `agent/providers.py` has never been exercised; every
committed number came from `ClaudeCLIProvider`. Set `ANTHROPIC_API_KEY` and run
a 3-question smoke test with `--provider api` before anything larger.

**Caveat if you use it for a full configuration:** the CLI prepends its own
system prompt and tool definitions, so token counts, cost and latency are not
comparable across providers. A configuration run on the API cannot sit in the
same table as the CLI runs without saying so explicitly.

### 4. Rebuild the database from live Yahoo Finance

The pipeline is implemented but was never executed end to end; the committed DB
came from cached data. Expect small numeric differences in historical prices.
Row counts far from 866 per ticker, or missing tickers, indicate a real problem.

### 5. Widen the unanswerable tier

Eight questions means each one is worth 12.5% of that tier's accuracy, and
refusal accuracy (62.5%) is the weakest headline number. Doubling to 16 would
make it meaningfully readable.

---

## Rules that still hold

**Immutable without invalidating committed runs:** benchmark questions, ground
truth, grader logic, agent prompt. If a scoring bug is found, use
`--rescore --note "why"` rather than re-running the model.

**Rate limiting:** more than 2 workers produces HTTP 429s, which measure harness
overhead rather than agent behaviour. `total_rate_limit_429s` must be 0 in any
summary you report from.

**Pre-flight, all three must pass:**

```bash
python3 -m pytest tests/ -q          # 58 passed
python3 -m eval.ground_truth         # All self-checks passed
python3 -m eval.audit_ties           # exit 0
```

**Windows note:** rendering writes `Δ` and other non-ASCII, which the default
cp1252 console codepage cannot encode. Set `PYTHONUTF8=1` before running
`eval.report` or redirecting its output, or the file is silently truncated
mid-write.

**Never open a pull request.** Commit and push to `main`.
