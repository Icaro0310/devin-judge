"""Unit tests use a stdlib HTTP server as a fake extension endpoint; no VS Code
extension host, model or key is accessed."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from poordjaevin.backends.vscode_lm import (
    VscodeLmBackend,
    VscodeLmUnavailable,
    _default_endpoint_file,
)

TOKEN = "test-token"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length))
        except ValueError:
            self._reply(400, {"error": "bad json"})
            return
        if payload.get("warmup"):
            self._reply(200, {"warmup": True, "model": "fake-copilot"})
            return
        pairs = payload.get("pairs") or []
        if any("no model" in h for _, h in pairs):
            self._reply(503, {"error": "no chat model selected"})
            return
        if any("malformed" in h for _, h in pairs):
            self._reply(200, {"probs": "not-a-list"})
            return
        if any("short" in h for _, h in pairs):
            self._reply(200, {"probs": [50], "model": "fake-copilot"})
            return
        if any("nan" in h for _, h in pairs):
            body = b'{"probs": [NaN, 50], "model": "fake-copilot"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # deterministic: i*10 + 40, plus edge values to exercise the clamp
        probs = [min(150, max(-5, 40 + 10 * i)) for i in range(len(pairs))]
        self._reply(200, {"probs": probs, "model": "fake-copilot"})


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    """Run the fake extension endpoint and point the backend at its file."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    spec = tmp_path / "vscode-lm-endpoint.json"
    url = f"http://127.0.0.1:{server.server_address[1]}/score"
    spec.write_text(json.dumps({"url": url, "token": TOKEN, "pid": os.getpid()}))
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", str(spec))
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_FALLBACK", raising=False)
    yield url
    server.shutdown()
    server.server_close()


class _FakeFallback:
    def __init__(self):
        self.calls = []

    def entail_probs(self, pairs):
        self.calls.append(list(pairs))
        return [0.99] * len(pairs)


def test_default_endpoint_file_platform_roots(tmp_path):
    linux = _default_endpoint_file("linux", env={}, home=tmp_path)
    assert linux.endswith(os.path.join(".local", "share", "poordjaevin",
                                       "vscode-lm-endpoint.json"))
    mac = _default_endpoint_file("darwin", env={}, home=tmp_path)
    assert "Application Support" in mac
    win = _default_endpoint_file("win32", env={"LOCALAPPDATA": str(tmp_path)},
                                 home=tmp_path)
    assert win.startswith(str(tmp_path))


def test_entail_probs_scores_and_clamps(endpoint):
    backend = VscodeLmBackend(timeout=10)
    probs = backend.entail_probs([("p1", "h1"), ("p2", "h2"), ("p3", "h3")])
    assert probs == [0.4, 0.5, 0.6]
    assert backend.requests == 1
    assert backend.model == "fake-copilot"


def test_entail_probs_empty(endpoint):
    backend = VscodeLmBackend()
    assert backend.entail_probs([]) == []
    assert backend.requests == 0


def test_missing_endpoint_file_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT",
                       str(tmp_path / "nada.json"))
    backend = VscodeLmBackend()
    with pytest.raises(VscodeLmUnavailable, match="extension running"):
        backend.entail_probs([("p", "h")])


def test_503_no_model_is_unavailable(endpoint):
    backend = VscodeLmBackend()
    with pytest.raises(VscodeLmUnavailable, match="no chat model"):
        backend.entail_probs([("p", "no model")])


def test_wrong_token_is_protocol_error(endpoint, tmp_path, monkeypatch):
    spec = tmp_path / "bad.json"
    spec.write_text(json.dumps({"url": endpoint, "token": "wrong"}))
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", str(spec))
    backend = VscodeLmBackend()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        backend.entail_probs([("p", "h")])


def test_malformed_reply_is_not_fallbackable(endpoint):
    fallback = _FakeFallback()
    backend = VscodeLmBackend(fallback=fallback)
    with pytest.raises(RuntimeError):
        backend.entail_probs([("p", "malformed")])
    assert fallback.calls == []


def test_short_reply_is_protocol_error(endpoint):
    backend = VscodeLmBackend(fallback=_FakeFallback())
    with pytest.raises(RuntimeError, match="scores for"):
        backend.entail_probs([("p", "short"), ("p", "short")])


def test_fallback_only_on_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT",
                       str(tmp_path / "nada.json"))
    fallback = _FakeFallback()
    backend = VscodeLmBackend(fallback=fallback)
    assert backend.entail_probs([("p", "h")]) == [0.99]
    assert fallback.calls == [[("p", "h")]]
    assert backend.fallback_requests == 1


def test_fallback_env_selects_nli(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT",
                       str(tmp_path / "nada.json"))
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_FALLBACK", "nli")
    backend = VscodeLmBackend()
    assert backend._fallback == "nli"
    with pytest.raises(VscodeLmUnavailable):  # sanity: wrong env still resolves
        monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_FALLBACK", "off")
        VscodeLmBackend().entail_probs([("p", "h")])


