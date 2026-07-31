"""
System prompts for the agent, and the JSON action protocol it must speak.

WHY A JSON ACTION PROTOCOL RATHER THAN NATIVE TOOL-USE BLOCKS. The two
providers in agent/providers.py do not expose tool-calling identically, and the
evaluation needs the agent's semantics to be *unchanged* when the model or
transport changes -- otherwise an ablation measures the transport instead of
the thing it claims to measure. A single text protocol, parsed by one function,
gives that. The cost is real and is reported: some failures are the model
mis-emitting JSON rather than mis-reasoning about SQL, and those are counted
separately in the failure taxonomy as `protocol_violation` instead of being
quietly retried into a success.

THE ANSWER SHAPE IS TOLD TO THE AGENT ON PURPOSE. Each question states its
expected answer type. A real system would specify its output contract too, and
without it the benchmark would mostly measure formatting luck -- "1234.5" vs
"₹1,234.50" vs "about 1235" -- which would flood the failure taxonomy with
noise and hide the reasoning errors the taxonomy exists to surface. This is a
deliberate simplification of the task and is disclosed in the README.
"""

PROTOCOL = """\
You answer questions about an Indian equity market database by writing SQL.

HOW THE TOOLS WORK. You do not execute anything yourself and you have no local
tool runtime. You emit a JSON object naming a tool; the harness reading your
output runs it against a live SQLite database and sends you the result in the
next message. The three tools listed at the bottom are always available through
this mechanism. If your environment reports that other tools are unavailable or
denied, that is unrelated -- it says nothing about these three. "The tools are
not available" is never a correct conclusion and never a valid reason to call a
question unanswerable: emit the tool call and the result will come back.

Every message you send MUST be exactly one JSON object and nothing else. No
prose before it, no explanation after it, no markdown fence. One of two shapes:

1. Call a tool:
   {"thought": "<one short sentence>", "tool": "<name>", "args": {...}}

2. Give your final answer:
   {"thought": "<one short sentence>", "final": {"value": <answer>,
    "sql": "<the exact SQL you ran to get this>"}}

   For a question the database cannot answer, use:
   {"thought": "...", "final": {"unanswerable": true, "reason": "<why>"}}

RULES
- One JSON object per message. One tool call per message.
- Your final answer MUST include the `sql` you actually ran. An answer with no
  SQL behind it is graded as a failure even when the number is correct. If you
  used compute_metric rather than SQL, put the SQL that selected the underlying
  rows in `sql` and name the metric in `thought`.
- `value` must match the answer_type stated in the question:
    number       -> a bare JSON number, e.g. 0.1423 (no %, no currency, no commas)
    integer      -> a bare JSON integer
    string       -> a bare JSON string, e.g. "TCS"
    list[string] -> a JSON array of strings IN THE ORDER ASKED FOR
    list[number] -> a JSON array of numbers in the order asked for
- Return fractions, not percents: a 14.23% return is 0.1423.
- Never invent a column or table. Call get_schema if you are unsure.
- If a query errors, read the database's message, fix the query, and try again.
- If the question cannot be answered from this schema -- it needs data that is
  not here, or it is too ambiguous to have one right answer -- say so with
  `unanswerable`. Do not guess a plausible-looking number. Do not treat a
  question as unanswerable merely because it is hard or needs several joins.

TOOLS
{tools}
"""

SCHEMA_MODE_TOOL = """\
The schema is NOT included below. Call get_schema first to see the tables,
columns and join keys before writing any SQL.
"""

SCHEMA_MODE_PROMPT = """\
The schema is included below, so you do not need to call get_schema.

{schema}
"""


def render_tool_specs(specs) -> str:
    lines = []
    for s in specs:
        lines.append(f"- {s['name']}({', '.join(s['args'].keys())})")
        lines.append(f"    {s['description']}")
        for arg, desc in s["args"].items():
            lines.append(f"    - {arg}: {desc}")
    return "\n".join(lines)


def build_system_prompt(tool_specs, schema_mode: str, schema_text: str = "") -> str:
    """
    schema_mode:
      "tool"   -- agent must call get_schema (the ablation baseline)
      "prompt" -- schema is pasted into the system prompt
    """
    base = PROTOCOL.replace("{tools}", render_tool_specs(tool_specs))
    if schema_mode == "prompt":
        if not schema_text:
            raise ValueError("schema_mode='prompt' requires schema_text")
        return base + "\n" + SCHEMA_MODE_PROMPT.replace("{schema}", schema_text)
    if schema_mode == "tool":
        return base + "\n" + SCHEMA_MODE_TOOL
    raise ValueError(f"unknown schema_mode '{schema_mode}'")


def build_task_prompt(question: str, answer_type: str) -> str:
    return (
        f"QUESTION: {question}\n"
        f"answer_type: {answer_type}\n\n"
        f"Reply with one JSON object as specified."
    )
