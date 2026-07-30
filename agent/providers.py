"""
Model providers behind one interface, so the model is swappable and every
reported number can be attributed to a specific model version.

    LLMProvider (ABC)
      |- ClaudeCLIProvider    subprocess `claude -p --output-format json`
      |- AnthropicAPIProvider POST /v1/messages  (needs ANTHROPIC_API_KEY)

Both take the same (system, transcript) pair and return the same LLMResponse,
so agent/loop.py is provider-agnostic and the ablations compare agents, not
transports.

WHY THE TRANSCRIPT IS A STRING. The loop renders the whole conversation into
one prompt each step rather than maintaining provider-side session state. That
is what a Messages-API loop does anyway (the API is stateless; you resend the
message list), and it means both providers see byte-identical input for the
same task. No hidden state, and a task can be replayed from its log.

COST ACCOUNTING — READ THIS BEFORE QUOTING A COST NUMBER.
The Claude Code CLI prepends its own system prompt and tool definitions to
every call. Measured here at ~20,200 tokens (see results/harness_overhead.json,
produced by `calibrate_overhead()`), that is overhead of the *transport*, not
of the agent being evaluated, and it would swamp any per-task cost figure.
So LLMResponse separates them:

    prompt_tokens / completion_tokens   attributable to the agent
    cache_write_tokens / cache_read_tokens  the CLI's own prompt

and reports two costs:

    cost_usd_measured   what the provider says the whole call cost
    cost_usd_agent      the agent's own tokens priced at published rates

The README quotes cost_usd_agent as cost-per-task and states the measured
figure and the overhead alongside it. Neither number is hidden, because
picking whichever one flatters the project is exactly the kind of thing this
project exists to not do.

The decomposition is verified, not assumed: a null call to Haiku 4.5 reported
535 prompt + 67 completion + 20,174 cache-write tokens and a cost of
$0.041218. Pricing those at the rates in PRICES below gives $0.041218. The
tables and the arithmetic agree with the provider to the cent.
"""
import json
import os
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict

# Published per-million-token rates, USD. Cache-read is 0.1x input; cache-write
# is 1.25x input at the 5-minute TTL and 2.0x at the 1-hour TTL. The Claude Code
# CLI uses the 1-hour TTL, so CACHE_WRITE_TTL is "1h" for the CLI provider.
#
# Sonnet 5 carries introductory pricing of $2.00 / $10.00 through 2026-08-31;
# `sonnet5_intro` is a separate entry rather than a conditional, so a rerun
# after that date does not silently reprice a historical result. Any run that
# used it says so in its manifest.
PRICES = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-sonnet-5-intro": {"input": 2.00, "output": 10.00},
    "claude-opus-5": {"input": 5.00, "output": 25.00},
}
CACHE_READ_MULTIPLIER = 0.10
CACHE_WRITE_MULTIPLIER = {"5m": 1.25, "1h": 2.00}


def price_tokens(model: str, prompt: int, completion: int) -> float:
    """USD for prompt+completion tokens at published rates. -1.0 if unpriced."""
    key = model if model in PRICES else model.rsplit("-", 1)[0]
    if key not in PRICES:
        return -1.0
    p = PRICES[key]
    return prompt / 1e6 * p["input"] + completion / 1e6 * p["output"]


def price_cache(model: str, write: int, read: int, ttl: str = "1h") -> float:
    """USD for cache-write and cache-read tokens. -1.0 if unpriced."""
    key = model if model in PRICES else model.rsplit("-", 1)[0]
    if key not in PRICES:
        return -1.0
    rate = PRICES[key]["input"]
    return (
        write / 1e6 * rate * CACHE_WRITE_MULTIPLIER[ttl]
        + read / 1e6 * rate * CACHE_READ_MULTIPLIER
    )


@dataclass
class LLMResponse:
    text: str
    model: str  # exact model id the provider reports
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_write_tokens: int = 0  # transport overhead, not the agent's
    cache_read_tokens: int = 0  # transport overhead, not the agent's
    cost_usd_measured: float = 0.0  # provider-reported, includes overhead
    cost_usd_agent: float = 0.0  # agent tokens at published rates
    latency_s: float = 0.0
    error: str | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self):
        d = asdict(self)
        d.pop("raw", None)  # keep per-task logs readable
        return d


class LLMProvider(ABC):
    """One model call. Implementations must never raise for a model-side
    failure -- return an LLMResponse with `error` set, so a provider hiccup is
    recorded as a task outcome instead of aborting a benchmark run."""

    name: str = "abstract"

    @abstractmethod
    def complete(self, system: str, transcript: str, max_tokens: int = 2048) -> LLMResponse:
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...


