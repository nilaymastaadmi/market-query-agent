"""
Guardrails for run_sql.

Four independent layers, because any single one of them is bypassable and a
guardrail you have only tested one way is a guardrail you don't know the shape
of. Layers, outermost first:

  1. TEXTUAL SCREEN  (`screen_sql`)
     Strip comments and string literals, then require the statement to begin
     with SELECT or WITH, reject a second statement, and reject a denylist of
     keywords (DROP, DELETE, INSERT, UPDATE, ALTER, CREATE, REPLACE, PRAGMA,
     ATTACH, DETACH, VACUUM, REINDEX, ...). Stripping literals first matters
     in both directions: `SELECT 'DROP TABLE x'` is legitimate and must pass,
     while `SELECT 1 /* */; DROP TABLE prices` must not.

  2. READ-ONLY CONNECTION
     The database is opened via URI with mode=ro, so the SQLite library itself
     refuses writes even if a statement got past layer 1.

  3. SQLITE AUTHORIZER  (`_authorizer`)
     A callback SQLite consults for every action it is about to take, at the
     parse/prepare level rather than the string level. Only SQLITE_SELECT,
     SQLITE_READ, SQLITE_FUNCTION and SQLITE_RECURSIVE are permitted;
     everything else — including PRAGMA and ATTACH, which have their own
     action codes — is denied. This is the layer that does not care how
     cleverly the SQL was written.

  4. BUDGETS
     A wall-clock statement timeout enforced through SQLite's progress
     handler (so a runaway cross-join is interrupted mid-execution, not
     merely reported afterwards), and a hard cap on rows returned to the
     model. The cap is applied at fetch time, not by rewriting the query,
     so a query that legitimately matches a million rows is truncated with
     an explicit `truncated: true` flag rather than silently answered wrong.

Layer 1 exists to give the model a *readable* error it can correct itself
from. Layers 2-4 exist because layer 1 will eventually be wrong.

Every one of these is exercised by tests/test_guardrails.py, including a
prompt-injection case where the attack arrives inside the user's question
text rather than inside the SQL.
"""
import re
import sqlite3
import time

# --- budgets ---------------------------------------------------------------
MAX_ROWS = 200  # rows handed back to the model
TIMEOUT_S = 5.0  # wall-clock per statement
PROGRESS_INTERVAL = 1000  # VM instructions between progress callbacks

# --- layer 1: textual screen ----------------------------------------------
ALLOWED_PREFIXES = ("select", "with")

FORBIDDEN_KEYWORDS = (
    "alter",
    "analyze",
    "attach",
    "begin",
    "commit",
    "create",
    "delete",
    "detach",
    "drop",
    "insert",
    "pragma",
    "reindex",
    "release",
    "rename",
    "replace",
    "rollback",
    "savepoint",
    "truncate",
    "update",
    "upsert",
    "vacuum",
)

# SQLite authorizer action codes we permit. sqlite3 exposes these as module
# constants but not all of them on every build, so they are pinned here with
# the constant name recorded next to the value.
SQLITE_OK = 0
SQLITE_DENY = 1
_ALLOWED_ACTIONS = {
    20,  # SQLITE_READ       read a column
    21,  # SQLITE_SELECT     prepare a SELECT
    31,  # SQLITE_FUNCTION   call a scalar/aggregate function
    33,  # SQLITE_RECURSIVE  recursive CTE
}


class SQLGuardError(Exception):
    """Raised when a statement is refused. The message is shown to the model."""


