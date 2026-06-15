# Privacy

Inkfoot runs on your machine and keeps your data on your machine. The
runs, events, token counts, costs, and — in replay mode — request and
response bodies all live in your local SQLite database (or your own
Postgres server). Inkfoot never sends any of it anywhere.

The one and only thing Inkfoot can send off the machine is a single,
anonymous **install ping**, and only after you have explicitly said
yes. This page documents exactly what that ping contains, when it
fires, and how to turn it off and keep it off.

## What is never sent

Your data never leaves your environment. None of the following is
ever transmitted by Inkfoot:

- prompts, messages, system instructions, or tool definitions;
- model outputs, completions, or tool-call arguments;
- token counts, costs, nanodollar totals, or any ledger figures;
- run names, task names, tags, metadata, or outcome labels;
- file paths, hostnames, usernames, IP-identifying fields, or
  environment variables;
- API keys or any provider credentials.

There is no background reporting, no usage stream, and no "phone home"
on the call hot path. The only network calls Inkfoot makes on its own
behalf are the optional install ping described below and the
OpenTelemetry export you configure explicitly with
`otel_export_endpoint`.

## The install ping

### It is off by default

With no prior choice recorded and no environment override, telemetry
is **denied**. The first time `inkfoot.instrument()` runs in an
interactive terminal, Inkfoot asks once:

```
Inkfoot can record an anonymous install ping to help us understand
usage. None of your data is sent. Enable? (y/N)
```

The default is **No** — pressing Enter, answering anything other than
`y`/`yes`, or aborting the prompt all decline. Your answer is recorded
in `~/.inkfoot/telemetry.json` so you are never asked again.

In any non-interactive context — CI, a container, a service process, a
notebook kernel — the prompt is **never shown**. Inkfoot records
"denied" and continues. It will never block startup waiting for an
answer.

### Exactly what it contains

If, and only if, you opt in, Inkfoot sends one HTTP request carrying
this payload and nothing else:

| Field | Example | What it is |
|---|---|---|
| `event` | `"install"` | A constant string marking the ping kind. |
| `installation` | `"9f86d081…"` (64 hex chars) | The SHA-256 of a random id generated locally on first run. The raw id stays in `~/.inkfoot/telemetry.json` and is never transmitted; only this hash is sent, so repeat pings can be recognised as the same install without the value being reversible to anything on your machine. |
| `inkfoot_version` | `"1.0.0"` | The installed Inkfoot version. |
| `python` | `"3.12"` | The Python `major.minor` only. |
| `os` | `"Linux"` | The operating-system family from `platform.system()` (`Linux`, `Darwin`, or `Windows`). |

That is the complete payload. There are no other fields.

### It fires at most once per version

The ping is sent a single time per installation per version. Re-running
`inkfoot.instrument()` does nothing further; upgrading and running
again sends one more ping (so adoption can be sized across versions),
and never more than that. Delivery runs on a background thread with a
short timeout — it never delays your program, and if it fails (no
network, a blocked proxy, a collector that is down) it is silently
dropped.

## Turning it off and keeping it off

You have several ways to decline, all of which suppress both the
prompt and any ping:

| Mechanism | Effect |
|---|---|
| Answer `N` at the prompt | Records "denied" permanently in `~/.inkfoot/telemetry.json`. |
| `DO_NOT_TRACK=1` | Honours the cross-tool [Console Do Not Track](https://consoledonottrack.com/) convention; forces telemetry off and never prompts. |
| `INKFOOT_DO_NOT_TRACK=1` | Inkfoot-specific opt-out; same effect. |
| `INKFOOT_TELEMETRY=0` | Explicit off for scripted environments. |

To opt **in** without an interactive prompt — for example, to enable
the ping in a controlled CI environment — set `INKFOOT_TELEMETRY=1`.

The collector endpoint can be redirected with
`INKFOOT_TELEMETRY_ENDPOINT` (for self-hosters who want to point it at
their own sink), but redirecting is not required to disable telemetry —
any of the opt-outs above stop the ping at the source.

## Where the consent record lives

Your choice is stored in `telemetry.json` inside your Inkfoot home
directory (`~/.inkfoot` by default, or wherever `INKFOOT_HOME` points).
It holds the recorded consent flag, the locally generated installation
id, and the last version pinged. Delete the file to reset the choice;
the next interactive run will ask again.
