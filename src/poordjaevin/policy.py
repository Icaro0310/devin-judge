"""Pre-backend policy checks for the decision pipeline.

Order matters: privacy, permissions, availability and budget are all
checked BEFORE the backend is invoked. A model's confidence can never be
an argument for sending data somewhere — the policy runs first, always.

Defaults preserve current behavior: ``Policy()`` denies nothing. The
unsafe move is opting IN to restrictions, never out.

Backends declare two attributes used here:
  ``is_remote``       — does the payload leave this machine? (bool)
  ``estimated_cost``  — provable per-call cost, or None for "unknown".

An unknown cost under a finite budget is denied (fail-closed): a backend
that cannot prove its price cannot be trusted inside one.
"""

from __future__ import annotations

from dataclasses import dataclass


class PolicyError(RuntimeError):
    """A policy check failed; the backend was never called.

    Carries the failing rule name so callers and logs can distinguish
    privacy denials from budget denials without parsing the message.
    """

    def __init__(self, rule: str, detail: str):
        self.rule = rule
        super().__init__(f"policy:{rule}: {detail}")


@dataclass(frozen=True)
class Policy:
    """Constraints evaluated before any backend call. All fields default
    to permissive so existing clients behave exactly as before."""

    allow_remote: bool = True
    """False: only backends with ``is_remote = False`` may run (privacy)."""

    allowed_backends: tuple | None = None
    """Optional tuple of permitted backend class names (permissions).
    None permits all."""

    max_cost: float | None = None
    """Per-call budget. A backend must prove ``estimated_cost <= max_cost``;
    an unknown estimate is denied (fail-closed)."""

    require_calibration: bool = False
    """True: only a client backed by a fitted calibrator (calibrated_source
    set by build_client) may decide. Raw-confidence clients are denied."""

    def check(self, backend, calibrated: bool = True) -> None:
        """Raise PolicyError on the first violated rule, else return None."""
        name = type(backend).__name__
        if self.allowed_backends is not None and name not in self.allowed_backends:
            raise PolicyError("backend_not_allowed",
                              f"{name} not in allowed_backends")
        if not self.allow_remote and getattr(backend, "is_remote", True):
            # an undeclared backend is treated as remote: privacy fails closed
            raise PolicyError("remote_denied", f"{name} is remote")
        if self.max_cost is not None:
            est = getattr(backend, "estimated_cost", None)
            if est is None or est > self.max_cost:
                raise PolicyError(
                    "budget",
                    f"{name} estimated_cost {est!r} > max_cost "
                    f"{self.max_cost}")
        if self.require_calibration and not calibrated:
            raise PolicyError("uncalibrated",
                              "no fitted calibrator (require_calibration)")
