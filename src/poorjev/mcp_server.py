"""poorjev as an MCP server: a fast, local, calibrated decision layer that any
MCP client (Claude Code, Claude Desktop) can call as tools.

The point: an agent that wants to gate a tool call, route a request, or classify
an input should not pay an LLM round trip and token cost for a decision, and
should not trust an uncalibrated made-up confidence. poorjev answers locally,
with no API key, and its confidence is calibrated.

Tools exposed (advisory contract — every answer is data, never a command):
  - judge(statement, context?)      -> {answer, p_yes, p_no, confidence, low_confidence}
  - classify(text, options, context?) -> {choice, distribution, confidence, low_confidence}
  - rate(text, scale, criteria?)    -> {level, score, distribution, confidence, low_confidence}
  - decide(questions[])             -> {answers: [{id, ...}]}
  - gate(action, kind?)             -> {risk, distribution, confidence, low_confidence,
                                       advisory: true, details}
Errors surface as {error, detail}; a decision is never invented.

The decision logic is factored into plain do_* functions so it is unit-testable
without a running server. `build_server()` wraps them as MCP tools.
"""

from __future__ import annotations

import json
import os
import re

from .client import Client
from .primitives import Choice, Score, Noul
from . import djaevin_log


# --------------------------------------------------------------------------- #
# DERIVA DESATIVADA (reversivel) — ver DJAEVIN-LOCAL.md
#
# O Djævin original: 5 tools MCP opt-in que respondem e devolvem
# distribuicao/confianca; o Devin decide o que fazer. Nada bloqueia, nada abstem
# sozinho, nada corre automaticamente.
#
# Cada interruptor abaixo reativa, numa linha, o que as rondas de otimizacao
# tinham ligado por omissao.
# --------------------------------------------------------------------------- #

# Abstention em runtime: existia para o gate automatico poder dizer "nao sei" e
# bloquear. O Djævin original responde sempre com value + confidence.
# Reativar: POORJEV_ABSTAIN=on
ABSTAIN_ENABLED = os.environ.get("POORJEV_ABSTAIN", "off").strip().lower() == "on"

# Tools `usage` e `keepalive`: existiam para medir o gate e manter o modelo
# quente para ele. O Djævin original tem exatamente 5 tools.
# Reativar: POORJEV_EXTRA_TOOLS=on
EXTRA_TOOLS_ENABLED = os.environ.get("POORJEV_EXTRA_TOOLS", "off").strip().lower() == "on"

# Gate original: duas perguntas nomeadas (dinheiro, dados), com detalhe por
# categoria. A variante de pergunta unica foi uma otimizacao do gate automatico.
DEFAULT_GATE_CHECKS = {
    "moves_money": "This action moves, sends, or refunds money.",
    "deletes_data": "This action deletes or destroys data.",
}


ABSTAIN = "ABSTAIN"

