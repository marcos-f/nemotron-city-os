# breaker

## What it is

**breaker** is the telemetry feed for the federation: microgrid telemetry
in, state-of-charge and temperature divergence detection out, dispatch
proposals that **wait at the gate**. The name is the metaphor: a breaker
trips on divergence and stays tripped until a human resets it. breaker never
dispatches on its own authority.

## Feature-area coordinate

`feature-area://breaker`

## Role in the federation

breaker ingests microgrid telemetry as signals, detects SOC/temperature
divergence, and proposes a dispatch effect. That effect is always
irreversible-typed at the throughline gate, so it always waits for a human
decision. helm surfaces breaker's proposals in its Grid Watch tab.

## Contract surfaces

- OpenAPI: `contracts/openapi.yaml`
- opencli: `contracts/opencli.yaml`
- MCP: `mcp/tools.json`

## Running it

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/python -m breaker            # serves on :8602, /docs for the API
.venv/bin/breaker stream --interval 0.5    # drive the fixture, watch it diverge
```

Environment: `BREAKER_PORT` (default 8602), `BREAKER_HOST`, `THROUGHLINE_URL`
(default `http://127.0.0.1:8600`), `BREAKER_SUBSTRATE` (`auto` | `real` |
`mock`), `BREAKER_GATE_PROBE_MAX_AGE` (default `3.0` — seconds a `/healthz`
gate reachability probe is reused before another is taken).

## What happens, in order

1. **Telemetry streams in.** The spec-03 fixture is nine units over forty
   one-minute ticks, 360 records, deterministic — `battery_4` loses state of
   charge while its temperature climbs and its charge current collapses.
2. **A rule decides, and shows its working.** Not a model: three thresholds,
   each rendered with its own verdict.

   ```
   soc_delta(-7.1%) < -5%            ✓
   temp_slope(+1.7°C/10m) > 1.5      ✓
   charge_current collapse           ✓
                                     → DIVERGENCE
   ```

   On the fixture this fires at **tick 23**, and no healthy unit ever fires.
   Two checks out of three is not a divergence, and the pane says which one
   held it back.
3. **The dispatch waits.** A load-shed dispatch is irreversible, so breaker
   records signal → judgment → effect on throughline and then **verifies the
   gate held it**. If a substrate ever returns an unheld irreversible effect,
   breaker refuses to dispatch and says so — it does not assume the best.
4. **A human releases it.** `POST /proposals/{id}/decide` with an OIDC subject
   is the only path to execution, and the dispatch executes exactly once
   however many times the release is observed.
5. **The chain walks back.** effect → judgment → signal, three hops.

There is no deadline anywhere on that path. A proposal nobody decides waits
forever, which is the only correct behaviour for an irreversible effect.

## The judgment substrates

The rule is one substrate behind a contract, not the only conceivable one.
`config/substrates.yaml` registers four, and every judgment names the one that
produced it — nothing can attribute the rule's verdict to something else.

| id | state | what it is |
|---|---|---|
| `nemotron` | **active** | `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` on GB10 Grace Blackwell (`dgx-spark:8000`) |
| `rule` | available | the deterministic threshold rule above |
| `dsv4` | available | `deepseek-v4-flash-0731` on the DGX Spark (`dgx-spark:18000`) |
| `cuopt` | registered, unavailable | GPU solver — never installed here |

**`nemotron`** is the substrate the NVIDIA bounty requires in the CONDITION
slot: an NVIDIA model, actually judging live traffic, not merely registered.
It is active by default because a rule quietly doing all the work while the
product claimed an LLM judged was the defect this file exists to close.

**`dsv4`** is a real model-backed substrate, operator-authorised on 2026-08-16
as an exception to the federation's entitled-models line (which otherwise binds
judgments to the hosted NVIDIA set) — kept registered as an alternate, but it
is DeepSeek, not Nemotron, so it is never the default. Every rule below binds
both model substrates:

- **The model never does arithmetic.** `soc_delta`, `temp_slope` and the charge
  ratio are computed in `rule.compute_metrics` and handed over as facts; the
  model returns a verdict and a rationale, nothing else. Both substrates quote
  identical numbers.
