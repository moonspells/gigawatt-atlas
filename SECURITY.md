# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue:

- Email **security@moonspells.dev**, or
- open a private advisory: [Report a vulnerability](https://github.com/moonspells/gigawatt-atlas/security/advisories/new).

Include the steps to reproduce, what you expected to happen, and what happened instead.

## What to expect

- An acknowledgment within 3 business days.
- Triage within 7 days.
- Serious issues fixed as fast as possible.
- Credit on the colophon if you want it. There is no bounty.

## Scope

In scope: this repository (`moonspells/gigawatt-atlas`), its GitHub Actions workflows, the
releases published on `tiles.moonspells.dev`, and, once it is live, the Atlas MCP server on
`mcp.moonspells.dev`. Only the current `main` branch gets fixes.

Good-faith research is welcome. Out of scope: denial of service, spam, social engineering, physical
attacks, and anything that touches other people's data.

A wrong value in the data, or a request to take something down, is not a security report:

- a wrong value: open an issue in this repository (no personal data in it);
- a takedown (personal data, legal notices): never a public issue. Email **hello@moonspells.dev**
  with "Atlas takedown" in the subject ([DATA-LICENSE.md](DATA-LICENSE.md#corrections-and-takedowns)
  says what to include). A private moonspells.dev/contact form takes over once it ships.

The full policy is at [moonspells.dev/colophon/#security](https://moonspells.dev/colophon/#security),
and the machine-readable contact is at
[/.well-known/security.txt](https://moonspells.dev/.well-known/security.txt).
