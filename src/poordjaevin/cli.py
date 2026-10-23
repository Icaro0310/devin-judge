"""poordjaevin command line.

    poordjaevin eval   --set evalset/tasks.jsonl [--plots] [--verbose]
    poordjaevin ask    --state-file ticket.txt --questions questions.yaml   (M4+)
    poordjaevin calibrate --set evalset/tasks.jsonl                          (M4)

M3 ships ``eval``: run the set through the local backend and print accuracy +
ECE + Brier + AURC, overall and per task. ``--plots`` writes the reliability
diagram (the launch asset) once matplotlib is installed.
"""

from __future__ import annotations

import argparse
import os
import sys


def _env_or_local_client(calibrator_path: str | None = None, **client_kwargs):
    """Client honoring POORDJAEVIN_BACKEND when set — plain Client()
    (lazy local NLI) when unset, preserving pre-selector behaviour.
    The selected-backend arm loads `calibrator_path` via build_client
    (None falls back to build_client's default calibration.json)."""
    if not os.environ.get("POORDJAEVIN_BACKEND"):
        from .client import Client
        return Client(**client_kwargs)
    from .mcp_server import build_client
    return build_client(calibrator_path)


def _env_or_local_raw_client():
    """The selected backend WITHOUT deployment calibration — evaluate and
    calibrate must collect raw scores, or a refit would fit already-
    temperature-scaled probabilities and the saved calibrator would then
    mis-scale every later answer."""
    if not os.environ.get("POORDJAEVIN_BACKEND"):
        from .client import Client
        return Client()
    from .mcp_server import _select_backend
    return Client(backend=_select_backend())


def _fmt(summary: dict) -> str:
    return (
        f"n={summary['n']:>4}  "
        f"acc={summary['accuracy']:.3f}  "
        f"ECE={summary['ece']:.3f}  "
        f"Brier={summary['brier']:.3f}  "
        f"AURC={summary['aurc']:.3f}"
    )


def cmd_eval(args) -> int:
    from .evaluate import evaluate, report

    print(f"Running eval set: {args.set}")
    backend_name = os.environ.get("POORDJAEVIN_BACKEND")
    if backend_name:
        print(f"(backend: {backend_name})\n")
    else:
        print("(first run downloads a ~400MB model; then offline)\n")
    client = _env_or_local_raw_client()
    records, _ = evaluate(args.set, client=client, verbose=args.verbose)
    rep = report(records)

    print("\n== overall ==")
    print("  " + _fmt(rep["overall"]))
    print("\n== by task ==")
    for task, s in rep["by_task"].items():
        print(f"  {task:<16} " + _fmt(s))

    if args.plots:
        try:
            from .plots import reliability_diagram
            out = reliability_diagram(records, title="poordjaevin, raw (uncalibrated)",
                                      path=args.plot_path)
            print(f"\nwrote reliability diagram -> {out}")
        except ImportError:
            print("\n[--plots needs the 'plots' extra: pip install 'poordjaevin[plots]']")
    return 0


def cmd_calibrate(args) -> int:
    import json

    from .calibration import fit_global_temperature, summarize_calibration
    from .evaluate import evaluate

    print(f"Calibrating on: {args.set}")
    backend_name = os.environ.get("POORDJAEVIN_BACKEND")
    if backend_name:
        print(f"(backend: {backend_name})\n")
    else:
        print("(runs the eval set through the local model once)\n")
    client = _env_or_local_raw_client()
    raw, _ = evaluate(args.set, client=client)

    summ = summarize_calibration(raw, k=args.folds, target_risk=args.target_risk)
    global_T = fit_global_temperature(raw)  # the deployable scalar
    backend_tag = getattr(client.backend, "confidence_source", "local_nli")

    print("== calibration (5-fold held-out) ==")
    print(f"  temperature (mean)   : {summ['temperature']:.3f}")
    print(f"  ECE  before -> after : {summ['ece_before']:.3f} -> {summ['ece_after']:.3f}")
    print(f"  selective @ risk<={summ['target_risk']:.2f}: "
          f"coverage={summ['coverage_at_target']:.2f} "
          f"(threshold {summ['abstain_threshold']:.3f})")

    saved = {
        "temperature": global_T,
        "target_risk": summ["target_risk"],
        "abstain_threshold": summ["abstain_threshold"],
        "ece_before": summ["ece_before"],
        "ece_after": summ["ece_after"],
        # Tag the confidence source of the backend that supplied the fit
        # samples — load_calibration defaults a missing tag to local_nli
        # and build_client would then discard this temperature for ACP or
        # vscode-lm deployments.
        "backend": backend_tag,
    }
    with open(args.out, "w") as f:
        json.dump(saved, f, indent=2)
    print(f"\nsaved calibrator -> {args.out}")

    if args.profiles_out:
        import hashlib

        from .calibration import fit_profiles
        with open(args.set, "rb") as f:
            digest = "sha256:" + hashlib.sha256(f.read()).hexdigest()[:16]
        doc = fit_profiles(raw, backend=backend_tag,
                           min_samples=args.min_samples,
                           k=args.folds, target_risk=args.target_risk,
                           dataset_digest=digest)
        print("\n== profiles (task:decision_type) ==")
        for key, p in doc["profiles"].items():
            if p["status"] == "validated":
                print(f"  {key:<28} n={p['n']:>3} validated "
                      f"T={p['temperature']:.2f} "
                      f"ECE {p['ece_before']:.3f}->{p['ece_after']:.3f} "
                      f"Brier={p['brier_after']:.3f} "
                      f"cov={p['coverage_at_target']:.2f} "
                      f"risk={p['risk_at_target']:.3f} "
                      f"CI95={p['risk_ci95']}")
            else:
                print(f"  {key:<28} n={p['n']:>3} ADVISORY "
                      f"({p['reason']}) — global fallback")
        with open(args.profiles_out, "w") as f:
            json.dump(doc, f, indent=2)
        print(f"saved profiles -> {args.profiles_out}")

    if args.plots:
        try:
            from .calibration import cross_val_calibrate
            from .plots import reliability_pair, risk_coverage_plot
            cal, _ = cross_val_calibrate(raw, k=args.folds)
            p1 = reliability_pair(raw, cal, path=args.plot_path)
            p2 = risk_coverage_plot(cal)
            print(f"wrote {p1}")
            print(f"wrote {p2}")
        except ImportError:
            print("[--plots needs the 'plots' extra: pip install 'poordjaevin[plots]']")
    return 0


