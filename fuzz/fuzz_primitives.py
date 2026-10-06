"""ClusterFuzzLite fuzz target: numeric primitives under NaN/inf/extremes."""

import sys

import atheris

with atheris.instrument_imports():
    from poordjaevin.primitives import (
        normalize_probs,
        sigmoid,
        softmax,
    )
    from poordjaevin.verdicts import NOUL_VERDICTS, noul_verdict


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    n = fdp.ConsumeIntInRange(0, 64)
    scores = [fdp.ConsumeFloat() for _ in range(n)]
    try:
        out = softmax(scores)
        # invariant: a valid distribution for any non-empty input
        assert len(out) == len(scores)
        assert all(p >= 0.0 for p in out)
        assert abs(sum(out) - 1.0) < 1e-6
    except ValueError:
        assert not scores  # only empty input is allowed to raise
    out = normalize_probs(scores)
    assert abs(sum(out) - 1.0) < 1e-6
    sig = sigmoid(fdp.ConsumeFloat())
    assert 0.0 <= sig <= 1.0
    verdict = noul_verdict(fdp.ConsumeFloat(), fdp.ConsumeBool())
    assert verdict in NOUL_VERDICTS


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
