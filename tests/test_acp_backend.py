"""Unit tests use fake ACP processes and synthetic credentials; no Devin session or model is accessed."""

import json
import os
import shutil

import pytest

from poordjaevin.backends.acp_devin import AcpDevinBackend

FIXTURE = os.path.join(os.path.dirname(__file__),
                       "fixtures", "fake_acp_bridge.mjs")
FAKE_DEVIN_CLI = os.path.join(os.path.dirname(__file__),
                              "fixtures", "fake_devin_cli.mjs")
pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not os.path.exists(FIXTURE) or not os.path.exists(FAKE_DEVIN_CLI),
    reason="node or ACP bridge fixture unavailable")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("POORDJAEVIN_ACP_MAX_COST", raising=False)
    instance = AcpDevinBackend(bridge=FIXTURE, cwd=str(tmp_path), timeout=30)
    yield instance
    instance.close()


def test_default_bridge_is_packaged():
    from poordjaevin.backends.acp_devin import _default_bridge

    assert os.path.isfile(_default_bridge())


def test_packaged_bridge_uses_devin_acp_with_synthetic_credentials(tmp_path, monkeypatch):
    from poordjaevin.backends.acp_devin import AcpDevinBackend

    node = shutil.which("node")
    credentials = tmp_path / "credentials.toml"
    credentials.write_text('windsurf_api_key = "synthetic-session-token"\n')
    monkeypatch.setenv("DEVIN_CLI_PATH", node)
    monkeypatch.setenv("DEVIN_CREDENTIALS_PATH", str(credentials))
    monkeypatch.setenv("POORDJAEVIN_ACP_NODE", node)
    monkeypatch.setenv("POORDJAEVIN_ACP_DEVIN_ARGS", json.dumps([FAKE_DEVIN_CLI]))
    monkeypatch.setenv("POORDJAEVIN_ACP_TIMEOUT", "10")
    monkeypatch.delenv("POORDJAEVIN_ACP_BRIDGE", raising=False)
    monkeypatch.delenv("POORDJAEVIN_ACP_MODEL", raising=False)
    monkeypatch.delenv("POORDJAEVIN_ACP_MAX_COST", raising=False)

    backend = AcpDevinBackend(cwd=str(tmp_path), timeout=10)
    try:
        assert backend.entail_probs([("premise 1", "hypothesis 1"), ("premise 2", "hypothesis 2")]) == [0.5, 0.75]
        assert backend.model == "fake-model"
        assert backend.last_cost == 0.0
    finally:
        backend.close()


def test_entail_probs_returns_unit_interval(backend):
    probs = backend.entail_probs([("p1", "h1"), ("p2", "h2"), ("p3", "h3")])
    assert probs == [0.5, 0.51, 0.52]  # o fixture devolve 50+i
    assert backend.requests == 1
    assert backend.model == "fake"


def test_entail_probs_empty(backend):
    assert backend.entail_probs([]) == []
    assert backend.requests == 0


def test_bridge_error_propagates(backend):
    with pytest.raises(RuntimeError, match="fake bridge error"):
        backend.entail_probs([("p", "this will error")])


def test_warmup_and_usage(backend):
    backend.warmup()
    usage = backend.usage()
    assert usage["backend"] == "devin-acp"
    assert usage["model"] == "fake"
    assert backend.confidence_source == "self_report"


def test_missing_bridge_raises(tmp_path):
    bad = AcpDevinBackend(bridge=str(tmp_path / "nada.mjs"), cwd=str(tmp_path))
    with pytest.raises(FileNotFoundError):
        bad.entail_probs([("p", "h")])


def test_cost_monitor(backend):
    backend.entail_probs([("p", "normal")])
    assert backend.last_cost == 0.0
    assert backend.total_cost == 0.0
    backend.entail_probs([("p", "this is a paid turn")])
    assert backend.last_cost == 5.0
    assert backend.total_cost == 5.0
    usage = backend.usage()
    assert usage["total_cost"] == 5.0
    assert usage["confidence_source"] == "self_report"


def test_missing_cost_remains_unknown_in_monitor_only_usage(backend):
    backend.entail_probs([("p", "normal")])
    assert backend.total_cost == 0.0
    probs = backend.entail_probs([("p", "unknown cost telemetry")])
    assert probs == [0.5]
    assert backend.last_cost is None
    usage = backend.usage()
    assert usage["last_turn_cost"] is None
    assert usage["total_cost"] is None


def test_quota_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_ACP_MAX_COST", "1")
    guarded = AcpDevinBackend(bridge=FIXTURE, cwd=str(tmp_path), timeout=30)
    guarded.entail_probs([("p", "free turn")])  # cost 0, ok
    with pytest.raises(RuntimeError, match="QuotaExceeded"):
        guarded.entail_probs([("p", "this is a paid turn")])


def test_quota_guard_fails_closed_on_missing_cost(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_ACP_MAX_COST", "0")
    guarded = AcpDevinBackend(bridge=FIXTURE, cwd=str(tmp_path), timeout=30)
    with pytest.raises(RuntimeError, match="QuotaExceeded: turn cost unknown"):
        guarded.entail_probs([("p", "unknown cost telemetry")])
    assert guarded.last_cost is None
    assert guarded.total_cost is None
    assert guarded.usage()["last_turn_cost"] is None
    assert guarded.usage()["total_cost"] is None


def test_free_only_mode(tmp_path, monkeypatch):
    """At a zero ceiling, only an explicit zero passes; unknown fails closed."""
    monkeypatch.setenv("POORDJAEVIN_ACP_MAX_COST", "0")
    guarded = AcpDevinBackend(bridge=FIXTURE, cwd=str(tmp_path), timeout=30)
    guarded.entail_probs([("p", "free turn")])  # cost 0 passa
    with pytest.raises(RuntimeError, match="QuotaExceeded"):
        guarded.entail_probs([("p", "this is a paid turn")])


def test_guard_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("POORDJAEVIN_ACP_MAX_COST", raising=False)
    free = AcpDevinBackend(bridge=FIXTURE, cwd=str(tmp_path), timeout=30)
    free.entail_probs([("p", "this is a paid turn")])  # sem guard: passa
    assert free.total_cost == 5.0
