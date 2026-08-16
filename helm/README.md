# helm

## What it is

**helm** is the operator console for the NemoCity federation, and **signet**
is the identity binding inside it. helm is where a human sees the whole loop
— four unrelated domains flowing through one substrate — and where the one
irreversible moment is put in front of a person.

Its thesis is a single sentence: *everything an agent operator can drive,
except approving its own irreversible effect.*

The name is the ship's wheel: helm is where a human (or a supervised agent)
actually steers the system, never the thing making irreversible calls
unsupervised.

## Feature-area coordinate

`feature-area://helm` · `use-case://helm/console` ·
`use-case://helm/agent-refused` · `use-case://signet/bind-identity`

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m helm            # serves on :8610, /docs for the API
```

| Variable | Default | Meaning |
|---|---|---|
| `HELM_PORT` / `HELM_HOST` | `8610` / `127.0.0.1` | where it serves |
| `HELM_ENV` | `dev` | `dev`/`test` honour the identity-minting flags; `staging`/`production` refuse to boot while anything can mint an identity, and refuse again if none is configured |
| `THROUGHLINE_URL` | `http://127.0.0.1:8600` | the substrate |
| `DOCKET_URL` `BREAKER_URL` `SIREN_URL` `BLINDSPOT_URL` | `:8601`–`:8604` | the sibling feeds |
| `HELM_OIDC_ISSUER` | unset | signet's real issuer, e.g. `https://git.nemotron.example.com` |
| `HELM_OIDC_CLIENT_ID` / `HELM_OIDC_CLIENT_SECRET` | unset | the OAuth client. **From the environment or the fleet secret store (`.secrets/nemocity-signet.yaml`) — never committed** |
| `HELM_OIDC_SCOPES` | `openid profile email read_user` | requested scopes |
| `HELM_OIDC_REDIRECT_PATH` | `/auth/callback` | appended to `HELM_PUBLIC_URL` to form the redirect URI, which must match the one registered with the provider |
| `HELM_OIDC_CA_BUNDLE` | unset | CA for an internally-signed issuer (`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` also read). There is no setting that disables verification |
| `HELM_OIDC_TIMEOUT` | `4` | seconds. Short on purpose: an unreachable provider must fail fast to the labelled mock, never hang the login page |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | unset | GitHub OAuth (kept; signet's earlier provider) |
| `MOCK_LOGIN` | `0` | fixed identity, **labelled MOCK LOGIN on screen** |
| `AUTH_DISABLED` | `0` | dev/test only — see above |
| `NEMOCLERK_BASE_URL` / `NEMOCLERK_MODEL` | `http://dgx-spark.nemotron.example.com:8000/v1` / `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | rung 1 — the DGX Spark (GB10) rung, and NemoClerk's primary model |
| `NEMOCLERK_FALLBACK_BASE_URL` / `NEMOCLERK_FALLBACK_MODEL` | `http://rtx-workstation.nemotron.example.com:9997/v1` / `qwen3.6-27b` | rung 2 — the RTX 5090 rung |
| `HELM_OFFLINE` | `0` | never contact a model; replay cached turns, then labelled MOCK |

helm is the **last** thing to start. Each tab reads its sibling live on that
sibling's fixed port, so a helm booted against an empty federation renders
four **component offline** panes — correct behaviour that looks exactly like
a broken console. Bring `throughline` up first, then the feeds, then helm;
the full boot order is in the orchestrator README's *Run the whole thing*.

## The demo

`scripts/demo.py` **is the demo** — the scripted sequence, in order, recorded
to `artifacts/demo-run.json`. It is not a test, though CI also runs it as
one: judgment, divergence, hold, agent-refusal, approve, walk, hot-reload,
refused-config, grounded answer.

```bash
# Against the LIVE federation on its fixed ports. This is what the recorded
# evidence run does. Boot the substrate and the feeds first.
.venv/bin/python scripts/demo.py

# Self-contained. Boots an in-process, contract-faithful substrate and arms a
# socket guard that refuses every outbound connection — and the guard is
# itself asserted to bite, so "offline" is proven rather than claimed.
# Nothing else installed, no ports, no federation.
.venv/bin/python scripts/demo.py --offline --self-contained
```

Observed on the self-contained path: **9/9 steps passed, complete=True,
offline guard bites: True**. Note that `--self-contained` walks 2 hops rather
than 3 — the in-process substitute seeds a shorter chain — so the full
effect → judgment → signal walk is a live-federation result.

If someone wants to see the demo and does not want to boot six services, the
second command is the answer, and it needs nothing but this repository.

## The five things it does

1. **Renders the loop.** The 3-column app frame from
   `.viper-context/specs/v0.1.0/ux/wireframes.md` rev 6: edge-docked feeds
   rail, centre canvas, NemoClerk chat rail — both rails resizable, both
   collapsible, both remembered per signet subject. The **StageTimeline**
   (signal → judged → gated → approved → executed → ledgered) puts every
   event's position in the one loop at a glance.
2. **Proxies the real siblings.** Each tab reads the sibling on its fixed
   port. A sibling that is not running does not get faked: its tab renders
   the frozen wireframe pane, labelled **component offline**. blindspot is
   the standing example — designed, not built.
3. **Gates approvals by role.** `decided_by` (the signet subject) is required
   by the schema; the ROLE decides whether the decision is allowed at all.
   `viewer` may read and not decide. `agent` holds every read and reload tool
   and none of the approve ones.
4. **Hosts NemoClerk.** A tool-grounded assistant in the right rail with a
   session per `(subject, feature-area)`. Grounding is a mechanism, not a
   prompt: a data question executes tools and the answer is composed from the
   tool RESULTS. **No chip, no claim.**
5. **Refuses itself, on the record.** When NemoClerk tries to approve an
   irreversible effect, helm refuses it by role and throughline appends the
   refusal to the ledger with the principal
   `agent:nemoclerk(<model>@<tier>)`. Two inches away, the same human's
   approval succeeds. That is the closing argument.

## NemoClerk's ladder

Named in the rail header, degraded automatically, announced honestly. This
table is not prose about the ladder — it is what `GET /nemoclerk/runtime`
returned on 2026-08-16, and the ordering in code is `Ladder.tiers()` in
[`helm/nemoclerk/agent.py`](helm/nemoclerk/agent.py):

1. **DGX Spark (GB10)** — `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`,
   vLLM on NVIDIA GB10 Grace Blackwell at `dgx-spark:8000`. **An NVIDIA
   model on NVIDIA silicon, locally, with no internet** — this rung makes the
   hardware claim and the model claim at once, and it is the rung that
   actually answers. It has **no tool-call parser** (`chooses_tools: false`),
   so helm sends `/no_think` and grounds the turn with its own deterministic
   router; grounding is a mechanism here, never a capability rented from a
   model. It reports `vision: true`.
2. **RTX 5090** — `qwen3.6-27b`, llama.cpp at `rtx-workstation:9997`, ~82 tok/s
   with thinking off. The fallback, and the only rung that can also *choose*
   tools (`chooses_tools: true`). Qwen3-class models are reasoning models:
   helm always sends `chat_template_kwargs: {enable_thinking: false}`, and an
   empty content string is treated as a failed turn, never rendered as a
   blank bubble.
3. **Cached** scripted turns — the demo replays with the network off, and
   says on screen that the turn is cached. The ledgered principal becomes
   `cache@<tier>`, so a replay never wears a live model's name.
4. **Labelled MOCK** — a deterministic composer over the same tool results.
   Principal `mock@none`, announced on screen.

There is **no DeepSeek rung.** `deepseek-v4-flash-0731` on `dgx-spark:18000`
was retired to free the GB10's 121 GiB for the Nemotron above; the port refuses
connections (verified 2026-08-16). Earlier revisions of this README described
that rung as live — it is gone, and this section is regenerated from the
endpoint rather than carried forward.

Both live rungs are shared infrastructure with tight concurrency limits
(`--enforce-eager`, `--max-num-seqs 2` on the GB10; `-np 1` on the 5090), so
helm serializes its model calls behind one lock and keeps answers short. A
rung earns its name **per turn**: a transport failure evicts it and forces a
re-probe, so the principal written into the ledger names what actually served.
CI and pytest never contact them; the live smoke test skips when the endpoint
is unreachable.

Observed live, `HELM_OFFLINE=0`, 2026-08-16:

```
$ curl -s :8610/nemoclerk/runtime
{"tier":"tier 1 · dgx-spark",
 "model":"nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
 "hardware":"NVIDIA GB10 Grace Blackwell (DGX Spark) · vLLM",
 "rungs":[{"number":1,"name":"dgx-spark","state":"active", ...},
          {"number":2,"name":"rtx-5090","model":"qwen3.6-27b","state":"standby", ...},
          {"number":3,"name":"cache","state":"standby", ...},
          {"number":4,"name":"mock","state":"standby", ...}]}
```

After one grounded console turn the principal read
`agent:nemoclerk(nvidia/nemotron-3-nano-omni-30b-a3b-reasoning@dgx-spark)`.

## Role in the federation

helm reads from every producer repo (docket, breaker, siren, blindspot) and
from throughline's approval queue and ledger, presenting them in one console.
Its MCP surface exposes the same operations to an agent operator, with the
approval-of-irreversible-effects path refused BY ROLE for agent principals.

## Contract surfaces

- OpenAPI: `contracts/openapi.yaml` — 27 paths, every console action
- opencli: `contracts/opencli.yaml`
- MCP: `mcp/tools.json` — the same nine tools, with their role class

## Verifying it

```bash
.venv/bin/python -m pytest tests --cov=helm --cov-fail-under=70   # unit+
bash scripts/bvt.sh                                               # smoke/BVT+
.venv/bin/python scripts/demo.py --offline --self-contained       # e2e+ — see "The demo"
HELM_REQUIRE_REAL_SUBSTRATE=1 .venv/bin/python scripts/integration_check.py  # int+
.venv/bin/python scripts/visual_check.py                          # visual+
```

The third line is the demo script wearing its CI hat. It is listed here
because it discharges `e2e+`, not because it is a test — see
[The demo](#the-demo).

## Data provenance

helm holds **no dataset of its own**. Every fact on screen is fetched at read
time from a sibling on its fixed port, and nothing is cached into a corpus
here.

| Source | Port | What helm reads from it |
| --- | --- | --- |
| `throughline` | `:8600` | approval queue, ledger, verify result, cause walks |
| `docket` | `:8601` | permit queue and judgment cards |
| `breaker` | `:8602` | divergence proposals and the line-by-line evidence pane |
| `siren` | `:8603` | incidents, map rows, hot-reload timeline |
| `blindspot` | `:8604` | **nothing — the service does not exist.** The tab renders the frozen wireframe labelled *component offline / designed, not built*. |

For the provenance of the underlying records — Socrata `76t5-zqzr` permits,
Socrata `kzjm-xkqj` incidents, breaker's synthetic telemetry — see each
sibling's own `## Data provenance`. helm deliberately does not restate them,
so they cannot drift.

**Models.** NemoClerk's phrasing comes from a named rung of the ladder below;
its **facts** come from tool results, never from the model. Grounding is a
mechanism, not a prompt: no chip, no claim. Cached scripted turns live in the
repository; `scripts/cache_demo_turns.py` refuses to bank the mock composer's
own text as if it were a recording, so the cache contains recordings or
nothing.

## Known limitations

1. **NemoClerk's rung 1 is an NVIDIA model on NVIDIA silicon — say the
   qualifiers, not just the headline.** Rung 1 is
   `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, vLLM on **NVIDIA GB10
   Grace Blackwell** (DGX Spark) at `dgx-spark:8000`, locally, with no
   internet; rung 2 is `qwen3.6-27b` on an RTX 5090 (llama.cpp). Verified on
   2026-08-16: `/nemoclerk/runtime` reports `tier 1 · dgx-spark` with rung 1
   `state: active`, and after a grounded console turn the ledgered principal
   read `agent:nemoclerk(nvidia/nemotron-3-nano-omni-30b-a3b-reasoning@dgx-spark)`.

   The honest qualifiers, all of which survive the correction: it is the
   *nano-omni 30B-A3B* model, not a frontier one; it runs `--enforce-eager`
   at roughly 5 tok/s with `--max-num-seqs 2`, so calls are serialized and
   answers are capped at 220 tokens; and it has **no tool-call parser**, so
   the grounding is helm's deterministic router, not the model's tool
   choice. An earlier revision of this README said NemoClerk's own models
   were "Qwen and DeepSeek, not Nemotron" and pointed the Nemotron claim at
   `blindspot` alone. That was true when written and is now false — this
   entry is regenerated from the endpoint.

   Elsewhere in the federation Nemotron is used in two further ways that must
   not be blurred together with this one: `docket`'s judgments call
   `nvidia/nemotron-3-super-120b-a12b` over NVIDIA's **hosted** API, and
   `blindspot`'s caption model — the same `nano-omni` weights — runs locally
   on the GB10 but is **consumed by nothing**, because blindspot is
   deliberately unbuilt. Do not let the three claims borrow each other's
   credibility. The full breakdown is in the orchestrator README's *Known
   limitations*.
2. **There is no DeepSeek rung any more, and the docs used to say there was.**
   `deepseek-v4-flash-0731` on `dgx-spark:18000` was a two-node
   tensor-parallel vLLM job across the GB10 Sparks. The pair's 121 GiB cannot
   hold both it and the Nemotron above, and the operator authorised stopping
   DeepSeek on 2026-08-16 to free the GB10. The port now refuses connections
   (verified by direct curl, and by `breaker`'s
   `GET /substrates/dsv4/health` returning
   `reachable: false, "Connection refused"`). That is a deliberate trade, not
   an outage; the restore recipe is in `blindspot`'s README and it requires
   stopping the Nemotron first. `breaker` still registers `dsv4` as a
   substrate and **abstains** when it is unreachable — that registration is
   intact and is a different thing from a helm rung.
3. **Both rungs are still shared infrastructure, and both have been down at
   once during this build** — at which point the ladder degrades to the
   labelled MOCK composer, which is a designed rung and is announced on
   screen. Because the facts come from tools, the demo completes either way,
   but **a live model turn is not something to promise on stage**.
4. **No scripted model phrasings are committed.** `data/model-cache/` fills
   up at runtime from whichever rung answered on that machine, and it is not
   tracked in git; the repository ships no banked turns. `cache_demo_turns.py`
   refuses to bank the mock composer's own text as if it were a recording, so
   on a fresh clone with both rungs down, rung 3 falls straight through to
   rung 4.
5. **Both live rungs are serialized behind one lock.** They run with tight
   concurrency limits (`-np 1`, `--max-num-seqs 2`), so helm sends one model
   call at a time and keeps answers short. Two people driving NemoClerk at
   once will queue.
6. **CI and pytest never contact the models.** The live smoke test skips when
   the endpoint is unreachable, so "the model answered" is never something a
   green pipeline has proved.
7. **Rung 2's configured model name does not match what its endpoint
   currently serves.** `NEMOCLERK_FALLBACK_MODEL` defaults to `qwen3.6-27b`,
   but on 2026-08-16 `GET rtx-workstation.nemotron.example.com:9997/v1/models` listed
   **`qwen3.8-27b`** (root `/models/nvfp4`). Rung 1 answers, so this has not
   bitten in practice and it is not visible on stage — but a fallback to
   rung 2 would send a model id the server does not know. Recorded here
   rather than quietly patched, because the fix is an operator decision about
   which model that host should serve.
8. **`blindspot` is deliberately unbuilt**, so the Floor Watch tab is a
   frozen wireframe. Four tabs, not five, is the correct state.
9. **Real OIDC is the default when it is configured, and the mock is
   labelled when it is not.** `HELM_OIDC_ISSUER` / `HELM_OIDC_CLIENT_ID` /
   `HELM_OIDC_CLIENT_SECRET` (fleet store bundle `nemocity-signet`) turn on
   the authorization-code + PKCE round trip against `git.nemotron.example.com`; the ID
   token is verified — signature against the JWKS, plus `iss`, `aud`, `exp`
   and `nonce` — before any session exists, and the badge then reads
   `SIGNET · git.nemotron.example.com`. With no provider, or offline, or on a provider
   that cannot be reached, the console falls back to the labelled MOCK LOGIN
   identity and says so on every page. `/auth/mock` is refused outside
   `dev`/`test`, and `staging`/`production` refuse to BOOT while anything can
   mint an identity without a credential — including the built-in session
   secret. GitHub OAuth is still there.
10. **The subject in the record is the verified one, and it names its
   issuer.** An approval writes both, and so does its ledger entry, so "an
   identified human approved" is checkable against the authority rather than
   taken on the console's word. A mock approval is ledgered
   `auth_mode: mock`, never as an absent field.
11. **The role gate here is the real one.** throughline's `caller_role` check
   is a backstop that a caller could lie to; helm holds the authenticated
   identity, so bypassing helm weakens the guarantee to that backstop.
12. **No `LICENSE` file** and no `license` field in `pyproject.toml`.

## Design authority

`.viper-context/specs/v0.1.0/ux/wireframes.md` rev 6 (FROZEN),
`.viper-context/specs/v0.1.0/ux/NEMOCLERK.md`, and the ten reference pages in
`.viper-context/specs/v0.1.0/ux/web/wireframes/`. Divergence from those files
is a defect against them, not a design choice. PALETTE v2.1 is locked:
graphite surfaces, **light blue** for anything interactive, **amber** for
waiting-on-human, **red** for refused, and **green #76b900 reserved** for
verified and flowing — never for chrome, never for interaction.

## Orchestrator

See https://git.nemotron.example.com/nvidia-hackathon/nemo-nvidia-demo/nemo-nvidia-demo-system
