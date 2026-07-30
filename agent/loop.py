"""
The tool-calling loop: explicit step budget, retry-on-SQL-error, and a complete
per-task log of every tool call, token count and wall-clock measurement.

The log is the product here. Accuracy alone tells you nothing about *why* an
agent is wrong, so every run writes a record that is sufficient to reconstruct
the episode and hand-classify the failure: the raw model text at each step, the
parsed action, the SQL, the database's error message where there was one, and
the token/latency/cost breakdown. results/runs/<config>/<task_id>.json is what
the failure taxonomy in the README was built by reading.

TERMINATION. The episode ends when the agent emits a final answer, exhausts the
step budget, violates the protocol too many times, or (with retry disabled)
issues its first failing query. Each of those is a distinct recorded outcome,
because "ran out of steps" and "confidently answered wrong" are different
failures and collapsing them would destroy the taxonomy.
"""
import json
import re
import time
from dataclasses import dataclass, field

from .prompts import build_system_prompt, build_task_prompt
from .providers import LLMProvider, price_cache
from .tools import TOOL_SPECS, Tools

MAX_STEPS = 8
MAX_PROTOCOL_VIOLATIONS = 3
# Tool results are truncated before entering the transcript. Without this a
# single wide SELECT can crowd out the question itself, which would make the
# step budget meaningless.
MAX_RESULT_CHARS = 4000


@dataclass
class AgentConfig:
    schema_mode: str = "tool"  # "tool" | "prompt"
    retry_on_sql_error: bool = True
    max_steps: int = MAX_STEPS
    label: str = "default"

    def to_dict(self):
        return {
            "label": self.label,
            "schema_mode": self.schema_mode,
            "retry_on_sql_error": self.retry_on_sql_error,
            "max_steps": self.max_steps,
        }


@dataclass
class TaskResult:
    task_id: str
    tier: str
    question: str
    answer_type: str
    config: dict
    model: str
    provider: str

    # what the agent concluded
    value: object = None
    cited_sql: str | None = None
    claimed_unanswerable: bool = False
    refusal_reason: str | None = None

    # how it went
    outcome: str = "unknown"
    # answered | answered_no_sql | refused | step_budget_exhausted
    # | protocol_failure | terminated_sql_error | provider_error
    steps_used: int = 0
    tool_calls: int = 0
    sql_calls: int = 0
    sql_errors: int = 0
    blocked_calls: int = 0
    retries: int = 0  # SQL calls issued after seeing an error
    protocol_violations: int = 0
    transport_retries: int = 0  # provider-level flakes, not agent behaviour

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd_measured: float = 0.0
    cost_usd_agent: float = 0.0
    cost_usd_cache: float = 0.0  # transport overhead, priced separately
    latency_s: float = 0.0
    llm_latency_s: float = 0.0

    error: str | None = None
    transcript: list = field(default_factory=list)
    tool_call_log: list = field(default_factory=list)

    def to_dict(self):
        d = dict(self.__dict__)
        return d


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_action(text: str) -> tuple[dict | None, str | None]:
    """
    Pull one JSON action object out of the model's message.

    Tolerates a markdown fence and surrounding prose, because those are
    presentation slips rather than reasoning errors and counting them as
    reasoning failures would misattribute them in the taxonomy. Does NOT
    tolerate a missing or malformed action -- that is a real protocol
    violation and is recorded as one.
    """
    if not text or not text.strip():
        return None, "empty response"

    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    candidates.append(text.strip())
    # last resort: the outermost {...} span
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        candidates.append(text[first : last + 1])

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if "final" in obj or "tool" in obj:
            return obj, None
    return None, "no JSON object with a 'tool' or 'final' key"


def _render_result(result: dict) -> str:
    s = json.dumps(result, default=str)
    if len(s) > MAX_RESULT_CHARS:
        s = s[:MAX_RESULT_CHARS] + f'... [truncated at {MAX_RESULT_CHARS} chars]'
    return s


