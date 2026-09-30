"""Tests for the shared verdict tiers (verdicts.py) and their presence in the
MCP tool contract. The bands are the operational vocabulary every caller
(janitor, calibrator, heartbeat rotation) agrees on.
"""

import pytest

from poordjaevin.verdicts import (
    REVIEW_LOW, REVIEW_HIGH, STRONG_LOW, STRONG_HIGH,
    noul_verdict, dist_verdict,
)


@pytest.mark.parametrize("p,expected", [
    (0.0, "strong_no"),
    (0.15, "strong_no"),
    (0.16, "no"),
    (0.40, "no"),
    (0.41, "review"),
    (0.50, "review"),
    (0.64, "review"),
    (0.65, "yes"),
    (0.84, "yes"),
    (0.85, "strong_yes"),
    (1.0, "strong_yes"),
])
def test_noul_bands(p, expected):
    assert noul_verdict(p) == expected


def test_low_confidence_always_forces_review():
    # A decisive-looking p_yes with low_confidence still lands in review.
    assert noul_verdict(0.99, low_confidence=True) == "review"
    assert noul_verdict(0.01, low_confidence=True) == "review"


def test_noul_verdict_clamps_out_of_range():
    assert noul_verdict(1.7) == "strong_yes"
    assert noul_verdict(-0.3) == "strong_no"


def test_dist_verdict_is_binary():
    assert dist_verdict(False) == "confident"
    assert dist_verdict(True) == "review"
