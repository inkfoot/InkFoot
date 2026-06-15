# Security Policy

Thanks for helping keep Inkfoot and its users safe. This document
explains which versions receive security fixes and how to report a
vulnerability privately.

## Supported versions

Security fixes land on the latest released minor of the current major,
published to PyPI as a patch release.

| Version | Supported |
|---|---|
| `1.0.x` | ✅ |
| `< 1.0` (pre-release builds) | ❌ |

If you are on an older line, upgrade to the latest `1.0.x` before
reporting — the issue may already be fixed.

## Reporting a vulnerability

**Please do not open a public GitHub issue, pull request, or discussion
for a security problem.** Public disclosure before a fix is available
puts every user at risk.

Use either private channel:

1. **GitHub private vulnerability reporting** (preferred). On the
   repository, go to **Security → Report a vulnerability**. This opens a
   private advisory visible only to you and the maintainers, and keeps
   the whole exchange — including any fix — confidential until we
   publish.
2. **Email** `security@inkfoot.dev`. If you would like to encrypt your
   report, ask for our current PGP public key in your first message (or
   fetch it from the security page on `inkfoot.dev`) and we will reply
   with an encrypted thread.

A good report includes:

- the Inkfoot version (`python -c "import inkfoot; print(inkfoot.__version__)"`),
  Python version, and operating system;
- a description of the issue and its impact (what an attacker can do);
- the smallest steps or proof-of-concept that reproduces it;
- any known mitigations or workarounds.

Please report **privately first** and give us a reasonable window to
ship a fix before any public write-up.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement of your report | within **3 business days** |
| Initial assessment + severity triage | within **7 business days** |
| Fix or mitigation plan shared with you | within **30 days** for confirmed issues |

We practise coordinated disclosure: we will agree a disclosure date
with you, ship the fix to PyPI, publish a GitHub Security Advisory with
a CVE where warranted, and credit you in the advisory unless you ask to
remain anonymous.

## Scope

In scope: the `inkfoot` Python package, its command-line interface, the
storage backends, and the published `inkfoot/diff-action` GitHub Action.

Out of scope: vulnerabilities in third-party provider SDKs or
frameworks Inkfoot integrates with (report those to their maintainers),
issues that require a pre-compromised machine or a malicious local
operator, and findings against the documentation site's hosting
infrastructure.

## A note on what Inkfoot stores

Inkfoot keeps run data on your own machine (local SQLite by default, or
a Postgres server you control) and does not transmit your prompts,
outputs, or costs anywhere. In replay mode, request and response bodies
are written to that local store after passing through the redaction hook
(see the privacy guide). When reporting an issue, please redact any
secrets — API keys, tokens, customer data — from logs and reproductions
you attach.
