"""Devin ACP backend: a fresh Devin session as the entailment scorer.

The other backends derive P(entailment) from model internals (logprobs /
classifier head). This one asks the model selected by Devin for its ACP session
to self-report a 0-100 score per (premise, hypothesis) pair, in ONE prompt turn
per batch. The numbers are honest self-reports, not calibrated probabilities —
`confidence_source = "self_report"` says so.

Why a persistent child and not a daemon: the bridge is spawned lazily by THIS
server process and dies with it. No watchdog, no port, no scheduler — just a
stdio child exactly like this MCP server is to Devin.

Env:
    POORDJAEVIN_ACP_BRIDGE   bridge script override (default: packaged resource)
    POORDJAEVIN_ACP_NODE     Node executable (default: "node")
    DEVIN_CLI_PATH           Devin CLI executable (default: "devin"/"devin.exe")
    DEVIN_CREDENTIALS_PATH   credentials.toml override
    POORDJAEVIN_ACP_TIMEOUT  seconds per ACP turn (default 120)
    POORDJAEVIN_ACP_MODEL    model value to select in the session (optional)
    POORDJAEVIN_ACP_MAX_COST per-turn cost ceiling; unset means monitor-only

Stdlib only. The bridge and the model are the moving parts, not this file.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import subprocess
import threading
import time


def _default_bridge() -> str:
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(package_root, "scripts", "djaevin-acp-bridge.mjs")


def _terminate_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        if proc.stdin:
            proc.stdin.close()
    except (OSError, ValueError):
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except OSError:
                pass


class AcpDevinBackend:
    """entail_probs via a persistent Devin ACP session (one turn per batch)."""

    confidence_source = "self_report"

    def __init__(self, bridge: str | None = None, node: str | None = None,
                 cwd: str | None = None, timeout: float | None = None):
        self.bridge = bridge or os.environ.get("POORDJAEVIN_ACP_BRIDGE") \
            or _default_bridge()
        self.node = node or os.environ.get("POORDJAEVIN_ACP_NODE", "node")
        self.cwd = cwd or os.getcwd()
        self.timeout = timeout if timeout is not None else float(
            os.environ.get("POORDJAEVIN_ACP_TIMEOUT", "120"))
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._proc: subprocess.Popen | None = None
        self._responses: dict[int, dict] = {}
        self._id = 0
        self.requests = 0
        self.model: str | None = None
        self.last_cost: float | None = None
        self.total_cost: float | None = None
        # Guard opt-in: POORDJAEVIN_ACP_MAX_COST = teto de quota por turno.
        # Nao configurado (-1) = monitor so; custo nao reportado fica None.
        # Com qualquer teto >= 0, custo desconhecido falha fechado antes de
        # devolver scores; zero explicito passa e custos acima do teto abortam.
        self.max_cost = float(os.environ.get("POORDJAEVIN_ACP_MAX_COST", "-1"))
        atexit.register(self.close)

    # -- child process -------------------------------------------------- #

    def _ensure_proc(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if not os.path.exists(self.bridge):
            raise FileNotFoundError(f"ACP bridge not found: {self.bridge}")
        proc = subprocess.Popen(
            [self.node, self.bridge],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, cwd=self.cwd)
        self._proc = proc
        self._responses = {}
        threading.Thread(target=self._read_loop, args=(proc,), daemon=True).start()

    def _read_loop(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self._cond:
                    if self._proc is not proc:
                        continue
                    self._responses[msg["id"]] = msg
                    self._cond.notify_all()
        with self._cond:
            if self._proc is proc:
                self._proc = None
                self._cond.notify_all()

    def _request(self, payload: dict, timeout: float | None = None) -> dict:
        """Send one request, wait for the matching-id response."""
        timeout = timeout or self.timeout
        with self._cond:
            self._ensure_proc()
            proc = self._proc
            self._id += 1
            mid = self._id
            assert proc is not None and proc.stdin is not None
            try:
                proc.stdin.write(
                    json.dumps({**payload, "id": mid}) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                self._proc = None
                self._responses.clear()
                _terminate_process(proc)
                raise RuntimeError("ACP bridge process ended") from error
            deadline = time.monotonic() + timeout
            while mid not in self._responses:
                if proc.poll() is not None or self._proc is not proc:
                    self._proc = None
                    self._responses.clear()
                    raise RuntimeError("ACP bridge process ended")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._proc = None  # respawn limpo na proxima chamada
                    self._responses.clear()
                    _terminate_process(proc)
                    raise TimeoutError(f"ACP bridge timeout {timeout}s")
                self._cond.wait(remaining)
            msg = self._responses.pop(mid)
        if "error" in msg:
            raise RuntimeError(msg["error"])
        return msg

    def close(self) -> None:
        with self._cond:
            proc = self._proc
            self._proc = None
            self._responses.clear()
            self._cond.notify_all()
        _terminate_process(proc)

    # -- poordjaevin backend contract --------------------------------------- #

    def entail_probs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """One ACP turn for the whole batch -> self-reported 0-1 scores."""
        if not pairs:
            return []
        msg = self._request({"pairs": [list(p) for p in pairs]})
        self.requests += 1
        self.model = msg.get("model") or self.model
        raw_cost = msg.get("cost")
        try:
            reported_cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float))
                and not isinstance(raw_cost, bool)
                else None)
        except (OverflowError, TypeError, ValueError):
            reported_cost = None
        if reported_cost is None and msg.get("verified_free") is True:
            # Ledger da sessao Devin provou totais zero — custo nulo verificado,
            # nao confundir com "provider nao reportou".
            reported_cost = 0.0
        self.last_cost = (
            reported_cost
            if reported_cost is not None
            and math.isfinite(reported_cost)
            and reported_cost >= 0
            else None)
        if self.last_cost is None:
            # One missing turn makes the cumulative session cost unknowable.
            self.total_cost = None
        elif self.requests == 1:
            self.total_cost = self.last_cost
        elif self.total_cost is not None:
            self.total_cost += self.last_cost
        if self.max_cost >= 0:
            if self.last_cost is None:
                raise RuntimeError(
                    "QuotaExceeded: turn cost unknown; cannot verify "
                    f"POORDJAEVIN_ACP_MAX_COST {self.max_cost} (model {self.model})")
            if self.last_cost > self.max_cost:
                raise RuntimeError(
                    f"QuotaExceeded: turn cost {self.last_cost} > "
                    f"POORDJAEVIN_ACP_MAX_COST {self.max_cost} (model {self.model})")
        probs = msg["probs"]
        if len(probs) != len(pairs):
            raise RuntimeError(
                f"ACP bridge returned {len(probs)} scores for "
                f"{len(pairs)} pairs")
        return [max(0.0, min(1.0, float(p) / 100.0)) for p in probs]

    def warmup(self) -> None:
        """Spawn the bridge + ACP session early so the first real call skips
        the startup cost. Best-effort."""
        try:
            msg = self._request({"warmup": True}, timeout=self.timeout)
            self.model = msg.get("model") or self.model
        except Exception:  # noqa: BLE001, S110 - warmup is best-effort by design
            pass

    def usage(self) -> dict:
        return {
            "backend": "devin-acp",
            "model": self.model or "session-default",
            "acp_requests": self.requests,
            "last_turn_cost": self.last_cost,
            "total_cost": self.total_cost,
            "confidence_source": self.confidence_source,
        }
