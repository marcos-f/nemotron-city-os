# Nemotron City OS

A governed-autonomy console for city-scale operations.

An LLM sits in the **condition** slot of an Event–Condition–Action loop. The
**action** is gated: an irreversible effect is held until an identified human
releases it. Every decision, and every refusal, lands on a hash-chained ledger.

Live: **https://nemotron.platformalchemist.com** — sign in with GitHub. Anyone
may sign in and read; only subjects named in the local role table may decide.

---

## The four beats

These are the demo. Each is verified against the real substrate, not a double:

| Beat | What it proves |
|---|---|
| **refusal-named** | A bad config is refused naming file, rule and line — `dispatch_units`, line 28, `no-auto-execute-on-irreversible` |
| **ledger-verify** | The chain verifies; a one-byte corruption names the offending sequence number |
| **gate-hold** | An irreversible effect is HELD, with no timeout path anywhere in the package |
| **agent-refusal** | The assistant attempts an approval, is refused **by role**, and its refusal is ledgered with its own principal |

## Components

| Directory | Port | Role |
|---|---|---|
| `throughline/` | 8600 | The ECA substrate: ledger, effect registry, reversibility gate, config loader, approvals |
| `docket/` | 8601 | Permit intake — Nemotron judgments with verbatim-quote validation and abstention |
| `breaker/` | 8602 | Microgrid telemetry — divergence rule, proposal emitter |
| `siren/` | 8603 | 911 incident intake — live poll, cached snapshot, hot reload |
| `helm/` | 8610 | Console, signet identity, NemoClerk assistant, MCP host, warrant authority |

Each directory is a self-contained Python package with its own `README.md`,
`pyproject.toml` and test suite. Start with the component you care about; each
README explains how to run it alone.


## Order matters

`throughline` is the substrate; everything else calls it. Start it first, then
any feed, then `helm`. A console booted against an empty federation renders
four *component offline* panes — correct behaviour that looks exactly like
breakage.

Each service binds `127.0.0.1` by design. `throughline` holds the gate, the
ledger and the approvals, and its only network control is a shared caller
token: **a publicly routable throughline is a publicly writable hash chain.**
Expose the console, never the substrate.

## Tests

```sh
cd throughline && python -m pytest -q
cd helm        && python -m pytest -q
```

Each component's suite runs against its own package.


## What is not true

A project claiming provable restraint should lead with its own limits:

- **The chain is tamper-evident, not tamper-proof.** A reviewer rewrote an
  entry, re-linked the digests, and verification still passed. No signatures,
  no external anchor. It detects accident and third parties, not a determined
  operator.
- **The dim-2048 retriever ingest is declared and unimplemented.** Retrieval is
  a linear scan. The constants remain and a test asserts nothing consumes them,
  so it fails the day the capability starts to exist.
- **cuOpt is a registered alternate**, labelled unavailable-without-GPU, not
  faked.
- **Dataset licences are asserted, not cited.** The registry records `unknown`
  where it cannot verify rather than guessing.
- **Video is batch, not streaming.**

## Provenance

Built for the NVIDIA Spark hackathon.
`nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` runs locally on an **NVIDIA
GB10 Grace Blackwell (DGX Spark)** and serves the console assistant. The model,
host and silicon recorded in each ledger entry are derived from what actually
served that turn rather than from configuration — an earlier build notarised
the GB10 while a stub answered, which is the defect that rule exists to prevent.

Data: Seattle Socrata `76t5-zqzr` (permits) and `kzjm-xkqj` (911 incidents);
`breaker`'s microgrid telemetry is synthetic and labelled so.