def run_task(
    task: dict,
    tools: Tools,
    provider: LLMProvider,
    config: AgentConfig,
) -> TaskResult:
    """
    Run one benchmark question. `task` needs id, tier, question, answer_type.

    Never raises for a model or database failure: everything becomes a recorded
    outcome, so one bad task cannot abort a 40-question run.
    """
    res = TaskResult(
        task_id=task["id"],
        tier=task["tier"],
        question=task["question"],
        answer_type=task["answer_type"],
        config=config.to_dict(),
        model=provider.model_id,
        provider=provider.name,
    )

    schema_text = tools.get_schema()["schema"] if config.schema_mode == "prompt" else ""
    system = build_system_prompt(TOOL_SPECS, config.schema_mode, schema_text)
    if config.schema_mode == "prompt":
        # get_schema() above is a harness action, not an agent decision -- do not
        # let it inflate the agent's tool-call count.
        tools.reset_log()

    lines = [build_task_prompt(task["question"], task["answer_type"])]
    saw_sql_error = False
    t_start = time.monotonic()

    for step in range(1, config.max_steps + 1):
        res.steps_used = step
        transcript = "\n\n".join(lines)
        llm = provider.complete(system, transcript)
        res.prompt_tokens += llm.prompt_tokens
        res.completion_tokens += llm.completion_tokens
        res.cache_write_tokens += llm.cache_write_tokens
        res.cache_read_tokens += llm.cache_read_tokens
        res.cost_usd_measured += llm.cost_usd_measured
        res.cost_usd_agent += max(0.0, llm.cost_usd_agent)
        res.llm_latency_s += llm.latency_s

        entry = {"step": step, "llm": llm.to_dict()}

        if llm.error:
            entry["halt"] = "provider_error"
            res.transcript.append(entry)
            res.outcome = "provider_error"
            res.error = llm.error
            break

        action, parse_err = parse_action(llm.text)
        entry["parsed"] = action
        if parse_err:
            res.protocol_violations += 1
            entry["protocol_violation"] = parse_err
            res.transcript.append(entry)
            if res.protocol_violations >= MAX_PROTOCOL_VIOLATIONS:
                res.outcome = "protocol_failure"
                res.error = f"{res.protocol_violations} protocol violations: {parse_err}"
                break
            lines.append(llm.text.strip())
            lines.append(
                f"PROTOCOL ERROR: {parse_err}. Reply with exactly one JSON object "
                f'of the form {{"thought": "...", "tool": "...", "args": {{...}}}} '
                f'or {{"thought": "...", "final": {{"value": ..., "sql": "..."}}}}.'
            )
            continue

        # ---- final answer ------------------------------------------------
        if "final" in action:
            final = action["final"] if isinstance(action["final"], dict) else {}
            res.transcript.append(entry)
            if final.get("unanswerable") is True:
                res.claimed_unanswerable = True
                res.refusal_reason = str(final.get("reason", ""))[:1000]
                res.outcome = "refused"
            else:
                res.value = final.get("value")
                sql = final.get("sql")
                res.cited_sql = sql.strip() if isinstance(sql, str) and sql.strip() else None
                # The citation requirement, enforced: a right number with no
                # query behind it is a failure, recorded as its own outcome
                # rather than folded into the accuracy number.
                res.outcome = "answered" if res.cited_sql else "answered_no_sql"
            break

        # ---- tool call ---------------------------------------------------
        tool_name = action.get("tool")
        args = action.get("args") if isinstance(action.get("args"), dict) else {}
        res.tool_calls += 1

        if tool_name == "get_schema":
            out = tools.get_schema()
        elif tool_name == "run_sql":
            res.sql_calls += 1
            if saw_sql_error:
                res.retries += 1
            out = tools.run_sql(args.get("query", ""))
            if out.get("blocked"):
                res.blocked_calls += 1
            if out.get("sql_error") or out.get("blocked"):
                res.sql_errors += 1
                if not config.retry_on_sql_error:
                    entry["tool_result"] = out
                    entry["halt"] = "terminated_sql_error"
                    res.transcript.append(entry)
                    res.outcome = "terminated_sql_error"
                    res.error = out.get("error")
                    break
                saw_sql_error = True
            else:
                saw_sql_error = False
        elif tool_name == "compute_metric":
            out = tools.compute_metric(
                metric=str(args.get("metric", "")),
                ticker=str(args.get("ticker", "")),
                start=args.get("start"),
                end=args.get("end"),
            )
        else:
            out = {
                "error": f"unknown tool '{tool_name}'; available: "
                f"get_schema, run_sql, compute_metric"
            }

        entry["tool_result"] = out
        res.transcript.append(entry)
        lines.append(json.dumps({k: v for k, v in action.items() if k != "thought"}))
        lines.append(f"TOOL RESULT ({tool_name}): {_render_result(out)}")
    else:
        res.outcome = "step_budget_exhausted"
        res.error = f"no final answer within {config.max_steps} steps"

    res.latency_s = time.monotonic() - t_start
    res.transport_retries = getattr(provider, "transport_retry_count", 0)
    # Transport overhead priced separately so a cost table can show both.
    res.cost_usd_cache = price_cache(
        res.model, res.cache_write_tokens, res.cache_read_tokens, ttl="1h"
    )
    res.tool_call_log = list(tools.calls)
    tools.reset_log()
    return res
