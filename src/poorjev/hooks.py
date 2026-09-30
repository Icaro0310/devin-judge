"""INFRAESTRUTURA DE ENFORCEMENT — DESATIVADA, NAO APAGADA.

Este modulo era a politica do gate automatico (classificacao deterministica,
cache de veredictos, aprovacoes one-shot, fail-closed). Nao faz parte do Jevin
original: o Jevin e um MCP server opt-in com 5 tools que respondem e devolvem
confianca; nada bloqueia.

Nada aqui e importado pelo mcp_server. Fica inerte enquanto os hooks estiverem
desativados (ver .devin/JEV-ENFORCEMENT-DISABLED.md).

Reativar: repor os hooks (hooks.v1.json.jev-disabled -> hooks.v1.json) e
relancar daemon + tarefa + VBS. O codigo esta intacto.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path


ABSTAIN = "ABSTAIN"
APPROVAL_TTL_SECONDS = 600
VERDICT_TTL_SECONDS = 600
# Measured cold load on the VM backend is 7.4 s, so a 5 s budget was shorter
# than the infrastructure it wraps and manufactured false positives: 4 timeout
# blocks on benign `memory retain` / `vault write` calls in real use. The budget
# must cover a cold model, otherwise it flags healthy backends as broken.
GATE_BUDGET_SECONDS = 12.0
_CONFIRM_RE = re.compile(r"^\s*JEV_CONFIRM\s+([a-f0-9]{16})\s*$", re.IGNORECASE)
_BATCH_PREFIX_RE = re.compile(r"^\s*JEV_BATCH\s+", re.IGNORECASE)
_REQUEST_PREFIX_RE = re.compile(
    r"^\s*(?:JEV\s+)?(classify|classifique|classificar|categorize|judge|julgue|rate|score|avalie|decide|decida|gate)\s*:?[ \t]*",
    re.IGNORECASE,
)
_TOOL_NAMES = {
    "classify": "classify",
    "classifique": "classify",
    "classificar": "classify",
    "categorize": "classify",
    "judge": "judge",
    "julgue": "judge",
    "rate": "rate",
    "score": "rate",
    "avalie": "rate",
    "decide": "decide",
    "decida": "decide",
    "gate": "gate",
}
_DELETE_RE = re.compile(
    r"\brm\s+-(?=[^\s]*r)(?=[^\s]*f)[^\s]+|"
    r"\b(?:rmdir|remove-item|remove|del|erase|delete|destroy|truncate|purge|wipe|shred|unlink|"
    r"apagar|excluir|eliminar|remover|destruir|truncar|esvaziar|formatar)\b|"
    r"\bdrop\s+(?:table|database|schema|collection|index)\b|"
    r"\bremove-item\b(?=.*\b-recurse\b)(?=.*\b-force\b)",
    re.IGNORECASE | re.DOTALL,
)
_MONEY_RE = re.compile(
    r"\b(?:transfer|transferir|wire|refund|refundir|reembolsar|estornar|charge|cobrar|withdraw|sacar|payout|disburse|payment|pagamento|pagar)\b|"
    r"\b(?:send|move|enviar|movimentar)\s+(?:money|funds|cash|payment|balance|dinheiro|fundos|saldo)\b",
    re.IGNORECASE,
)
_MUTATING_TOOLS = {"exec", "write", "edit", "notebook_edit", "write_to_process"}
_READ_ONLY_MCP_SUFFIX = re.compile(
    r"(?:^|_)(?:read|list|search|recall|status|health|describe|get|fetch|query)$",
    re.IGNORECASE,
)

# Deterministic first pass. These run before any model call: a match decides the
# gate for free. Anything that matches neither list is "ambiguous" and pays one
# binary model call.
# A destructive verb counts when it is a *command* — at the start of a shell
# segment — not when the word merely appears inside prose, a grep pattern or a
# commit message. That distinction is what keeps this deterministic pass from
# blocking `grep -rn 'remove-item' docs`.
_DESTRUCTIVE_SEGMENT_RE = re.compile(
    # A backtick counts as a segment boundary because action descriptions write
    # commands inline as `DROP TABLE users;`. Single/double quotes deliberately
    # do NOT, so `git commit -m "drop table support"` stays benign.
    r"(?:^|[;&|(\n`])\s*(?:"
    r"rm|del|erase|rd|rmdir|remove|remove-item|purge|wipe|shred|unlink|"
    r"format|mkfs(?:\.[a-z0-9]+)?|diskpart|bcdedit|fdisk|"
    r"shutdown|reboot|halt|poweroff|dd|"
    r"drop|truncate|delete|"
    r"chmod|chown|reg\s+delete|sc\s+delete|taskkill|net\s+user|"
    r"apagar|eliminar|excluir|remover|destruir|formatar|esvaziar"
    r")\b",
    re.IGNORECASE,
)
# "command + destructive flag" shapes are unambiguous signals wherever they
# appear (including behind `cmd /c`), and unlike a bare verb they do not collide
# with prose, so these stay unanchored.
_DESTRUCTIVE_FLAG_RE = re.compile(
    r"\brm\s+-(?=[^\s]*r)(?=[^\s]*f)[^\s]+|"
    r"\b(?:del|erase)\s+/[fsq]\b|"
    r"\brd\s+/[sq]\b|"
    r"\brmdir\s+/[sq]\b|"
    r"\bremove-item\b(?=[^\n]*-recurse)(?=[^\n]*-force)|"
    r"\bdd\s+(?:if|of)=|"
    r"\bnet\s+user\b[^\n]*/delete|"
    r"\btaskkill\b[^\n]*/f\b",
    re.IGNORECASE,
)
_FORCE_GIT_RE = re.compile(
    r"\bgit\s+push\b[^;|&]*(?:--force|-f\b)|"
    r"\bgit\s+reset\s+--hard\b|"
    r"\bgit\s+clean\s+-[a-z]*f\b|"
    r"\bgit\s+checkout\s+--\s",
    re.IGNORECASE,
)
_DB_CLIENT_RE = re.compile(
    r"\b(?:sqlite3|psql|mysql|mariadb|sqlcmd|sqlplus|mongosh?|redis-cli)\b", re.IGNORECASE
)
_SQL_DESTRUCTIVE_RE = re.compile(
    r"\b(?:drop|truncate)\s+(?:table|database|schema|collection|index)\b|\bdelete\s+from\b",
    re.IGNORECASE,
)
_DEVICE_WRITE_RE = re.compile(r">\s*/dev/(?:sd|nvme|hd)", re.IGNORECASE)
_SAFE_CMD_RE = re.compile(
    r"^(?:"
    r"ls|dir|pwd|whoami|hostname|date|echo|cat|type|head|tail|wc|stat|du|df|tree|file|sort|uniq|cut|which|where|"
    r"git\s+(?:status|diff|log|show|branch|remote|rev-parse|describe|ls-files|blame|shortlog|tag)(?:\s|$)|"
    r"(?:node|python|python3|npm|pip|flutter|dart|cargo|go)\s+--version(?:\s|$)|"
    r"pytest(?:\s|$)|ruff\s+check(?:\s|$)|mypy(?:\s|$)|"
    r"grep(?:\s|$)|rg(?:\s|$)|"
    r"gh\s+(?:pr|issue|repo|run)\s+(?:list|view|status|diff|checks)(?:\s|$)"
    r")",
    re.IGNORECASE,
)
_SHELL_CHAIN_RE = re.compile(r"[;|&><`$]|\n")
_BINARY_DATA_RE = re.compile(r"\.(?:db|sqlite|sqlite3)\b", re.IGNORECASE)


def classify_action(tool_name: str, action: str) -> tuple[str, str]:
    """Return (kind, reason) where kind is block | allow | ambiguous.

    Patterns run against the action text only (never the tool name), so the
    segment anchors mean what they say.
    """
    if (_DESTRUCTIVE_SEGMENT_RE.search(action) or _DESTRUCTIVE_FLAG_RE.search(action)
            or _FORCE_GIT_RE.search(action) or _DEVICE_WRITE_RE.search(action)):
        return "block", "deterministic pattern: destructive or irreversible action"
    # Money words stay unanchored on purpose: "Send a $5 refund to the customer"
    # is a natural-language action, not a shell command, and money movement is
    # the highest-severity category. The cost is conservative false positives.
    if _MONEY_RE.search(action):
        return "block", "deterministic pattern: money movement"
    if _DB_CLIENT_RE.search(action) and _SQL_DESTRUCTIVE_RE.search(action):
        return "block", "deterministic pattern: destructive SQL through a database client"
    if tool_name in {"write", "edit", "notebook_edit"}:
        target = action.lower()
        if _BINARY_DATA_RE.search(target) or target.rstrip().endswith(".sql"):
            return "block", "deterministic pattern: database or migration target"
    if tool_name == "exec":
        stripped = action.strip()
        if stripped and not _SHELL_CHAIN_RE.search(stripped) and _SAFE_CMD_RE.match(stripped):
            return "allow", "deterministic allowlist: read-only command"
    return "ambiguous", ""


def verdict_cache_key(tool_name: str, action: str) -> str:
    return hashlib.sha256(f"{tool_name}\0{action}".encode("utf-8")).hexdigest()


def _nonempty_string(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_request(tool: str, arguments: dict) -> dict:
    if not isinstance(arguments, dict):
        raise ValueError("JEV request payload must be a JSON object")
    if tool == "classify":
        _nonempty_string(arguments.get("text"), "text")
        options = arguments.get("options")
        if not isinstance(options, list) or len(options) < 2 or any(not isinstance(x, str) or not x for x in options):
            raise ValueError("classify requires at least two non-empty string options")
    elif tool == "judge":
        _nonempty_string(arguments.get("text"), "text")
        _nonempty_string(arguments.get("statement"), "statement")
    elif tool == "rate":
        _nonempty_string(arguments.get("text"), "text")
        levels = arguments.get("levels")
        if not isinstance(levels, list) or len(levels) < 2 or any(not isinstance(x, str) or not x for x in levels):
            raise ValueError("rate requires at least two non-empty string levels")
    elif tool == "decide":
        _nonempty_string(arguments.get("state"), "state")
        if not isinstance(arguments.get("questions"), dict) or not arguments["questions"]:
            raise ValueError("decide requires a non-empty questions object")
        arguments = {**arguments, "questions": _without_gold(arguments["questions"])}
    elif tool == "gate":
        _nonempty_string(arguments.get("action"), "action")
    return arguments


def _without_gold(questions: dict) -> dict:
    clean = {}
    for name, spec in questions.items():
        if not isinstance(spec, dict):
            raise ValueError(f"question {name!r} must be an object")
        clean[name] = {key: value for key, value in spec.items() if key not in {"gold", "expected"}}
    return clean


def parse_decision_prompt(prompt: str) -> dict | None:
    if not isinstance(prompt, str):
        return None
    decoder = json.JSONDecoder()
    match = _BATCH_PREFIX_RE.match(prompt)
    if match:
        payload, _ = decoder.raw_decode(prompt[match.end():].lstrip())
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise ValueError("JEV_BATCH requires a non-empty items array")
        clean_items = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"JEV_BATCH item {index} must be an object")
            state = _nonempty_string(item.get("state"), f"items[{index}].state")
            questions = item.get("questions")
            if not isinstance(questions, dict) or not questions:
                raise ValueError(f"JEV_BATCH item {index} requires questions")
            clean_items.append({
                "id": str(item.get("id", index)),
                "state": state,
                "questions": _without_gold(questions),
            })
        return {"tool": "batch", "items": clean_items}

    match = _REQUEST_PREFIX_RE.match(prompt)
    if not match:
        return None
    tool = _TOOL_NAMES[match.group(1).lower()]
    arguments, _ = decoder.raw_decode(prompt[match.end():].lstrip())
    return {"tool": tool, "arguments": _validate_request(tool, arguments)}


def _context(text: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


def handle_user_prompt(event: dict, call_tool, approvals: ApprovalStore) -> dict:
    session_id = str(event.get("session_id") or "")
    prompt = event.get("prompt", "")
    confirmation = _CONFIRM_RE.fullmatch(prompt) if isinstance(prompt, str) else None
    if confirmation:
        granted = approvals.grant(session_id, confirmation.group(1))
        if not granted:
            return _context("JEV_CONFIRM rejected: this session id or fingerprint is invalid.")
        return _context("JEV explicit one-time approval recorded. Retry only the exact blocked tool call in this session.")

    try:
        request = parse_decision_prompt(prompt)
    except (ValueError, json.JSONDecodeError) as error:
        return _context(f"JEV typed request rejected: {error}. No decision was run.")
    if request is None:
        return {}

    try:
        if request["tool"] == "batch":
            results = []
            for item in request["items"]:
                answer = call_tool("decide", {"state": item["state"], "questions": item["questions"]})
                results.append({"id": item["id"], "answers": answer})
            result = {"items": results}
        else:
            result = call_tool(request["tool"], request["arguments"])
    except Exception as error:
        return _context(
            f"JEV AUTOMATIC DECISION FAILED ({type(error).__name__}). Do not substitute an unverified decision; retry after jev-local is healthy."
        )

    return _context(
        "JEV decision is mandatory for this structured request. Use the tool result verbatim; do not independently infer or override it. If a value is ABSTAIN, return ABSTAIN and do not guess.\n"
        "AUTOMATIC JEV DECISION (invoked by UserPromptSubmit hook; this is the tool result):\n"
        + json.dumps(result, ensure_ascii=False, sort_keys=True)
    )


def _is_mutating_tool(tool_name: str) -> bool:
    if tool_name in _MUTATING_TOOLS:
        return True
    if tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        if len(parts) >= 3 and parts[1] == "jev-local":
            return False
        suffix = parts[-1] if parts else ""
        return not bool(_READ_ONLY_MCP_SUFFIX.search(suffix))
    return False


def _action_text(tool_name: str, tool_input: dict) -> str:
    if tool_name == "exec":
        command = tool_input.get("command")
        return command if isinstance(command, str) and command else json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    if tool_name in {"write", "edit", "notebook_edit"}:
        target = tool_input.get("file_path") or tool_input.get("path") or "<unknown target>"
        content = tool_input.get("content") or tool_input.get("new_string") or tool_input.get("new_source")
        if str(target).lower().endswith(".sql") and isinstance(content, str):
            return f"{tool_name} target: {target}\n{content[:5000]}"
        return f"{tool_name} target: {target}"
    if tool_name == "write_to_process":
        return json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    return f"{tool_name} {json.dumps(tool_input, ensure_ascii=False, sort_keys=True)}"


def _explicit_risk(tool_name: str, action: str) -> bool:
    kind, _ = classify_action(tool_name, action)
    return kind == "block"


def action_fingerprint(session_id: str, tool_name: str, tool_input: dict) -> str:
    canonical = json.dumps(
        {"tool_name": tool_name, "tool_input": tool_input},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(f"{session_id}\0{canonical}".encode("utf-8")).hexdigest()
    return digest[:16]


def evaluate_action(tool_name: str, action: str, call_tool, cache: "VerdictCache | None" = None) -> dict:
    """Decide one action: deterministic pattern first, one binary model call only
    when ambiguous. Model verdicts and deterministic verdicts are cached;
    failures (timeout / unavailable) are never cached, so a dead backend cannot
    pin a stale verdict."""
    key = verdict_cache_key(tool_name, action)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return {**cached, "cached": True}

    kind, why = classify_action(tool_name, action)
    if kind == "block":
        verdict = {"block": True, "source": "deterministic", "reason": why}
    elif kind == "allow":
        verdict = {"block": False, "source": "deterministic", "reason": why}
    else:
        try:
            raw = call_tool("gate", {"action": action})
            if not isinstance(raw, dict) or not isinstance(raw.get("block"), bool):
                raise ValueError("gate returned an invalid decision")
            verdict = {"block": raw["block"], "source": "model", "details": raw.get("details", {})}
        except TimeoutError:
            return {"block": True, "source": "timeout", "reason": "Jev gate exceeded the latency budget"}
        except Exception as error:
            return {"block": True, "source": "unavailable", "reason": f"Jev gate unavailable ({type(error).__name__})"}

    if cache is not None:
        cache.put(key, verdict)
    return verdict


def handle_pretool(event: dict, call_tool, approvals: ApprovalStore,
                   cache: "VerdictCache | None" = None) -> dict:
    tool_name = str(event.get("tool_name") or "")
    if not _is_mutating_tool(tool_name):
        return {}
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    session_id = str(event.get("session_id") or "")
    action = _action_text(tool_name, tool_input)
    fingerprint = action_fingerprint(session_id, tool_name, tool_input)

    verdict = evaluate_action(tool_name, action, call_tool, cache)
    if not verdict["block"]:
        return {}
    if approvals.consume(session_id, fingerprint):
        return {"decision": "approve", "reason": "One-time user confirmation matched this exact tool call."}
    reason = {
        "deterministic": verdict.get("reason", "destructive action"),
        "model": "Jev gate identified or could not rule out money movement/data deletion.",
        "timeout": f"Jev gate exceeded the {GATE_BUDGET_SECONDS:.0f}s budget for a mutating action.",
        "unavailable": verdict.get("reason", "Jev gate unavailable."),
    }.get(verdict["source"], "Jev gate blocked this action.")
    return {
        "decision": "block",
        "reason": f"{reason} No action was executed. To confirm this exact call once, submit: JEV_CONFIRM {fingerprint}",
    }


class ApprovalStore:
    def __init__(self, path: str, ttl_seconds: int = APPROVAL_TTL_SECONDS):
        self.path = path
        self.ttl_seconds = ttl_seconds
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS approvals ("
            "session_id TEXT NOT NULL, fingerprint TEXT NOT NULL, expires_at REAL NOT NULL, "
            "PRIMARY KEY (session_id, fingerprint))"
        )
        self.connection.commit()

    def grant(self, session_id: str, fingerprint: str, now: float | None = None) -> bool:
        if not session_id or not re.fullmatch(r"[a-f0-9]{16}", fingerprint, re.IGNORECASE):
            return False
        expires_at = (time.time() if now is None else now) + self.ttl_seconds
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO approvals(session_id, fingerprint, expires_at) VALUES (?, ?, ?)",
                (session_id, fingerprint.lower(), expires_at),
            )
        return True

    def consume(self, session_id: str, fingerprint: str, now: float | None = None) -> bool:
        if not session_id:
            return False
        timestamp = time.time() if now is None else now
        with self.connection:
            row = self.connection.execute(
                "SELECT expires_at FROM approvals WHERE session_id = ? AND fingerprint = ?",
                (session_id, fingerprint.lower()),
            ).fetchone()
            self.connection.execute(
                "DELETE FROM approvals WHERE session_id = ? AND fingerprint = ?",
                (session_id, fingerprint.lower()),
            )
        return bool(row and row[0] >= timestamp)

    def close(self) -> None:
        self.connection.close()


class VerdictCache:
    """Persistent verdict cache, shared across the one-shot hook processes.

    The hook spawns a fresh Python process per tool call, so an in-memory cache
    would never hit. This lives in the same SQLite file as the approvals and
    survives across invocations, which is what makes repeated identical actions
    free.
    """

    def __init__(self, path: str, ttl_seconds: int = VERDICT_TTL_SECONDS):
        self.path = path
        self.ttl_seconds = ttl_seconds
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS verdicts ("
            "key TEXT PRIMARY KEY, verdict TEXT NOT NULL, expires_at REAL NOT NULL)"
        )
        self.connection.commit()

    def get(self, key: str, now: float | None = None) -> dict | None:
        timestamp = time.time() if now is None else now
        row = self.connection.execute(
            "SELECT verdict, expires_at FROM verdicts WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row[1] < timestamp:
            with self.connection:
                self.connection.execute("DELETE FROM verdicts WHERE key = ?", (key,))
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def put(self, key: str, verdict: dict, now: float | None = None) -> None:
        expires_at = (time.time() if now is None else now) + self.ttl_seconds
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO verdicts(key, verdict, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(verdict, sort_keys=True), expires_at),
            )

    def close(self) -> None:
        self.connection.close()
