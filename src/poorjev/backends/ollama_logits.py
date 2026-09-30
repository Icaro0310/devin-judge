"""Ollama logprobs backend: Jev-style single-token scoring over a local model.

Implements the same ``entail_probs(pairs) -> list[float]`` contract as the NLI
backend, but instead of a forward pass through a classifier it asks a local
chat model (Ollama, native /api/chat) a yes/no entailment question and reads
the answer off the *first token's logprobs* — the cerno technique:

    prompt -> num_predict=1, logprobs=true, top_logprobs=N
    P(entailment) = sum(exp lp of "yes" variants) / (sum yes + sum no)

That gives a real probability per (state, hypothesis) pair, which the Client
then normalises across a Choice's options and temperature-scales, exactly like
the NLI path. No API key, no tokens billed, everything stays on this machine.

Why a judge prompt instead of label tokens (A/B/C/1-5): the Client's contract
is per-pair entailment probability, not a per-question distribution, so all
three primitives (Choice/Score/Noul) work through this one method unchanged.

Config via environment:
    POORJEV_MODEL          model name in Ollama (default qwen2.5:1.5b)
    OLLAMA_HOST            base URL (default http://localhost:11434)
    POORJEV_TOP_LOGPROBS   how many candidates to read (default 20)
    POORJEV_HTTP_TIMEOUT   seconds per call (default 60)
    POORJEV_PARALLEL       max concurrent pair requests (default 4)
    POORJEV_KEEP_ALIVE     Ollama keep-alive per request (unset = Ollama default)

Dependency-free: stdlib urllib only.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_MODEL = "qwen2.5:1.5b"
DEFAULT_HOST = "http://localhost:11434"

_YES = ("yes",)
_NO = ("no",)

_PROMPT = (
    "Premise: {premise}\n"
    "Hypothesis: {hypothesis}\n"
    "Given the premise, is the hypothesis true? Answer with a single word: Yes or No."
)

_SYSTEM = (
    "You are a strict entailment judge. Reply with exactly one word: Yes or No. "
    "Yes means the hypothesis is true or strongly implied by the premise."
)


class OllamaLogitsBackend:
    """Scores (premise, hypothesis) pairs via Ollama first-token logprobs."""

    confidence_source = "logprobs"

    def __init__(
        self,
        model: str | None = None,
        host: str | None = None,
        top_logprobs: int | None = None,
        timeout: float | None = None,
        parallel: int | None = None,
    ):
        self.model = model or os.environ.get("POORJEV_MODEL", DEFAULT_MODEL)
        self.host = _dial_host(host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST)
        self.top_logprobs = top_logprobs or int(os.environ.get("POORJEV_TOP_LOGPROBS", "20"))
        self.timeout = timeout or float(os.environ.get("POORJEV_HTTP_TIMEOUT", "60"))
        self.parallel = parallel or int(os.environ.get("POORJEV_PARALLEL", "4"))
        # Spec: keep_alive = padrao do Ollama (~5 min) a menos que o utilizador
        # o configure. Se a env nao existir, a chave nao e enviada no pedido.
        self.keep_alive = os.environ.get("POORJEV_KEEP_ALIVE")
        self._stats_lock = threading.Lock()
        self._stats = {
            "ollama_requests": 0,
            "ollama_prompt_tokens": 0,
            "ollama_completion_tokens": 0,
            "ollama_elapsed_ms": 0.0,
            "warmup_requests": 0,
            "warmup_prompt_tokens": 0,
            "warmup_completion_tokens": 0,
            "warmup_elapsed_ms": 0.0,
        }
        self._thread_state = threading.local()

    def usage(self) -> dict:
        with self._stats_lock:
            stats = dict(self._stats)
        return {
            "backend": "ollama-logprobs",
            "model": self.model,
            **stats,
            "external_api_tokens": 0,
            "external_credit_cost": 0,
        }

    def is_resident(self) -> bool:
        """True when Ollama already holds this model in memory.

        Used to skip the warmup request: on a CPU-only backend a cold load costs
        seconds, and paying it on every hook invocation would dominate the gate.
        """
        req = urllib.request.Request(f"{self.host}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        names = {m.get("name") or m.get("model") for m in data.get("models", [])}
        return self.model in names

    def warmup(self) -> None:
        """Load the model into Ollama's resident memory so the first real
        call doesn't pay cold-start. Best-effort: never raises."""
        self._thread_state.warmup = True
        try:
            self._pair_prob("The service is running.", "This example is a warmup.")
        except Exception:
            pass
        finally:
            self._thread_state.warmup = False

    # -- the one method the Client calls ---------------------------------- #
    def entail_probs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Return P(entailment) in [0, 1] for each (premise, hypothesis) pair."""
        if not pairs:
            return []
        if len(pairs) == 1 or self.parallel <= 1:
            return [self._pair_prob(p, h) for p, h in pairs]
        with ThreadPoolExecutor(max_workers=min(self.parallel, len(pairs))) as pool:
            return list(pool.map(lambda ph: self._pair_prob(*ph), pairs))

    # -- internals --------------------------------------------------------- #
    def _pair_prob(self, premise: str, hypothesis: str) -> float:
        top = self._first_token_logprobs(premise, hypothesis)
        p_yes = sum(math.exp(lp) for tok, lp in top if _norm(tok) in _YES)
        p_no = sum(math.exp(lp) for tok, lp in top if _norm(tok) in _NO)
        total = p_yes + p_no
        if total <= 0.0:
            # Neither Yes nor No appeared in the top candidates: the model is
            # off-distribution. 0.5 is the honest answer — downstream
            # normalisation/temperature then sees maximum uncertainty rather
            # than a fabricated confident score.
            return 0.5
        return p_yes / total

    def _first_token_logprobs(self, premise: str, hypothesis: str) -> list[tuple[str, float]]:
        # Native /api/chat, not the OpenAI-compatible /v1/chat/completions.
        # Measured on this backend: the OpenAI-compatible endpoint silently
        # ignores keep_alive (model unloaded after the 5 min default, so every
        # gate paid a cold load), while /api/chat honours it and returns the
        # same logprobs/top_logprobs shape.
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _PROMPT.format(premise=premise, hypothesis=hypothesis)},
            ],
            "stream": False,
            "logprobs": True,
            "top_logprobs": self.top_logprobs,
            "options": {"num_predict": 1, "temperature": 0},
        }
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        if os.environ.get("POORJEV_DEBUG_PROMPT"):
            # Verificacao de few-shot/limpeza: mostra o prompt gerado no stderr
            # (stdout e o canal do protocolo MCP — nunca escrever la).
            print("PROMPT>>> " + body["messages"][1]["content"],
                  file=sys.stderr, flush=True)
        req = urllib.request.Request(
            f"{self.host}/api/chat",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read())
        elapsed_ms = (time.perf_counter() - started) * 1000
        try:
            prompt_tokens = int(data.get("prompt_eval_count") or 0)
        except (TypeError, ValueError):
            prompt_tokens = 0
        try:
            completion_tokens = int(data.get("eval_count") or 1)
        except (TypeError, ValueError):
            completion_tokens = 1
        warmup = bool(getattr(self._thread_state, "warmup", False))
        prefix = "warmup" if warmup else "ollama"
        with self._stats_lock:
            self._stats[f"{prefix}_requests"] += 1
            self._stats[f"{prefix}_prompt_tokens"] += prompt_tokens
            self._stats[f"{prefix}_completion_tokens"] += completion_tokens
            self._stats[f"{prefix}_elapsed_ms"] += elapsed_ms
        entries = data.get("logprobs") or []
        if not entries:
            raise RuntimeError(
                f"Ollama returned no logprobs (model={self.model}, host={self.host})"
            )
        return [(t["token"], float(t["logprob"])) for t in entries[0].get("top_logprobs", [])]


def _norm(token: str) -> str:
    return token.strip().lower()


def _dial_host(raw: str) -> str:
    """Normalise OLLAMA_HOST into a dialable URL.

    `0.0.0.0`/`::` are bind addresses, not dial targets — swap to `localhost`
    (NOT 127.0.0.1: on this box a second Ollama instance with a different model
    store answers on IPv4, while `localhost` resolves to ::1 where the models
    live).
    A bare `host:port` gets http:// prepended so urllib accepts it, and a
    missing port gets Ollama's default 11434 (e.g. OLLAMA_HOST=0.0.0.0).
    """
    h = (raw or DEFAULT_HOST).strip()
    if "://" not in h:
        h = "http://" + h
    h = h.replace("://0.0.0.0", "://localhost").replace("://[::]", "://localhost")
    hostpart = h.split("://", 1)[1].split("/", 1)[0]
    if ":" not in hostpart:
        h = h.replace(hostpart, hostpart + ":11434", 1)
    return h.rstrip("/")
