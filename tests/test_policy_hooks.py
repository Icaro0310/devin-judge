"""Pre-backend policy + uniform lifecycle hooks.

Pins the two contracts: policy rules (privacy, permissions, budget,
calibration availability) run BEFORE the backend is ever invoked and
fail closed; the six hooks fire in order, receive metadata rather than
payloads, stay inert by default, and propagate hook errors loudly.
"""

import pytest

from poordjaevin import Choice, Client, Noul, Policy, PolicyError
from poordjaevin.client import HOOK_POINTS


class FakeBackend:
    def __init__(self, scores, **attrs):
        self.scores = list(scores)
        self.calls = 0
        self.seen_pairs = None
        for k, v in attrs.items():
            setattr(self, k, v)

    def entail_probs(self, pairs):
        self.calls += 1
        self.seen_pairs = list(pairs)
        return self.scores


def _client(**kw):
    return Client(backend=FakeBackend([0.9], is_remote=False,
                                      estimated_cost=0.0), **kw)


# --------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------- #

def test_default_policy_denies_remote_without_authorization():
    # policy omitted -> default closed Policy(), not "no check"
    backend = FakeBackend([0.9], is_remote=True, estimated_cost=0.0)
    client = Client(backend=backend)
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "remote_denied"
    assert backend.calls == 0          # never invoked: fail-closed


def test_default_policy_denies_undeclared_destination():
    # is_remote undeclared -> destination unknown -> denied, distinct rule
    backend = FakeBackend([0.9])       # no attrs set
    client = Client(backend=backend)
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "unknown_destination"
    assert backend.calls == 0


def test_default_policy_passes_declared_local():
    client = _client()   # is_remote=False declared: local path preserved
    assert client.ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_explicit_authorization_allows_remote():
    backend = FakeBackend([0.9], is_remote=True, estimated_cost=0.0)
    client = Client(backend=backend, policy=Policy(allow_remote=True))
    assert client.ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_allow_remote_false_blocks_remote_backend():
    backend = FakeBackend([0.9], is_remote=True, estimated_cost=0.0)
    client = Client(backend=backend, policy=Policy(allow_remote=False))
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "remote_denied"
    assert backend.calls == 0          # never invoked: fail-closed


def test_allow_remote_false_passes_local_backend():
    client = _client(policy=Policy(allow_remote=False))
    assert client.ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_undeclared_backend_is_treated_as_remote():
    # no is_remote attribute -> unknown destination under allow_remote=False
    backend = FakeBackend([0.9])       # no attrs set
    client = Client(backend=backend, policy=Policy(allow_remote=False))
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "unknown_destination"
    assert backend.calls == 0


def test_allowed_backends_filters_by_class_name():
    client = _client(policy=Policy(allowed_backends=("LocalNLIBackend",)))
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "backend_not_allowed"


def test_max_cost_zero_blocks_unknown_estimate():
    backend = FakeBackend([0.9], is_remote=False, estimated_cost=None)
    client = Client(backend=backend, policy=Policy(max_cost=0.0))
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "budget"
    assert backend.calls == 0


def test_max_cost_passes_provable_free_backend():
    client = _client(policy=Policy(max_cost=0.0))
    assert client.ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_max_cost_blocks_overpriced_backend():
    backend = FakeBackend([0.9], is_remote=False, estimated_cost=0.05)
    client = Client(backend=backend, policy=Policy(max_cost=0.0))
    with pytest.raises(PolicyError):
        client.ask("s", {"q": Noul("x")})
    assert backend.calls == 0


def test_require_calibration_blocks_uncalibrated_client():
    # no calibrated_source -> no fitted calibrator behind this client
    client = _client(policy=Policy(require_calibration=True))
    with pytest.raises(PolicyError) as e:
        client.ask("s", {"q": Noul("x")})
    assert e.value.rule == "uncalibrated"


def test_require_calibration_passes_calibrated_client():
    backend = FakeBackend([0.9], is_remote=False, estimated_cost=0.0,
                          confidence_source="local_nli")
    client = Client(backend=backend, temperature=2.0,
                    calibrated_source="local_nli",
                    policy=Policy(require_calibration=True))
    assert client.ask("s", {"q": Noul("x")})["q"].value is True


def test_backends_declare_privacy_and_cost():
    from poordjaevin.backends.acp_devin import AcpDevinBackend
    from poordjaevin.backends.local_nli import LocalNLIBackend
    from poordjaevin.backends.ollama_logits import OllamaLogitsBackend
    from poordjaevin.backends.vscode_lm import VscodeLmBackend
    assert (LocalNLIBackend.is_remote, LocalNLIBackend.estimated_cost) == (False, 0.0)
    assert (OllamaLogitsBackend.is_remote, OllamaLogitsBackend.estimated_cost) == (False, 0.0)
    assert AcpDevinBackend.is_remote is True
    assert AcpDevinBackend.estimated_cost is None
    assert VscodeLmBackend.is_remote is True
    assert VscodeLmBackend.estimated_cost is None


# --------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------- #

def test_no_hooks_is_inert():
    assert _client().ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_hooks_fire_in_order():
    order = []
    hooks = {p: (lambda ctx, p=p: order.append(p)) for p in HOOK_POINTS
             if p not in ("on_disagreement", "on_error")}
    _client(hooks=hooks).ask("s", {"q": Noul("x")}, task="action_risk")
    assert order == ["before_judge", "before_backend",
                     "after_backend", "after_calibration"]


def test_before_backend_sees_metadata_not_payload():
    seen = {}
    _client(hooks={"before_backend": lambda ctx: seen.update(ctx)}) \
        .ask("state text", {"q": Noul("x")}, task="t")
    assert seen == {"backend": "FakeBackend", "pairs_count": 1, "task": "t"}
    assert "state" not in str(seen)


