"""Meta-package shim for the ``devin-judge`` PyPI distribution.

``devin-judge`` is the new distribution name for the tool historically
published as ``poordjaevin``. This package vendors nothing: it depends on
``poordjaevin`` and re-exports its console entry point so that installing
``devin-judge`` provides both the ``devin-judge`` and ``poordjaevin``
commands, resolving to the same CLI.
"""

from poordjaevin.cli import main as _main

__all__ = ["_main"]
