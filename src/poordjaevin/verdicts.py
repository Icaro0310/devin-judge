"""Formal verdict tiers shared by every poordjaevin caller.

A raw probability is not a decision policy. This module is the single place
where "what does this answer mean operationally" is defined, so the MCP tools,
the session janitor, the weekly calibrator, and the heartbeat rotation all
speak the same vocabulary instead of each inventing its own cutoffs.

Noul (yes/no) bands, on P(yes):

    strong_no     p <= 0.15         confident rejection
    no            0.15 < p <= 0.40  probable rejection
    review        0.40 < p < 0.65   borderline — never auto-decides, escalates
    yes           0.65 <= p < 0.85  probable confirmation
    strong_yes    p >= 0.85         confident confirmation

A ``low_confidence`` flag (from the caller's threshold policy) always forces
``review``: an uncertain answer never lands in an actionable band. The review
band is the formal REVIEW tier — producers must route those items to a queue
for a human or a later pass, never treat them as either confirmation or
rejection.

Distribution answers (classify/rate/gate) collapse to two tiers:
``confident`` when the caller's confidence policy passes, ``review`` when it
does not. The winning label stays advisory either way.
"""

from __future__ import annotations

REVIEW_LOW = 0.40
REVIEW_HIGH = 0.65
STRONG_LOW = 0.15
STRONG_HIGH = 0.85

NOUL_VERDICTS = ("strong_no", "no", "review", "yes", "strong_yes")
DIST_VERDICTS = ("confident", "review")


def noul_verdict(p_yes: float, low_confidence: bool = False) -> str:
    """Map a yes/no probability to its formal tier.

    ``low_confidence`` is the caller-side flag (confidence below the tool's
    threshold): it forces ``review`` even when ``p_yes`` looks decisive,
    because the model itself reported it could not tell.
    """
    p = min(1.0, max(0.0, float(p_yes)))
    if low_confidence or REVIEW_LOW < p < REVIEW_HIGH:
        return "review"
    if p <= STRONG_LOW:
        return "strong_no"
    if p <= REVIEW_LOW:
        return "no"
    if p >= STRONG_HIGH:
        return "strong_yes"
    return "yes"


def dist_verdict(low_confidence: bool = False) -> str:
    """Tier for Choice/Score answers: confident or review, nothing else."""
    return "review" if low_confidence else "confident"
