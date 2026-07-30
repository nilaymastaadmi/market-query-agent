"""
Grading. Tolerance-based for floats, exact for counts, strings and rankings.

THREE THINGS THIS GRADER REFUSES TO DO, because each would inflate accuracy:

1. It does not accept an answer with no SQL behind it. `answered_no_sql` is
   graded WRONG even when the number matches, and is reported as its own
   category. A right number with no query behind it is not a working agent.

2. It does not accept a percentage where a fraction was asked for. The question
   says "return a fraction, not a percentage", so 9.4985 when the answer is
   0.094985 is a failed instruction, not a formatting quirk. It is graded wrong
   and labelled `unit_error` so the taxonomy can count it separately from
   arithmetic mistakes.

3. It does not credit a refusal on an answerable question, or an answer on an
   unanswerable one. Those are `wrong_refusal` and `confidently_wrong`, and the
   second is the single most important number in the report.

WHAT IT DOES FORGIVE, and why that is not cheating: a numeric answer arriving as
the string "1234.5" or "1,234.5" instead of the JSON number 1234.5, and casing
or surrounding whitespace on tickers. Those are transport-shaped slips that say
nothing about whether the agent understood the database. Every forgiveness is
counted in `coerced` and reported, so a reader can subtract them if they
disagree with the call.
"""
import json
import math
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
GT_PATH = os.path.join(HERE, "ground_truth.json")

UNANSWERABLE = "UNANSWERABLE"

VERDICTS = (
    "correct",           # matched, with SQL cited
    "correct_refusal",   # correctly refused an unanswerable question
    "wrong_value",       # produced a number/string that does not match
    "unit_error",        # right magnitude, wrong unit (percent vs fraction)
    "no_sql",            # value matched or not, but no SQL was cited
    "wrong_refusal",     # refused a question that is answerable
    "confidently_wrong", # answered an unanswerable question with a value
    "no_answer",         # step budget, protocol failure, provider error, etc.
    "malformed",         # answer present but not coercible to the asked type
)


def load_ground_truth(path: str = GT_PATH) -> dict:
    if not os.path.exists(path):
        raise SystemExit(
            f"{path} not found. Generate it with: python3 -m eval.ground_truth"
        )
    with open(path) as f:
        return json.load(f)


_NUM = re.compile(r"^-?[\d,]*\.?\d+(?:[eE][-+]?\d+)?$")


def _to_number(v):
    """Coerce to float. Returns (value, was_coerced) or (None, False)."""
    if isinstance(v, bool):
        return None, False
    if isinstance(v, (int, float)):
        return float(v), False
    if isinstance(v, str):
        s = v.strip().replace("₹", "").replace("$", "").strip()
        pct = s.endswith("%")
        if pct:
            s = s[:-1].strip()
        s2 = s.replace(",", "")
        if _NUM.match(s):
            f = float(s2)
            return (f / 100.0 if pct else f), True
        return None, False
    return None, False


def _norm_str(v) -> str | None:
    if isinstance(v, str):
        return v.strip().upper()
    return None


def _close(a: float, b: float, tol: float) -> bool:
    if math.isnan(a) or math.isnan(b):
        return False
    if b == 0:
        return abs(a) <= max(tol, 1e-9)
    return abs(a - b) / abs(b) <= tol


