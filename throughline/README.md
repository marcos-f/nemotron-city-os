# throughline

## What it is

**throughline** is the core execution substrate for the federation: a
hash-chained provenance ledger, a reversibility-typed effect gate, a durable
approval queue, and a config loader that **refuses to auto-execute
irreversible effects**. Every other repo in this federation ingests signals
and proposes effects; throughline is where those effects are typed, gated,
recorded, and — for anything irreversible — held for a human.

The name is literal: a throughline is the thread that runs unbroken from
cause to consequence. Nothing here executes an effect without first being
able to point back to the signal, the actor, and the ledger entry that
justify it.

## Feature-area coordinate

`feature-area://throughline`

## Role in the federation

Every producer repo (docket, breaker, siren, blindspot) emits signals that
ultimately pass through throughline's gate before becoming an effect in the
world. helm reads throughline's approval queue and ledger to give operators
(human or agent) visibility and control. throughline itself produces
nothing domain-specific — it is the substrate the rest of the system stands
on.

## Contract surfaces

- OpenAPI: `contracts/openapi.yaml`
- opencli: `contracts/opencli.yaml`
- MCP: `mcp/tools.json`

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m throughline          # serves on :8600, /docs for the API
```

Environment: `THROUGHLINE_PORT` (default 8600), `THROUGHLINE_HOST`,
`THROUGHLINE_DATA_DIR` (default `data/`), `THROUGHLINE_CONFIG` (default
`config/effects.yaml`), `THROUGHLINE_CONFIG_DIRS` (extra directories
`/config/reload` may read from, `:`-separated — it only ever narrows what is
accepted), `THROUGHLINE_LEDGER` (serve against a specific chain file).

### Pinned demo chain

The live ledger grows every time anyone touches the system, so evidence that
cites "the refusal at seq 96" goes stale within the hour.
`fixtures/demo-ledger.jsonl` is a chain that does not move, and
`fixtures/demo-ledger.head.json` records its head hash, its length and the
sequence number of every beat worth citing.

```bash
cp fixtures/demo-ledger.jsonl /tmp/demo/ledger.jsonl
.venv/bin/throughline serve --ledger /tmp/demo/ledger.jsonl   # or THROUGHLINE_LEDGER
```

Serve a **copy**: a running service appends to whatever chain it is given, and
the fixture is meant to stay put. Record the head hash reported by `/healthz`
in any artifact you produce, so a claim is tied to the chain state it was true
for. Regenerate with `python3 scripts/make-demo-ledger.py --regenerate`. The
live path is untouched by all of this — omit the flag and nothing changes.

## The five properties that carry the pitch

1. **The ledger is written before the effect runs.** `data/ledger.jsonl` is
   append-only, each line carrying `{seq, prev_hash, sha256}`. If the ledger
   cannot be written, the effect does not run — the API answers `503` and
   says so.
2. **One chain, however many writers.** Every append takes an exclusive file
   lock and re-reads the tail under it, so a second process — a restart racing
   a running instance, an operator starting the service twice — extends the
   same chain instead of forking it. A chain that fails verification at boot
   raises an alarm into the ledger and shows red on `/healthz`; it is never
   quietly repaired.
3. **Verification can fail.** `GET /ledger/verify` recomputes every digest
   and every link. Corrupt one byte of one line and it returns
   `valid: false` naming the sequence number.
4. **Irreversible effects wait for a human.** The class comes from the effect
   registry, not from the caller's claim; an unregistered effect type is
   treated as irreversible. Queued effects have no deadline and no timeout
   code path — `POST /approvals/{id}/decide` is the only way one ever
   executes, and only from an authenticated caller presenting a decision
   that names the authority which vouched for the approver. See *Who may
   release*.
5. **An unsafe config is refused whole.** A rule marking an irreversible
   effect `auto_execute: true` refuses the entire file, naming file, rule id
   and line, and the refusal is ledgered. A refused reload leaves the
   previously running config untouched.

### Who may release

Releasing an irreversible effect takes three things, and the substrate itself
checks all three. It used to check only the first, which is how a review
executed a `payment.release` with three unauthenticated curls.

1. **An authenticated caller.** `POST /approvals/{id}/decide` requires the
   header `X-Throughline-Caller-Token`. See *Who may reach the gate* below.
2. **A declared role the substrate recognises**, and one that agrees with the
   subject.
3. **An attestation**: an `auth_mode` on the accepted allowlist, naming the
   `issuer` that vouched for `decided_by`.

Six named refusals live on that path, each one ledgered with a
`refusal_reason`, each one leaving the approval **pending** and the effect
**held**:

| `refusal_reason` | when |
| --- | --- |
| `caller-token-required` | the caller presented no valid caller token (ledgered as `write.refused`) |
| `caller-role-required` | the field is missing or empty |
| `caller-role-unrecognised` | it is anything but `human` or `agent` |
| `agent-may-not-approve-irreversible` | a declared agent approves an irreversible effect |
| `agent-subject-may-not-claim-the-human-role` | `decided_by` names an agent principal (`agent:`, `bot:`, `svc:`) while the role says `human` |
| `unattested-decision-may-not-release-irreversible` | approving an irreversible effect with an `auth_mode` that is absent, empty or not on the allowlist |
| `attestation-names-no-issuer` | `auth_mode: oidc` with no `issuer` — an authority nobody can be sent back to |

The accepted auth modes are an **allowlist**, defaulting to `oidc` alone,
which is exactly helm/signet's own rule for a verified identity. `/healthz`
publishes the live list as `gate.attested_auth_modes`. A console running mock
login cannot release an irreversible effect unless an operator widens the
allowlist deliberately with `THROUGHLINE_ATTESTED_AUTH_MODES=oidc,mock`; that
widening is visible in `/healthz`, in every refusal row, and in the
`auth_mode` recorded on each release. The one thing it cannot do is readmit a
decision that sends **no** `auth_mode`: an empty entry is discarded, so the
request the reviewer actually used stays refused at every setting.

Scope, precisely: this applies to **approving an irreversible effect**.
Rejecting needs no attestation — refusing to act is never the dangerous
direction — and a reversible effect held by rule releases exactly as it did
before. Reversible auto-executing effects (docket routes every permit that
way) never reach this path at all.

The earlier round of this fix recorded `auth_mode: unattested` on the ledger
entry and executed the payment anyway. That preserved the audit trail and none
of the restraint. Labelling a hole is not closing it, and a document
describing a hole is not a gate.

**What this still is not.** throughline does not *verify* the attestation. It
holds no session, no cookie and no signing key, so a caller that presents a
valid caller token and fabricates `auth_mode: oidc` with a plausible issuer is
recorded as attested and released. Verification of the human belongs to helm,
which authenticates the session and binds the attestation to it. What the
substrate now guarantees on its own is narrower and worth stating exactly:

* an irreversible effect is never released on a decision that claims no
  authority at all, and
* the caller making that claim is authenticated to this substrate.

### Who may reach the gate

throughline cannot authenticate a *person*. It can authenticate the *caller*,
and on the acts where forging one changes who may do what, it does:

| act | needs `X-Throughline-Caller-Token` |
| --- | --- |
| `POST /approvals/{id}/decide` | yes |
| `POST /effects` with an `authz.*` effect type | yes |
| `POST /signals` with a `warrant.*` class | yes |
| everything else — ordinary signals, judgments, effects, all reads | no |

That split is deliberate. The fleet's ordinary traffic (docket's routes,
siren's alerts, breaker's probes) changes no permissions and stays open, so
this is deployable without a flag day; the acts that *are* the authority model
are closed.

The token comes from `THROUGHLINE_CALLER_TOKEN`, or failing that from a file
the substrate mints for itself at `<data-dir>/caller-token`, mode 0600. There
is no way to switch the check off. A caller that can read that file is a
caller with access to the substrate's own data directory; a caller that can
only reach the port is not. `/healthz` reports where the token lives under
`gate.caller_token`, never the token itself. The bundled CLI and
`scripts/smoke.py` read it automatically.

A refusal here is ledgered as `write.refused` with `refusal_reason:
caller-token-required`, so an attempt against the substrate leaves a row in
the chain even though nothing else about it is recorded.

**Residual risk, stated rather than implied.** A same-host process running as
the same user can read the token file, and on this deployment the assistant is
such a process. Closing *that* needs an OS boundary (a separate uid, or a
socket with peer credentials), which is a deployment change and not a code
one. What the token removes is the "anything that can reach port 8600" class
of caller, which is what every proven exploit so far has been.

### Who may change what the gate holds

The effect registry decides which effects are held, so the reload surface is
part of the gate:

* `POST /config/reload` reads only from the allowlisted config directory
  (`/healthz` reports it as `config.allowlist`). Any other path is refused
  403, and the offending path is named in the ledger as `config.refused` with
  policy `config-source-must-be-allowlisted`.
* A candidate that would reclassify **any** effect type down to `reversible`
  — including a type that was merely unregistered, since the gate treats
  unregistered as irreversible — is not applied. It is proposed as an
  irreversible `config.reversibility_downgrade` effect and held for a human
  (202), and `config.downgrade_held` records exactly what would have been
  loosened. Approving it applies the config; rejecting it does not.

Both of these close holes a security review proved: two unauthenticated curls
reclassified `grid.load_shed` and executed a load-shed with no human and no
approval record.

### What a grant confers decides its class

`authz.grant` is a reversible, auto-executing registry row, because granting
somebody the reader role is genuinely undoable. `authz.grant.admin` is
irreversible and held, because widening who may widen access is not. A review
found the obvious consequence: an `authz.grant` carrying `role: admin` matched
the reversible row, auto-executed, and never touched the gate — while warrant,
reading the same record, rendered it in the chain as `authz.grant.admin`, the
type that *is* held. The registry row and the rendered label disagreed, and
the label was the honest-looking one.

The gate now reads what the effect **confers**, not what it is called. For
`authz.grant` and `authz.delegate`, a payload naming an escalating role
(`admin`, `owner`, `federation-steward`) is classified irreversible and held
under `RULE-017` whatever the type string says. The role is read from the
proposal's `payload`, or from the `payload_ref` of the signal it names as its
cause — warrant writes it in the second place, so both are checked. The
ledger records `class_source: payload-confers-admin`, so an auditor can see
that the class came from the payload rather than the row.

One exemption, named rather than buried: `authz.dataset.claim` still
auto-executes and still confers administration, because onboarding cannot
require a pre-existing administrator to approve it (RULE-006). A caller
authenticated to this substrate can therefore still claim an *unclaimed*
dataset. What stands in front of that is the caller token and warrant's
already-claimed guard, not the gate.

### The queue's file is a cache, the chain is the record

`data/approvals.json` is a convenience snapshot. On boot the queue is rebuilt
from the ledger's `effect.queued` and `approval.decided` entries; the cache can
only add holds the chain never saw, never contradict it, and a release the
chain does not record leaves the effect **held**. A cache that fails to load
is an alarm — logged, appended as `approvals.alarm`, and reported by `/healthz`
as `status: degraded` with the reason — because the previous behaviour was to
return an empty queue, which showed an operator a clear gate while holds were
still pending in the chain.

## The cause walk

`GET /effects/{id}/walk` resolves an effect back to its cause in at most three
hops — effect → judgment → signal — recomputing each hop's digest at read time.
A hop whose content was altered comes back `verified: false`, which is what
makes the ticks in helm's pane worth drawing. Producers post their judgments to
`POST /judgments` and reference them as `judgment_id` on the effect.

## NeMo Relay

Effects execute as NeMo Relay 0.7.3 tool calls behind a conditional-execution
guardrail registered with `register_tool_conditional_execution`; the
guardrail's `{allowed, rejected, rejection_reason}` verdict is mirrored into
the ledger. Relay ships lineage, not integrity — the hash chain is ours, and
the Relay record is corroboration. Where the package is unavailable the
mirror runs in `mock` mode and every record says so.

## Command line

`contracts/opencli.yaml` is implemented by the installed `throughline` script,
which drives the HTTP API rather than reimplementing it — the gate, the ledger
and the refusal behave the same whichever way you call in.

```bash
throughline serve                                     # or: python -m throughline
throughline signal ingest --class fire.incident --source seattle.fire.911
throughline effect propose --effect-type payment.release --signal-id sig-1
throughline approval list
throughline approval decide --id apr-1 --decision approve --decided-by "oidc|you"
throughline ledger verify                             # exits 1 on a broken chain
throughline effect walk --id eff-1
```

`ledger verify` exits non-zero when the chain is broken, so it is usable as a
check in a pipeline rather than a message on a screen.

## Federation

Consumers post their signals to `POST /signals` and propose effects to
`POST /effects`; helm reads `GET /approvals` and `GET /ledger`. The approval
record carries both `state` and `status` — the same value under both names —
because helm's published contract uses `status` and ours used `state`.

`integration/feeds.json` holds one signal envelope per producer feed, shaped by
each sibling's published contract on their merged main (SHAs recorded in the
file). `scripts/check-sibling-contracts.py` re-reads those merged mains and
fails on drift; run it before switching anything from mock to real.

## Data provenance

**throughline ingests no external dataset.** It has no source to cite,
because it produces its own data: every row it holds is something a producer
posted to it or something it wrote about that posting.

| Artifact | What it is | Real or synthetic |
| --- | --- | --- |
| `data/ledger.jsonl` | The hash chain. Append-only, one JSON object per line carrying `{seq, prev_hash, sha256}`. **Written by this service at runtime**, never seeded and never edited. | generated |
| `config/effects.yaml` | The effect registry — the reversibility class per effect type, and the rules. **Authored**, and the authority for the class: the caller's claim is never trusted. | authored |
| Signals, judgments, effects | Posted in by `docket`, `breaker`, `siren` and `blindspot`. **Their provenance is theirs** — see each producer's own `## Data provenance`. | per producer |
| `integration/feeds.json` | One signal envelope per producer feed, shaped by each sibling's published contract on their merged main, with the SHAs recorded in the file. | authored from contracts |