- **It never silently becomes the rule.** Unreachable, slow, or answering with
  something that is not a verdict ⇒ the judgment **abstains**, and an abstention
  proposes nothing. Falling back to the rule with the model's name still on the
  screen is exactly the dishonesty this repo exists to prevent.
- **Every response is cached to disk** (`fixtures/model-cache/` in the repo,
  `data/model-cache/` at runtime), so the demo path replays with no endpoint at
  all. `BREAKER_MODEL_OFFLINE=1` refuses the network outright.

It is also only consulted once a check has tripped: asking a remote model about
nine healthy units every tick would be an hour-long demo and a bill for nothing.

**The two substrates disagree, and the pane shows it.** On this fixture the rule
fires at tick 23; `dsv4` calls it at **22**, while `temp_slope` and
`charge_current` are still failing:

```
soc_delta(-5.9%) < -5%            ✓
temp_slope(+1.4°C/10m) > 1.5      ✗
charge_current collapse           ✗
                → DIVERGENCE  [judged by deepseek-v4-flash-0731 @ … (cached, confidence 0.95)]
```

Two crosses above a DIVERGENCE verdict is not a bug to hide — it is a second
substrate reading the same numbers differently, in the open, with its name on
the call. The dispatch is irreversible and waits at the gate either way: the
substrate changed, the gate did not.

**`cuopt`** stays registered and unavailable — labelled that way everywhere,
refused with 409 rather than faked, and never installed (GPU-only, ~10GB, and
the PyPI name `cuopt` is a squatted stub; the real distribution is `cuopt-cu12`
from pypi.nvidia.com). A GPU host existing in the fleet does not change that:
the constraint is "do not install cuOpt here", not "no GPU exists".

Select a substrate at runtime with `POST /substrates/{id}`; probe one with
`GET /substrates/{id}/health`, which reports what it actually finds rather than
what the config claims.

## Substrate mode

`BREAKER_SUBSTRATE=auto` (the default) probes throughline once at boot: real
gate if it answers, mock if it does not — and `/healthz` says which, in those
words. `real` refuses to start against a missing substrate rather than quietly
degrading to a mock that always says yes. The mock implements the same hold
semantics as the real gate, because a mock that is easier to satisfy than the
real thing is how integration surprises get made.

## When the gate is unreachable

Boot-time selection answers "which gate was configured". It does not answer
"is that gate alive right now", and a gate that answered at boot can die at
03:00. Two things make that legible rather than merely survivable.

**Every path answers 503, naming the dependency.** `SubstrateUnreachable` is
handled app-wide, so no path can re-open the hole by forgetting a `try`:

```json
{
  "error": "substrate_unreachable",
  "dependency": "throughline",
  "dependency_url": "http://127.0.0.1:8600",
  "detail": "throughline unreachable at http://127.0.0.1:8600: Connection refused",
  "fail_closed": true,
  "invariant": "no proposal was emitted and no effect was created; breaker holds when it cannot reach the gate",
  "gate": {"reachable": false, "label": "gate offline"}
}
```

The `gate` block in that body is probed *after* the failure, so it cannot
contradict the 503 it accompanies. A gate that answers but does not hold an
irreversible effect is a different failure and says so: 502 `gate_violation`.

**A gate that refuses is not a gate that is missing.** When throughline
answers 4xx — say `403 caller-role-required` — it is alive, it received the
request and it applied its policy. That comes back as `substrate_refused`
with the gate's own status code and its refusal record passed through
verbatim, not as a 503; calling it unreachable would send an operator hunting
a process that is running fine and hide the one line they need to read.
`SubstrateRefused` subclasses `SubstrateUnreachable`, so every fail-closed
path still holds on it — nothing dispatches either way.

## Who is deciding

`caller_role` is **required** by the gate on every decision and its absence
is a refusal, not a quiet pass: an omitted role used to skip throughline's
agent check entirely. breaker always sends one — `human` by default on
`POST /proposals/{id}/decide` and `breaker proposal decide`, overridable with
`caller_role` / `--caller-role`.

