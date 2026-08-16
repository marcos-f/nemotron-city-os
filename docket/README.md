# docket

## What it is

**docket** is the document feed for the federation: Seattle building permits
in, judgment cards out. Every judgment card carries a **verbatim quoted
description** lifted straight from the source permit plus citations back to
that source. When docket cannot support a judgment with a direct quote, it
takes the **explicit abstention path** instead of guessing.

The name is the thing itself: a docket is a list of matters to be reviewed,
not a verdict. docket proposes a route to a human reviewer; it does not
decide the permit.

## Feature-area coordinate

`feature-area://docket`

## Role in the federation

docket ingests permit documents as signals and emits Judgment records
(finding, confidence, citations, abstained) plus a route-to-reviewer
proposal. Any effect docket wants to trigger — routing, flagging — passes
through throughline's gate. helm surfaces docket's queue in its Permit
Triage tab.

## Contract surfaces

- OpenAPI: `contracts/openapi.yaml`
- opencli: `contracts/opencli.yaml`
- MCP: `mcp/tools.json`

## Stage

**RUNNING (v0.2.0).** FastAPI service on **:8601**.

## Run it

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m docket            # serves http://127.0.0.1:8601/docs
```

The permit corpus is already snapshotted to `data/permits.json` (500 records
from Socrata `76t5-zqzr`), so the service runs **offline**. Re-snapshot only if
you want fresher records:

```bash
python3 scripts/snapshot_permits.py --limit 500
```

## Modes

| variable | effect |
|---|---|
| `MOCK_JUDGMENT=1` | judgments come from `data/fixtures/`, labelled `MOCK` in every payload and in `/healthz` |
| `MOCK_THROUGHLINE=1` | route effects go to an in-process substrate instead of throughline |
| `NVIDIA_API_KEY` | unset ⇒ mock mode is automatic; the service never claims a model it could not reach |
| `THROUGHLINE_URL` | default `http://127.0.0.1:8600` |

## The quote rule

Every published finding carries a span copied out of the permit description.
A deterministic validator — no model involved — string-matches it against the
source record before the judgment may leave the service.

Digit-group spaces are normalized (`92 000` → `92000`) because Seattle's prose
splits numbers inconsistently, **but never inside quotation marks**: rewriting
the inside of a quote to make a comparison succeed is exactly the dishonesty
the validator exists to prevent.

When a judgment cannot cite itself, docket regenerates **once**, and then
abstains. It does not republish the same claim at lower confidence.

```
thin evidence?   -> abstain (no model is called at all)
quote invalid?   -> regenerate once
still invalid?   -> abstain, reason "uncited"
```

An abstention proposes no route and emits no effect. Routing an abstention is
refused with `409`.

## Verify

```bash
pytest --cov=docket --cov-fail-under=70   # unit + api
bash scripts/bvt.sh                       # clean venv, real boot, offline demo path
```

## Real judgments (SG3)

Judgments run against the hosted model as soon as a key is present:

```bash
export NVIDIA_API_KEY=$(cd ~/source/git.infra/shared-secrets \
  && sops -d --extract '["env"]["NVIDIA_API_KEY"]' .secrets/nvidia-env.yaml)
python3 scripts/judge-live.py --limit 12    # caches every response to data/cache/
```

The hosted path is held to the **same** verbatim-quote validator as the mock
path — a real model that paraphrases is rejected exactly as a fixture one is,
and `judge-live.py` reports how often that happened.

Decrypting that bundle requires an age identity on its recipient list; see
`.build-status.json` blockers for the current entitlement state.

In CI, adding a masked `NVIDIA_API_KEY` variable is enough — the gated
`real-judgments` stage runs `judge-live.py` by itself and publishes
`data/cache/` as an artifact. Because `main` is the only protected branch, a
*protected* variable makes that job run on `main` pipelines only; it will be
absent from feature-branch MRs, which is expected rather than broken.

## Data provenance

The permits are **real records** from a public Socrata dataset, snapshotted
so the service runs offline. The snapshot file records its own provenance —
`dataset`, `source`, `count`, `snapshot_utc` and `real_or_synthetic` — so the
claim travels with the data rather than living only here.

| Field | Value |
| --- | --- |
| Dataset | **Seattle building permits** |
| Socrata id | **`76t5-zqzr`** |
| Endpoint | `https://data.seattle.gov/resource/76t5-zqzr.json` |
| Publisher | City of Seattle open data portal (`data.seattle.gov`) |
| App token | **None required.** The dataset is public. |
| Real or synthetic | **`real`**, recorded — the value is carried in the file |
| Records | **500**, committed to [`data/permits.json`](data/permits.json) |
| Snapshot taken | `2026-08-16T03:42:06Z` |
| Re-snapshot | `python3 scripts/snapshot_permits.py --limit 500` |

Judgments are a separate provenance question from the permits. The quoted
span in every published finding is copied out of the source record above and
string-matched against it by a deterministic, model-free validator before the
judgment may leave the service — so the citation's provenance is checked
mechanically, not trusted. The finding's prose comes from
`nvidia/nemotron-3-super-120b-a12b` over NVIDIA's **hosted** endpoint when
`NVIDIA_API_KEY` is set, and from `data/fixtures/` labelled **MOCK** in every
payload and in `/healthz` when it is not. The service never claims a model it
could not reach.

