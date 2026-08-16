# siren

**The class that hot-loads mid-demo from one config file.**

siren is the City Pulse component of the nemo-nvidia-demo federation. It polls
Seattle's Real-Time Fire 911 feed, emits each incident as a federation
`Signal`, and owns `config/incident.yaml` — the file the demo physically drops
into throughline, live, to register a signal class that was not there when the
service booted. No redeploy.

It exists to prove two things:

1. **The envelope generalizes.** An incident is not telemetry, not a permit and
   not a video frame — yet it rides the identical `Signal`. Nothing about the
   envelope bends to accommodate 911 data.
2. **The refusal is real.** Drop `config/incident.invalid.yaml` instead and
   throughline refuses the whole file by name, file and line, and the config
   that was already running keeps running.

## Run it

```bash
pip install -e ".[test]"
python -m siren                 # :8603, /docs served
```

| Variable | Default | Meaning |
|---|---|---|
| `SIREN_PORT` | `8603` | Fixed by federation convention |
| `OFFLINE_MODE` | unset | `1` forces snapshot-only: no poll, no substrate socket |
| `SIREN_SUBSTRATE` | `mock` | `real` talks to throughline; `OFFLINE_MODE` always wins |
| `THROUGHLINE_URL` | `http://127.0.0.1:8600` | Where the substrate lives |
| `SIREN_DATA_DIR` | `data` | Where the poll cache is written |
| `SIREN_INCIDENT_CONFIG` | `config/incident.yaml` | The drop artifact |
| `SIREN_DATASETS` | `config/datasets.yaml` | The dataset registry |

siren depends on **throughline** (it imports throughline's dataset registry
loader rather than reimplementing it) and throughline is not on PyPI. Install
the substrate first:

```bash
bash scripts/fetch-throughline.sh /tmp/throughline && pip install /tmp/throughline
pip install -e ".[test]"
```

## Offline, at runtime

`OFFLINE_MODE=1` is read from the environment, so it can only be set before
the process starts. That left a hole: a scripted demo arms a socket guard
inside its **own** process, and siren runs beside it in another one, so
`demo.py --offline` was offline for the demo and not for siren — which went
on pulling live Socrata while the UI labelled the data cached. A demo risk
and an honesty problem at once.

```bash
curl -XPOST :8603/feed/mode -d '{"mode":"offline"}' -H 'content-type: application/json'
curl :8603/feed/mode        # offline: true, source: runtime-switch
curl -XPOST :8603/feed/mode -d '{"mode":"live"}'  -H 'content-type: application/json'
curl -XPOST :8603/feed/mode -d '{"mode":"env"}'   -H 'content-type: application/json'
```

Offline is a guarantee, not a preference:

- the cached snapshot is served, with **its own** as-of label — never
  re-stamped with the moment the switch was flipped;
- **no outbound request is attempted.** `fetch_live` refuses before a URL is
  built, so an offline poll is a request that was never made rather than one
  that failed;
- the substrate is rebuilt as the in-process mock, so a stale
  `SIREN_SUBSTRATE=real` cannot keep a socket open behind the switch.

Live mode is untouched. This adds a promise; it does not replace one.

`{"substrate": "keep"}` keeps the configured substrate instead of dropping to
the mock. The orchestrator boots siren with `SIREN_SUBSTRATE=real`, and
siren's refusal beat is only **ledgered by throughline** in that mode;
throughline is on loopback, and a scripted offline run allows loopback while
forbidding the internet. Forcing the mock unconditionally would take the
demo's refusal off the ledger to prevent a connection the run was never going
to make. `keep` buys a loopback substrate — never an outbound poll.

**The orchestrator half is not implemented here.** `helm/scripts/demo.py`
owns `--offline` and is outside this repo's scope. For a plain `--offline`
run against the live federation it should `POST :8603/feed/mode
{"mode":"offline"}` before the sequence and `{"mode":"env"}` after it. Until
it does, run `--offline` demos against a siren already switched offline by
hand, or start siren with `OFFLINE_MODE=1`.