class ClaudeCLIProvider(LLMProvider):
    """
    Calls the authenticated `claude` CLI in headless print mode.

    This is the provider that produced every number in the README, because the
    sandbox this project was built in has no ANTHROPIC_API_KEY. It reports real
    token counts, real cost and real wall-clock latency; its one drawback is the
    fixed system-prompt overhead documented at the top of this module.

    The prompt goes over stdin, not argv, so a long transcript cannot hit
    ARG_MAX.

    TWO FLAG DETAILS THAT COST REAL DEBUGGING TIME, recorded so nobody has to
    rediscover them:

    - `--allowed-tools ""` does NOT disable the CLI's built-in tools. Given a
      prompt describing `run_sql`, the model reached for the CLI's own Bash tool
      and tried to `curl` a nonexistent local server. Every built-in is named
      explicitly in DISALLOWED_TOOLS instead. This matters beyond tidiness: the
      loop under evaluation is the one in agent/loop.py, and a nested CLI-side
      agent loop would make every measurement here meaningless.
    - `--max-turns 1` looks like the right way to force a single completion but
      makes the CLI exit non-zero with `subtype: error_max_turns` on a perfectly
      good response -- its internal turn accounting reaches 3 for one answer.
      The bound is set high enough to never trip on a healthy call; with no
      tools available there is nothing for extra turns to do anyway.
    """

    name = "claude-cli"

    DISALLOWED_TOOLS = (
        "Bash Read Write Edit MultiEdit Glob Grep WebFetch WebSearch Task Agent "
        "TodoWrite NotebookEdit BashOutput KillShell SlashCommand Skill"
    ).split()

    def __init__(self, model: str = "claude-haiku-4-5-20251001", timeout_s: float = 240.0,
                 transport_retries: int = 2):
        if shutil.which("claude") is None:
            raise RuntimeError(
                "the `claude` CLI is not on PATH. Use AnthropicAPIProvider with "
                "ANTHROPIC_API_KEY set, or install the CLI."
            )
        self._model = model
        self.timeout_s = timeout_s
        self.transport_retries = transport_retries
        self.transport_retry_count = 0

    @property
    def model_id(self) -> str:
        return self._model

    def complete(self, system: str, transcript: str, max_tokens: int = 2048) -> LLMResponse:
        """
        Retries a *transport* failure (non-zero exit, unparseable output) up to
        `transport_retries` times.

        This is deliberately separate from the agent's retry-on-SQL-error, which
        is a behaviour under test. A subprocess that exits 1 with an empty
        stderr is a flaky pipe, not the model reasoning badly, and charging it
        to the agent would understate accuracy for a reason that has nothing to
        do with the agent. Every retry is counted in `transport_retry_count` and
        reported in the run summary, so the flake rate is visible rather than
        laundered away.
        """
        last = None
        for attempt in range(self.transport_retries + 1):
            last = self._call_once(system, transcript)
            if not last.error:
                return last
            if attempt < self.transport_retries:
                self.transport_retry_count += 1
                time.sleep(1.5 * (attempt + 1))
        return last

    def _call_once(self, system: str, transcript: str) -> LLMResponse:
        cmd = [
            "claude",
            "-p",
            "--output-format", "json",
            "--model", self._model,
            "--system-prompt", system,
            "--disallowed-tools", *self.DISALLOWED_TOOLS,
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--max-turns", "8",
        ]
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                input=transcript,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
            )
        except subprocess.TimeoutExpired:
            return LLMResponse(
                text="", model=self._model, latency_s=time.monotonic() - t0,
                error=f"provider timeout after {self.timeout_s:g}s",
            )
        latency = time.monotonic() - t0

        if proc.returncode != 0:
            # The CLI often exits non-zero with an empty stderr and puts the real
            # reason in its JSON payload on stdout. Dig it out -- "exited 1" with
            # no detail is undiagnosable, and this path is exactly where a
            # misdiagnosis turns into a wrong accuracy number.
            detail = proc.stderr.strip()[:300]
            try:
                p = json.loads(proc.stdout)
                detail = (
                    f"subtype={p.get('subtype')} "
                    f"terminal_reason={p.get('terminal_reason')} "
                    f"api_error_status={p.get('api_error_status')} "
                    f"errors={p.get('errors')} {detail}"
                ).strip()
            except (json.JSONDecodeError, TypeError):
                if not detail:
                    detail = f"empty stderr; stdout={proc.stdout[:200]!r}"
            return LLMResponse(
                text="", model=self._model, latency_s=latency,
                error=f"claude CLI exited {proc.returncode}: {detail}",
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            return LLMResponse(
                text="", model=self._model, latency_s=latency,
                error=f"could not parse CLI JSON: {e}; stdout={proc.stdout[:300]}",
            )

        if payload.get("is_error"):
            return LLMResponse(
                text=str(payload.get("result", "")), model=self._model, latency_s=latency,
                error=f"CLI reported error: {payload.get('api_error_status')}",
                raw=payload,
            )

        # Prefer modelUsage: it separates the agent's tokens from the CLI's
        # cached system prompt, which top-level `usage` conflates.
        mu = payload.get("modelUsage") or {}
        model_key = next(iter(mu), self._model)
        u = mu.get(model_key, {})
        prompt = int(u.get("inputTokens", 0))
        completion = int(u.get("outputTokens", 0))
        cache_w = int(u.get("cacheCreationInputTokens", 0))
        cache_r = int(u.get("cacheReadInputTokens", 0))

        return LLMResponse(
            text=payload.get("result", "") or "",
            model=model_key,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_write_tokens=cache_w,
            cache_read_tokens=cache_r,
            cost_usd_measured=float(payload.get("total_cost_usd", 0.0)),
            cost_usd_agent=price_tokens(model_key, prompt, completion),
            latency_s=latency,
            raw=payload,
        )


class AnthropicAPIProvider(LLMProvider):
    """
    Calls POST /v1/messages directly. No transport system prompt, so
    cost_usd_measured and cost_usd_agent are the same number apart from
    cache tokens.

    *** NOT EXERCISED IN THIS PROJECT'S REPORTED RUNS. *** The sandbox had no
    ANTHROPIC_API_KEY, so this path is written from the documented request
    shape and has never been executed. It is committed because the swappable-
    model requirement is about the interface, and because a reader with a key
    should be able to reproduce the benchmark on the API rather than the CLI --
    but it is unverified, and the README says so rather than implying parity.
    Verify it with: python3 -m eval.run_eval --provider api --limit 3
    """

    name = "anthropic-api"

    def __init__(self, model: str = "claude-haiku-4-5-20251001", timeout_s: float = 240.0):
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export a key, or use "
                "--provider cli to run against the authenticated Claude CLI."
            )
        self.base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        self._model = model
        self.timeout_s = timeout_s

    @property
    def model_id(self) -> str:
        return self._model

    def complete(self, system: str, transcript: str, max_tokens: int = 2048) -> LLMResponse:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": transcript}],
        }).encode()
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=body,
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": self.api_key,
            },
        )
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return LLMResponse(
                text="", model=self._model, latency_s=time.monotonic() - t0,
                error=f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}",
            )
        except Exception as e:  # network, timeout, malformed JSON
            return LLMResponse(
                text="", model=self._model, latency_s=time.monotonic() - t0,
                error=f"{type(e).__name__}: {e}",
            )
        latency = time.monotonic() - t0

        text = "".join(
            b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text"
        )
        u = payload.get("usage", {})
        prompt = int(u.get("input_tokens", 0))
        completion = int(u.get("output_tokens", 0))
        cache_w = int(u.get("cache_creation_input_tokens", 0))
        cache_r = int(u.get("cache_read_input_tokens", 0))
        model = payload.get("model", self._model)
        agent_cost = price_tokens(model, prompt, completion)

        return LLMResponse(
            text=text,
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_write_tokens=cache_w,
            cache_read_tokens=cache_r,
            cost_usd_measured=agent_cost + price_cache(model, cache_w, cache_r, ttl="5m"),
            cost_usd_agent=agent_cost,
            latency_s=latency,
            raw=payload,
        )


