"""Unit tests for djaevin_log: SQLite decision log, disagreement promotion,
Jaccard few-shot retrieval, and the calibrate report. Stdlib only — no
model, no server, tmp DB per test."""

from pathlib import Path

import pytest

from poordjaevin import djaevin_log


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_LOG_DB", str(tmp_path / "djaevin_log.db"))
    return tmp_path / "djaevin_log.db"


def test_default_log_path_uses_xdg_data_home(tmp_path):
    path = djaevin_log._db_path(
        platform="linux", env={"XDG_DATA_HOME": str(tmp_path)}, home=tmp_path
    )
    assert Path(path) == tmp_path / "poordjaevin" / "djaevin_log.db"


def test_windows_log_path_uses_local_app_data(tmp_path):
    path = djaevin_log._db_path(
        platform="win32", env={"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}, home=tmp_path
    )
    assert path.replace("\\", "/").startswith("C:/Users/Test/AppData/Local/")
    assert path.replace("\\", "/").endswith("/poordjaevin/djaevin_log.db")


def test_log_path_override_wins(tmp_path):
    custom = tmp_path / "custom.db"
    assert djaevin_log._db_path(
        platform="linux", env={"POORDJAEVIN_LOG_DB": str(custom)}, home=tmp_path
    ) == str(custom)


def test_log_decision_creates_db_and_returns_id(db):
    decision_id = djaevin_log.log_decision("judge", "The sky is blue.", "yes", 0.9, False)
    assert decision_id is not None
    assert db.exists()
    decision_id_2 = djaevin_log.log_decision("rate", "Um texto.", "3", 0.5, True)
    assert decision_id_2 > decision_id


def test_mark_disagreement_promotes_to_examples(db):
    decision_id = djaevin_log.log_decision(
        "classify", "Servico terrivel, nunca mais volto.", "bom", 0.44, True)
    out = djaevin_log.mark_disagreement(decision_id, "mau")
    assert out == {"ok": True, "promoted_to_examples": True,
                   "rejection_reason": None}
    examples = djaevin_log.get_few_shot("classify", "servico horrivel")
    assert examples[0]["choice"] == "mau"


def test_mark_disagreement_without_correct_choice(db):
    decision_id = djaevin_log.log_decision("judge", "2+2=4", "no", 0.9, False)
    out = djaevin_log.mark_disagreement(decision_id)
    assert out == {"ok": True, "promoted_to_examples": False,
                   "rejection_reason": "no_ground_truth"}
    out = djaevin_log.mark_disagreement(9999)
    assert out["ok"] is False


def test_few_shot_block_empty_when_no_examples(db):
    assert djaevin_log.few_shot_block("gate", "qualquer coisa") == ""


def test_few_shot_block_format(db):
    decision_id = djaevin_log.log_decision(
        "judge", "Chove sempre em Lisboa.", "yes", 0.8, False)
    djaevin_log.mark_disagreement(decision_id, "no")
    block = djaevin_log.few_shot_block("judge", "Chove em Lisboa?")
    assert "Exemplos validados" in block
    assert 'Correto: "no"' in block
    assert block.endswith("---\n")


def test_few_shot_is_scoped_per_tool(db):
    decision_id = djaevin_log.log_decision(
        "rate", "texto de exemplo suficiente", "1", 0.5, False)
    djaevin_log.mark_disagreement(decision_id, "5")
    assert djaevin_log.get_few_shot("judge", "texto x") == []
    assert djaevin_log.get_few_shot(
        "rate", "texto de exemplo")[0]["choice"] == "5"


def test_calibrate_reports_disagreements(db):
    bad = djaevin_log.log_decision("rate", "a", "5", 0.4, False)
    djaevin_log.log_decision("rate", "b", "3", 0.9, False)
    djaevin_log.mark_disagreement(bad, "1")
    out = djaevin_log.calibrate(0.6)
    assert out["total_disagreements"] == 1
    report = out["reports"][0]
    assert report["tool"] == "rate"
    assert report["mean_confidence_on_errors"] == 0.4
    assert report["suggested_threshold"] == 0.6
    assert report["current_threshold"] == 0.6


def test_calibrate_empty_is_valid(db):
    out = djaevin_log.calibrate(0.6)
    assert out["reports"] == []
    assert out["total_disagreements"] == 0


def test_calibrate_per_tool_threshold_map(db):
    bad = djaevin_log.log_decision("rate", "a", "5", 0.4, False)
    djaevin_log.mark_disagreement(bad, "1")
    out = djaevin_log.calibrate({"rate": 0.75, "default": 0.6})
    assert out["reports"][0]["current_threshold"] == 0.75
    bad = djaevin_log.log_decision("judge", "b", "no", 0.4, False)
    djaevin_log.mark_disagreement(bad, "yes")
    out = djaevin_log.calibrate({"rate": 0.75, "default": 0.6})
    judge_report = next(r for r in out["reports"] if r["tool"] == "judge")
    assert judge_report["current_threshold"] == 0.6  # fallback para default


def test_parse_thresholds(monkeypatch):
    from poordjaevin.mcp_server import (
        _low_conf,
        _parse_thresholds,
    )
    assert _parse_thresholds("0.7") == {"default": 0.7}
    assert _parse_thresholds('{"rate": 0.75, "default": 0.5}') == {
        "rate": 0.75, "default": 0.5}
    assert _parse_thresholds("lixo") == {"default": 0.6}
    # _low_conf usa o mapa de modulo (default 0.6 quando a env nao esta set)
    assert _low_conf("gate", 0.5) is True
    assert _low_conf("gate", 0.8) is False


def test_usage_summary_tracks_model_and_cost(db):
    djaevin_log.log_decision("judge", "a", "yes", 0.9, False,
                         model="swe-2", cost=0.0)
    djaevin_log.log_decision("judge", "b", "no", 0.9, False,
                         model="paid-model", cost=3.5)
    djaevin_log.log_decision("classify", "c", "x", 0.9, False)  # backend sem custo
    out = djaevin_log.usage_summary()
    assert out["total_decisions"] == 3
    assert out["total_cost"] == 3.5
    assert out["paid_decisions"] == 1
    paid = next(m for m in out["by_model"] if m["model"] == "paid-model")
    assert paid["cost"] == 3.5


# --- E2E: ciclo de aprendizagem incremental --------------------------------
# Cadeia auditada: decisao -> desacordo -> candidato -> gate de promocao ->
# djaevin_examples -> consumidor few-shot. Cobre os 6 casos exigidos pela
# auditoria do pipeline (2026-10-23): elegivel chega ao consumidor, triviais/
# fora-de-dominio/near-dup/sem-ground-truth rejeitados, idempotente,
# sem dados sensiveis fora do schema do exemplo.


def test_learning_cycle_e2e_eligible_reaches_consumer(db):
    """Positive eligible candidate traverses the whole chain and is served
    to the few-shot consumer — not just stored."""
    did = djaevin_log.log_decision(
        "judge", "The CLAIM below is supported by the provided context.",
        "yes", 0.95, False)
    out = djaevin_log.mark_disagreement(did, "no")
    assert out["promoted_to_examples"] is True
    assert out["rejection_reason"] is None
    # consumer side: a similar statement retrieves and injects the example
    block = djaevin_log.few_shot_block(
        "judge", "The CLAIM is supported by the provided context.")
    assert 'Correto: "no"' in block
    assert "Exemplos validados" in block


def test_learning_cycle_rejects_trivial_input(db):
    did = djaevin_log.log_decision("judge", "sim", "yes", 0.95, False)
    out = djaevin_log.mark_disagreement(did, "no")
    assert out["promoted_to_examples"] is False
    assert out["rejection_reason"] == "trivial"
    # o flag disagreed marca-se na mesma — so a promocao e bloqueada
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    assert con.execute(
        "SELECT disagreed FROM djaevin_decisions WHERE id = ?",
        (did,)).fetchone()[0] == 1
    con.close()


def test_learning_cycle_rejects_out_of_domain_choice(db):
    did = djaevin_log.log_decision(
        "judge", "The claim is supported by context here.", "yes", 0.95,
        False)
    out = djaevin_log.mark_disagreement(did, "maybe")
    assert out["rejection_reason"] == "out_of_domain"
    assert djaevin_log.get_few_shot("judge", "claim supported") == []


def test_learning_cycle_free_domain_tool_accepts_any_choice(db):
    """classify/rate/decide have no fixed domain — any non-empty choice is
    promotable (same rule as the second-pass curator)."""
    did = djaevin_log.log_decision(
        "classify", "Servico horrivel, nunca mais volto.", "bom", 0.9, False)
    out = djaevin_log.mark_disagreement(did, "pessimo")
    assert out["promoted_to_examples"] is True


def test_learning_cycle_rejects_near_duplicate(db):
    base = "The claim that backups ran is supported by the context."
    did1 = djaevin_log.log_decision("judge", base, "yes", 0.95, False)
    assert djaevin_log.mark_disagreement(did1, "no")[
        "promoted_to_examples"] is True
    # second decision with a near-identical input (Jaccard >= 0.9) is a
    # duplicate candidate, not new material
    did2 = djaevin_log.log_decision(
        "judge", base + " really", "yes", 0.95, False)
    out = djaevin_log.mark_disagreement(did2, "no")
    assert out["rejection_reason"] == "near_duplicate"
    assert len(djaevin_log.get_few_shot("judge", "backups")) == 1


def test_learning_cycle_idempotent_on_repeated_marks(db):
    did = djaevin_log.log_decision(
        "judge", "The claim is supported by the context provided.",
        "yes", 0.95, False)
    assert djaevin_log.mark_disagreement(did, "no")[
        "promoted_to_examples"] is True
    # re-marking the same decision hits the near-duplicate gate — the store
    # stays at one example, never stacks copies
    out = djaevin_log.mark_disagreement(did, "no")
    assert out["rejection_reason"] == "near_duplicate"
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    assert con.execute("SELECT COUNT(*) FROM djaevin_examples").fetchone()[
        0] == 1
    con.close()


def test_learning_cycle_example_schema_carries_no_extra_fields(db):
    """The promoted example row holds only (tool, input_text,
    correct_choice, added_ts) — no confidence, model, cost or premise
    material leaks into the few-shot store."""
    did = djaevin_log.log_decision(
        "judge", "The claim is supported by the given context.", "yes",
        0.95, False, model="swe-2", cost=0.0)
    djaevin_log.mark_disagreement(did, "no")
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    cols = [r[1] for r in con.execute(
        "PRAGMA table_info(djaevin_examples)").fetchall()]
    row = con.execute("SELECT * FROM djaevin_examples").fetchone()
    con.close()
    assert cols == ["id", "tool", "input_text", "correct_choice",
                    "added_ts", "source_decision_id"]
    assert len(row) == 6


def test_promotion_records_source_decision_id(db):
    did = djaevin_log.log_decision(
        "judge", "Backups completed last night per the log.", "no", 0.8,
        False)
    djaevin_log.mark_disagreement(did, "yes")
    examples = djaevin_log.get_few_shot("judge", "Backups completed")
    assert len(examples) == 1
    assert examples[0]["source_decision_id"] == did
    # provenance join resolves to the original wrong decision
    src = djaevin_log.get_decision(examples[0]["source_decision_id"])
    assert src is not None
    assert src["id"] == did
    assert src["choice"] == "no"
    assert src["disagreed"] is True
    assert src["tool"] == "judge"


def test_get_decision_returns_none_for_missing_id(db):
    assert djaevin_log.get_decision(99999) is None


def test_mark_disagreement_on_missing_decision_creates_no_example(db):
    out = djaevin_log.mark_disagreement(424242, "yes")
    assert out["ok"] is False
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    assert con.execute(
        "SELECT COUNT(*) FROM djaevin_examples").fetchone()[0] == 0
    con.close()


def test_legacy_example_without_source_keeps_null_provenance(db):
    """Pre-migration rows (or manually seeded examples) carry no
    source_decision_id — NULL, never a fabricated reference."""
    import sqlite3
    djaevin_log.get_few_shot("judge", "x")  # force schema creation
    con = sqlite3.connect(djaevin_log._db_path())
    con.execute(
        "INSERT INTO djaevin_examples "
        "(tool, input_text, correct_choice, added_ts) "
        "VALUES ('judge', 'Legacy seeded example.', 'yes', '2020-01-01')")
    con.commit()
    con.close()
    examples = djaevin_log.get_few_shot("judge", "Legacy seeded example.")
    assert examples[0]["source_decision_id"] is None


def test_remark_keeps_single_example_with_original_source(db):
    did = djaevin_log.log_decision(
        "judge", "Backups completed last night per the log.", "no", 0.8,
        False)
    assert djaevin_log.mark_disagreement(did, "yes")[
        "promoted_to_examples"] is True
    out = djaevin_log.mark_disagreement(did, "yes")
    assert out["rejection_reason"] == "near_duplicate"
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    rows = con.execute(
        "SELECT source_decision_id FROM djaevin_examples").fetchall()
    con.close()
    assert rows == [(did,)]


def _promote(tool: str, input_text: str, wrong: str, right: str) -> int:
    did = djaevin_log.log_decision(tool, input_text, wrong, 0.9, False)
    assert djaevin_log.mark_disagreement(did, right)[
        "promoted_to_examples"] is True
    return did


def test_few_shot_block_ids_reports_injected_examples(db):
    _promote("judge", "Backups completed last night per the log.", "no", "yes")
    _promote("judge", "The deploy tag matches the release notes.", "no", "yes")
    block, ids = djaevin_log.few_shot_block_ids(
        "judge", "Backups completed last night per the log.")
    assert "Exemplos validados" in block
    assert len(ids) >= 1
    # ids correspond to real example rows, in the same order as the block
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    for eid in ids:
        row = con.execute(
            "SELECT tool FROM djaevin_examples WHERE id = ?",
            (eid,)).fetchone()
        assert row and row[0] == "judge"
    con.close()
    # legacy helper stays text-only and consistent
    assert djaevin_log.few_shot_block(
        "judge", "Backups completed last night per the log.") == block


def test_few_shot_pool_size_counts_tool_examples(db):
    assert djaevin_log.few_shot_pool_size("judge") == 0
    _promote("judge", "Backups completed last night per the log.", "no", "yes")
    _promote("judge", "The deploy tag matches the release notes.", "no", "yes")
    assert djaevin_log.few_shot_pool_size("judge") == 2
    assert djaevin_log.few_shot_pool_size("rate") == 0


def test_log_decision_records_few_shot_observability(db):
    did = djaevin_log.log_decision(
        "judge", "The claim is supported.", "yes", 0.9, False,
        few_shot_ids=[3, 7], few_shot_pool=4)
    did2 = djaevin_log.log_decision("judge", "No examples.", "no", 0.8, False)
    import json
    import sqlite3
    con = sqlite3.connect(djaevin_log._db_path())
    row = con.execute(
        "SELECT few_shot_ids, few_shot_pool FROM djaevin_decisions "
        "WHERE id = ?", (did,)).fetchone()
    row2 = con.execute(
        "SELECT few_shot_ids, few_shot_pool FROM djaevin_decisions "
        "WHERE id = ?", (did2,)).fetchone()
    con.close()
    assert json.loads(row[0]) == [3, 7]
    assert row[1] == 4
    # columns exist and default to NULL when the caller passes nothing —
    # old rows pre-migration read the same way
    assert row2 == (None, None)


def test_few_shot_stats_aggregates_eligible_and_used(db):
    # two decisions before any example existed: not eligible
    djaevin_log.log_decision("judge", "First plain input.", "yes", 0.9, False)
    djaevin_log.log_decision("judge", "Second plain input.", "no", 0.8, False)
    # promote one example, then log one decision that used it and one that
    # did not pass the ids through (record=False path)
    _promote("judge", "Backups completed last night per the log.", "no", "yes")
    _block, ids = djaevin_log.few_shot_block_ids("judge", "Backups completed")
    assert ids
    djaevin_log.log_decision(
        "judge", "Backups completed last night per the log.", "yes", 0.9,
        False, few_shot_ids=ids,
        few_shot_pool=djaevin_log.few_shot_pool_size("judge"))
    djaevin_log.log_decision(
        "judge", "record=False internal check.", "yes", 0.5, False)
    stats = djaevin_log.few_shot_stats()
    j = stats["judge"]
    assert j["decisions"] == 5  # 2 plain + the promoted one + used + internal
    # only the decision that actually recorded a non-empty pool counts as
    # eligible — rows without the field (record=False, pre-migration) are
    # unknown, not eligible
    assert j["eligible"] == 1
    assert j["used"] == 1
    assert j["avg_examples_per_used"] == float(len(ids))