### Known deployment requirement (orchestrator-owned)

throughline refuses a reload whose source is outside its allowlist
(`config-source-must-be-allowlisted`), and that allowlist is
`<throughline>/config` plus `THROUGHLINE_CONFIG_DIRS`. siren's config lives in
**this** repo, so a federation booted without that variable refuses siren's
drop by path before it reads a rule — the hot-reload beat never reaches the
refusal it is meant to demonstrate. Verified on scratch instances: without
the variable, refused by path; with
`THROUGHLINE_CONFIG_DIRS=<siren>/config`, the beat runs and the swap is held
at the gate as designed.

That is a deployment fix in throughline/orchestrator territory, reported
rather than made here. `tests/test_integration_throughline.py` asserts the
correct siren behaviour in **both** deployments rather than skipping — a skip
reads like a pass.

## Timestamps

Socrata writes `datetime` as a **floating Seattle wall clock with no zone**.
siren used to stamp a `Z` on it and call it UTC, which was a seven-hour error
in summer wearing a perfectly well-formed value — undetectable downstream,
and inherited by every age computation and by the on-disk snapshot. Every row
now carries the conversion rather than the result of one:

| field | meaning |
|---|---|
| `reported_at` | the instant in **UTC**, converted through `tz` — or `null` |
| `reported_at_local` | the same instant on the source's wall clock, with its offset |
| `tz` | `America/Los_Angeles`, named so the conversion can be checked |
| `reported_at_missing` | `true` when the source gave no usable timestamp |

The zone is named rather than an offset because Seattle is UTC-7 in August
and UTC-8 in January. A missing source timestamp is `null` plus the flag: it
used to be filled in with `utcnow()`, which made an undated row sort as the
freshest incident on the board — invention, in a product whose thesis is that
nothing is invented. **A consuming UI must render "time unknown" for those
rows.** helm does not display incident times today, so nothing is broken; the
requirement lands on whoever adds that column.

`/feed/status` and `/healthz` publish `newest_reported_at` and
`newest_age_seconds` so a regression shows up in the payload rather than in
someone's head — that number was 25 633 s (7.12 h) while the bug was live.

Snapshots written before the fix are corrected on read, once, guarded by a
`schema` stamp so a repaired cache is never repaired twice; the packaged seed
was re-cut from the source.

## Honesty rules this service holds itself to

- **Cached rows are labelled cached.** `/pulse` binds every incident list to the
  `as_of` timestamp and the words the pane displays. A snapshot served now keeps
  the snapshot's own timestamp; it is never re-stamped with the current time.
- **Cached 911 records stay `real`.** They are real records, replayed. Their age
  is reported in `staleness`, not laundered through `real_or_synthetic`.
- **A mock substrate says it is a mock.** `/healthz` and every emit response
  name the substrate in use.
- **Unmappable rows are dropped, not placed at 0,0.** A map pane that invents a
  location is worse than one that omits a row.
- **`flowing` is not green on intent.** The hot-reload timeline reports
  `registered` only when the substrate **applied** the swap, and `flowing`
  only once signals of the new class actually went out.
- **A held swap says held.** throughline treats a reversibility downgrade as
  an irreversible effect, so the config swap itself waits at the gate: it
  answers `refused: false, held: true` with an approval id, accepted and not
  applied. siren renders that as `state: held` (HTTP 202), names the approval
  and the policy, registers no class and flows nothing — the previous config
  is still the running one. Reading only `refused` turned that into "the
  substrate accepted the swap", which is the failure this rule exists to
  prevent, arriving from a direction nobody was watching.

## Cut order

Cut order #2: **live mode degrades first.** Everything downstream reads a
snapshot exactly as it reads a live poll, so losing the network costs the demo
an as-of label and not a beat. The hot-reload beat needs no network at all.

## Data provenance

The incidents are **real records** from a public Socrata dataset — served
live when the network is there, replayed from a snapshot when it is not.
Cached rows stay labelled `real`; their age is reported in `staleness`, never
laundered through `real_or_synthetic`.