The declared role is **relayed, not filtered**. An agent that says it is an
agent must be refused *on the ledger*; rewriting the claim to one that passes
would turn a recorded refusal into a silent success. The mock gate enforces
the same allowlist as the real one — it is this repo's own rule that a mock
easier to satisfy than the real thing manufactures integration surprises, and
this defect reached main exactly that way: every mocked test passed while the
real gate had begun refusing the shape breaker was sending.

**`/healthz` separates configured from reachable.** `gate.mode` still says how
the gate is configured; `gate.reachable` says whether it answered just now,
from a probe cached for `BREAKER_GATE_PROBE_MAX_AGE` seconds (default 3) — the
same idiom helm uses for its five siblings. Top-level `status` goes `degraded`
and `degraded[]` names why. The status code stays 200 because the process is
alive and `/healthz` is a liveness probe; what changed is that the body no
longer claims green while breaker's only dependency is dead.

The same distinction now runs through the substrate block. `available` is a
claim from `config/substrates.yaml` — this substrate is registered and
permitted to run here. `reachable` is a probe. Only the *active* model
endpoint is probed, because a health check must not reach across the network
to a box nobody selected; an unprobed one reports `reachable: null` and says
how to probe it, since `true` would be a lie.

None of this changes what breaker *does* when the gate is gone. It held
before and it holds now: no proposal, no effect, no dispatch. See
`tests/test_unreachable_gate.py`, where every legibility assertion is paired
with an assertion that the invariant still holds.

## Integration with the real gate

```bash
scripts/with-throughline.sh pytest tests/test_integration_real_gate.py
```

That script installs throughline — from `../throughline` if it is checked out
beside this repo, otherwise from its repo archive over the GitLab API — boots it
on a free port and points breaker at it. The same script runs in CI, so "held by
the real gate" is a reproducible check rather than something that was true once
on a laptop. `BREAKER_REQUIRE_REAL_GATE=1` turns "no substrate" into a failure
instead of a skip, because a skipped integration test reads like a pass.

## Data provenance

All of this is declared machine-readably in
[`config/datasets.yaml`](config/datasets.yaml) and served at `GET /datasets`,
`GET /datasets/{id}` and `POST /datasets/reload` (`breaker dataset list|show
|validate`). The loader is throughline's — imported, not reimplemented — and it
refuses the WHOLE file, naming file, entry id and line, if an entry omits its
licence or provenance, if a cached entry carries no as-of time, if a fixture is
labelled real, or if a cache reported available cannot say where it lives.
`unknown` is an accepted licence; blank is not. `breaker dataset validate` exits
non-zero on a refusal and runs in CI, so the prose below cannot drift away from
the file.

**The telemetry is SYNTHETIC.** No utility feed was recorded, no dataset was
downloaded, and there is no telemetry data file in this repository. Say it
first, because this is the component whose whole pitch is not inventing
figures — and the numbers it reads were authored, not measured.

| Field | Value |
| --- | --- |
| Source | None. Generated in code by [`breaker/telemetry.py`](breaker/telemetry.py). |
| Real or synthetic | **Synthetic** |
| Records | **360** — 9 units (`battery_1`…`battery_9`) x 40 ticks (0–39) |
| Tick interval | 60 s (`TICK_SECONDS`), from a fixed epoch `2026-08-15T02:00:00Z` |
| Feeder | `feeder-7` on every row |
| Licence | n/a — nothing third-party is used |

How it is generated, so the shape is auditable without reading the module:

- Healthy baseline is **linear** in unit index and tick — e.g.
  `soc = 82.0 - index*0.6 - tick*0.15 + wobble`.
- `wobble` is a **closed-form sine of the indices**,
  `scale * sin(tick*0.7 + unit_index*1.9)` — not a random draw. There is no
  RNG and no seed anywhere in the module; determinism comes from the sine.
  (**GAP-003 is now fixed at source**: the module docstring used to say "fixed
  seed", which implied something you could reseed to get a different fixture.
  You cannot — this fixture varies only by changing the formula in `_wobble`.)
- The fault is **injected**: for `battery_4` from `ONSET_TICK = 18`, SOC falls
  1.10/tick, temperature rises 0.35/tick, and charge current falls 5.80/tick
  floored at 0.4.