def grade(task: dict, result: dict, ground_truth: dict) -> dict:
    """
    Grade one TaskResult dict against ground truth.

    Returns {verdict, correct: bool, expected, got, detail, coerced: bool}.
    """
    expected = ground_truth.get(task["gt_key"])
    out = {
        "task_id": task["id"],
        "tier": task["tier"],
        "expected": expected,
        "got": result.get("value"),
        "coerced": False,
        "detail": "",
    }

    outcome = result.get("outcome")
    refused = bool(result.get("claimed_unanswerable"))
    is_unanswerable = expected == UNANSWERABLE

    # --- episodes that never produced an answer --------------------------
    if outcome in ("step_budget_exhausted", "protocol_failure", "provider_error",
                   "terminated_sql_error"):
        out["verdict"] = "no_answer"
        out["detail"] = f"{outcome}: {str(result.get('error'))[:200]}"
        out["correct"] = False
        return out

    # --- refusals ---------------------------------------------------------
    if refused:
        if is_unanswerable:
            out["verdict"] = "correct_refusal"
            out["correct"] = True
            out["detail"] = str(result.get("refusal_reason", ""))[:300]
        else:
            out["verdict"] = "wrong_refusal"
            out["correct"] = False
            out["detail"] = (
                f"refused an answerable question: "
                f"{str(result.get('refusal_reason', ''))[:200]}"
            )
        return out

    if is_unanswerable:
        # Answered a question the schema cannot answer. The worst failure mode
        # in the whole benchmark, because it is indistinguishable from a real
        # answer to anyone who does not already know the schema.
        out["verdict"] = "confidently_wrong"
        out["correct"] = False
        out["detail"] = (
            f"produced value {result.get('value')!r} for an unanswerable question "
            f"({task.get('why', 'see benchmark.py')})"
        )
        return out

    # --- the citation requirement ----------------------------------------
    if outcome == "answered_no_sql" or not result.get("cited_sql"):
        out["verdict"] = "no_sql"
        out["correct"] = False
        out["detail"] = "no SQL cited; graded as a failure regardless of the value"
        return out

    got = result.get("value")
    atype = task["answer_type"]
    tol = task.get("tolerance", 0)

    # --- numeric ----------------------------------------------------------
    if atype in ("number", "integer"):
        gv, coerced = _to_number(got)
        out["coerced"] = coerced
        if gv is None:
            out["verdict"] = "malformed"
            out["correct"] = False
            out["detail"] = f"expected a {atype}, got {type(got).__name__}: {got!r}"
            return out
        ev = float(expected)
        if atype == "integer":
            ok = abs(gv - ev) < 0.5
        else:
            ok = _close(gv, ev, max(tol, 1e-9))
        if ok:
            out["verdict"] = "correct"
            out["correct"] = True
            return out
        # right number, wrong unit: fraction reported as a percentage
        if ev != 0 and _close(gv, ev * 100.0, max(tol, 1e-6)):
            out["verdict"] = "unit_error"
            out["correct"] = False
            out["detail"] = (
                f"answer is the correct value expressed as a percentage "
                f"({gv:g} vs {ev:g}); the question asked for a fraction"
            )
            return out
        out["verdict"] = "wrong_value"
        out["correct"] = False
        rel = abs(gv - ev) / abs(ev) if ev else float("inf")
        out["detail"] = f"got {gv:g}, expected {ev:g} (relative error {rel:.3g})"
        return out

    # --- single string ----------------------------------------------------
    if atype == "string":
        gs, es = _norm_str(got), _norm_str(expected)
        if gs is None:
            out["verdict"] = "malformed"
            out["correct"] = False
            out["detail"] = f"expected a string, got {type(got).__name__}: {got!r}"
            return out
        out["coerced"] = gs != got
        if gs == es:
            out["verdict"] = "correct"
            out["correct"] = True
        else:
            out["verdict"] = "wrong_value"
            out["correct"] = False
            out["detail"] = f"got {got!r}, expected {expected!r}"
        return out

    # --- ordered lists ----------------------------------------------------
    if atype in ("list[string]", "list[number]"):
        if not isinstance(got, list):
            out["verdict"] = "malformed"
            out["correct"] = False
            out["detail"] = f"expected a JSON array, got {type(got).__name__}: {got!r}"
            return out
        if len(got) != len(expected):
            out["verdict"] = "wrong_value"
            out["correct"] = False
            out["detail"] = f"expected {len(expected)} items, got {len(got)}: {got!r}"
            return out
        if atype == "list[string]":
            gl = [_norm_str(x) for x in got]
            el = [_norm_str(x) for x in expected]
            if any(x is None for x in gl):
                out["verdict"] = "malformed"
                out["correct"] = False
                out["detail"] = f"non-string element in {got!r}"
                return out
            out["coerced"] = gl != got
            if gl == el:
                out["verdict"] = "correct"
                out["correct"] = True
            else:
                out["verdict"] = "wrong_value"
                out["correct"] = False
                # Ordering errors are worth naming: same set, wrong order is a
                # different mistake from picking the wrong tickers.
                if sorted(gl) == sorted(el):
                    out["detail"] = f"right set, WRONG ORDER: got {got!r}"
                else:
                    out["detail"] = f"got {got!r}, expected {expected!r}"
            return out
        # list[number]
        pairs = [_to_number(x) for x in got]
        if any(p[0] is None for p in pairs):
            out["verdict"] = "malformed"
            out["correct"] = False
            out["detail"] = f"non-numeric element in {got!r}"
            return out
        out["coerced"] = any(p[1] for p in pairs)
        gl = [p[0] for p in pairs]
        if all(_close(a, float(b), max(tol, 1e-9)) for a, b in zip(gl, expected)):
            out["verdict"] = "correct"
            out["correct"] = True
        else:
            out["verdict"] = "wrong_value"
            out["correct"] = False
            out["detail"] = f"got {gl!r}, expected {expected!r}"
        return out

    out["verdict"] = "malformed"
    out["correct"] = False
    out["detail"] = f"unknown answer_type {atype!r}"
    return out


