"""The Client: ask a batch of typed questions about one state, in a single pass.

The Client owns orchestration so backends stay simple. It:
  1. turns each question into (state, hypothesis) pairs,
  2. concatenates every pair across every question into ONE batched call,
  3. hands the raw entailment probabilities back to each primitive to decide.

A backend only has to implement ``entail_probs(pairs) -> list[float]``. The
default backend is the local NLI model (keyless, offline). Pass your own backend
(e.g. a fake in tests, or the future [llm] backend) to swap it out.
"""

from __future__ import annotations

from typing import Protocol

from .primitives import Choice, Noul, Score

DEFAULT_TEMPLATE = "This example is {}."


class Backend(Protocol):
    def entail_probs(self, pairs: list[tuple[str, str]]) -> list[float]: ...


class Client:
    def __init__(self, backend: Backend | None = None,
                 hypothesis_template: str = DEFAULT_TEMPLATE,
                 temperature: float = 1.0,
                 abstain_threshold: float = 0.0,
                 calibrated_source: str | None = None,
                 profiles: dict | None = None):
        self._backend = backend
        self.hypothesis_template = hypothesis_template
        # A fitted temperature (from `poordjaevin calibrate`) makes ask()'s
        # confidences calibrated. 1.0 is a no-op (raw).
        self.temperature = temperature
        self.abstain_threshold = abstain_threshold
        # The confidence source the temperature was fitted for. Calibration
        # does not transfer: when the backend serves a batch from another
        # source (e.g. vscode-lm falling back to NLI), raw confidence is the
        # honest answer. None keeps the legacy always-apply behaviour.
        self._calibrated_source = calibrated_source
        # Optional per-slice calibration profiles, keyed "task:decision_type"
        # (see calibration.fit_profiles). Only "validated" entries apply;
        # anything else falls back to the global temperature above.
        self._profiles = (profiles or {}).get("profiles") or {}

    @property
    def backend(self) -> Backend:
        if self._backend is None:
            from .backends.local_nli import LocalNLIBackend
            self._backend = LocalNLIBackend()
        return self._backend

    def _decision_type(self, prim) -> str:
        if isinstance(prim, Noul):
            return "noul"
        if isinstance(prim, Choice):
            return "choice"
        return "score"

    def ask(self, state: str, questions: dict, task: str | None = None):
        """Answer every question about ``state`` in one batched pass.

        Returns a dict mapping each question name to its typed answer
        (ChoiceAnswer / ScoreAnswer / NoulAnswer). ``task`` is an optional
        caller-declared slice hint ("action_risk", "support_triage", ...):
        when a validated profile exists for (task, question type) its
        temperature and abstain threshold apply instead of the global ones.
        """
        if not isinstance(state, str) or state == "":
            raise ValueError("state must be a non-empty string")
        if not questions:
            raise ValueError("ask() needs at least one question")

        pairs: list[tuple[str, str]] = []
        plan: list[tuple[str, object, int, int, str]] = []  # name, prim, start, count, mode

        for name, prim in questions.items():
            if isinstance(prim, Noul):
                hyps = [prim.statement]
                mode = "noul"
            elif isinstance(prim, (Choice, Score)):
                hyps = [self.hypothesis_template.format(h) for h in prim.hypotheses]
                mode = "dist"
            else:
                raise TypeError(
                    f"question {name!r} must be a Choice, Score or Noul, got {type(prim).__name__}"
                )
            start = len(pairs)
            pairs.extend((state, h) for h in hyps)
            plan.append((name, prim, start, len(hyps), mode))

        probs = self.backend.entail_probs(pairs)  # the single pass
        if len(probs) != len(pairs):
            raise RuntimeError(
                f"backend returned {len(probs)} scores for {len(pairs)} pairs"
            )

        calibrated = (
            self._calibrated_source is None
            or getattr(self.backend, "confidence_source", self._calibrated_source)
            == self._calibrated_source
        )
        temperature = self.temperature if calibrated else 1.0
        abstain = self.abstain_threshold if calibrated else 0.0

        out: dict[str, object] = {}
        for name, prim, start, count, mode in plan:
            # Per-question calibration: a validated profile for the declared
            # task slice overrides the global temperature and threshold;
            # anything else (advisory, missing, no hint) uses the globals.
            q_temp, q_abstain = temperature, abstain
            if calibrated and task:
                profile = self._profiles.get(f"{task}:{self._decision_type(prim)}")
                if profile and profile.get("status") == "validated":
                    q_temp = profile.get("temperature") or q_temp
                    q_abstain = profile.get("abstain_threshold") or q_abstain
            seg = probs[start:start + count]
            if mode == "noul":
                p_true = seg[0]
                if q_temp != 1.0:
                    from .calibration import apply_temperature
                    p_true = apply_temperature([1.0 - p_true, p_true], q_temp)[1]
                out[name] = prim.decide(
                    p_true,
                    kind="prob",
                    abstain_below=q_abstain,
                )
            else:
                # entailment probs per option/level, normalised across the set
                dist = seg
                if q_temp != 1.0:
                    from .calibration import apply_temperature
                    total = sum(max(0.0, s) for s in seg) or 1.0
                    norm = [max(0.0, s) / total for s in seg]
                    dist = apply_temperature(norm, q_temp)
                out[name] = prim.decide(
                    dist,
                    kind="probs",
                    threshold=q_abstain,
                )
        return out