# Limiar de low_confidence do contrato hibrido: informativo, nunca imposto.
# POORJEV_LOW_CONFIDENCE aceita um float global ("0.6") ou um mapa JSON por
# tool: '{"rate": 0.75, "judge": 0.5, "default": 0.6}' — alinhado com o que
# djaevin_calibrate() reporta por tool.
def _parse_thresholds(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {str(k): float(v) for k, v in parsed.items()}
        return {"default": float(parsed)}
    except (ValueError, TypeError):
        return {"default": 0.6}


LOW_CONFIDENCE_THRESHOLDS = _parse_thresholds(
    os.environ.get("POORJEV_LOW_CONFIDENCE", "0.6"))
LOW_CONFIDENCE_THRESHOLD = LOW_CONFIDENCE_THRESHOLDS["default"]


def _low_conf(tool: str, confidence: float) -> bool:
    return confidence < LOW_CONFIDENCE_THRESHOLDS.get(
        tool, LOW_CONFIDENCE_THRESHOLD)

# Premissa neutra quando judge e chamado sem contexto.
_JUDGE_PREMISE = "Decide whether the statement is true."

GATE_RISK_LEVELS = ["low", "medium", "high"]


def _err(error: Exception) -> dict:
    return {"error": type(error).__name__, "detail": str(error)[:500]}


def _levels(scale) -> "list[str] | dict":
    """Normalise `scale`: a list of named levels, an int (1..n), or a short
    numeric range like "1-5". Returns a dict (error) when unparseable."""
    if isinstance(scale, bool):
        return _err(ValueError("scale must be a list, int, or range like '1-5'"))
    if isinstance(scale, int):
        return [str(i) for i in range(1, scale + 1)]
    if isinstance(scale, str):
        match = re.fullmatch(r"\s*(-?\d+)\s*(?:-|\.\.)\s*(-?\d+)\s*", scale)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            if b >= a and b - a <= 20:
                return [str(i) for i in range(a, b + 1)]
        return _err(ValueError(f"cannot parse scale {scale!r}"))
    levels = [str(x) for x in scale]
    return levels if levels else _err(ValueError("scale must not be empty"))


def load_calibration(calibrator_path: str | None) -> dict[str, float]:
    """Load serving calibration.

    `temperature` (calibracao de confianca) e do Djævin original e fica sempre
    ativa. `abstain_threshold` so e aplicado quando POORJEV_ABSTAIN=on; por
    omissao e 0.0, ou seja as tools respondem sempre em vez de absterem.
    """
    defaults = {"temperature": 1.0, "abstain_threshold": 0.0}
    if not calibrator_path or not os.path.exists(calibrator_path):
        return defaults
    try:
        with open(calibrator_path, encoding="utf-8") as f:
            saved = json.load(f)
        temperature = float(saved.get("temperature", 1.0))
        threshold = float(saved.get("abstain_threshold", 0.0))
        if not 0.0 < temperature < float("inf"):
            temperature = 1.0
        if not 0.0 <= threshold <= 1.01:
            threshold = 0.0
        if not ABSTAIN_ENABLED:
            threshold = 0.0
        return {"temperature": temperature, "abstain_threshold": threshold}
    except (ValueError, OSError, TypeError):
        return defaults


def load_temperature(calibrator_path: str | None) -> float:
    return load_calibration(calibrator_path)["temperature"]


# --------------------------------------------------------------------------- #
# Plain decision functions (unit-testable, take a Client)
# --------------------------------------------------------------------------- #

def do_classify(client: Client, text: str, options: list[str],
                hypothesis_template: str | None = None) -> dict:
    tmpl = hypothesis_template or client.hypothesis_template
    prev = client.hypothesis_template
    client.hypothesis_template = tmpl
    try:
        ans = client.ask(text, {"q": Choice(options)})["q"]
    finally:
        client.hypothesis_template = prev
    return {
        "value": ABSTAIN if ans.abstained else ans.value,
        "confidence": round(ans.confidence, 4),
        "abstained": ans.abstained,
        "probs": {k: round(v, 4) for k, v in ans.probs.items()},
    }


def do_rate(client: Client, text: str, levels: list[str]) -> dict:
    ans = client.ask(text, {"q": Score(levels=levels)})["q"]
    return {
        "value": ABSTAIN if ans.abstained else ans.value,
        "score": round(ans.score, 4),
        "confidence": round(ans.confidence, 4),
        "abstained": ans.abstained,
        "distribution": {k: round(v, 4) for k, v in ans.distribution.items()},
    }


def do_judge(client: Client, text: str, statement: str) -> dict:
    ans = client.ask(text, {"q": Noul(statement)})["q"]
    return {
        "value": ABSTAIN if ans.abstained else bool(ans.value),
        "prob_true": round(ans.prob, 4),
        "confidence": round(ans.confidence, 4),
        "abstained": ans.abstained,
    }


def do_gate(client: Client, action: str, checks: dict | None = None) -> dict:
    """Return a blocking verdict for a detected risk or an uncertain risk check."""
    checks = checks or DEFAULT_GATE_CHECKS
    questions = {name: Noul(stmt) for name, stmt in checks.items()}
    answers = client.ask(action, questions)
    triggered = {
        name: (ABSTAIN if answer.abstained else round(answer.prob, 4))
        for name, answer in answers.items()
        if answer.value or answer.abstained
    }
    blocked = bool(triggered)
    return {
        "block": blocked,
        "requires_confirmation": blocked,
        "verdict": "block, require explicit confirmation" if blocked else "allow",
        "triggered": triggered,
        "details": {
            name: {
                "value": ABSTAIN if answer.abstained else bool(answer.value),
                "prob_true": round(answer.prob, 4),
                "confidence": round(answer.confidence, 4),
                "abstained": answer.abstained,
            }
            for name, answer in answers.items()
        },
    }


def do_decide(client: Client, text: str, questions: dict) -> dict:
    """Answer typed questions and preserve distributions for audit and evaluation."""
    from .evaluate import _build_primitive
    prims = {name: _build_primitive(spec) for name, spec in questions.items()}
    answers = client.ask(text, prims)
    out = {}
    for name, ans in answers.items():
        entry = {
            "value": ABSTAIN if ans.abstained else ans.value,
            "confidence": round(ans.confidence, 4),
            "abstained": ans.abstained,
        }
        if hasattr(ans, "prob"):
            entry["prob_true"] = round(ans.prob, 4)
        elif hasattr(ans, "probs"):
            entry["distribution"] = {k: round(v, 4) for k, v in ans.probs.items()}
        elif hasattr(ans, "distribution"):
            entry["distribution"] = {k: round(v, 4) for k, v in ans.distribution.items()}
        if hasattr(ans, "score"):
            entry["score"] = round(ans.score, 4)
        out[name] = entry
    return out


# --------------------------------------------------------------------------- #
# MCP server
# --------------------------------------------------------------------------- #

def _make_app(name: str):
    """Return an MCP server app across SDK versions.

    mcp 2.x renamed FastMCP -> MCPServer; both expose the same .tool() decorator
    and .run(transport='stdio'). Support whichever is installed.
    """
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer
        return MCPServer(name)
    except ImportError:
        pass
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP
        return FastMCP(name)
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "The MCP server needs the 'mcp' extra: pip install 'poorjev[local,mcp]'"
        ) from e


