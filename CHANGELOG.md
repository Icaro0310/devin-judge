# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-03

### Added

- Initial public release of poordjaevin, an open source local-first
  decision layer for LLM apps: typed `Choice` / `Score` / `Noul` questions
  answered in one pass with calibrated confidence.
- Calibrated decision layer: temperature scaling fit by k-fold CV plus
  conformal abstention. On the shipped eval set it cuts calibration error
  (ECE) from 0.170 to 0.071 with zero loss of accuracy.
- Devin ACP backend as the default: `poordjaevin serve` scores through the
  model your Devin CLI already uses, with automatic model rotation and
  per-call cost telemetry. No extra model download, no Ollama, no API key
  beyond Devin's own credentials. The keyless offline NLI backend remains
  as `POORDJAEVIN_BACKEND=nli`.
- MCP server mode (`poordjaevin serve`, stdio): exposes gate, judge,
  classify, rate and decide tools so Devin or any MCP client can make
  fast, calibrated decisions; `server.json` manifest for the MCP registry.
- Hand-labelled eval set (`evalset/tasks.jsonl`), stdlib-only metrics
  (accuracy, ECE, Brier, risk-coverage) and reliability diagrams via
  `poordjaevin eval` and `poordjaevin calibrate`.
- CLI: `poordjaevin eval | calibrate | ask | serve`. PyPI publish workflow
  shared via the ecosystem's reusable `pypi-publish.yml`.
