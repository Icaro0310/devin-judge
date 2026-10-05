# poordjaevin — Corporate Windows guide

This guide covers restricted Windows setup only. For unrestricted Windows, see [README.windows.md](README.windows.md); for features, shared commands, limitations, and the safety model, see [README.md](README.md).

Corporate Windows is a local-only environment: no Devin VM, QwenPaw, Slack dependency, external compute, workload delegation or required external integration.

## Prerequisites

- `uv` and Python 3.10 or newer; `uv` can manage Python.

## Install

Install the isolated Python CLI:

```powershell
uv tool install "https://github.com/Icaro0310/poordjaevin/archive/648be6e479a06c99494d3cbc1592f20d6d116bb0.tar.gz"
```

## Devin paths

Session data normally lives under `%APPDATA%\devin\cli\`; UI state and ACP stores under `%APPDATA%\Devin\User\`.
Use the tool's documented `--data-dir` or `--config-dir` flags for non-default locations.

## Environment notes

- Keep execution local; do not configure VM, QwenPaw, external compute or workload delegation.
- Registry-declared external integrations remain optional and are not installed by this guide.
- macOS is planned but not claimed as tested.

## Troubleshooting

- If a command is not found, reopen PowerShell and run `uv tool update-shell`.
