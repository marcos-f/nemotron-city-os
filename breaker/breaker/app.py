"""HTTP surface. Implements contracts/openapi.yaml on :8602.

Two rules govern how failure is *reported* here, both of which exist because
breaker's fail-closed behaviour was already correct and merely illegible:

* An unreachable substrate is a **503 naming the dependency**, never a bare
  500. The invariant is unchanged — no proposal, no effect, no dispatch —
  but the operator gets told which thing died instead of reading an empty
  body. This is enforced by an app-wide exception handler, so a path added
  later cannot re-open the hole by forgetting a ``try``.
* ``/healthz`` distinguishes "gate configured" from "gate reachable". A
  health check that reports green while its only dependency is unreachable
  is worse than no health check at all.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from throughline.datasets_api import DEFAULT_REGISTRY_ENV, attach_datasets

from . import __version__
from .engine import ProposalNotFound
from .models import TelemetryReading
from .rule import (
    CHARGE_COLLAPSE_RATIO,
    SOC_DELTA_MAX_PCT,
    TEMP_SLOPE_MIN_C,
    WINDOW_TICKS,
)
from .service import GATE_DEPENDENCY, BreakerService, Settings
from .substrates import SubstrateUnavailable
from .telemetry import DIVERGENT_UNIT, TICKS, UNITS, fixture, reading_for, series
from .throughline import (
    CallerTokenMissing,
    DEFAULT_CALLER_ROLE,
    GateViolation,
    SubstrateRefused,
    SubstrateUnreachable,
)

#: Component-specific override for the dataset registry path, checked before
#: the federation-wide DATASETS_CONFIG and before the in-repo default. The BVT
#: boots from a copied source tree, so the path has to be settable without a
#: code change.
DATASETS_ENV = "BREAKER_DATASETS"

#: Anchored to the repository, not to the current directory — the same
#: convention config/substrates.yaml already follows, so `breaker dataset list`
#: reads the same registry whichever directory it is run from.
DEFAULT_DATASETS = Path(__file__).resolve().parents[1] / "config" / "datasets.yaml"


def datasets_path() -> Path:
    """Where the dataset registry lives: env override, then the in-repo file."""
    override = os.environ.get(DATASETS_ENV) or os.environ.get(DEFAULT_REGISTRY_ENV)
    return Path(override) if override else DEFAULT_DATASETS

DESCRIPTION = (
    "A breaker trips and only a human resets it. Microgrid telemetry in, a "
    "deterministic divergence rule shown line by line, and an irreversible "
    "load-shed dispatch that waits at throughline's gate for an identified human."
)

#: Env flag gating ``POST /fixture/run`` — the bulk synthetic-fixture driver.
#:
#: A review found a 346-deep backlog of near-duplicate SYNTHETIC battery_4
#: load-shed proposals sitting in a live approval queue, oldest ~10 hours,
#: generated continuously with no throttling. The generator was this
#: endpoint: mounted unconditionally on every boot, unauthenticated, and
#: driving the *entire* synthetic fixture (breaker/telemetry.py — 9 units x
#: 40 ticks, closed-form sine) through intake on every call, with nothing
#: requiring an operator to have actually asked for a demo run. throughline
#: has since added a pending-duplicate cap at its gate to limit the damage
#: downstream, but the generator itself needed to stop running by default.
#:
#: Per the operator directive — synthetic data is permitted in the product
#: only behind an explicitly-labelled, non-default setting — this endpoint
#: now refuses (403) unless BREAKER_ENABLE_FIXTURE_DRIVE=1 is set. It is
#: read fresh on every request, not baked into Settings at construction
#: time, so an operator (or a test) can flip it without reconstructing the
#: service.
#:
#: POST /signals/telemetry (one reading, one deliberate act) and the CLI
#: `breaker stream` demo driver (already bounded — it stops at the first
#: proposal, and requires a human to type the command) are untouched: they
#: are already deliberate, operator-initiated, and real telemetry has to
#: keep flowing through /signals/telemetry unimpeded.
FIXTURE_DRIVE_ENV = "BREAKER_ENABLE_FIXTURE_DRIVE"


def create_app(settings: Optional[Settings] = None,
               service: Optional[BreakerService] = None) -> FastAPI:
    app = FastAPI(title="breaker", version=__version__, description=DESCRIPTION)
    app.state.service = service or BreakerService(settings)

    # The dataset discovery surface, mounted from throughline rather than
    # reimplemented: GET /datasets, GET /datasets/{id}, POST /datasets/reload.
    # breaker keeps no ledger of its own — every consequence it records is
    # posted to throughline's — so ledger=None, and a refused registry shows up
    # on GET /datasets as a refusal rather than in a local append-only file.
    attach_datasets(app, component="breaker", ledger=None,
                    path=datasets_path())

    def svc(request: Request) -> BreakerService:
        return request.app.state.service

    # -------------------------------------------------- failure, made legible

    @app.exception_handler(SubstrateUnreachable)
    async def substrate_unreachable_handler(
        request: Request, exc: SubstrateUnreachable
    ) -> JSONResponse:
        """503, naming the dependency, on every path — present and future.

        breaker has exactly one dependency. When it cannot be reached breaker
        holds: no proposal is emitted, no effect is created, nothing
        dispatches. That invariant was already correct; what was missing was
        anyone being able to read it off the wire. A bare 500 with no body
        says "breaker is broken". A 503 naming throughline says "breaker is
        fine and is refusing to act because its gate is gone", which is the
        truth and is also actionable.
        """
        service = getattr(request.app.state, "service", None)
        reachability = None
        url = None
        if service is not None:
            url = service.settings.throughline_url
            # Probed after the failure, not before: a cached "reachable" taken
            # two seconds ago would contradict the 503 we are answering with.
            reachability = service.gate_reachability(force=True)
        return JSONResponse(
            status_code=503,
            content={
                "error": "substrate_unreachable",
                "dependency": GATE_DEPENDENCY,
                "dependency_url": url,
                "detail": (f"{GATE_DEPENDENCY} unreachable at {url}: {exc}"
                           if url else f"{GATE_DEPENDENCY} unreachable: {exc}"),
                "reason": str(exc),
                "fail_closed": True,
                "invariant": ("no proposal was emitted and no effect was created; "
                              "breaker holds when it cannot reach the gate"),
                "gate": reachability,
            },
        )

    @app.exception_handler(CallerTokenMissing)
    async def caller_token_missing_handler(
        request: Request, exc: CallerTokenMissing
    ) -> JSONResponse:
        """WE COULD NOT AUTHENTICATE. THE GATE DID NOT REFUSE THE EFFECT.

        Registered ahead of the ``SubstrateRefused`` handler because it is a
        strictly narrower fact, and the narrower fact is the useful one. A 403
        surfaced as "the gate refused this effect" when the truth is "breaker
        could not authenticate to the substrate" sends an operator to argue
        with a policy instead of to configure a token — and, worse, tells them
        their release was considered and denied when it was never seen.

        Nothing dispatched, exactly as before: this subclasses
        ``SubstrateRefused`` and so ``SubstrateUnreachable``, and every
        fail-closed ``except`` upstream still catches it.
        """
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": "caller_token_missing",
                "dependency": GATE_DEPENDENCY,
                "authenticated": False,
                # Deliberately NOT `substrate_refused`: this is not a refusal.
                "reached": bool(exc.refusal.get("attempted", True)),
                "substrate_status": exc.status,
                "detail": str(exc),
                "refusal_reason": exc.refusal_reason,
                "token_source": exc.token_source,
                "fail_closed": True,
                "invariant": (
                    "breaker could not authenticate to the gate, so the gate "
                    "never judged the decision; nothing dispatched and the "
                    "effect is still held. This is an AUTHENTICATION problem, "
                    "not a refusal of the effect."
                ),
                "remedy": (
                    "set THROUGHLINE_CALLER_TOKEN, or run breaker where the "
                    "substrate's <data-dir>/caller-token file is readable"
                ),
            },
        )

    @app.exception_handler(SubstrateRefused)
    async def substrate_refused_handler(
        request: Request, exc: SubstrateRefused
    ) -> JSONResponse:
        """The gate was REACHED and refused. That is not a dead dependency.

        A 403 "caller_role is required" means throughline is alive, received
        the request and applied its policy. Reporting it as unreachable sends
        an operator hunting a process that is running fine, and hides the one
        thing they need to read — the refusal. The gate's own refusal record
        is passed through verbatim, and its status code is preserved.

        Nothing dispatched, exactly as before: ``SubstrateRefused`` subclasses
        ``SubstrateUnreachable``, so every fail-closed path still catches it.
        """
        refusal = exc.refusal
        return JSONResponse(
            status_code=exc.status,
            content={
                "error": "substrate_refused",
                "dependency": GATE_DEPENDENCY,
                "reached": True,
                "substrate_status": exc.status,
                "detail": str(exc),
                "refusal_reason": refusal.get("refusal_reason"),
                "refusal": refusal or None,
                "fail_closed": True,
                "invariant": ("the gate answered and declined; nothing dispatched, "
                              "and the effect is still held"),
            },
        )

    @app.exception_handler(GateViolation)
    async def gate_violation_handler(
        request: Request, exc: GateViolation
    ) -> JSONResponse:
        """502: the gate answered, but did not hold an irreversible effect."""
        return JSONResponse(
            status_code=502,
            content={
                "error": "gate_violation",
                "dependency": GATE_DEPENDENCY,
                "detail": str(exc),
                "fail_closed": True,
                "invariant": "breaker will not dispatch an effect the gate did not hold",
            },
        )

    def _reading_from(body: dict[str, Any]) -> Optional[TelemetryReading]:
        """Accept the reading inline or flat; the envelope alone is also fine."""
        payload = body.get("reading") or {
            key: body[key] for key in
            ("unit_id", "tick", "soc_pct", "temp_c", "charge_current_a",
             "voltage_v", "feeder", "ts")
            if key in body
        }
        if not payload or "unit_id" not in payload:
            return None
        return TelemetryReading.model_validate(payload)

    # ----------------------------------------------------------- contract

    @app.post("/signals/telemetry", status_code=201, tags=["signals"])
    async def post_telemetry_signal(request: Request, body: dict[str, Any] = Body(...)):
        """Ingest microgrid telemetry as a signal.

        The contract's Signal envelope is the body. A telemetry reading may ride
        with it (inline as ``reading`` or flat); when one does, the divergence
        rule runs and its evidence comes back with the signal.
        """
        service = svc(request)
        reading = _reading_from(body)
        signal = {
            "id": body.get("id") or (
                f"sig-{reading.unit_id}-{reading.tick}" if reading else "sig-breaker"),
            "class": body.get("class", "microgrid.telemetry"),
            "source": body.get("source", "breaker.microgrid.feeder-7"),
            "ingest_time": body.get("ingest_time"),
            "real_or_synthetic": body.get("real_or_synthetic", "synthetic"),
            "payload_ref": body.get("payload_ref"),
            "staleness": body.get("staleness"),
        }
        if signal["ingest_time"] is None:
            from .models import utcnow

            signal["ingest_time"] = utcnow().isoformat()

        if reading is None:
            return {**signal, "evaluation": None, "proposal": None}

        # GateViolation -> 502 and SubstrateUnreachable -> 503 are handled
        # app-wide (see the handlers above), which is how every path gets the
        # same body naming the dependency instead of each path inventing its
        # own shape — or, as on /fixture/run, forgetting to have one.
        outcome = service.watch.ingest(reading)
        return {**signal, **outcome}

    @app.get("/proposals/{id}", tags=["proposals"])
    async def get_proposal(id: str, request: Request):
        """Get a dispatch proposal, held at the gate pending human decision."""
        try:
            return svc(request).watch.get(id).model_dump(mode="json")
        except ProposalNotFound:
            raise HTTPException(status_code=404, detail=f"unknown proposal {id}")

    # ------------------------------------------------ additive read surface

    @app.get("/proposals", tags=["proposals"])
    async def list_proposals(request: Request):
        service = svc(request)
        proposals = [p.model_dump(mode="json") for p in service.watch.all_proposals()]
        return {
            "proposals": proposals,
            "waiting_at_gate": sum(1 for p in proposals if p["status"] == "waiting_at_gate"),
            "gate": service.gate_status(),
        }

    @app.post("/proposals/{id}/decide", tags=["proposals"])
    async def decide_proposal(id: str, request: Request, body: dict[str, Any] = Body(...)):
        """Relay an identified decision to throughline. The gate decides."""
        decided_by = body.get("decided_by")
        if not decided_by:
            raise HTTPException(
                status_code=422,
                detail="decided_by (the approver's OIDC subject) is required",
            )
        decision = body.get("decision", "approve")
        if decision not in ("approve", "reject"):
            raise HTTPException(status_code=422, detail="decision must be approve or reject")
        # Relayed, not decided here, and defaulted rather than hard-coded:
        # the gate owns the allowlist, and an agent that declares itself must
        # be refused ON THE LEDGER rather than filtered out before it gets
        # there. Omitting it is what the gate refuses, so breaker always
        # sends one.
        caller_role = body.get("caller_role") or DEFAULT_CALLER_ROLE
        # The ATTESTATION, relayed on exactly the same terms as the role: the
        # gate will not release an irreversible effect on a decision that does
        # not name the authority which authenticated the approver. breaker
        # authenticates nobody and so must not supply a default here — an
        # invented `auth_mode` would forge the very claim being asked for.
        # Absent, it is sent absent, and the gate refuses ON THE LEDGER where
        # the refusal can be seen.
        auth_mode = body.get("auth_mode") or ""
        issuer = body.get("issuer") or ""
        try:
            proposal = svc(request).watch.decide(
                id, decision, decided_by, body.get("rationale"),
                caller_role=caller_role, auth_mode=auth_mode, issuer=issuer)
        except ProposalNotFound:
            raise HTTPException(status_code=404, detail=f"unknown proposal {id}")
        return proposal.model_dump(mode="json")

    @app.get("/proposals/{id}/walk", tags=["proposals"])
    async def walk_proposal(id: str, request: Request):
        """effect → judgment → signal, as the substrate records it."""
        try:
            return svc(request).watch.walk(id)
        except ProposalNotFound:
            raise HTTPException(status_code=404, detail=f"unknown proposal {id}")

    @app.get("/evidence/{unit_id}", tags=["rule"])
    async def get_evidence(unit_id: str, request: Request):
        """The rule's latest working for one unit — the evidence pane."""
        evaluation = svc(request).watch.evaluation_for(unit_id)
        if evaluation is None:
            raise HTTPException(status_code=404, detail=f"no evaluation for {unit_id}")
        return evaluation.as_dict()

    @app.get("/telemetry/fixture", tags=["telemetry"])
    async def get_fixture(
        ticks: int = Query(default=TICKS, ge=1, le=TICKS),
        unit: Optional[str] = Query(default=None),
    ):
        rows = [r.as_dict() for r in fixture(ticks) if unit is None or r.unit_id == unit]
        return {
            "records": rows, "ticks": ticks, "units": list(UNITS),
            "divergent_unit": DIVERGENT_UNIT, "real_or_synthetic": "synthetic",
        }

    @app.get("/telemetry/series", tags=["telemetry"])
    async def get_series(
        unit: str = Query(default=DIVERGENT_UNIT),
        ticks: int = Query(default=TICKS, ge=1, le=TICKS),
    ):
        """Sparkline data: one unit's fields across the fixture."""
        return {
            "unit": unit, "ticks": ticks,
            "soc_pct": series(unit, "soc_pct", ticks),
            "temp_c": series(unit, "temp_c", ticks),
            "charge_current_a": series(unit, "charge_current_a", ticks),
            "real_or_synthetic": "synthetic",
        }

    @app.post("/fixture/run", tags=["telemetry"])
    async def run_fixture(
        request: Request,
        ticks: int = Query(default=TICKS, ge=1, le=TICKS),
        unit: Optional[str] = Query(default=None),
    ):
        """Drive the fixture through intake; returns where divergence fired.

        Demo-only, and OFF by default. This drives the entire synthetic
        fixture through intake in a single call, which is how a 346-deep
        backlog of near-duplicate SYNTHETIC battery_4 proposals accumulated
        in a live approval queue with nobody having asked for it. Refuses
        unless an operator has explicitly set BREAKER_ENABLE_FIXTURE_DRIVE=1.
        """
        if os.environ.get(FIXTURE_DRIVE_ENV) != "1":
            raise HTTPException(
                status_code=403,
                detail=(
                    f"POST /fixture/run is disabled by default. It drives the "
                    f"entire SYNTHETIC fixture through intake and can raise a "
                    f"real, irreversible dispatch proposal at throughline's "
                    f"gate — set {FIXTURE_DRIVE_ENV}=1 to run it deliberately."
                ),
            )
        service = svc(request)
        fired: list[dict[str, Any]] = []
        for record in fixture(ticks):
            if unit is not None and record.unit_id != unit:
                continue
            outcome = service.watch.ingest(TelemetryReading(**record.as_dict()))
            if outcome["proposal"]:
                fired.append(outcome["proposal"])
        return {
            "ticks": ticks,
            "records": len(fixture(ticks)) if unit is None else ticks,
            "proposals": fired,
            "gate": service.gate_status(),
        }

    @app.get("/substrates", tags=["rule"])
    async def get_substrates(request: Request):
        return svc(request).registry.as_dict()

    @app.post("/substrates/{substrate_id}", tags=["rule"])
    async def select_substrate(substrate_id: str, request: Request):
        """Swap the judgment substrate. Refuses one that cannot run here."""
        try:
            substrate = svc(request).registry.select(substrate_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown substrate {substrate_id}")
        except SubstrateUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return substrate.as_dict()

    @app.get("/substrates/{substrate_id}/health", tags=["rule"])
    async def substrate_health(substrate_id: str, request: Request):
        """Is this substrate actually usable? Probed, not asserted."""
        service = svc(request)
        try:
            substrate = service.registry.get(substrate_id)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"unknown substrate {substrate_id}")

        payload = {"substrate": substrate.as_dict()}
        if substrate.kind == "model-endpoint":
            payload["health"] = service.watch.judge.health()
        elif substrate.kind == "gpu-solver":
            payload["health"] = {
                "reachable": False,
                "detail": ("not installed here by design: GPU-only, ~10GB, and the "
                           "PyPI name `cuopt` is a squatted stub"),
            }
        else:
            payload["health"] = {"reachable": True, "detail": "in-process rule"}
        return payload

    @app.get("/abstentions", tags=["rule"])
    async def get_abstentions(request: Request):
        """Windows where the active substrate declined to judge."""
        return {"abstentions": svc(request).watch.abstentions}

    @app.get("/dispatches", tags=["proposals"])
    async def get_dispatches(request: Request):
        return {"dispatches": svc(request).watch.dispatches}

    @app.get("/ledger", tags=["proposals"])
    async def get_ledger(request: Request):
        """breaker's own append-only record of what happened to a proposal.

        Not throughline's ledger — this one is breaker's, and it exists
        because proposal closure (a divergence that stopped diverging,
        closing its own proposal) and repeat-observation updates have
        nowhere else to be recorded. Entries are appended only; nothing here
        is ever edited or removed.
        """
        return {"ledger": svc(request).watch.ledger}

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request):
        """Liveness, plus the reachability of the one dependency breaker has.

        Still 200 when the gate is down — the process IS alive, and flipping
        the status code would break every caller that treats /healthz as a
        liveness probe. What changed is that the body no longer claims green:
        ``status`` goes ``degraded``, ``degraded`` names why, and
        ``gate.reachable`` is false.
        """
        service = svc(request)
        gate = service.gate_status()
        degraded: list[str] = []
        if gate.get("reachable") is False:
            degraded.append(
                f"{GATE_DEPENDENCY} at {gate['url']} is not reachable: "
                f"{gate['reachability']['detail']}"
            )
        if service.watch.gate_violations:
            degraded.append(
                f"{len(service.watch.gate_violations)} gate violation(s) recorded")
        # `available` is a config claim; `reachable` is a probe. Both are
        # reported, on every registered substrate, for the same reason the
        # gate block reports both.
        reachability = service.substrate_reachability()
        active_reach = reachability.get(service.registry.active.id, {})
        if active_reach.get("reachable") is False:
            degraded.append(
                f"active judgment substrate {service.registry.active.id} is not "
                f"reachable: {active_reach.get('detail', '')}")
        return {
            "component": "breaker",
            "version": __version__,
            "status": "degraded" if degraded else "ok",
            "degraded": degraded,
            "gate": gate,
            "substrate": {
                "active": service.registry.active.id,
                "label": service.registry.active.label,
                "reachable": active_reach.get("reachable"),
                "reachability": reachability,
                "registered": [
                    {**s.as_dict(), **{
                        "reachable": reachability.get(s.id, {}).get("reachable"),
                        "reachable_detail": reachability.get(s.id, {}).get("detail", ""),
                    }}
                    for s in service.registry.all()
                ],
            },
            "rule": {
                "window_ticks": WINDOW_TICKS,
                "soc_delta_max_pct": SOC_DELTA_MAX_PCT,
                "temp_slope_min_c": TEMP_SLOPE_MIN_C,
                "charge_collapse_ratio": CHARGE_COLLAPSE_RATIO,
            },
            "proposals": len(service.watch.proposals),
            "waiting_at_gate": sum(
                1 for p in service.watch.all_proposals() if p.status == "waiting_at_gate"),
            "gate_violations": service.watch.gate_violations,
        }

    return app


def __getattr__(name: str):
    """Build the default app lazily — importing must not boot a service."""
    if name == "app":
        global _default_app
        try:
            return _default_app
        except NameError:
            _default_app = create_app()
            return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
