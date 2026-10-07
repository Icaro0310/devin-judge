# Security Policy

## What this tool does with your data

- **No telemetry.** This project sends nothing anywhere.
- **No network by default.** All processing is local unless a command
  explicitly says otherwise (and it will say so in `--help`).
- **Data stays on your machine.** Files it reads and writes are documented
  in the README.

## Sensitive data handling

- Output intended for sharing must pass through
  [`devin-redact`](https://github.com/Icaro0310/devin-redact) before publication.
- Never commit Devin session databases, `.env` files, tokens, or pairing codes.

## Reporting a vulnerability

Open a **private** security advisory on GitHub, or open an issue marked
`[SECURITY]` **without** including the vulnerable data itself.

Do not file public issues containing secrets, tokens, or session content.