Two consequences worth stating. First, the ledger is **not reproducible** —
it is a record of a particular run, and two runs produce two different
chains. Verifiability is the property it offers, not determinism. Second,
because the class comes from the registry rather than the caller, an
**unregistered effect type is treated as irreversible** — the safe default
when provenance for the class is missing.

## Known limitations

1. **The role gate is a backstop, not authentication.** throughline cannot
   tell an undeclared agent from a human; it can refuse a caller that declares
   itself `agent`, a caller that declares nothing, a caller that declares
   nonsense, and a caller whose subject contradicts its declared role. It
   cannot refuse a hostile caller who simply says `human` with a
   human-looking subject. `helm` holds the authenticated identity and its
   contract is where the role gate is really enforced.
   Also here: `/config/reload` and every other endpoint are **unauthenticated**.
   The allowlist and the downgrade hold mean an unauthenticated caller can no
   longer loosen the gate, but they are containment, not authentication.
1. **An untyped effect is classified on the caller's word.** A proposal with
   no `effect_type` that claims `reversibility: reversible` auto-executes;
   docket routes permits this way. The registry has nothing to check it
   against, so the chain records `class_source: caller-claim` on the
   `effect.proposed` entry rather than pretending the registry asserted it.
   A *typed* effect the registry does not know is irreversible, always.
