"""VS Code Language Model API backend: the model VS Code is already serving.

Symmetric to acp_devin.py — same one-call-per-batch, same self-reported
0-100 contract — but the bridge lives inside the VS Code extension host:
``vscode.lm`` does not exist outside it, so no child process can reach it.
The devin-judge VS Code extension (publisher ``icaro0310``) serves a small
localhost endpoint and this backend POSTs scoring batches to it. Whatever
model the user picked in VS Code — Copilot, Codex or BYOK — answers; the
calibration layer on top does not change.

Discovery: the extension writes ``vscode-lm-endpoint.json`` into the
platform data dir (same root as ``djaevin_log.db``) holding
``{"url": "http://127.0.0.1:<port>/<path>", "token": "..."}``. The file is
re-read on every request, so an extension restart on a new port is picked
up for free. ``POORDJAEVIN_VSCODE_LM_ENDPOINT`` overrides it: either a path
to an endpoint file or a direct ``http(s)://`` URL (dev/testing, no token).

Scoring protocol mirrors the ACP bridge: POST ``{"pairs": [[p, h], ...]}``
-> ``{"probs": [0-100...], "model": "..."}``; ``{"warmup": true}`` ->
``{"warmup": true, "model": "..."}``. The extension must run the same
entailment prompt as ``djaevin-acp-bridge.mjs`` so a score means the same
thing whichever process serves it.

Failure taxonomy matters because a fallback must not hide bugs:
``VscodeLmUnavailable`` covers *unavailability* — no endpoint file,
connection refused, timeout, or HTTP 503 (extension up, no model selected).
Malformed replies raise ``RuntimeError`` instead: the endpoint answered,
so something is broken, and silently falling back would bury it. Only
unavailability is eligible for ``POORDJAEVIN_VSCODE_LM_FALLBACK=nli``.

Env:
    POORDJAEVIN_VSCODE_LM_ENDPOINT  endpoint file path or http(s):// URL
    POORDJAEVIN_VSCODE_LM_TIMEOUT   seconds per request (default 120)
    POORDJAEVIN_VSCODE_LM_FALLBACK  "nli" routes unavailability to the local
                                  NLI backend (default: errors propagate)

Stdlib only. The extension is the moving part, not this file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_ENDPOINT = "POORDJAEVIN_VSCODE_LM_ENDPOINT"
ENV_TIMEOUT = "POORDJAEVIN_VSCODE_LM_TIMEOUT"
ENV_FALLBACK = "POORDJAEVIN_VSCODE_LM_FALLBACK"


class VscodeLmUnavailable(RuntimeError):
    """The extension endpoint cannot be reached or has no model to serve.

    Distinct from a malformed reply (plain ``RuntimeError``): this is the
    only error a fallback may legitimately absorb.
    """


def _default_endpoint_file(platform: str | None = None, env=None, home=None) -> str:
    """Endpoint file under the same per-user data dir as djaevin_log.db."""
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = Path(home) if home is not None else Path.home()
    if platform == "nt" or platform.startswith("win"):
        base = Path(env.get("LOCALAPPDATA") or env.get("APPDATA")
                    or home / "AppData" / "Local")
    elif platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(env.get("XDG_DATA_HOME") or home / ".local" / "share")
    return str(base / "poordjaevin" / "vscode-lm-endpoint.json")


class VscodeLmBackend:
    """entail_probs via the devin-judge VS Code extension's localhost endpoint."""

    confidence_source = "self_report"

    def __init__(self, endpoint: str | None = None, timeout: float | None = None,
                 fallback=None):
        self._endpoint_override = endpoint or os.environ.get(ENV_ENDPOINT)
        self.timeout = timeout if timeout is not None else float(
            os.environ.get(ENV_TIMEOUT, "120"))
        # fallback: a backend object (tests) or "nli" for the local NLI one.
        self._fallback = fallback
        if fallback is None and os.environ.get(ENV_FALLBACK, "").strip().lower() == "nli":
            self._fallback = "nli"
        self._nli_backend = None
        self.requests = 0
        self.fallback_requests = 0
        self.model: str | None = None

    # -- endpoint discovery + transport ----------------------------------- #

    def _resolve_endpoint(self) -> tuple[str, str | None]:
        """Return (url, token). Unavailable when nothing can be resolved."""
        source = self._endpoint_override or _default_endpoint_file()
        if source.startswith(("http://", "https://")):
            return source, None
        try:
            with open(source, encoding="utf-8") as f:
                spec = json.load(f)
        except FileNotFoundError as error:
            raise VscodeLmUnavailable(
                f"no VS Code LM endpoint (looked for {source}); is the "
                "icaro0310.devin-judge extension running?") from error
        except (OSError, ValueError) as error:
            raise VscodeLmUnavailable(
                f"cannot read VS Code LM endpoint file {source}: {error}") from error
        url = spec.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise RuntimeError(
                f"endpoint file {source} has no valid http(s) url")
        token = spec.get("token")
        return url, token if isinstance(token, str) and token else None

    def _request(self, payload: dict) -> dict:
        url, token = self._resolve_endpoint()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if error.code == 503:
                raise VscodeLmUnavailable(
                    "VS Code extension reports no chat model available "
                    "(select one in VS Code or install Copilot/BYOK)") from error
            detail = ""
            try:
                detail = error.read().decode("utf-8")[:300]
            except OSError:
                pass
            raise RuntimeError(
                f"VS Code LM endpoint returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise VscodeLmUnavailable(
                f"VS Code LM endpoint unreachable ({url}): {error}") from error
        try:
            msg = json.loads(body)
        except ValueError as error:
            raise RuntimeError(
                "VS Code LM endpoint returned non-JSON reply") from error
        if not isinstance(msg, dict):
            # Protocol errors are RuntimeError across backends (see acp_devin).
            raise RuntimeError(  # noqa: TRY004
                "VS Code LM endpoint returned a non-object reply")
        if "error" in msg:
            raise RuntimeError(str(msg["error"]))
        return msg

    # -- fallback --------------------------------------------------------- #

    def _get_fallback(self):
        if self._fallback is None:
            return None
        if self._fallback == "nli":
            if self._nli_backend is None:
                from .local_nli import LocalNLIBackend
                self._nli_backend = LocalNLIBackend()
            return self._nli_backend
        return self._fallback

    def _unavailable_or_fallback(self, pairs, error: VscodeLmUnavailable) -> list[float]:
        fallback = self._get_fallback()
        if fallback is None:
            raise error
        self.fallback_requests += 1
        return fallback.entail_probs(pairs)

    # -- poordjaevin backend contract --------------------------------------- #

    def entail_probs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """One POST for the whole batch -> self-reported 0-1 scores."""
        if not pairs:
            return []
        try:
            msg = self._request({"pairs": [list(p) for p in pairs]})
        except VscodeLmUnavailable as error:
            return self._unavailable_or_fallback(pairs, error)
        self.requests += 1
        self.model = msg.get("model") or self.model
        probs = msg.get("probs")
        if not isinstance(probs, list) or len(probs) != len(pairs):
            raise RuntimeError(
                f"VS Code LM endpoint returned {len(probs) if isinstance(probs, list) else 'no'} "
                f"scores for {len(pairs)} pairs")
        out = []
        for p in probs:
            if not isinstance(p, (int, float)) or isinstance(p, bool):
                raise RuntimeError(  # noqa: TRY004
                    "VS Code LM endpoint returned a non-numeric score")
            out.append(max(0.0, min(1.0, float(p) / 100.0)))
        return out

    def warmup(self) -> None:
        """Ping the endpoint early so the first real call skips discovery and
        model-selection cost. Best-effort."""
        try:
            msg = self._request({"warmup": True})
            self.model = msg.get("model") or self.model
        except Exception:  # noqa: BLE001, S110 - warmup is best-effort by design
            pass

    def usage(self) -> dict:
        return {
            "backend": "vscode-lm",
            "model": self.model or "extension-default",
            "lm_requests": self.requests,
            "fallback_requests": self.fallback_requests,
            "confidence_source": self.confidence_source,
        }
