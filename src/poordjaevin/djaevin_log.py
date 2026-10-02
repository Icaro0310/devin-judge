"""djaevin_log — auto-aprendizagem do Djævin (SQLite append-only, stdlib only).

Camada dos Niveis 1 e 2 do plano de melhoria no caso de uso legitimo
(decisoes triviais em volume):

- loga cada decisao bem-sucedida das 5 tools em djaevin_decisions;
- regista disagreements do Devin e promove-os a exemplos validados;
- serve few-shot dinamico (similaridade Jaccard, sem embeddings) nas
  chamadas seguintes;
- djaevin_calibrate() sugere limiares de low_confidence por tool — so sugere,
  nunca aplica.

Nada aqui bloqueia, forca ou decide: a camada observa e sugere. Falhas da DB
sao fail-open — um problema de log nunca impede uma decisao de ser devolvida.

Config: POORDJAEVIN_LOG_DB (caminho do ficheiro; default = raiz do projeto).
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS djaevin_decisions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT    NOT NULL,
  tool        TEXT    NOT NULL,
  input_text  TEXT    NOT NULL,
  choice      TEXT    NOT NULL,
  confidence  REAL    NOT NULL,
  low_conf    INTEGER NOT NULL,
  disagreed   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS djaevin_examples (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  tool            TEXT NOT NULL,
  input_text      TEXT NOT NULL,
  correct_choice  TEXT NOT NULL,
  added_ts        TEXT NOT NULL
);
"""

# Colunas adicionadas depois (monitor de modelo/custo ACP). ALTER tolerante:
# falhar com "duplicate column" so significa que ja existe.
_MIGRATIONS = [
    "ALTER TABLE djaevin_decisions ADD COLUMN model TEXT",
    "ALTER TABLE djaevin_decisions ADD COLUMN cost REAL",
]


def _db_path() -> str:
    env = os.environ.get("POORDJAEVIN_LOG_DB")
    if env:
        return env
    # vendor/poordjaevin/src/poordjaevin/djaevin_log.py -> 4 niveis acima = raiz do projeto
    here = os.path.dirname(os.path.abspath(__file__))
    for _ in range(4):
        here = os.path.dirname(here)
    return os.path.join(here, "djaevin_log.db")


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(_db_path())
    con.executescript(_SCHEMA)
    for stmt in _MIGRATIONS:
        try:
            con.execute(stmt)
        except sqlite3.OperationalError:
            pass  # coluna ja existe
    return con


def log_decision(tool: str, input_text: str, choice: str,
                 confidence: float, low_conf: bool,
                 model: str | None = None,
                 cost: float | None = None) -> int | None:
    """Insert one decision row; return its id (None when the log fails —
    fail-open, the decision itself already went out). `model`/`cost` alimentam
    o monitor de quota (backend ACP); ficam NULL no backend Ollama."""
    try:
        with _connect() as con:
            cur = con.execute(
                "INSERT INTO djaevin_decisions "
                "(ts, tool, input_text, choice, confidence, low_conf, "
                " model, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (datetime.now(timezone.utc).isoformat(), tool, input_text,
                 str(choice), float(confidence), 1 if low_conf else 0,
                 model, cost))
            return cur.lastrowid
    except (OSError, sqlite3.Error):
        return None