if __name__ == "__main__":
    # Grader self-test. A grader nobody tested is a number nobody should trust.
    gt = {
        "num": 0.094985, "cnt": 28, "str": "TCS",
        "lst": ["A", "B", "C"], "unanswerable": UNANSWERABLE,
    }
    def T(gt_key, atype, tol=1e-4, tier="x", why=""):
        return {"id": "X", "tier": tier, "gt_key": gt_key,
                "answer_type": atype, "tolerance": tol, "why": why}
    def R(**kw):
        base = {"outcome": "answered", "cited_sql": "SELECT 1",
                "claimed_unanswerable": False, "value": None}
        base.update(kw)
        return base

    cases = [
        ("exact float",        T("num", "number"),  R(value=0.094985),   "correct"),
        ("float in tolerance", T("num", "number"),  R(value=0.0949851),  "correct"),
        ("float out of tol",   T("num", "number"),  R(value=0.11),       "wrong_value"),
        ("percent form",       T("num", "number"),  R(value=9.4985),     "unit_error"),
        ("percent string",     T("num", "number"),  R(value="9.4985%"),  "correct"),
        ("numeric string",     T("cnt", "integer"), R(value="28"),       "correct"),
        ("comma string",       T("cnt", "integer"), R(value="1,28"),     "wrong_value"),
        ("wrong count",        T("cnt", "integer"), R(value=27),         "wrong_value"),
        ("string match",       T("str", "string"),  R(value="tcs "),     "correct"),
        ("string mismatch",    T("str", "string"),  R(value="INFY"),     "wrong_value"),
        ("list exact",         T("lst", "list[string]"), R(value=["A","B","C"]), "correct"),
        ("list misordered",    T("lst", "list[string]"), R(value=["B","A","C"]), "wrong_value"),
        ("list short",         T("lst", "list[string]"), R(value=["A","B"]),     "wrong_value"),
        ("no sql cited",       T("num", "number"),  R(value=0.094985, cited_sql=None,
                                                     outcome="answered_no_sql"), "no_sql"),
        ("budget exhausted",   T("num", "number"),  R(outcome="step_budget_exhausted",
                                                     error="out of steps"), "no_answer"),
        ("bad type",           T("num", "number"),  R(value={"a": 1}),   "malformed"),
        ("correct refusal",    T("unanswerable", "number", tier="unanswerable"),
                               R(claimed_unanswerable=True, outcome="refused"),
                               "correct_refusal"),
        ("confidently wrong",  T("unanswerable", "number", tier="unanswerable"),
                               R(value=25.4), "confidently_wrong"),
        ("wrong refusal",      T("num", "number"),
                               R(claimed_unanswerable=True, outcome="refused"),
                               "wrong_refusal"),
    ]
    failures = 0
    print(f"{'CASE':<20} {'EXPECTED':<18} {'GOT':<18} OK")
    print("-" * 68)
    for name, task, result, want in cases:
        v = grade(task, result, gt)["verdict"]
        ok = v == want
        failures += not ok
        print(f"{name:<20} {want:<18} {v:<18} {'yes' if ok else 'NO'}")
    print("-" * 68)
    print(f"{len(cases) - failures}/{len(cases)} grader self-tests passed")
    if failures:
        raise SystemExit(1)
