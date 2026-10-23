"""Pre-backend policy checks for the decision pipeline.

Order matters: privacy, permissions, availability and budget are all
checked BEFORE the backend is invoked. A model's confidence can never be
an argument for sending data somewhere — the policy runs first, always.

Defaults fail closed on privacy: ``Policy()`` denies remote backends and
backends whose destination is undeclared. Remote inference requires
explicit authorization (``allow_remote=True``, granted by the caller or
by an operator-selected backend in build_client). The other dimensions
stay permissive unless set, so a declared-local backend runs exactly as
before.

Backends declare two attributes used here:
  ``is_remote``       — does the payload leave this machine? (bool;
                        undeclared means unknown destination -> denied
                        unless remote is authorized)
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
    """Constraints evaluated before any backend call.

    Privacy defaults to closed: remote and undeclared-destination
    backends are denied unless ``allow_remote`` authorizes them. The
    other fields default to permissive so declared-local backends keep
    their current behavior."""

    allow_remote: bool = False
    """True: remote and unknown-destination backends may run (the explicit
    authorization). False: only declared ``is_remote = False`` backends."""

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
        remote = getattr(backend, "is_remote", None)
        if remote is not False and not self.allow_remote:
            # destination unknown or declared remote: privacy fails closed
            rule = "remote_denied" if remote else "unknown_destination"
            detail = f"{name} is remote" if remote else \
                f"{name} does not declare is_remote"
            raise PolicyError(rule, detail)
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