def _select_backend():
    """Pick the scoring backend via POORJEV_BACKEND env var.

    "ollama" (default) uses the local Ollama server with first-token logprobs —
    no model download, no torch. "nli" falls back to the original local NLI
    backend, which lazily needs the 'local' extra (torch + transformers).
    """
    name = os.environ.get("POORJEV_BACKEND", "ollama").lower()
    if name == "ollama":
        from .backends.ollama_logits import OllamaLogitsBackend
        return OllamaLogitsBackend()
    if name == "acp":
        from .backends.acp_devin import AcpDevinBackend
        return AcpDevinBackend()
    if name == "nli":
        return None  # Client lazily builds LocalNLIBackend
    raise ValueError(
        f"unknown POORJEV_BACKEND: {name!r} (expected 'ollama', 'acp' or 'nli')")


def build_client(calibrator_path: str | None = "calibration.json", backend=None) -> Client:
    calibration = load_calibration(calibrator_path)
    selected_backend = backend if backend is not None else _select_backend()
    return Client(
        backend=selected_backend,
        temperature=calibration["temperature"],
        abstain_threshold=calibration["abstain_threshold"],
    )


def build_server(calibrator_path: str | None = "calibration.json"):
    # Warmup eager no arranque removido: existia porque o hook spawnava um
    # servidor por chamada e pagava cold load. Numa sessao normal o servidor
    # arranca uma vez, portanto e desnecessario. O metodo continua disponivel em
    # OllamaLogitsBackend.warmup() para reativar:
    #   threading.Thread(target=client._backend.warmup, daemon=True).start()
    client = build_client(calibrator_path)
    server = _make_app("poorjev")
    # Proveniencia da confianca: "logprobs" (ollama/nli) ou "self_report"
    # (acp) — o campo nunca finge ser probabilidade calibrada quando nao e.
    confidence_source = getattr(
        client._backend, "confidence_source", "logprobs")

    # Monitor de quota: o backend ACP expoe last_cost/model por turno; no
    # Ollama ficam None e os campos saem como dados (NULL no log).
    def _usage_fields() -> dict:
        backend = client._backend
        return {
            "cost": getattr(backend, "last_cost", None),
            "model": getattr(backend, "model", None),
        }

    def _log(tool, input_text, choice, confidence, low_conf):
        return djaevin_log.log_decision(
            tool, input_text, choice, confidence, low_conf,
            **_usage_fields())

    # `record=True`: a decisao sai para o chamador — loga e injeta few-shot.
    # `record=False`: sub-pergunta interna (ex.: checks do gate) — nao loga.
    def _judge(statement: str, context: str = "", record: bool = True) -> dict:
        premise = context or _JUDGE_PREMISE
        if record:
            premise = djaevin_log.few_shot_block("judge", statement) + premise
        try:
            ans = do_judge(client, premise, statement)
        except Exception as error:
            return _err(error)
        p_yes = ans["prob_true"]
        out = {
            "answer": "yes" if ans["value"] is True else "no",
            "p_yes": p_yes,
            "p_no": round(1 - p_yes, 4),
            "confidence": ans["confidence"],
            "confidence_source": confidence_source,
            "low_confidence": _low_conf("judge", ans["confidence"]),
        }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                "judge", statement, out["answer"],
                out["confidence"], out["low_confidence"])
        return out

    def _classify(text: str, options: list[str], context: str = "",
                  record: bool = True) -> dict:
        body = f"{context}\n{text}" if context else text
        if record:
            body = djaevin_log.few_shot_block("classify", text) + body
        try:
            ans = do_classify(client, body, [str(o) for o in options])
        except Exception as error:
            return _err(error)
        out = {
            "choice": ans["value"],
            "distribution": ans["probs"],
            "confidence": ans["confidence"],
            "confidence_source": confidence_source,
            "low_confidence": _low_conf("classify", ans["confidence"]),
        }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                "classify", text, out["choice"],
                out["confidence"], out["low_confidence"])
        return out

    def _rate(text: str, scale, criteria: str = "",
              record: bool = True) -> dict:
        levels = _levels(scale)
        if isinstance(levels, dict):  # invalid scale -> structured error
            return levels
        body = f"{text}\nCriteria: {criteria}" if criteria else text
        if record:
            body = djaevin_log.few_shot_block("rate", text) + body
        try:
            ans = do_rate(client, body, levels)
        except Exception as error:
            return _err(error)
        out = {
            "level": ans["value"],
            "score": ans["score"],
            "distribution": ans["distribution"],
            "confidence": ans["confidence"],
            "confidence_source": confidence_source,
            "low_confidence": _low_conf("rate", ans["confidence"]),
        }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                "rate", text, out["level"],
                out["confidence"], out["low_confidence"])
        return out

    @server.tool()
    def judge(statement: str, context: str = "") -> dict:
        """Yes/no opinion on `statement` (optional `context`). Returns
        {answer: "yes"|"no", p_yes, p_no, confidence, low_confidence}.
        Advisory: a second opinion to weigh, never a verdict."""
        return _judge(statement, context)

    @server.tool()
    def classify(text: str, options: list[str], context: str = "") -> dict:
        """Pick exactly one of `options` for `text` (optional `context`).
        Returns {choice, distribution, confidence, low_confidence}."""
        return _classify(text, options, context)

    @server.tool()
    def rate(text: str, scale, criteria: str = "") -> dict:
        """Rate `text` on `scale`: a short numeric range ("1-5") or a list of
        ordered named levels; `criteria` says what is being rated. Returns
        {level, score, distribution, confidence, low_confidence}."""
        return _rate(text, scale, criteria)

    @server.tool()
    def decide(questions: list[dict]) -> dict:
        """Answer a batch of typed questions in one call. Each item is
        {"id": <name>, "type": "judge"|"classify"|"rate", ...that tool's args}.
        Returns {answers: [{id: <name>, ...that tool's result}]}"""
        handlers = {"judge": _judge, "classify": _classify, "rate": _rate}
        answers = []
        for q in questions:
            handler = handlers.get(q.get("type"))
            kwargs = {k: v for k, v in q.items() if k not in ("id", "type")}
            try:
                result = handler(**kwargs) if handler else _err(
                    ValueError(f"unknown type {q.get('type')!r}"))
            except TypeError as error:
                result = _err(error)
            answers.append({"id": q.get("id"), **result})
        return {"answers": answers}

    @server.tool()
    def gate(action: str, kind: str = "") -> dict:
        """Advisory opinion: risk that `action` moves money or destroys data
        (optional `kind`, e.g. "money"|"data"). Returns
        {risk: "low"|"medium"|"high", distribution, confidence,
         low_confidence, advisory: true, details}.
        Pure opinion — nothing is blocked, approved or enforced."""
        hint = f" (concern: {kind})" if kind else ""
        risk = _classify(
            djaevin_log.few_shot_block("gate", action)
            + f"Action: {action}{hint}\nDoes it move money or delete/destroy "
            f"data? Rate the risk level.",
            GATE_RISK_LEVELS, record=False)
        if "error" in risk:
            return risk
        # Hibrido: os dois checks nomeados do contrato antigo ficam como
        # evidencia em `details`, ao lado da distribuicao de risco do spec.
        # Sub-perguntas internas: record=False (nao sao decisoes do chamador).
        details = {}
        for name, stmt in DEFAULT_GATE_CHECKS.items():
            check = _judge(stmt, action, record=False)
            details[name] = check.get("p_yes") if "error" not in check else check
        out = {
            "risk": risk["choice"],
            "distribution": risk["distribution"],
            "confidence": risk["confidence"],
            "confidence_source": confidence_source,
            "low_confidence": _low_conf("gate", risk["confidence"]),
            "advisory": True,
            "details": details,
        }
        # O gate loga como tool "gate" (input = action); o bloco de exemplos
        # foi injetado acima porque o _classify interno correu com record=False.
        out.update(_usage_fields())
        out["decision_id"] = _log(
            "gate", action, out["risk"],
            out["confidence"], out["low_confidence"])
        return out

    # ---------------------------------------------------------------- #
    # Tools auxiliares de auto-aprendizagem (Nivel 1/2). Nao sao tools
    # de decisao: observam e sugerem, nunca bloqueiam nem decidem.
    # ---------------------------------------------------------------- #

    @server.tool()
    def mark_disagreement(decision_id: int, correct_choice: str = "") -> dict:
        """Mark a logged decision (its `decision_id`) as wrong. With
        `correct_choice`, the pair input->correct is promoted to validated
        few-shot examples. Only mark when the right answer is obvious —
        disagreement on ambiguous questions has no value."""
        return djaevin_log.mark_disagreement(decision_id, correct_choice)

    @server.tool()
    def djaevin_calibrate() -> dict:
        """Report: mean confidence on disagreed decisions per tool and a
        suggested low_confidence threshold. Suggestion only — change
        POORJEV_LOW_CONFIDENCE manually if you agree; nothing is applied."""
        return djaevin_log.calibrate(LOW_CONFIDENCE_THRESHOLDS)

    @server.tool()
    def djaevin_usage() -> dict:
        """Model/quota monitor: decisions and measured ACP turn cost, grouped
        by model and by tool, read from djaevin_log.db. Observational only — it
        never changes behavior. A paid model shows up as cost > 0."""
        return djaevin_log.usage_summary()

    # `usage` e `keepalive` eram instrumentacao do gate automatico, nao do Djævin
    # original (5 tools). Ficam no codigo, registadas so com POORJEV_EXTRA_TOOLS=on.
    if EXTRA_TOOLS_ENABLED:
        @server.tool()
        def usage() -> dict:
            """Return local inference usage counters for this server process."""
            backend = client._backend
            return backend.usage() if hasattr(backend, "usage") else {
                "backend": "nli",
                "external_api_tokens": 0,
                "external_credit_cost": 0,
            }

        @server.tool()
        def keepalive() -> dict:
            """Renew the model's keep_alive window with one trivial request.

            Existe para o gate automatico nao pagar cold load; nao faz parte do
            Djævin original.
            """
            backend = client._backend
            if not hasattr(backend, "warmup"):
                return {"renewed": False, "reason": "backend has no warmup"}
            try:
                backend.warmup()
            except Exception as error:
                return {"renewed": False, "reason": type(error).__name__}
            return {"renewed": True, "resident": bool(
                backend.is_resident() if hasattr(backend, "is_resident") else False)}

    return server


def main(calibrator_path: str | None = "calibration.json") -> None:
    # POORJEV_CALIBRATOR (env) wins over the default relative path, so the MCP
    # config can point at an absolute calibrator regardless of the server's cwd.
    calibrator_path = os.environ.get("POORJEV_CALIBRATOR", calibrator_path)
    build_server(calibrator_path).run()  # stdio transport


if __name__ == "__main__":
    main()