- The rule needs a full 10-tick window plus one reading, so the trip lands
  five ticks after onset — **tick 23**, `battery_4`, and no healthy unit ever
  fires. That is asserted rather than asserted-about:
  `tests/test_divergence_rule.py` pins `FIXTURE_DIVERGENCE_TICK = 23` and
  `assert len(records) == 360`.

The judgment substrates have their own provenance. `rule` is deterministic
and local. `dsv4` is `deepseek-v4-flash-0731` served on NVIDIA GB10 Grace
Blackwell hardware at `dgx-spark:18000`, and every response it has given
is cached to `fixtures/model-cache/` so the demo path replays with no
endpoint at all. `cuopt` has produced no data, because it is not installed.

### `spec-03`: which section says what

`spec-03`, cited above and throughout this repository, **is a real document in
a repository this checkout does not contain**. It is
`specs/03_Grid_Guardian.md` in the `nvidia-hackathon-system` governance
catalog — a *peer* repository, not a sibling in this suite.

*GAP-001, which said the document does not exist, is retracted.* It was filed
after grepping this tree and finding nothing, which is the reasonable
conclusion from inside this repository and the wrong one. (For the record, the
same gap claimed there was no spec-01 or spec-02 either; `01_Seattle_Resilience_Digital_Twin.md`
and `02_StormOps_Seattle.md` are in that directory too.)

The citations were then wrong a **second** way, which matters more: nearly all
of them pointed at **§3.2**, and §3.2 is *Controlled demonstration sequence* —
not a schema. The sections are:

| Section | What it actually is | breaker's relationship to it |
| --- | --- | --- |
| §3.1 Hero scenario | The site: PV, grid, inverter, **four** battery modules, critical load, deferrable GPU job | **Divergent.** This fixture generates **nine** units. `battery_4` is the divergent unit in both, so the beat is faithful and the peer set is simply wider than specified. |
| §3.2 Controlled demonstration sequence | The 3-minute beat sheet. Row 0:25–0:55: *"Battery anomaly / Detect SOC/current divergence / Peer comparison"* | **Implemented.** This is the beat breaker stages, and it is the only thing §3.2 was ever a correct citation for. |
| §4.1 FR-01 | Ingest time-stamped V, A, W, Wh, temperature, SOC, status, alarms | **Partial.** V, A, SOC and temperature are ingested. W, Wh, status and alarms are not. |
| §6.2 Telemetry schema | The spec's row shape | **Not implemented — see below.** |

### breaker's row shape diverges from spec-03 §6.2, and it is not going to be pretended otherwise

§6.2 specifies:

```json
{"timestamp": "2026-08-13T19:20:00Z", "asset_id": "battery_4",
 "measurements": {"voltage_v": 51.8, "current_a": 2.1,
                  "soc_pct": 18.0, "temperature_c": 31.2},
 "status": "charging", "quality": "good"}
```

`GET :8602/telemetry/fixture` returns:

```json
{"unit_id": "battery_1", "tick": 0, "ts": "2026-08-15T02:00:00+00:00",
 "soc_pct": 82.0, "temp_c": 27.5, "charge_current_a": 40.0,
 "voltage_v": 48.1, "feeder": "feeder-7"}
```

Flat, not nested. `unit_id` not `asset_id`, `ts` not `timestamp`, `temp_c` not
`temperature_c`, `charge_current_a` not `current_a`; `tick` and `feeder` are
breaker's own; `status` and `quality` are absent entirely.

The definition of record for what breaker ingests and emits is the `Reading`
dataclass in [`breaker/telemetry.py`](breaker/telemetry.py) and its mirror in
`contracts/openapi.yaml`. **The spec is not being edited to match the code and
the code is not being described as if it matched the spec.** Aligning the two
is unstarted work, and until it is done every §6.2 citation in this repository
should be read as *"the section that specifies a row shape we do not
implement"*.

## Known limitations

1. **The telemetry is synthetic** (see above). The rule reading it is
   deterministic and renders every check line by line, so nothing about the
   verdict is hidden — but the numbers under the verdict were authored.