def test_after_backend_reports_counts_and_duration():
    seen = {}
    Client(backend=FakeBackend([0.9, 0.8, 0.2], is_remote=False,
                             estimated_cost=0.0),
           hooks={"after_backend": lambda ctx: seen.update(ctx)}) \
        .ask("s", {"a": Noul("x"), "b": Choice(["y", "z"])})
    assert seen["probs_count"] == seen["pairs_count"] == 3  # 1 + 2
    assert seen["duration_s"] >= 0.0


def test_after_calibration_sees_typed_answers_only():
    seen = {}
    _client(hooks={"after_calibration": lambda ctx: seen.update(ctx)}) \
        .ask("s", {"q": Noul("x")})
    assert seen["answers"] == {"q": {"value": True, "confidence": 0.9,
                                    "abstained": False}}


def test_before_judge_can_redact_state():
    backend = FakeBackend([0.9], is_remote=False, estimated_cost=0.0)
    client = Client(backend=backend, hooks={
        "before_judge": lambda ctx: {"state": "[REDACTED]"}})
    client.ask("secret payload", {"q": Noul("x")})
    assert backend.seen_pairs[0][0] == "[REDACTED]"


def test_on_error_fires_on_backend_failure_and_propagates():
    errors = []

    class BoomBackend:
        is_remote = False
        estimated_cost = 0.0

        def entail_probs(self, pairs):
            raise ConnectionError("backend down")

    client = Client(backend=BoomBackend(),
                    hooks={"on_error": lambda ctx: errors.append(ctx)})
    with pytest.raises(ConnectionError):
        client.ask("s", {"q": Noul("x")})
    assert errors[0]["stage"] == "backend"
    assert errors[0]["error_type"] == "ConnectionError"
    assert errors[0]["error_code"] == "BACKEND_FAILURE"
    assert errors[0]["retryable"] is True   # ConnectionError is transient


def test_on_error_fires_on_policy_denial():
    errors = []
    client = _client(policy=Policy(allow_remote=False),
                     hooks={"on_error": lambda ctx: errors.append(ctx)})
    # local backend passes; now flip: remote declared backend
    client._backend.is_remote = True
    with pytest.raises(PolicyError):
        client.ask("s", {"q": Noul("x")})
    assert errors[0]["stage"] == "policy"
    assert errors[0]["error_code"] == "POLICY_DENIED"
    assert errors[0]["policy_rule"] == "remote_denied"


def test_on_error_never_carries_raw_message_or_secret():
    # A backend error message may embed state fragments; hooks must receive
    # the structured event only — truncation is not redaction.
    secret = "sk-test-SECRET-fragment-9x2"

    class LeakyBackend:
        is_remote = False
        estimated_cost = 0.0

        def entail_probs(self, pairs):
            raise RuntimeError(f"downstream choked on {secret} badly")

    errors = []
    client = Client(backend=LeakyBackend(),
                    hooks={"on_error": lambda ctx: errors.append(ctx)})
    with pytest.raises(RuntimeError):
        client.ask("s", {"q": Noul("x")})
    assert secret not in str(errors)
    assert "detail" not in errors[0]
    assert set(errors[0]) <= {
        "event", "stage", "backend", "error_code",
        "error_type", "retryable", "policy_rule"}
    assert errors[0]["error_code"] == "BACKEND_FAILURE"
    assert errors[0]["retryable"] is False


def test_on_error_fires_on_short_score_count():
    errors = []
    client = Client(backend=FakeBackend([0.9, 0.9], is_remote=False,
                                        estimated_cost=0.0),  # 2 for 1 pair
                    hooks={"on_error": lambda ctx: errors.append(ctx)})
    with pytest.raises(RuntimeError):
        client.ask("s", {"q": Noul("x")})
    assert errors[0]["stage"] == "backend"
    assert errors[0]["error_code"] == "BACKEND_FAILURE"


def test_hook_exception_propagates_loudly():
    def bad_hook(ctx):
        raise RuntimeError("hook exploded")

    with pytest.raises(RuntimeError, match="hook exploded"):
        _client(hooks={"before_backend": bad_hook}).ask("s", {"q": Noul("x")})


def test_unknown_hook_names_are_ignored():
    client = _client(hooks={"nonsense": lambda ctx: 1 / 0})
    assert client.ask("s", {"q": Noul("x")})["q"].prob == pytest.approx(0.9)


def test_notify_disagreement_fires_on_disagreement():
    seen = []
    client = _client(hooks={"on_disagreement": lambda ctx: seen.append(ctx)})
    client.notify_disagreement({"decision_id": 7, "promoted": True})
    assert seen == [{"decision_id": 7, "promoted": True}]


def test_hook_points_constant():
    assert HOOK_POINTS == ("before_judge", "before_backend", "after_backend",
                           "after_calibration", "on_disagreement", "on_error")


# --------------------------------------------------------------------- #
# Operator-selected backend = the explicit remote authorization
# --------------------------------------------------------------------- #

def test_build_client_authorizes_operator_selected_remote(monkeypatch):
    # POORDJAEVIN_BACKEND=acp is the operator's explicit choice: the remote
    # backend it yields is authorized, everything else stays closed.
    monkeypatch.setenv("POORDJAEVIN_BACKEND", "acp")
    from poordjaevin import mcp_server
    client = mcp_server.build_client(calibrator_path=None, profiles_path=None)
    assert client._policy.allow_remote is True


def test_build_client_keeps_closed_policy_for_local(monkeypatch):
    monkeypatch.setenv("POORDJAEVIN_BACKEND", "nli")
    from poordjaevin import mcp_server
    client = mcp_server.build_client(calibrator_path=None, profiles_path=None)
    assert client._policy.allow_remote is False
