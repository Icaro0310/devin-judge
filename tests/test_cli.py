"""CLI contract for the `gate` subcommand: exit codes are the gate —
0 allow, 1 block/error (fail-closed), 2 usage/io. do_gate is mocked so
no backend is needed."""

import json

import poordjaevin.mcp_server as mcp_srv
from poordjaevin.cli import main


def _patch_gate(monkeypatch, out=None, raises=None):
    def fake(client, action, checks=None):
        if raises is not None:
            raise raises
        return out
    monkeypatch.setattr(mcp_srv, "do_gate", fake)


def test_gate_allow_exits_zero(monkeypatch, capsys):
    _patch_gate(monkeypatch, out={
        "block": False, "verdict": "allow", "triggered": {}})
    assert main(["gate", "--action", "read the logs"]) == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "allow"


def test_gate_block_exits_one(monkeypatch, capsys):
    _patch_gate(monkeypatch, out={
        "block": True, "verdict": "block, require explicit confirmation",
        "triggered": {"moves_money": 0.98}})
    assert main(["gate", "--action", "wire $250k"]) == 1
    assert json.loads(capsys.readouterr().out)["block"] is True


def test_gate_backend_error_fails_closed(monkeypatch, capsys):
    _patch_gate(monkeypatch, raises=RuntimeError("bridge dead"))
    assert main(["gate", "--action", "anything"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "backend"
    assert out["verdict"].startswith("block")


def test_gate_no_action_is_usage_error(capsys):
    assert main(["gate"]) == 2


def test_gate_action_file(tmp_path, monkeypatch, capsys):
    f = tmp_path / "action.txt"
    f.write_text("drop the table")
    seen = {}

    def fake(client, action, checks=None):
        seen["action"] = action
        return {"block": False, "verdict": "allow", "triggered": {}}
    monkeypatch.setattr(mcp_srv, "do_gate", fake)
    assert main(["gate", "--action-file", str(f)]) == 0
    assert seen["action"] == "drop the table"
