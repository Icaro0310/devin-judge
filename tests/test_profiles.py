"""Per-(backend, task, decision_type) calibration profiles.

Pins the contract: slices below the minimum mass stay advisory and never
gate (the global calibrator is the only fallback); validated slices carry
out-of-fold metrics plus uncertainty; runtime selection in Client.ask
applies a validated profile per question type and falls back otherwise.
No model needed — records and backend are synthetic.
"""


import pytest

from poordjaevin import Choice, Client, Noul
from poordjaevin.calibration import (
    MIN_PROFILE_SAMPLES,
    fit_profiles,
    select_profile,
    wilson_ci,
)
from poordjaevin.metrics import DecisionRecord


def _record(task, dtype, dist, gold_index, correct):
    return DecisionRecord(
        confidence=max(dist),
        correct=correct,
        prob_gold=dist[gold_index],
        n_classes=len(dist),
        task=task,
        question="q",
        dist=dist,
        gold_index=gold_index,
        decision_type=dtype,
    )


def _noul_records(task, n, overconfident=True):
    """n binary records: mostly right, confident. gold alternates so both
    classes appear; ~20% wrong so risk has signal."""
    out = []
    for i in range(n):
        p = 0.95 if overconfident else 0.6
        gold = i % 2                      # alternate True/False
        correct = (i % 5) != 4            # 80% accuracy
        pred_true = gold if correct else 1 - gold
        dist = (1 - p, p) if pred_true == 1 else (p, 1 - p)
        out.append(_record(task, "noul", dist, gold, correct))
    return out


def _choice_records(task, n):
    out = []
    for i in range(n):
        gold = i % 3
        correct = (i % 4) != 3            # 75% accuracy
        pred = gold if correct else (gold + 1) % 3
        dist = [0.05, 0.05, 0.05]
        dist[pred] = 0.9
        out.append(_record(task, "choice", tuple(dist), gold, correct))
    return out


# --------------------------------------------------------------------- #
# fit / select
# --------------------------------------------------------------------- #

def test_fit_profiles_validates_slice_with_mass():
    recs = _noul_records("action_risk", 60)
    doc = fit_profiles(recs, backend="local_nli")
    p = doc["profiles"]["action_risk:noul"]
    assert p["status"] == "validated"
    assert p["n"] == 60
    assert p["temperature"] is not None
    assert p["validation"] == "5-fold-cv out-of-fold"
    for field in ("ece_before", "ece_after", "brier_after",
                  "coverage_at_target", "risk_at_target",
                  "answered", "risk_ci95"):
        assert field in p
    lo, hi = p["risk_ci95"]
    assert 0.0 <= lo <= p["risk_at_target"] <= hi <= 1.0


def test_fit_profiles_thin_slice_stays_advisory():
    recs = _noul_records("intent", MIN_PROFILE_SAMPLES - 1)
    doc = fit_profiles(recs, backend="local_nli")
    p = doc["profiles"]["intent:noul"]
    assert p["status"] == "advisory"
    assert p["n"] == MIN_PROFILE_SAMPLES - 1
    assert p["temperature"] is None
    assert p["validation"] == "insufficient-data"
    assert "below_min_samples" in p["reason"]


def test_min_samples_boundary_is_inclusive():
    recs = _noul_records("t", MIN_PROFILE_SAMPLES)
    doc = fit_profiles(recs, backend="local_nli")
    assert doc["profiles"]["t:noul"]["status"] == "validated"


def test_fit_profiles_groups_by_task_and_type():
    recs = (_noul_records("action_risk", 40)
            + _choice_records("support_triage", 35))
    doc = fit_profiles(recs, backend="local_nli")
    assert set(doc["profiles"]) == {"action_risk:noul",
                                    "support_triage:choice"}


def test_select_profile_returns_validated_only():
    doc = {"profiles": {
        "a:noul": {"status": "validated", "temperature": 2.0},
        "b:noul": {"status": "advisory"},
    }}
    assert select_profile(doc, "a", "noul")["temperature"] == 2.0
    assert select_profile(doc, "b", "noul") is None      # advisory -> global
    assert select_profile(doc, "c", "noul") is None      # missing -> global
    assert select_profile(doc, None, "noul") is None     # no hint -> global
    assert select_profile(None, "a", "noul") is None


def test_wilson_ci_bounds():
    lo, hi = wilson_ci(0.1, 60)
    assert 0.0 <= lo <= 0.1 <= hi <= 1.0
    # shrinks with n
    _, hi_wide = wilson_ci(0.1, 10)
    _, hi_narrow = wilson_ci(0.1, 1000)
    assert hi_narrow < hi_wide
    assert wilson_ci(0.5, 0) == (0.0, 1.0)


# --------------------------------------------------------------------- #
# runtime selection in Client.ask
# --------------------------------------------------------------------- #

class FakeBackend:
    is_remote = False  # test double: declared local for the default policy
    estimated_cost = 0.0

    def __init__(self, scores):
        self.scores = list(scores)

    def entail_probs(self, pairs):
        assert len(self.scores) == len(pairs)
        return self.scores


def _profiles_doc(temp, abstain=0.0):
    return {"profiles": {
        "action_risk:noul": {"status": "validated",
                             "temperature": temp,
                             "abstain_threshold": abstain},
    }}