**Retrieval provenance — there is no retriever.** docket finds a permit by a
**linear scan**: `docket/corpus.py` does one `json.loads` of
`data/permits.json` and `get()` walks the 500-row list comparing `permitnum`.
The **NeMo Retriever ingest** named across this repository's plan documents —
`nvidia/llama-nemotron-embed-1b-v2` at **dim-2048**, configured as
`EMBED_MODEL` / `EMBED_DIM` in `docket/config.py` — is **declared and
contract-published, but NOT IMPLEMENTED**. Nothing embeds, nothing indexes,
nothing queries a vector store; the only consumer of either constant anywhere
in the tree is one assertion in `tests/test_hosted_judge.py`.

Say it the way the federation says the others: `breaker`'s cuOpt is a
*registered alternate, unavailable-without-GPU*; `blindspot` is *designed, not
built*; docket's Retriever ingest is **declared, not built**. A linear scan
over 500 records is entirely adequate and nothing about the demo depends on
the difference — but "NeMo Retriever ingest" in a plan document is not a
retriever in the pipeline, and this is the repository whose thesis is quoting
its sources or shutting up. It is declared as
`docket.retriever-embedding-index`, `mode: declared-unavailable`, in the
dataset registry below, so the gap is visible from the running service and not
only from prose. See also *Known limitations* 7.

**Licence, stated honestly:** the dataset is published openly by the City of
Seattle and needs no token, but no licence text, licence URL or attribution
notice is recorded in this repository, and there is no `LICENSE` file. Filed
as GAP-002 in the orchestrator's `issues/system-gaps.json`.

## Dataset registry

Every dataset docket stands on is declared in
[`config/datasets.yaml`](config/datasets.yaml) with its licence, its
provenance, its mode and — for a snapshot — the as-of time it was taken. The
loader is throughline's (`throughline/datasets.py`), imported rather than
reimplemented, so the whole federation refuses the same things in the same
words: an entry with no licence or no provenance, a cached entry with no
as-of, a fixture labelled `real`, or a declared-unavailable entry claiming to
be available is refused WHOLE, naming file, entry id and line.

```bash
docket dataset list                 # every entry, with licence + provenance
docket dataset show --id docket.retriever-embedding-index
docket dataset validate             # non-zero on a refusal; runs in CI
curl -s localhost:8601/datasets | jq '.datasets[] | {id, mode, licence}'
```

| Id | Mode | Licence |
| --- | --- | --- |
| `docket.seattle-building-permits` | cached | **unknown** |
| `docket.judgment-fixtures` | fixture | **unknown** (inherited) |
| `docket.hosted-judgment-response-cache` | cached (degraded) | **unknown** |
| `docket.nvidia-hosted-inference-endpoint` | live (degraded) | **unknown** |
| `docket.retriever-embedding-index` | **declared-unavailable** | unknown |
| `docket.smc-title-23` | **declared-unavailable** | unknown |

`unknown` is a deliberate, written-down answer, not a blank. The registry
refuses silence; it accepts honest ignorance. Nothing that does not exist is
labelled `real`.

## Known limitations

1. **SMC Title 23 grounding is descoped** (F5 bot wall, verified
   2026-08-15). docket quotes the permit record itself, never the municipal
   code. This is the single biggest gap between what a permit reviewer would
   want and what docket does: it can tell you what the application says, not
   whether the zoning allows it.
2. **The judgment model is hosted, not local.** Nemotron is genuinely used —
   `nvidia/nemotron-3-super-120b-a12b` — but over NVIDIA's hosted API. No
   inference for docket runs on the federation's local GB10 Grace Blackwell
   hardware, and docket's model is not one of the locally served ones.
3. **A hosted model that answers with something other than JSON makes docket
   abstain.** Observed live: a judgment came back
   `abstained: true`, `abstain_reason: "judge-unavailable: model did not
   return JSON"`. That is the designed behaviour and the honest one — an
   abstention proposes no route and emits no effect — but it means real
   findings depend on a hosted endpoint behaving, and a demo run can show an
   abstention where a finding was expected.
4. **The permit corpus is a 500-record snapshot, not the live dataset.** It
   is deliberately frozen so the demo runs offline; it ages.
5. **An abstention is terminal for that permit in that run.** docket
   regenerates once and then abstains, rather than republishing the same
   claim at lower confidence. Routing an abstention is refused `409`.
6. **No `LICENSE` file** and no `license` field in `pyproject.toml`.
7. **No embedding index is built, and four spec documents say otherwise.**
   `docket/config.py` configures `EMBED_MODEL =
   "nvidia/llama-nemotron-embed-1b-v2"` and `EMBED_DIM = 2048`, and
   `BUILD-GOAL-docket.md`, `ARCHITECTURE.md`, `PRODUCT.md` and
   `PRODUCT-ROADMAP.md` all describe a NeMo Retriever ingest as though it were
   implemented. It is not. The only consumer of either constant in the whole
   codebase is an assertion in `tests/test_hosted_judge.py`; retrieval is a
   `json.loads` and a linear scan in `docket/corpus.py`, which is entirely
   adequate for 500 records and is not what the specs describe. Declared as
   `docket.retriever-embedding-index`, `mode: declared-unavailable`, in the
   dataset registry above, so the discrepancy is visible from the running
   service rather than only from `.build-status.json`.
8. **`data/cache/.gitkeep` overstates its own emptiness.** Its prose says the
   directory is empty because `NVIDIA_API_KEY` was never available. One real
   hosted response has since been produced and sits in a developer checkout,
   untracked — so a clean clone still has none, but the flat claim is stale.
   The registry records this as `degraded` rather than `available`.

## Orchestrator

See https://git.nemotron.example.com/nvidia-hackathon/nemo-nvidia-demo/nemo-nvidia-demo-system
