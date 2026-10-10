"""poordjaevin: a local-first "System One" decision layer.

Ask typed questions about some state, get typed answers with calibrated
confidence, in one pass, with no API key by default. The one thing we prove:
the confidence is honest.

Fork renamed from `poorjev` (upstream: github.com/rupeshpoojary9/poorjev)
to avoid collision with the existing "Jev" name. Env vars read as
POORDJAEVIN_*; legacy POORJEV_* names are honoured as fallbacks.

M1 ships the contract (the three primitives + typed answers). Backends (M2/M5)
and calibration (M4) build on top without ever being able to break schema
validity, which is structural. See PRD.md.
"""

import os as _os

# Compat: POORDJAEVIN_X wins; a set POORJEV_X still works for old configs.
for _name in (
    "ABSTAIN", "ACP_BRIDGE", "ACP_MAX_COST", "ACP_MODEL", "ACP_NODE",
    "ACP_TIMEOUT", "BACKEND", "CALIBRATOR", "DEBUG_PROMPT", "DEVIN_DB",
    "EXTRA_TOOLS", "HTTP_TIMEOUT", "KEEP_ALIVE", "LOG_DB",
    "LOW_CONFIDENCE", "MODEL", "PARALLEL", "TOP_LOGPROBS",
):
    _new = f"POORDJAEVIN_{_name}"
    _old = f"POORJEV_{_name}"
    if _new not in _os.environ and _old in _os.environ:
        _os.environ[_new] = _os.environ[_old]

from .client import Client
from .primitives import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
)

__version__ = "0.1.2"

__all__ = [
    "Choice",
    "ChoiceAnswer",
    "Client",
    "Noul",
    "NoulAnswer",
    "Score",
    "ScoreAnswer",
    "__version__",
]