def cmd_ask(args) -> int:
    import json

    from .mcp_server import load_temperature

    temperature = load_temperature(args.calibrator)

    if args.state_file:
        with open(args.state_file) as f:
            state = f.read().strip()
    else:
        state = args.state
    if not state:
        print("provide --state or --state-file", file=sys.stderr)
        return 1

    with open(args.questions) as f:
        specs = json.load(f)   # {name: {"type":..., ...}}

    from .evaluate import _build_primitive
    questions = {name: _build_primitive(spec) for name, spec in specs.items()}
    try:
        res = _env_or_local_client(
            calibrator_path=args.calibrator,
            temperature=temperature).ask(state, questions)
    except Exception as exc:  # noqa: BLE001 — same fail-closed contract as `gate`
        print(json.dumps({"error": "backend", "detail": str(exc)}))
        return 1

    print(json.dumps({
        name: {
            "value": ans.value,
            "confidence": round(ans.confidence, 4),
        } for name, ans in res.items()
    }, indent=2))
    return 0


def cmd_gate(args) -> int:
    import json

    from .mcp_server import build_client, do_gate

    if args.action_file:
        try:
            with open(args.action_file) as f:
                action = f.read().strip()
        except OSError as exc:
            print(json.dumps({"error": "io", "detail": str(exc)}))
            return 2
    else:
        action = args.action
    if not action:
        print("provide --action or --action-file", file=sys.stderr)
        return 2

    try:
        out = do_gate(build_client(args.calibrator), action)
    except Exception as exc:  # noqa: BLE001 — a gate must fail closed
        print(json.dumps({"error": "backend", "detail": str(exc),
                          "verdict": "block, require explicit confirmation"}))
        return 1
    print(json.dumps(out, indent=2))
    return 1 if out["block"] else 0


def cmd_serve(args) -> int:
    from .mcp_server import main as serve_main
    print("Starting poordjaevin MCP server (stdio). Add it to your MCP client config.",
          file=sys.stderr)
    serve_main(calibrator_path=args.calibrator)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="poordjaevin", description="a local-first decision layer")
    sub = p.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("eval", help="score the eval set (accuracy + calibration)")
    pe.add_argument("--set", default="evalset/tasks.jsonl", help="path to tasks.jsonl")
    pe.add_argument("--plots", action="store_true", help="write the reliability diagram")
    pe.add_argument("--plot-path", default="reliability.png")
    pe.add_argument("--verbose", action="store_true", help="print each item's answers")
    pe.set_defaults(func=cmd_eval)

    pa = sub.add_parser("ask", help="answer typed questions about one state")
    pa.add_argument("--state", default="", help="the state text")
    pa.add_argument("--state-file", help="read state from a file")
    pa.add_argument("--questions", required=True, help="JSON file: {name: spec}")
    pa.add_argument("--calibrator", default="calibration.json",
                    help="JSON from `poordjaevin calibrate` (applies the fitted temperature)")
    pa.set_defaults(func=cmd_ask)

    pc = sub.add_parser("calibrate", help="fit calibration on the eval set")
    pc.add_argument("--set", default="evalset/tasks.jsonl")
    pc.add_argument("--folds", type=int, default=5)
    pc.add_argument("--target-risk", type=float, default=0.1)
    pc.add_argument("--plots", action="store_true", help="write before/after diagrams")
    pc.add_argument("--plot-path", default="docs/reliability_before_after.png")
    pc.add_argument("--out", default="calibration.json")
    pc.add_argument("--profiles-out", default="calibration_profiles.json",
                    help="write per-(task,decision_type) profiles here; "
                    "empty string disables")
    pc.add_argument("--min-samples", type=int, default=30,
                    help="minimum slice mass for a validated profile "
                    "(below stays advisory and falls back to global)")
    pc.set_defaults(func=cmd_calibrate)

    ps = sub.add_parser("serve", help="run the MCP server (for Devin, Claude Code etc.)")
    ps.add_argument("--calibrator", default="calibration.json",
                    help="calibration.json to apply (fitted temperature)")
    ps.set_defaults(func=cmd_serve)

    pg = sub.add_parser(
        "gate",
        help="advisory risk gate for one action — JSON verdict; exit 0 "
        "allow, 1 block/requires-confirmation, 2 usage/io error. Fails "
        "closed: a backend error exits 1 with a block verdict.")
    pg.add_argument("--action", default="", help="the action text to gate")
    pg.add_argument("--action-file",
                    help="read the action text from a file")
    pg.add_argument("--calibrator", default="calibration.json",
                    help="calibration.json to apply (fitted temperature)")
    pg.set_defaults(func=cmd_gate)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