def build_provider(kind: str, model: str) -> LLMProvider:
    if kind == "cli":
        return ClaudeCLIProvider(model=model)
    if kind == "api":
        return AnthropicAPIProvider(model=model)
    raise ValueError(f"unknown provider '{kind}'; expected 'cli' or 'api'")


def calibrate_overhead(provider: LLMProvider, repeats: int = 3) -> dict:
    """
    Measure the transport's fixed prompt overhead with a near-empty call, so
    the README can state it as a measured number rather than an estimate.
    """
    runs = []
    for _ in range(repeats):
        r = provider.complete("Reply with the single character: 1", "1")
        runs.append({
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cache_write_tokens": r.cache_write_tokens,
            "cache_read_tokens": r.cache_read_tokens,
            "cost_usd_measured": r.cost_usd_measured,
            "cost_usd_agent": r.cost_usd_agent,
            "latency_s": r.latency_s,
            "error": r.error,
        })
    ok = [r for r in runs if not r["error"]]
    overhead = [r["cache_write_tokens"] + r["cache_read_tokens"] for r in ok]
    return {
        "provider": provider.name,
        "model": provider.model_id,
        "repeats": repeats,
        "runs": runs,
        "mean_overhead_tokens": (sum(overhead) / len(overhead)) if overhead else None,
        "mean_cost_usd_measured": (
            sum(r["cost_usd_measured"] for r in ok) / len(ok) if ok else None
        ),
        "mean_latency_s": (sum(r["latency_s"] for r in ok) / len(ok) if ok else None),
    }