| Field | Value |
| --- | --- |
| Dataset | **Seattle Real-Time Fire 911 Calls** |
| Socrata id | **`kzjm-xkqj`** |
| Endpoint | `https://data.seattle.gov/resource/kzjm-xkqj.json?$limit=50&$order=datetime%20DESC` |
| Publisher | City of Seattle open data portal (`data.seattle.gov`) |
| App token | **None required.** The dataset is public; siren sends no token. |
| Real or synthetic | **Real**, recorded — live poll or replayed snapshot |
| Live poll | 50 rows, newest first; near-realtime, newest records run ~5 minutes behind wall clock |
| Committed fallback | [`siren/seed_snapshot.json`](siren/seed_snapshot.json) — **40 incidents**, packaged inside the Python wheel so a clean install serves the whole demo path before it has ever reached the network |
| Runtime cache | `data/snapshot.json` — whatever the last live poll wrote, atomically via tmp+rename. **Gitignored**, deliberately: the shipped fallback is the packaged seed, not one machine's leftovers. |
| Override | `SIREN_FEED_URL` |

Read order is runtime cache first, packaged seed second. A snapshot keeps its
own `as_of` and is never re-stamped with the current time.

**Licence, stated honestly:** the dataset is published openly by the City of
Seattle and needs no token, and this project's specs assert it is public
domain. That assertion is **not sourced to a licence URL anywhere in this
repository**, and there is no attribution notice or `LICENSE` file here.
Treat "public domain" as our unverified reading of the portal's terms, not as
a citation. Filed as GAP-002 in the orchestrator's `issues/system-gaps.json`.

## The dataset registry

`config/datasets.yaml` declares **every** dataset siren consumes, with its
licence, its provenance, its mode and — for a cached snapshot — the as-of time
it was taken. The loader is throughline's (`throughline/datasets.py`), imported
rather than copied, so a siren entry is refused by exactly the same rules as a
throughline one: the **whole file** is refused, naming file, entry id and line,
if an entry omits its licence or provenance, if a cached entry has no `as_of`,
if a fixture is labelled `real`, or if an entry claims to be more available
than it is.

| id | mode | real/synthetic | availability | licence |
|---|---|---|---|---|
| `siren.seattle-fire-911` | live | real | available | `unknown` |
| `siren.seed-snapshot` | cached | real | available | `unknown` |
| `siren.runtime-poll-cache` | cached | real | degraded | `unknown` |
| `siren.test-fixture-rows` | fixture | synthetic | available | n/a — authored here |

Three deliberate honesty calls in that table:

1. **`licence: unknown`, not "public domain".** The literal string `unknown` is
   an accepted licence value — the loader refuses *silence*, not honest
   ignorance. Writing the specs' unsourced "public domain" into the licence
   field would launder an assertion into a citation. The reason travels with
   the entry, in `provenance` and `notes`. See "Licence, stated honestly".
2. **The seed snapshot has an unevidenced capture procedure.** This repository
   commits no snapshot-taking script, so `siren/seed_snapshot.json` can only
   have come from running the service or from a hand edit. Nobody wrote down
   which; the entry says so rather than implying a process.
3. **The runtime poll cache asserts no fixed as-of.** `data/snapshot.json` is
   rewritten by every successful poll and carries its own `as_of` *inside the
   file*; there is no as-of that is true of it at rest, so the entry says that
   in words instead of stamping a time that would be false a minute later. It
   is `degraded`, not `available`: a clean checkout does not have this file at
   all.

Three surfaces, all read-only except the reload:

```bash
curl :8603/datasets                        # the whole registry, with provenance
curl :8603/datasets/siren.seed-snapshot    # one entry; 404 on an unknown id
curl -X POST :8603/datasets/reload         # atomic; 422 names file, rule, line

siren datasets list
siren datasets show --id siren.seattle-fire-911
siren datasets validate --path config/datasets.yaml   # non-zero on a refusal
```

