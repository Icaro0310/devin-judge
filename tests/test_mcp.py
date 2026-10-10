"""Tests for the MCP decision functions, backend-mocked so no model is needed.
The FastMCP wiring is thin; these pin the logic the tools return.
"""


import pytest

from poordjaevin import Client
from poordjaevin.mcp_server import (
    do_classify,
    do_decide,
    do_gate,
    do_judge,
    do_rate,
    load_temperature,
)


class ScriptedBackend:
    def __init__(self, scores):
        self.scores = list(scores)
        self.i = 0
    def entail_probs(self, pairs):
        n = len(pairs)
        seg = self.scores[self.i:self.i + n]
        assert len(seg) == n
        self.i += n
        return seg


def test_classify_returns_option_and_probs():
    client = Client(backend=ScriptedBackend([0.7, 0.2, 0.1]))
    out = do_classify(client, "some ticket", ["billing", "tech", "other"])
    assert out["value"] == "billing"
    assert set(out["probs"]) == {"billing", "tech", "other"}
    assert 0.0 <= out["confidence"] <= 1.0


def test_judge_returns_bool_and_prob():
    client = Client(backend=ScriptedBackend([0.9]))
    out = do_judge(client, "cancel my account now", "The customer wants to cancel.")
    assert out["value"] is True
    assert out["prob_true"] == 0.9


def test_rate_returns_level_and_score():
    client = Client(backend=ScriptedBackend([0.1, 0.2, 0.9]))
    out = do_rate(client, "this is infuriating", ["low", "medium", "high"])
    assert out["value"] == "high"
    assert "score" in out


def test_gate_blocks_on_money_or_data():
    # Gate original: duas perguntas nomeadas (moves_money, deletes_data).
    client = Client(backend=ScriptedBackend([0.98, 0.02]))
    out = do_gate(client, "Wire $250,000 to a new account.")
    assert out["block"] is True
    assert "moves_money" in out["triggered"]
    assert out["verdict"].startswith("block")


def test_gate_allows_safe_action():
    client = Client(backend=ScriptedBackend([0.02, 0.01]))
    out = do_gate(client, "Read the last 20 log lines.")
    assert out["block"] is False
    assert out["verdict"] == "allow"


def test_decide_multiple_questions_one_pass():
    # topic(2) + urgent(1) = 3 pairs
    client = Client(backend=ScriptedBackend([0.8, 0.2, 0.9]))
    out = do_decide(client, "I was double charged", {
        "topic": {"type": "choice", "options": ["billing", "tech"]},
        "urgent": {"type": "noul", "statement": "Needs a reply today."},
    })
    assert out["topic"]["value"] == "billing"
    assert out["urgent"]["value"] is True
    assert out["urgent"]["prob_true"] == 0.9


def test_load_temperature_missing_file_defaults_to_one(tmp_path, capsys):
    assert load_temperature(str(tmp_path / "nope.json")) == 1.0
    assert "no calibrator" in capsys.readouterr().err


def test_load_temperature_reads_value(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text('{"temperature": 2.71}')
    assert load_temperature(str(p)) == 2.71


def test_build_server_registers_advisory_tools(tmp_path, monkeypatch):
    """The built server exposes exactly the default advisory tool set
    (extras need POORDJAEVIN_EXTRA_TOOLS=on). Constructing it must not
    spawn the ACP bridge (backend init is lazy)."""
    pytest.importorskip("mcp")
    from poordjaevin import mcp_server

    monkeypatch.setattr(mcp_server, "EXTRA_TOOLS_ENABLED", False)
    server = mcp_server.build_server(calibrator_path=str(tmp_path / "none.json"))
    manager = getattr(server, "_tool_manager", None)
    tools = getattr(manager, "_tools", None) or getattr(
        server, "tools", None)
    assert tools is not None
    assert set(tools) == {
        "classify", "decide", "djaevin_calibrate", "djaevin_usage",
        "gate", "judge", "mark_disagreement", "rate",
    }


def _registered_tool_names() -> set[str]:
    """Tools the MCP server registers — derived statically so this test
    runs without the optional ``mcp`` extra installed."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).parents[1] / "src" / "poordjaevin" / "mcp_server.py"
    )
    tree = ast.parse(src.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "tool"
            for dec in node.decorator_list
        )
    }


def test_mcp_tool_surface_is_pinned():
    """Regression contract: the full decorated tool universe is exactly
    this set, including opt-in extras (`usage`, `keepalive` only register
    with POORDJAEVIN_EXTRA_TOOLS=on — the runtime default set is pinned
    by test_build_server_registers_advisory_tools). A new tool only lands
    after a deliberate edit here — check it stays read-only before
    widening."""
    assert _registered_tool_names() == {"classify","decide","djaevin_calibrate","djaevin_usage","gate","judge","keepalive","mark_disagreement","rate","usage"}
