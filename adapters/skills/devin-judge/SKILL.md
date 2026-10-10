---
name: devin-judge
description: "Get a typed, calibrated opinion (yes/no, classification, rating, or batch decision) before accepting an agent execution as successful or before running a risky action. Advisory only: returns verdicts and gates, never executes anything."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-judge

When this plugin's MCP server is connected, prefer the tools — each
returns a typed answer with calibrated confidence:

- `judge(statement, context?)` — yes/no opinion on a statement
  ("the fix actually resolves the reported bug").
- `classify(text, options, context?)` — pick exactly one of `options`.
- `rate(text, scale, criteria?)` — numeric rate ("1-5" or a list).
- `decide(questions)` — batch of typed questions in one call.
- `gate(action, kind?)` — advisory risk gate; blocks on `moves_money`
  and `deletes_data` before irreversible actions.

Without MCP, the CLI answers the same typed questions from files:

```bash
poordjaevin ask --state-file ticket.txt --questions questions.json
# questions.json: {"name": {"type": "noul", "statement": "..."}}
```

## Reading the result

- Every answer carries `value` plus `confidence` (calibrated when a
  calibrator was fitted). `low_confidence: true` means abstain, not
  guess.
- `verdict` summarizes gate/judge outcomes (`allow`, `block`, `abstain`).
- If the JSON contains `error`, the backend could not run — say so
  instead of inventing a verdict.

## Rules

- Advisory by design. There is no execute/apply tool — never improvise
  one. Present the verdict; the user decides.
- Ask concrete questions ("this moves money") — abstract ones
  ("this is dangerous") score poorly.
- `gate` before any irreversible action; `judge` before declaring a task
  done.