**siren keeps no local ledger.** It posts to throughline over HTTP, so a
dataset refusal here is reported on the wire (422, plus the refusal on
`GET /datasets`) and the previously accepted registry keeps running — but it is
not hash-chained locally. The ledgered form of the same refusal is throughline's,
recorded when a registry is reloaded there.

**Pre-existing CLI drift, named not deleted.** `contracts/opencli.yaml` also
declares `signal incident` and `incident list`. Those were written at spec time,
before siren had any CLI, and are **still unimplemented** — `siren/cli.py`
implements the three `datasets` commands only. They stay in the contract: a
declared command deleted to make a checker green is a promise that disappears
instead of a promise that is kept. Implementing them is separate work.

## Known limitations

1. **The live poll depends on `data.seattle.gov` being reachable.** Per cut
   order #2 above, that costs the demo an as-of label rather than a beat.
   `OFFLINE_MODE=1` serves the committed 40-incident seed, and the label
   changes to `snapshot (cached) — OFFLINE_MODE` so an intentional offline
   run is distinguishable from a failed poll.
2. **The dataset's licence is asserted, not cited** (see above).
3. **`OFFLINE_MODE` overrides `SIREN_SUBSTRATE=real`.** Offline means
   offline, so the substrate falls back to the in-process mock even when the
   real one is asked for. This is deliberate and tested — but it means the
   refusal beat run under `OFFLINE_MODE` is refused by **siren's pre-flight**
   rather than by throughline, and so is **not ledgered**. For the ledgered
   refusal, run online with `SIREN_SUBSTRATE=real`.
4. **The default substrate is `mock`, not `real`.** A siren started with no
   environment at all validates the drop itself and contacts no substrate;
   `/healthz` says so in those words. The federation boot order in the
   orchestrator README sets `SIREN_SUBSTRATE=real` for exactly this reason.
5. **Unmappable rows are dropped, not placed at 0,0** — so the map pane can
   show fewer incidents than the feed returned. A pane that invents a
   location is worse than one that omits a row.
6. **siren renders no surface of its own.** Its panes live in helm; there is
   nothing to look at on `:8603` but `/docs`.
7. **No `LICENSE` file** and no `license` field in `pyproject.toml`.
8. **No snapshot-taking script is committed.** `siren/seed_snapshot.json` was
   produced by running the service or by hand, and which of those is not
   recorded anywhere. Declared in `config/datasets.yaml` as a provenance gap.
9. **`signal incident` and `incident list` are declared in `opencli.yaml` and
   not implemented** (see "The dataset registry"). Pre-existing; not introduced
   by, and not fixed by, the registry work.
10. **throughline must be installed for siren to import at all.** The dataset
    registry loader lives there. It is not on PyPI; `scripts/fetch-throughline.sh`
    obtains it, in CI and locally.

## Verification

```bash
python -m pytest tests --cov=siren --cov-fail-under=70   # unit+
python3 scripts/check-contract-drift.py                  # api+, both directions
bash scripts/bvt.sh                                      # smoke/BVT+
```

`check-contract-drift.py` fails on a route served but undocumented **and** on a
path documented but unserved — `tests/test_api_contract.py` only checks the
second. Waivers go in `contracts/undocumented.yaml` with a reason; it is
currently empty, and the check reports all 14 served paths agreeing both ways.

## Status

`phase=done`. Verified from a clean clone of merged main, with the federation up:

| layer | evidence |
|---|---|
| static+ | CI `validate` green on main — yamllint, openapi-spec-validator, ultra gate 01–03 |
| unit+ | 80 pytests, coverage 93% against a failable `--cov-fail-under=70` |
| api+ | contract validates; every path and operation implemented both directions; all 10 paths smoked over HTTP on :8603 |
| int+ | 4 integration tests against real throughline: class registered by the drop, no redeploy, refusal ledgered by throughline |
| smoke/BVT+ | CI `verify` green — clean venv, install from pyproject, real boot, every contract path |

Component n/a: the service-level tests cover it. Visual n/a: siren renders no
surface of its own — its panes live in helm. Perf n/a: no numeric budget.
