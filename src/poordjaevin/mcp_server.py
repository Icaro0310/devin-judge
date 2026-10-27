"""poordjaevin as an MCP server: a fast, calibrated decision layer that any
MCP client (Devin, Claude Code, Claude Desktop) can call as tools.

The point: an agent that wants to gate a tool call, route a request, or classify
an input should not pay an LLM round trip and token cost for a decision, and
should not trust an uncalibrated made-up confidence. poordjaevin answers locally,
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
import sys

from . import djaevin_log
from .client import Client
from .primitives import Choice, Noul, Score
from .verdicts import dist_verdict, noul_verdict

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
# Reativar: POORDJAEVIN_ABSTAIN=on
ABSTAIN_ENABLED = os.environ.get("POORDJAEVIN_ABSTAIN", "off").strip().lower() == "on"

# Tools `usage` e `keepalive`: existiam para medir o gate e manter o modelo
# quente para ele. O Djævin original tem exatamente 5 tools.
# Reativar: POORDJAEVIN_EXTRA_TOOLS=on
EXTRA_TOOLS_ENABLED = os.environ.get("POORDJAEVIN_EXTRA_TOOLS", "off").strip().lower() == "on"

# Gate original: duas perguntas nomeadas (dinheiro, dados), com detalhe por
# categoria. A variante de pergunta unica foi uma otimizacao do gate automatico.
DEFAULT_GATE_CHECKS = {
    "moves_money": "This action moves, sends, or refunds money.",
    "deletes_data": "This action deletes or destroys data.",
}

# Preset for gating code-review/merge actions in CI. Concrete hypotheses,
# per the repo convention: the NLI model scores "this merges into a
# protected branch" reliably and "this is dangerous" poorly. A generic
# merge description can match several of these at once, so any triggered
# check blocks — same fail-closed rule as the default pair.
MERGE_GATE_CHECKS = {
    "protected_main": "This action merges code into the protected main "
                      "branch of a repository.",
    "runtime_destructive": "The code being merged can delete files, "
                           "terminate processes, or destroy resources "
                           "when it runs.",
    "degraded_coverage": "This merge proceeds while review coverage is "
                         "incomplete or degraded.",
    "sensitive_surface": "The change modifies credentials, secrets, "
                         "billing, or CI workflow files.",
}

GATE_CHECK_PRESETS = {
    "default": DEFAULT_GATE_CHECKS,
    "merge": MERGE_GATE_CHECKS,
}


ABSTAIN = "ABSTAIN"

# Limiar de low_confidence do contrato hibrido: informativo, nunca imposto.
# POORDJAEVIN_LOW_CONFIDENCE aceita um float global ("0.6") ou um mapa JSON por
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
    os.environ.get("POORDJAEVIN_LOW_CONFIDENCE", "0.6"))
LOW_CONFIDENCE_THRESHOLD = LOW_CONFIDENCE_THRESHOLDS["default"]


def _low_conf(tool: str, confidence: float) -> bool:
    return confidence < LOW_CONFIDENCE_THRESHOLDS.get(
        tool, LOW_CONFIDENCE_THRESHOLD)

# Hipotese usada quando judge e chamado sem contexto: a unica pergunta de
# entailment bem formada que resta e a auto-avaliacao do proprio statement —
# (statement, "the statement is true."). O resultado sai marcado
# `uncalibrated` para nunca ser lido como medicao.
_SELF_ASSESSMENT_HYPOTHESIS = "the statement is true."


def _judge_inputs(statement: str, context: str) -> tuple[str, str, bool]:
    """Resolve (premise, hypothesis, uncalibrated) for a judge call.

    An (instruction, claim) pair — the old no-context default — is not a
    well-formed entailment question: no premise entails a proposition, so
    the model's score is noise rendered as a verdict. Without context the
    only honest thing to ask is the statement's self-assessment, flagged.
    """
    if context.strip():
        return context, statement, False
    return statement, _SELF_ASSESSMENT_HYPOTHESIS, True

GATE_RISK_LEVELS = ["low", "medium", "high"]


def _err(error: Exception) -> dict:
    return {"error": type(error).__name__, "detail": str(error)[:500]}


def _levels(scale) -> list[str] | dict:
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


def load_calibration(calibrator_path: str | None) -> dict[str, object]:
    """Load backend-specific temperature calibration; abstention is opt-in."""
    defaults = {"temperature": 1.0, "abstain_threshold": 0.0, "backend": None}
    if not calibrator_path or not os.path.exists(calibrator_path):
        if calibrator_path:
            print(f"[no calibrator at {calibrator_path}; using raw confidence]",
                  file=sys.stderr)
        return defaults
    try:
        with open(calibrator_path, encoding="utf-8") as f:
            saved = json.load(f)
        temperature = float(saved.get("temperature", 1.0))
        threshold = float(saved.get("abstain_threshold", 0.0))
        backend = saved.get("backend", "local_nli")
        if not 0.0 < temperature < float("inf"):
            temperature = 1.0
        if not 0.0 <= threshold <= 1.01:
            threshold = 0.0
        if not ABSTAIN_ENABLED:
            threshold = 0.0
        if backend not in ("local_nli", "logprobs", "self_report"):
            backend = "unknown"
        return {"temperature": temperature, "abstain_threshold": threshold, "backend": backend}
    except (ValueError, OSError, TypeError):
        return defaults


def load_temperature(calibrator_path: str | None) -> float:
    return load_calibration(calibrator_path)["temperature"]


def load_profiles(profiles_path: str | None) -> dict:
    """Load per-slice calibration profiles (calibration.fit_profiles output).

    {} when absent or malformed — profile selection then always falls back
    to the global calibrator. The doc's ``backend`` tag is checked by the
    caller, same contract as the global temperature.
    """
    if not profiles_path or not os.path.exists(profiles_path):
        return {}
    try:
        with open(profiles_path, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), dict):
            return {}
        return doc
    except (ValueError, OSError, TypeError):
        return {}


# --------------------------------------------------------------------------- #
# Plain decision functions (unit-testable, take a Client)
# --------------------------------------------------------------------------- #

def do_classify(client: Client, text: str, options: list[str],
                hypothesis_template: str | None = None,
                task: str | None = None) -> dict:
    tmpl = hypothesis_template or client.hypothesis_template
    prev = client.hypothesis_template
    client.hypothesis_template = tmpl
    try:
        ans = client.ask(text, {"q": Choice(options)}, task=task)["q"]
    finally:
        client.hypothesis_template = prev
    return {
        "value": ABSTAIN if ans.abstained else ans.value,
        "confidence": round(ans.confidence, 4),
        "abstained": ans.abstained,
        "probs": {k: round(v, 4) for k, v in ans.probs.items()},
    }


def do_rate(client: Client, text: str, levels: list[str],
            task: str | None = None) -> dict:
    ans = client.ask(text, {"q": Score(levels=levels)}, task=task)["q"]
    return {
        "value": ABSTAIN if ans.abstained else ans.value,
        "score": round(ans.score, 4),
        "confidence": round(ans.confidence, 4),
        "abstained": ans.abstained,
        "distribution": {k: round(v, 4) for k, v in ans.distribution.items()},
    }


def do_judge(client: Client, text: str, statement: str,
             task: str | None = None) -> dict:
    ans = client.ask(text, {"q": Noul(statement)}, task=task)["q"]
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
    # The gate's checks are action-risk nouls by construction — the evalset
    # slice name is declared so a validated action_risk:noul profile applies.
    answers = client.ask(action, questions, task="action_risk")
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


def do_decide(client: Client, text: str, questions: dict,
              task: str | None = None) -> dict:
    """Answer typed questions and preserve distributions for audit and evaluation."""
    from .evaluate import _build_primitive
    prims = {name: _build_primitive(spec) for name, spec in questions.items()}
    answers = client.ask(text, prims, task=task)
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
            "The MCP server needs the 'mcp' extra: pip install 'poordjaevin[local,mcp]'"
        ) from e


def _select_backend():
    """Pick the scoring backend via POORDJAEVIN_BACKEND env var.

    "acp" (default) scores through Devin's own ACP session — the model already
    available to the Devin CLI, with automatic rotation, no extra install and
    no API key beyond the Devin credentials. "vscode-lm" scores through the
    VS Code Language Model API via the icaro0310.judge extension's
    localhost endpoint (Copilot, Codex or BYOK, whichever VS Code is serving).
    "nli" uses the fully offline local NLI backend, which lazily needs the
    'local' extra (torch + transformers, ~400MB model download on first run).
    """
    name = os.environ.get("POORDJAEVIN_BACKEND", "acp").lower()
    if name == "acp":
        from .backends.acp_devin import AcpDevinBackend
        return AcpDevinBackend()
    if name in ("vscode-lm", "vscode", "lm"):
        from .backends.vscode_lm import VscodeLmBackend
        return VscodeLmBackend()
    if name == "nli":
        return None  # Client lazily builds LocalNLIBackend
    raise ValueError(
        f"unknown POORDJAEVIN_BACKEND: {name!r} "
        "(expected 'acp', 'vscode-lm' or 'nli')")


def build_client(calibrator_path: str | None = "calibration.json", backend=None,
                 profiles_path: str | None = "calibration_profiles.json",
                 policy=None, hooks: dict | None = None) -> Client:
    calibration = load_calibration(calibrator_path)
    selected_backend = backend if backend is not None else _select_backend()
    source = "local_nli" if selected_backend is None else getattr(
        selected_backend, "confidence_source", "unknown")
    if calibration["backend"] and calibration["backend"] != source:
        print(
            f"Calibration backend {calibration['backend']} does not match {source}; using raw confidence.",
            file=sys.stderr,
        )
        temperature = 1.0
        abstain_threshold = 0.0
    else:
        temperature = calibration["temperature"]
        abstain_threshold = calibration["abstain_threshold"]
    # Per-slice profiles carry the same backend tag: a fit for one confidence
    # source must not leak into another backend's decisions.
    profiles_doc = load_profiles(profiles_path)
    profile_backend = profiles_doc.get("backend")
    if profiles_doc and profile_backend and profile_backend != source:
        print(
            f"Profiles backend {profile_backend} does not match {source}; using global calibration only.",
            file=sys.stderr,
        )
        profiles_doc = {}
    if policy is None:
        # An operator-selected remote backend (POORDJAEVIN_BACKEND) is the
        # explicit authorization Policy() requires; the default local NLI
        # path keeps the closed default. Undeclared-destination backends
        # selected this way are authorized by the same operator choice.
        from .policy import Policy
        policy = Policy(allow_remote=(
            selected_backend is not None
            and getattr(selected_backend, "is_remote", True)))
    return Client(
        backend=selected_backend,
        temperature=temperature,
        abstain_threshold=abstain_threshold,
        calibrated_source=source if calibration["backend"] else None,
        profiles=profiles_doc or None,
        policy=policy,
        hooks=hooks,
    )


def build_server(calibrator_path: str | None = "calibration.json"):
    client = build_client(calibrator_path)
    server = _make_app("poordjaevin")
    # Proveniencia da confianca: "local_nli" ou "self_report" (acp) — o campo
    # nunca finge ser probabilidade calibrada quando nao e. Lida por decisao:
    # um backend pode mudar de producer entre requests (fallback vscode-lm).
    def _confidence_source() -> str:
        return getattr(
            client._backend, "confidence_source", "local_nli")

    # Monitor de quota: o backend ACP expoe last_cost/model por turno; no
    # backend local ficam None e os campos saem como dados (NULL no log).
    def _usage_fields() -> dict:
        backend = client._backend
        return {
            "cost": getattr(backend, "last_cost", None),
            "model": getattr(backend, "model", None),
        }

    def _log(tool, input_text, choice, confidence, low_conf,
             few_shot_ids=None, few_shot_pool=None):
        return djaevin_log.log_decision(
            tool, input_text, choice, confidence, low_conf,
            few_shot_ids=few_shot_ids, few_shot_pool=few_shot_pool,
            **_usage_fields())

    # `record=True`: a decisao sai para o chamador — loga e injeta few-shot.
    # `record=False`: sub-pergunta interna (ex.: checks do gate) — nao loga.
    def _judge(statement: str, context: str = "", record: bool = True,
               task: str | None = None) -> dict:
        premise, hypothesis, uncalibrated = _judge_inputs(statement, context)
        # Few-shot examples encode premise-weighed judging; they do not
        # transfer to the self-assessment question shape.
        shot_ids, shot_pool = [], 0
        if record and not uncalibrated:
            block, shot_ids = djaevin_log.few_shot_block_ids("judge", statement)
            shot_pool = djaevin_log.few_shot_pool_size("judge")
            premise = block + premise
        try:
            ans = do_judge(client, premise, hypothesis, task=task)
        except Exception as error:  # noqa: BLE001 - tool boundary must not raise
            return _err(error)
        # Self-assessment answers use a different question shape — they log
        # and threshold under "judge_self_assessment" so djaevin_calibrate's
        # per-tool suggestions actually apply and they never enter the
        # "judge" few-shot pool.
        tool = "judge_self_assessment" if uncalibrated else "judge"
        p_yes = ans["prob_true"]
        low_conf = _low_conf(tool, ans["confidence"])
        if uncalibrated:
            # A self-assessment score is not a verdict: no yes/no answer,
            # no p_yes/p_no pair, no verdict band. confidence_source is the
            # literal "self_report" — the question shape is self-assessment
            # by definition, whichever backend produced the number.
            out = {
                "answer": None,
                "uncalibrated_score": p_yes,
                "confidence": ans["confidence"],
                "confidence_source": "self_report",
                "low_confidence": low_conf,
                "uncalibrated": True,
                "basis": "self-assessment only",
            }
        else:
            out = {
                "answer": "yes" if ans["value"] is True else "no",
                "p_yes": p_yes,
                "p_no": round(1 - p_yes, 4),
                "confidence": ans["confidence"],
                "confidence_source": _confidence_source(),
                "low_confidence": low_conf,
                "verdict": noul_verdict(p_yes, low_conf),
                "uncalibrated": False,
                "basis": "premise",
            }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                tool, statement, out["answer"],
                out["confidence"], out["low_confidence"],
                few_shot_ids=shot_ids or None, few_shot_pool=shot_pool)
        return out

    def _classify(text: str, options: list[str], context: str = "",
                  record: bool = True, task: str | None = None) -> dict:
        body = f"{context}\n{text}" if context else text
        shot_ids, shot_pool = [], 0
        if record:
            block, shot_ids = djaevin_log.few_shot_block_ids("classify", text)
            shot_pool = djaevin_log.few_shot_pool_size("classify")
            body = block + body
        try:
            ans = do_classify(client, body, [str(o) for o in options],
                              task=task)
        except Exception as error:  # noqa: BLE001 - tool boundary must not raise
            return _err(error)
        low_conf = _low_conf("classify", ans["confidence"])
        out = {
            "choice": ans["value"],
            "distribution": ans["probs"],
            "confidence": ans["confidence"],
            "confidence_source": _confidence_source(),
            "low_confidence": low_conf,
            "verdict": dist_verdict(low_conf),
        }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                "classify", text, out["choice"],
                out["confidence"], out["low_confidence"],
                few_shot_ids=shot_ids or None, few_shot_pool=shot_pool)
        return out

    def _rate(text: str, scale, criteria: str = "",
              record: bool = True, task: str | None = None) -> dict:
        levels = _levels(scale)
        if isinstance(levels, dict):  # invalid scale -> structured error
            return levels
        body = f"{text}\nCriteria: {criteria}" if criteria else text
        shot_ids, shot_pool = [], 0
        if record:
            block, shot_ids = djaevin_log.few_shot_block_ids("rate", text)
            shot_pool = djaevin_log.few_shot_pool_size("rate")
            body = block + body
        try:
            ans = do_rate(client, body, levels, task=task)
        except Exception as error:  # noqa: BLE001 - tool boundary must not raise
            return _err(error)
        low_conf = _low_conf("rate", ans["confidence"])
        out = {
            "level": ans["value"],
            "score": ans["score"],
            "distribution": ans["distribution"],
            "confidence": ans["confidence"],
            "confidence_source": _confidence_source(),
            "low_confidence": low_conf,
            "verdict": dist_verdict(low_conf),
        }
        if record:
            out.update(_usage_fields())
            out["decision_id"] = _log(
                "rate", text, out["level"],
                out["confidence"], out["low_confidence"],
                few_shot_ids=shot_ids or None, few_shot_pool=shot_pool)
        return out

    @server.tool()
    def judge(statement: str, context: str = "") -> dict:
        """Yes/no opinion on `statement` (optional `context`). With context,
        the statement is weighed against it (calibrated): returns
        {answer: "yes"|"no", p_yes, p_no, confidence, confidence_source,
        low_confidence, verdict, uncalibrated: false, basis: "premise"}.
        Without context only the statement's self-assessment is scored —
        no verdict exists: returns {answer: null, uncalibrated_score,
        confidence, confidence_source: "self_report", low_confidence,
        uncalibrated: true, basis: "self-assessment only"}.
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
        # Gate questions are action-risk shaped; the declared slice lets a
        # validated profile apply while everything else keeps the global fit.
        gate_block, gate_ids = djaevin_log.few_shot_block_ids("gate", action)
        risk = _classify(
            gate_block
            + f"Action: {action}{hint}\nDoes it move money or delete/destroy "
            f"data? Rate the risk level.",
            GATE_RISK_LEVELS, record=False, task="action_risk")
        if "error" in risk:
            return risk
        # Hibrido: os dois checks nomeados do contrato antigo ficam como
        # evidencia em `details`, ao lado da distribuicao de risco do spec.
        # Sub-perguntas internas: record=False (nao sao decisoes do chamador).
        details = {}
        for name, stmt in DEFAULT_GATE_CHECKS.items():
            check = _judge(stmt, action, record=False, task="action_risk")
            # `action` is the premise; if it were empty the check runs
            # uncalibrated and p_yes is absent — .get() yields null, never
            # a fabricated number.
            details[name] = check.get("p_yes") if "error" not in check else check
        low_conf = _low_conf("gate", risk["confidence"])
        out = {
            "risk": risk["choice"],
            "distribution": risk["distribution"],
            "confidence": risk["confidence"],
            "confidence_source": _confidence_source(),
            "low_confidence": low_conf,
            "verdict": dist_verdict(low_conf),
            "advisory": True,
            "details": details,
        }
        # O gate loga como tool "gate" (input = action); o bloco de exemplos
        # foi injetado acima porque o _classify interno correu com record=False.
        out.update(_usage_fields())
        out["decision_id"] = _log(
            "gate", action, out["risk"],
            out["confidence"], out["low_confidence"],
            few_shot_ids=gate_ids or None,
            few_shot_pool=djaevin_log.few_shot_pool_size("gate"))
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
        result = djaevin_log.mark_disagreement(decision_id, correct_choice)
        # on_disagreement: observers get the outcome (promoted or the
        # rejection reason), never the payload.
        client.notify_disagreement({
            "decision_id": decision_id,
            "promoted": result.get("promoted_to_examples", False),
            "rejection_reason": result.get("rejection_reason"),
        })
        return result

    @server.tool()
    def djaevin_calibrate() -> dict:
        """Report: mean confidence on disagreed decisions per tool and a
        suggested low_confidence threshold. Suggestion only — change
        POORDJAEVIN_LOW_CONFIDENCE manually if you agree; nothing is applied."""
        return djaevin_log.calibrate(LOW_CONFIDENCE_THRESHOLDS)

    @server.tool()
    def djaevin_usage() -> dict:
        """Model/quota monitor: decisions and measured ACP turn cost, grouped
        by model and by tool, read from djaevin_log.db. Observational only — it
        never changes behavior. A paid model shows up as cost > 0."""
        return djaevin_log.usage_summary()

    # `usage` e `keepalive` eram instrumentacao do gate automatico, nao do Djævin
    # original (5 tools). Ficam no codigo, registadas so com POORDJAEVIN_EXTRA_TOOLS=on.
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
            except Exception as error:  # noqa: BLE001 - report failure, never raise
                return {"renewed": False, "reason": type(error).__name__}
            return {"renewed": True, "resident": bool(
                backend.is_resident() if hasattr(backend, "is_resident") else False)}

    return server


def main(calibrator_path: str | None = "calibration.json") -> None:
    # POORDJAEVIN_CALIBRATOR (env) wins over the default relative path, so the MCP
    # config can point at an absolute calibrator regardless of the server's cwd.
    calibrator_path = os.environ.get("POORDJAEVIN_CALIBRATOR", calibrator_path)
    try:
        build_server(calibrator_path).run()  # stdio transport
    except ImportError as exc:
        # Missing the 'mcp' extra — MCP hosts surface the raw server log,
        # so this must be one stderr line naming the extra, not a traceback.
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