def strip_literals_and_comments(sql: str) -> str:
    """
    Replace string/quoted-identifier literals with a placeholder and remove
    comments, so keyword scanning sees only executable structure.

    Not a full SQL parser and not claimed to be one — it is a screen in front
    of the authorizer, which is the layer that actually has to be right.
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # line comment
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            out.append(" ")
            continue
        # block comment
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
            continue
        # single-quoted string ('' escapes a quote)
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append("''")
            continue
        # double-quoted / backtick / bracket quoted identifiers
        if c in '"`':
            close = c
            i += 1
            while i < n and sql[i] != close:
                i += 1
            i += 1
            out.append("_id_")
            continue
        if c == "[":
            i += 1
            while i < n and sql[i] != "]":
                i += 1
            i += 1
            out.append("_id_")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def screen_sql(sql: str) -> str:
    """
    Validate `sql` and return it normalised (trailing semicolon removed).
    Raises SQLGuardError with a model-readable reason on refusal.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SQLGuardError("empty query")

    bare = strip_literals_and_comments(sql)

    # multi-statement: any semicolon that is not trailing whitespace
    body = bare.rstrip()
    if body.endswith(";"):
        body = body[:-1]
    if ";" in body:
        raise SQLGuardError(
            "multiple statements are not allowed; send exactly one SELECT"
        )

    normalised = re.sub(r"\s+", " ", body).strip()
    if not normalised:
        raise SQLGuardError("empty query")

    first = normalised.split(None, 1)[0].lower()
    if first not in ALLOWED_PREFIXES:
        raise SQLGuardError(
            f"only read-only SELECT queries are allowed (statement began with "
            f"'{first.upper()}')"
        )

    tokens = set(re.findall(r"[a-z_]+", normalised.lower()))
    hits = sorted(tokens & set(FORBIDDEN_KEYWORDS))
    if hits:
        raise SQLGuardError(
            f"query contains forbidden keyword(s): {', '.join(k.upper() for k in hits)}"
        )

    # A WITH-prefixed statement in SQLite may still end in INSERT/UPDATE/DELETE.
    # The keyword denylist above already catches those; this asserts the
    # remaining requirement explicitly so the intent is not lost.
    if first == "with" and " select " not in f" {normalised.lower()} ":
        raise SQLGuardError("a WITH statement must contain a SELECT")

    # strip only the trailing semicolon from the ORIGINAL text
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    return cleaned


# --- layers 2-4: guarded execution ----------------------------------------
def _authorizer(action, arg1, arg2, db_name, trigger):
    return SQLITE_OK if action in _ALLOWED_ACTIONS else SQLITE_DENY


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open `db_path` read-only, with the authorizer installed."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=TIMEOUT_S)
    con.set_authorizer(_authorizer)
    return con


def execute_guarded(
    con: sqlite3.Connection,
    sql: str,
    max_rows: int = MAX_ROWS,
    timeout_s: float = TIMEOUT_S,
):
    """
    Run an already-screened SELECT under a wall-clock timeout and a row cap.

    Returns (columns, rows, truncated). Raises SQLGuardError on timeout or on
    an authorizer denial; raises sqlite3.Error for genuine SQL errors (unknown
    column, syntax error) so the agent can see the database's own message and
    correct itself.
    """
    deadline = time.monotonic() + timeout_s
    timed_out = {"hit": False}

    def progress():
        if time.monotonic() > deadline:
            timed_out["hit"] = True
            return 1  # non-zero aborts the running statement
        return 0

    con.set_progress_handler(progress, PROGRESS_INTERVAL)
    try:
        cur = con.execute(sql)
        rows = cur.fetchmany(max_rows + 1)
        cols = [d[0] for d in cur.description] if cur.description else []
    except sqlite3.OperationalError as e:
        msg = str(e)
        if timed_out["hit"] or "interrupted" in msg.lower():
            raise SQLGuardError(
                f"query exceeded the {timeout_s:g}s statement timeout and was "
                f"cancelled; narrow the date range or add a LIMIT"
            ) from e
        if "not authorized" in msg.lower():
            raise SQLGuardError(
                "the database authorizer refused this operation; only "
                "read-only SELECT queries against the documented tables are "
                "permitted"
            ) from e
        raise
    except sqlite3.Warning as e:
        # sqlite3 raises this for "You can only execute one statement at a time"
        raise SQLGuardError(f"rejected by the driver: {e}") from e
    finally:
        con.set_progress_handler(None, 0)

    truncated = len(rows) > max_rows
    return cols, rows[:max_rows], truncated
