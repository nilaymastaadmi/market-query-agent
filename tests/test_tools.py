"""
Tests for the three tools and the metric conventions they promise.

The metric tests matter more than they look: agent/tools.py and
eval/ground_truth.py implement the same conventions independently, and the
benchmark is only meaningful if they agree. These assert that agreement
directly, so a drift in either implementation fails here rather than showing up
as a mysterious accuracy drop.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import METRICS, Tools  # noqa: E402
from eval import ground_truth as gtmod  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "db", "market.db")


@pytest.fixture(scope="module")
def tools():
    t = Tools(DB)
    yield t
    t.close()


@pytest.fixture(scope="module")
def prices():
    u = gtmod.load_universe()
    return gtmod.load_prices(sorted(u["ticker"].tolist()))


def test_get_schema_mentions_every_table_and_the_missing_data_section(tools):
    s = tools.get_schema()["schema"]
    for table in ("sectors", "instruments", "prices", "index_prices"):
        assert table in s
    # The schema must be explicit about what is absent, otherwise the
    # unanswerable tier is testing the agent's guesswork rather than its
    # ability to read a schema.
    assert "WHAT IS *NOT* IN THIS DATABASE" in s
    assert "instrument_id" in s and "prices has NO ticker column" in s


def test_run_sql_returns_rows_and_columns(tools):
    out = tools.run_sql("SELECT ticker FROM instruments ORDER BY ticker LIMIT 3")
    assert out["columns"] == ["ticker"]
    assert out["row_count"] == 3
    assert out["rows"][0][0] == "ALKEM"


def test_run_sql_surfaces_the_databases_own_error_message(tools):
    """The agent has to see the real message to correct itself."""
    out = tools.run_sql("SELECT nonexistent_column FROM prices")
    assert out.get("sql_error") is True
    assert "no such column" in out["error"].lower()


def test_prices_has_no_ticker_column_so_joins_are_mandatory(tools):
    out = tools.run_sql("SELECT ticker FROM prices LIMIT 1")
    assert out.get("sql_error") is True


@pytest.mark.parametrize("metric", METRICS)
def test_compute_metric_agrees_with_independent_pandas(tools, prices, metric):
    """
    agent/tools.py and eval/ground_truth.py must agree to floating-point noise.
    They read different artefacts (SQLite vs CSV) via different code.
    """
    ticker = "HDFCBANK"
    got = tools.compute_metric(metric, ticker)
    assert "error" not in got, got
    close = gtmod.window(prices, ticker)["close"]
    expected = {
        "total_return": gtmod.total_return,
        "cagr": lambda c: float(
            (c.iloc[-1] / c.iloc[0]) ** (gtmod.TRADING_DAYS / (len(c) - 1)) - 1.0
        ),
        "ann_volatility": gtmod.ann_vol,
        "max_drawdown": gtmod.max_drawdown,
        "sharpe": gtmod.sharpe,
    }[metric](close)
    assert got["value"] == pytest.approx(expected, rel=1e-9)


def test_compute_metric_respects_the_date_window(tools, prices):
    got = tools.compute_metric("total_return", "TCS", "2024-01-01", "2024-12-31")
    expected = gtmod.total_return(
        gtmod.window(prices, "TCS", "2024-01-01", "2024-12-31")["close"]
    )
    assert got["value"] == pytest.approx(expected, rel=1e-9)
    assert got["first_date"] >= "2024-01-01" and got["last_date"] <= "2024-12-31"


def test_tool_calls_are_logged_for_the_eval(tools):
    tools.reset_log()
    tools.get_schema()
    tools.run_sql("SELECT 1")
    tools.run_sql("DROP TABLE prices")
    names = [c["tool"] for c in tools.calls]
    assert names == ["get_schema", "run_sql", "run_sql"]
    assert tools.calls[2]["ok"] is False
    tools.reset_log()