def test_ask_applies_validated_profile_to_matching_task_and_type():
    # one noul pair; p_true raw = 0.9
    client = Client(backend=FakeBackend([0.9]), temperature=1.0,
                    profiles=_profiles_doc(temp=4.0))
    ans = client.ask("state", {"q": Noul("It deletes data.")},
                     task="action_risk")["q"]
    # T=4 must soften the probability vs the raw 0.9
    assert ans.prob < 0.9


def test_ask_falls_back_to_global_without_task_hint():
    client = Client(backend=FakeBackend([0.9]), temperature=1.0,
                    profiles=_profiles_doc(temp=4.0))
    ans = client.ask("state", {"q": Noul("It deletes data.")})["q"]
    assert ans.prob == pytest.approx(0.9)   # raw: no task -> global T=1


def test_ask_falls_back_to_global_for_other_task():
    client = Client(backend=FakeBackend([0.9]), temperature=1.0,
                    profiles=_profiles_doc(temp=4.0))
    ans = client.ask("state", {"q": Noul("It deletes data.")},
                     task="intent")["q"]
    assert ans.prob == pytest.approx(0.9)


def test_ask_profile_is_per_question_type():
    # action_risk profile is noul-only; a Choice in the same task must use
    # the global temperature, not the noul profile's.
    questions = {"risk": Noul("It deletes data."),
                 "topic": Choice(["a", "b", "c"])}
    client = Client(backend=FakeBackend([0.9, 0.8, 0.3, 0.2]),
                    temperature=1.0, profiles=_profiles_doc(temp=4.0))
    out = client.ask("state", questions, task="action_risk")
    baseline = Client(backend=FakeBackend([0.9, 0.8, 0.3, 0.2]),
                      temperature=1.0).ask("state", questions)["topic"]
    assert out["risk"].prob < 0.9                       # profile applied
    assert out["topic"].probs == baseline.probs         # choice unaffected


def test_ask_profile_abstain_threshold_applies():
    client = Client(backend=FakeBackend([0.9]), temperature=1.0,
                    profiles=_profiles_doc(temp=1.0, abstain=0.99))
    ans = client.ask("state", {"q": Noul("It deletes data.")},
                     task="action_risk")["q"]
    assert ans.abstained


def test_ask_advisory_profile_never_applies():
    doc = {"profiles": {"action_risk:noul": {"status": "advisory",
                                            "temperature": None}}}
    client = Client(backend=FakeBackend([0.9]), temperature=1.0,
                    profiles=doc)
    ans = client.ask("state", {"q": Noul("It deletes data.")},
                     task="action_risk")["q"]
    assert ans.prob == pytest.approx(0.9)               # global, not advisory


def test_profile_temperature_respects_calibrated_source_guard():
    # backend serving a different confidence source must get raw confidence
    # even when a profile exists for the task.
    class ForeignBackend(FakeBackend):
        confidence_source = "other_source"

    client = Client(backend=ForeignBackend([0.9]), temperature=2.0,
                    calibrated_source="local_nli",
                    profiles=_profiles_doc(temp=4.0))
    ans = client.ask("state", {"q": Noul("It deletes data.")},
                     task="action_risk")["q"]
    assert ans.prob == pytest.approx(0.9)   # raw: source mismatch wins


# --------------------------------------------------------------------- #
# build_client integration: backend tag on the profiles doc
# --------------------------------------------------------------------- #

def test_build_client_drops_profiles_on_backend_mismatch(tmp_path, monkeypatch):
    import json

    from poordjaevin import mcp_server

    cal = tmp_path / "calibration.json"
    cal.write_text(json.dumps({"temperature": 2.0, "abstain_threshold": 0.0,
                               "backend": "local_nli"}))
    prof = tmp_path / "profiles.json"
    prof.write_text(json.dumps({"backend": "self_report", "profiles": {
        "action_risk:noul": {"status": "validated", "temperature": 9.0},
    }}))

    class B(FakeBackend):
        confidence_source = "local_nli"

    monkeypatch.setenv("POORDJAEVIN_BACKEND", "nli")
    client = mcp_server.build_client(str(cal), backend=B([0.9]),
                                     profiles_path=str(prof))
    ans = client.ask("s", {"q": Noul("x")}, task="action_risk")["q"]
    # profiles doc is for self_report; this backend is local_nli -> dropped,
    # global T=2.0 applies instead of profile T=9.0
    expected = 0.9 ** (1 / 2.0) / (0.9 ** (1 / 2.0) + 0.1 ** (1 / 2.0))
    assert ans.prob == pytest.approx(expected, rel=1e-4)


def test_load_profiles_missing_and_malformed(tmp_path):
    from poordjaevin.mcp_server import load_profiles
    assert load_profiles(None) == {}
    assert load_profiles(str(tmp_path / "nope.json")) == {}
    bad = tmp_path / "bad.json"
    bad.write_text("[1,2,3]")
    assert load_profiles(str(bad)) == {}
    noslice = tmp_path / "noslice.json"
    noslice.write_text('{"backend": "x"}')
    assert load_profiles(str(noslice)) == {}