2. **The queue is unbounded and nothing expires.** Queued effects have no
   deadline and no timeout code path, which is the only correct behaviour for
   an irreversible effect — and it means `approvals_pending` only ever grows
   across a long-lived demo. A run of this service had 219 pending approvals
   against a 1025-entry chain when this was written. Nothing is wrong; it is
   simply what "waits forever" looks like after a few hours.
3. **The ledger grows without bound and is never compacted.** No rotation, no
   pruning, no archive. Deliberate — pruning a hash chain is how integrity
   claims quietly die — but it is not an operational story.
4. **Verification is O(chain).** `GET /ledger/verify` recomputes every digest
   and every link on every call. It is honest and it gets slower.
5. **NeMo Relay may be mocked.** Effects execute as Relay 0.7.3 tool calls
   behind a `register_tool_conditional_execution` guardrail; where the
   package is unavailable the mirror runs in `mock` mode and **every record
   says so**. Relay ships lineage, not integrity — the hash chain is ours and
   the Relay record is corroboration, so a mocked mirror costs corroboration,
   not the integrity claim.
6. **This service runs no model.** Nothing here is Nemotron, DeepSeek or
   anything else; the gate is code. Model claims belong to the producers and
   to helm.
7. **No `LICENSE` file** and no `license` field in `pyproject.toml`.

## Tests

```bash
.venv/bin/python -m pytest tests --cov=throughline --cov-fail-under=80
bash scripts/bvt.sh     # clean venv, real boot, every contract path
```

## Orchestrator

See https://git.nemotron.example.com/nvidia-hackathon/nemo-nvidia-demo/nemo-nvidia-demo-system