2. **cuOpt is a registered alternate labelled unavailable-without-GPU, not
   faked.** `config/substrates.yaml` carries `available: false`; selecting it
   is refused `409` and the active substrate is unaffected rather than
   half-switching. It is never installed here: GPU-only, ~10 GB, and the PyPI
   name `cuopt` is a squatted stub — the real distribution is `cuopt-cu12`
   from pypi.nvidia.com. A GPU host existing in the fleet does not change
   that. Claiming a solver where a rule runs would be worse than not naming
   cuOpt at all.
3. **`nemotron` is the default now; `dsv4` is registered but not active.**
   `dsv4` is DeepSeek, not Nemotron, and switching to it would put a
   non-NVIDIA model in the CONDITION slot the bounty requires — so it stays a
   registered, available alternate rather than the default. Both run on the
   same GB10 pair (`dgx-spark`/`72`, 121 GiB unified memory); the
   local-hardware claim is about the hardware, `dgx-spark:8000` versus
   `dgx-spark:18000` is about which model is actually being asked.
4. **`dsv4` is currently unreachable, and it is on purpose.** The GB10 pair
   cannot hold both the 2-node `deepseek-v4-flash-0731` job and the local
   Nemotron model now serving `nemotron` (the same free-up that made
   `nemotron` reachable on `:8000`). The operator authorised stopping
   DeepSeek on 2026-08-16, so `dsv4`'s endpoint is down by decision rather
   than by failure. The restore recipe — `docker start dsv4-0731-rank0` on 71
   and `dsv4-0731-rank1` on 72, after stopping the Nemotron container — is in
   `blindspot`'s README; restoring it would also take `nemotron` down.

   It costs the demo nothing either way: the cache replays,
   `BREAKER_MODEL_OFFLINE=1` refuses the network outright, and an unreachable
   model **abstains** rather than silently becoming the rule with the model's
   name still on the screen. The `dsv4`-vs-`rule` disagreement at ticks 22 and
   23 is shown from cache.
5. **A model substrate is consulted only after a check has tripped**, not
   every tick, so the substrates are compared at the moment of divergence
   rather than across the whole run.
6. **breaker does not implement spec-03 §6.2's telemetry schema, and does not
   claim to.** §6.2 nests the four readings under `measurements` and carries
   `asset_id`, `status` and `quality`; breaker's row is flat, uses `unit_id`,
   and has no `status` or `quality`. §4.1 FR-01 is satisfied only in part
   (no W, Wh, status or alarms). The full field-by-field comparison is in
   *Data provenance* above. This is unstarted work, not a naming quibble.
7. **The fixture generates nine battery modules; spec-03 §3.1 specifies
   four.** The divergent unit is `battery_4` in both, so the §3.2 beat is
   faithful; the peer set is wider than specified.
8. **No `LICENSE` file** and no `license` field in `pyproject.toml`.

## Tests

```bash
.venv/bin/python -m pytest tests --cov=breaker --cov-fail-under=70
bash scripts/bvt.sh     # clean venv, real boot, every contract path + the CLI
python3 scripts/check-contract-drift.py   # contract vs routes, BOTH directions
```

`contracts/openapi.yaml` used to declare two paths while the service served
fifteen. The thirteen undeclared ones were a real read surface — `/proposals`,
`/telemetry/series`, `/evidence/{unit_id}`, `/substrates/*`, `/abstentions`,
`/dispatches`, `/healthz` and the rest — so all thirteen are now documented
rather than deleted or waived; `contracts/undocumented.yaml` is empty on
purpose. `scripts/check-contract-drift.py` runs as its own CI job and fails on
a served-but-undocumented path AND on a documented path the server does not
serve, which is the direction the existing contract test could never see.

**Known pre-existing gap, not introduced here:** `contracts/opencli.yaml`
declares 5 of the CLI's 12 commands (it declared 2 of 9 before this change).
The three `dataset` commands added here are
declared; `serve`, `proposal list`, `proposal decide`, `proposal walk`, `stream`,
`evidence` and `substrates` are still undeclared, and nothing was removed to
hide that.

## Orchestrator

See https://git.nemotron.example.com/nvidia-hackathon/nemo-nvidia-demo/nemo-nvidia-demo-system