def _isolate_data_home(tmp_path, monkeypatch):
    """Point every data-dir root at tmp so no real endpoint file leaks in."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("HOME", str(tmp_path))


def test_direct_url_override(endpoint, tmp_path, monkeypatch):
    _isolate_data_home(tmp_path, monkeypatch)
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_TOKEN", raising=False)
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", endpoint)
    backend = VscodeLmBackend()
    with pytest.raises(RuntimeError):  # no token anywhere -> 401
        backend.entail_probs([("p", "h")])


def test_direct_url_override_env_token(endpoint, tmp_path, monkeypatch):
    _isolate_data_home(tmp_path, monkeypatch)
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", endpoint)
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_TOKEN", TOKEN)
    backend = VscodeLmBackend()
    assert backend.entail_probs([("p", "h")]) == [0.4]


def test_direct_url_override_reuses_file_token(endpoint, tmp_path, monkeypatch):
    _isolate_data_home(tmp_path, monkeypatch)
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_TOKEN", raising=False)
    default = tmp_path / "xdg" / "poordjaevin" / "vscode-lm-endpoint.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps({"url": endpoint, "token": TOKEN}))
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", endpoint)
    backend = VscodeLmBackend()
    assert backend.entail_probs([("p", "h")]) == [0.4]


def test_file_token_never_sent_to_a_different_url(endpoint, tmp_path, monkeypatch):
    """The file's bearer must not leak to an unrelated override server."""
    _isolate_data_home(tmp_path, monkeypatch)
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_TOKEN", raising=False)
    default = tmp_path / "xdg" / "poordjaevin" / "vscode-lm-endpoint.json"
    default.parent.mkdir(parents=True)
    default.write_text(json.dumps(
        {"url": "http://127.0.0.1:9/other", "token": TOKEN}))
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", endpoint)
    backend = VscodeLmBackend()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        backend.entail_probs([("p", "h")])


def test_malformed_default_file_does_not_block_url_override(
        endpoint, tmp_path, monkeypatch):
    _isolate_data_home(tmp_path, monkeypatch)
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_TOKEN", raising=False)
    default = tmp_path / "xdg" / "poordjaevin" / "vscode-lm-endpoint.json"
    default.parent.mkdir(parents=True)
    default.write_text("[1, 2, 3]")  # valid JSON, not an object
    monkeypatch.setenv("POORDJAEVIN_VSCODE_LM_ENDPOINT", endpoint)
    backend = VscodeLmBackend()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        backend.entail_probs([("p", "h")])


def test_nonfinite_score_is_protocol_error(endpoint):
    backend = VscodeLmBackend(fallback=_FakeFallback())
    with pytest.raises(RuntimeError, match="non-finite"):
        backend.entail_probs([("p", "nan"), ("p", "nan")])


def test_fallback_reports_fallback_source(endpoint, tmp_path, monkeypatch):
    class _NliLikeFallback(_FakeFallback):
        confidence_source = "local_nli"

    spec_env = os.environ["POORDJAEVIN_VSCODE_LM_ENDPOINT"]
    fallback = _NliLikeFallback()
    backend = VscodeLmBackend(fallback=fallback)
    assert backend.confidence_source == "self_report"
    backend.entail_probs([("p", "h")])
    assert backend.confidence_source == "self_report"

    backend._endpoint_override = str(tmp_path / "nada.json")
    backend.entail_probs([("p", "h")])
    assert backend.confidence_source == "local_nli"

    backend._endpoint_override = spec_env
    backend.entail_probs([("p", "h")])
    assert backend.confidence_source == "self_report"


def test_client_drops_calibration_when_source_changes(endpoint):
    """A self_report-fitted temperature must not touch fallback probs."""
    from poordjaevin.client import Client
    from poordjaevin.primitives import Noul

    class _NliLikeFallback(_FakeFallback):
        confidence_source = "local_nli"

    backend = VscodeLmBackend(fallback=_NliLikeFallback())
    # declared-remote backend: the test is the explicit authorization
    from poordjaevin.policy import Policy
    client = Client(backend=backend, temperature=0.125, abstain_threshold=0.9,
                    calibrated_source="self_report",
                    policy=Policy(allow_remote=True))
    # Endpoint answered with raw 0.4: temperature 0.125 sharpens it to ~0.96
    # confidence in the "no" direction, above the abstain threshold.
    ans = client.ask("state", {"q": Noul("is X")})["q"]
    assert ans.value is False and ans.confidence > 0.9
    # Fallback produced the probs: calibrated_source mismatch -> raw + no
    # abstain, so the same request answers on the unmodified 0.99.
    backend._endpoint_override = "http://127.0.0.1:1/score"
    ans = client.ask("state", {"q": Noul("is X")})["q"]
    assert ans.value is True and abs(ans.confidence - 0.99) < 1e-6


def test_warmup_and_usage(endpoint):
    backend = VscodeLmBackend()
    backend.warmup()
    backend.entail_probs([("p", "h")])
    usage = backend.usage()
    assert usage["backend"] == "vscode-lm"
    assert usage["model"] == "fake-copilot"
    assert usage["lm_requests"] == 1
    assert backend.confidence_source == "self_report"


def test_select_backend_wires_vscode_lm(monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_BACKEND", "vscode-lm")
    monkeypatch.delenv("POORDJAEVIN_VSCODE_LM_FALLBACK", raising=False)
    from poordjaevin.mcp_server import _select_backend
    assert isinstance(_select_backend(), VscodeLmBackend)
    monkeypatch.setenv("POORDJAEVIN_BACKEND", "lm")
    assert isinstance(_select_backend(), VscodeLmBackend)
    monkeypatch.setenv("POORDJAEVIN_BACKEND", "bogus")
    with pytest.raises(ValueError, match="unknown POORDJAEVIN_BACKEND"):
        _select_backend()
