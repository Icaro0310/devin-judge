# Devin Judge

Calibrated, advisory decisions on the chat model VS Code is already
serving: Copilot, Codex or BYOK, whichever `vscode.lm` can reach. Two
surfaces:

- **`@judge` chat participant**: ask `/judge`, `/classify`, `/rate`,
  `/gate` or `/decide` in natural language; get a typed answer with a
  calibrated confidence.
- **Localhost scoring endpoint**: serves the same entailment-scoring
  contract as `djaevin-acp-bridge.mjs`, so `poordjaevin`'s
  `POORDJAEVIN_BACKEND=vscode-lm` can score through VS Code without an
  extra key or a separate model.

Zero runtime dependencies. All decision logic lives in `src/` (pure
TypeScript, no `vscode` import) with the extension-host wiring isolated
in `adapters/vscode/`.

## `@judge` commands

| Command | Prompt syntax |
|---|---|
| `/judge` | `@judge /judge the fix resolves the reported bug` |
| `/classify` | `@judge /classify my invoice is wrong \| billing \| technical \| other` |
| `/rate` | `@judge /rate the response \| 1-5` or `… \| poor,fair,good` |
| `/gate` | `@judge /gate delete the production database` |
| `/decide` | `@judge /decide {"state":"…","questions":{"ok":{"type":"noul","statement":"It works."}}}` |

With no slash command, `@judge <statement>` runs `judge`. Every answer
carries `confidence`, `confidence_source: self_report`, a
`low_confidence` flag (below 0.6) and a `verdict` band
(`strong_no/no/review/yes/strong_yes`, or `confident/review`).
**Advisory only**: a verdict to weigh, never a command.

The `Devin Judge: Judge/Classify/Rate/Gate/Decide` commands prefill
`@judge /<cmd> ` in the chat input; the participant does the rest.

## The localhost endpoint

For `poordjaevin` consumers (`POORDJAEVIN_BACKEND=vscode-lm`). The
extension listens on `127.0.0.1` (random port) and writes a discovery
file the Python backend re-reads per request:

- linux: `$XDG_DATA_HOME/poordjaevin/vscode-lm-endpoint.json` (or
  `~/.local/share/…`)
- macOS: `~/Library/Application Support/poordjaevin/vscode-lm-endpoint.json`
- windows: `%LOCALAPPDATA%\poordjaevin\vscode-lm-endpoint.json`

The file (`{"url","token","pid","extension","version"}`, mode `0600`)
is written after `listen()` succeeds and removed on deactivate.
Protocol: `POST <url>` with `Authorization: Bearer <token>`;
`{"pairs":[[premise,hypothesis],…]}` → `{"probs":[0-100…],"model"}`;
`{"warmup":true}` → `{"warmup":true,"model"}`. `503` means no model is
available (the only fallback-eligible signal upstream); any other
scoring failure comes back `200 {"error": …}`, a protocol error, not
unavailability. Requests serialize: one model turn at a time.

### Model consent

`vscode.lm.selectChatModels` can show a consent dialog that requires a
user gesture, so **nothing touches `vscode.lm` at activation**. The
endpoint listens without a model; the first request that needs one
selects lazily. If a `503` surprises you: **run `@judge` once in chat to
grant model access**, then the endpoint becomes usable. Restrict which
model answers via `devin-judge.model.vendor` / `.family` / `.id`
(passed to `selectChatModels`; empty = whatever VS Code serves).

## Privacy

Scoring text (premises, hypotheses, `@judge` prompts) goes to the
provider of the chat model VS Code is serving: Copilot, Codex or your
BYOK provider, over their channel. The extension itself keeps no
history and sends nothing anywhere else. The HTTP endpoint only listens
on `127.0.0.1` and requires the bearer token stored in the
`0600`-protected discovery file.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `devin-judge.calibratorPath` | `calibration.json` | poordjaevin calibrator (`{temperature, abstain_threshold, backend}`), resolved against the first workspace folder. Re-read per request. A calibrator fitted for another backend is ignored (raw confidence + a warning in the output channel). Abstention needs `POORDJAEVIN_ABSTAIN=on`, same as upstream. |
| `devin-judge.model.vendor` / `.family` / `.id` | `""` | Optional `selectChatModels` filters. |
| `devin-judge.endpoint.enabled` | `true` | Set `false` to run only the `@judge` participant. |

Requires VS Code 1.100+. MIT.

## Development

`npm run build` compiles `src/` + `adapters/` to `out/` (ESM).
`npm test` runs the pure suites (`node --test`, Node ≥ 22.18 strips the
`.ts` natively). `npm run smoke` activates the compiled extension against
`test/stubs/vscode.mjs` and exercises the real loopback HTTP endpoint.
`npm run vscode:package` produces `devin-judge.vsix` (`npx @vscode/vsce`
if `vsce` is not on PATH).
**`engines.vscode` and `@types/vscode` move together**: `vsce` rejects
a types version newer than `engines`; bump both in the same commit.
