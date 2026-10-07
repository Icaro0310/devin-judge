# Security Policy

## What this tool does with your data

- **No telemetry.** This project sends nothing to analytics or tracking.
- **Remote judging is the point.** The `acp` backend sends the decision
  text you submit to your configured Devin endpoint so a model can score
  it. If you need local-only behavior, do not configure a remote backend.
- **Data stays on your machine** unless you configure a remote backend
  as above. Files it reads and writes are documented in the README.

## Sensitive data handling

- Text submitted for judging travels to the configured backend. Do not
  submit secrets, tokens, or session content you would not send to that
  endpoint.
- Never commit `.env` files, tokens, or pairing codes.

## Reporting a vulnerability

Open a **private** security advisory on GitHub, or open an issue marked
`[SECURITY]` **without** including the vulnerable data itself.

Do not file public issues containing secrets, tokens, or session content.
