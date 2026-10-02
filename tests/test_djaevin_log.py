"""Unit tests for djaevin_log: SQLite decision log, disagreement promotion,
Jaccard few-shot retrieval, and the calibrate report. Stdlib only — no
model, no server, tmp DB per test."""

import pytest

from poordjaevin import djaevin_log


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_LOG_DB", str(tmp_path / "djaevin_log.db"))
    return tmp_path / "djaevin_log.db"


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
    assert out == {"ok": True, "promoted_to_examples": True}
    examples = djaevin_log.get_few_shot("classify", "servico horrivel")
    assert examples[0]["choice"] == "mau"


def test_mark_disagreement_without_correct_choice(db):
    decision_id = djaevin_log.log_decision("judge", "2+2=4", "no", 0.9, False)
    out = djaevin_log.mark_disagreement(decision_id)
    assert out == {"ok": True, "promoted_to_examples": False}
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
    decision_id = djaevin_log.log_decision("rate", "texto x", "1", 0.5, False)
    djaevin_log.mark_disagreement(decision_id, "5")
    assert djaevin_log.get_few_shot("judge", "texto x") == []
    assert djaevin_log.get_few_shot("rate", "texto x")[0]["choice"] == "5"


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
    from poordjaevin.mcp_server import _parse_thresholds, _low_conf, \
        LOW_CONFIDENCE_THRESHOLDS
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