def mark_disagreement(decision_id: int, correct_choice: str = "") -> dict:
    """Mark a logged decision as wrong; promote to djaevin_examples when a
    correct_choice is supplied."""
    try:
        with _connect() as con:
            cur = con.execute(
                "UPDATE djaevin_decisions SET disagreed = 1 WHERE id = ?",
                (decision_id,))
            if cur.rowcount == 0:
                return {"ok": False, "error": f"decision_id {decision_id} not found"}
            promoted = False
            if correct_choice:
                row = con.execute(
                    "SELECT tool, input_text FROM djaevin_decisions WHERE id = ?",
                    (decision_id,)).fetchone()
                if row:
                    con.execute(
                        "INSERT INTO djaevin_examples "
                        "(tool, input_text, correct_choice, added_ts) "
                        "VALUES (?, ?, ?, ?)",
                        (row[0], row[1], str(correct_choice),
                         datetime.now(timezone.utc).isoformat()))
                    promoted = True
        return {"ok": True, "promoted_to_examples": promoted}
    except (OSError, sqlite3.Error) as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def jaccard(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta and not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def get_few_shot(tool: str, input_text: str, n: int = 3) -> list[dict]:
    """The n validated examples for `tool` most similar to `input_text`
    (Jaccard over whitespace tokens). Empty list when nothing is stored or
    the DB is unavailable."""
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT input_text, correct_choice FROM djaevin_examples "
                "WHERE tool = ?", (tool,)).fetchall()
    except (OSError, sqlite3.Error):
        return []
    scored = sorted(rows, key=lambda r: jaccard(input_text, r[0]),
                    reverse=True)
    return [{"input": r[0], "choice": r[1]} for r in scored[:n]]


def few_shot_block(tool: str, input_text: str) -> str:
    """Text block to prepend to the premise, or "" when no examples exist —
    an empty block is never injected."""
    examples = get_few_shot(tool, input_text)
    if not examples:
        return ""
    lines = ["# Exemplos validados anteriormente:"]
    for ex in examples:
        lines.append(f'# Input: "{ex["input"]}" -> Correto: "{ex["choice"]}"')
    lines.append("# ---")
    return "\n".join(lines) + "\n"


def calibrate(current_threshold) -> dict:
    """Per-tool report over disagreed decisions: mean confidence on errors
    and a suggested low_confidence threshold. Suggestion only — never
    applied automatically. `current_threshold` may be a float or a per-tool
    dict ({"rate": 0.75, "default": 0.6})."""
    if isinstance(current_threshold, dict):
        per_tool = {str(k): float(v) for k, v in current_threshold.items()}
        default = per_tool.get("default", 0.6)
    else:
        per_tool, default = {}, float(current_threshold)
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT tool, AVG(confidence), COUNT(*) FROM djaevin_decisions "
                "WHERE disagreed = 1 GROUP BY tool").fetchall()
    except (OSError, sqlite3.Error) as error:
        return {"error": f"{type(error).__name__}: {error}"}
    reports = [{
        "tool": tool,
        "error_count": count,
        "mean_confidence_on_errors": round(mean_conf, 4),
        "suggested_threshold": min(0.95, round(mean_conf + 0.2, 2)),
        "current_threshold": per_tool.get(tool, default),
    } for tool, mean_conf, count in rows]
    return {
        "reports": reports,
        "total_disagreements": sum(r["error_count"] for r in reports),
    }


def usage_summary() -> dict:
    """Monitor de modelo/quota: totais por modelo e por tool, a partir das
    colunas model/cost (NULL no backend Ollama — so ACP mede custo)."""
    try:
        with _connect() as con:
            per_model = con.execute(
                "SELECT COALESCE(model, '(sem modelo)'), COUNT(*), "
                "COALESCE(SUM(cost), 0) FROM djaevin_decisions "
                "GROUP BY model").fetchall()
            per_tool = con.execute(
                "SELECT tool, COUNT(*), COALESCE(SUM(cost), 0) "
                "FROM djaevin_decisions GROUP BY tool").fetchall()
            total = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(cost), 0), "
                "SUM(CASE WHEN cost > 0 THEN 1 ELSE 0 END) "
                "FROM djaevin_decisions").fetchone()
    except (OSError, sqlite3.Error) as error:
        return {"error": f"{type(error).__name__}: {error}"}
    return {
        "total_decisions": total[0],
        "total_cost": round(total[1], 4),
        "paid_decisions": int(total[2] or 0),
        "by_model": [{"model": m, "decisions": n, "cost": round(c, 4)}
                     for m, n, c in per_model],
        "by_tool": [{"tool": t, "decisions": n, "cost": round(c, 4)}
                    for t, n, c in per_tool],
    }
