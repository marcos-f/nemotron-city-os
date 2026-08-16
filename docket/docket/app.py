"""FastAPI surface for docket. Serves contracts/openapi.yaml on :8601."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from throughline.datasets_api import attach_datasets

from . import config, corpus
from .clients.nvidia import build_client as build_judge_client
from .clients.throughline import SubstrateUnavailable
from .clients.throughline import build_client as build_substrate_client
from .judge import judge_permit
from .models import Judgment, PermitSummary, RouteEffect, Signal
from .routing import RouteRefused, route_judgment, signal_for

app = FastAPI(
    title="docket",
    version="0.2.0",
    description="The reviewer that quotes its sources or shuts up.",
)

# The dataset registry: GET /datasets, GET /datasets/{id}, POST /datasets/reload.
#
# throughline's loader and router are IMPORTED, not reimplemented — one
# provenance vocabulary across the federation, and one place where the refusal
# rules live. docket keeps no ledger of its own (throughline's is the durable
# record), so no ledger is passed and refusals surface on the HTTP response and
# in app.state.dataset_refusal rather than in a local chain.
attach_datasets(
    app,
    component="docket",
    ledger=None,
    # config.datasets_path() already honours DOCKET_DATASETS and
    # DATASETS_CONFIG, and additionally survives being run from a wheel.
    path=config.datasets_path(),
)

# Judgments are held in-process for the demo's lifetime; throughline's ledger
# is the durable record, not this dict.
_JUDGMENTS: dict[str, Judgment] = {}
_SIGNALS: dict[str, Signal] = {}
_EFFECTS: dict[str, RouteEffect] = {}

_judge_client = None
_substrate_client = None


def judge_client():
    global _judge_client
    if _judge_client is None:
        _judge_client = build_judge_client()
    return _judge_client


def substrate_client():
    """Build (and cache) the substrate client.

    Raises SubstrateUnavailable, uncaught, when the real substrate cannot be
    reached and mock was not explicitly requested — nothing here catches it
    and silently swaps in a working-looking fallback. Only a successful build
    is cached, so the next call retries once the substrate recovers.
    """
    global _substrate_client
    if _substrate_client is None:
        _substrate_client = build_substrate_client()
    return _substrate_client


def reset_state() -> None:
    """Tests need a clean service without a fresh process."""
    global _judge_client, _substrate_client
    _JUDGMENTS.clear()
    _SIGNALS.clear()
    _EFFECTS.clear()
    _judge_client = None
    _substrate_client = None


def _mode() -> dict:
    """The honesty banner. Every payload that can mislead carries it."""
    jc = judge_client()
    try:
        substrate = substrate_client().substrate
    except SubstrateUnavailable:
        # Report the truth rather than building a working-looking fallback
        # client just to have something to put in this field.
        substrate = "unavailable"
    return {
        "judgment_source": "MOCK" if jc.name == "mock" else "hosted",
        "judgment_model": jc.model,
        "substrate": substrate,
        "mock": jc.name == "mock",
    }


@app.get("/healthz", summary="Liveness and mode")
def healthz() -> dict:
    return {
        "status": "ok",
        "component": "docket",
        "version": "0.2.0",
        "port": config.PORT,
        "permits": corpus.count(),
        "mode": _mode(),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.get("/permits", summary="The permit queue")
def list_permits(limit: int = 25, only_judgeable: bool = False) -> dict:
    rows = corpus.permits()
    out: list[PermitSummary] = []
    for row in rows:
        desc = (row.get("description") or "").strip()
        summary = PermitSummary(
            permitnum=row.get("permitnum", ""),
            permitclass=row.get("permitclass"),
            permittypedesc=row.get("permittypedesc"),
            description=desc,
            statuscurrent=row.get("statuscurrent"),
            originaladdress1=row.get("originaladdress1"),
            applieddate=row.get("applieddate"),
            judgeable=len(desc) >= config.EVIDENCE_MIN_CHARS,
        )
        if only_judgeable and not summary.judgeable:
            continue
        out.append(summary)
        if len(out) >= limit:
            break
    return {
        "count": len(out),
        "total": len(rows),
        "snapshot_utc": corpus.snapshot().get("snapshot_utc"),
        "permits": [p.model_dump() for p in out],
        "mode": _mode(),
    }


@app.get("/permits/{permitnum}", summary="One permit record")
def get_permit(permitnum: str) -> dict:
    row = corpus.get(permitnum)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown permit {permitnum}")
    return {"permit": row, "mode": _mode()}


@app.post("/signals/document", status_code=201, summary="Ingest a permit document")
def post_document_signal(signal: Signal) -> JSONResponse:
    _SIGNALS[signal.id] = signal
    return JSONResponse(status_code=201, content=signal.model_dump(by_alias=True))


@app.post("/permits/{permitnum}/judge", summary="Judge a permit, or abstain")
def judge(permitnum: str) -> dict:
    row = corpus.get(permitnum)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown permit {permitnum}")

    signal = signal_for(row)
    _SIGNALS[signal.id] = signal

    judgment = judge_permit(row, client=judge_client())
    judgment.signal_id = signal.id
    _JUDGMENTS[judgment.id] = judgment

    return {"judgment": judgment.model_dump(), "mode": _mode()}


@app.get("/judgments/{id}", summary="Get a judgment card or abstention")
def get_judgment(id: str) -> dict:
    judgment = _JUDGMENTS.get(id)
    if judgment is None:
        raise HTTPException(status_code=404, detail=f"unknown judgment {id}")
    return judgment.model_dump()


@app.post("/judgments/{id}/route", status_code=202, summary="Route an accepted judgment")
def route(id: str) -> JSONResponse:
    judgment = _JUDGMENTS.get(id)
    if judgment is None:
        raise HTTPException(status_code=404, detail=f"unknown judgment {id}")

    row = corpus.get(judgment.permitnum or "")
    try:
        substrate = substrate_client()
    except SubstrateUnavailable as exc:
        # 503, not a silently fabricated effect: the real ledger cannot be
        # reached, so there is nothing honest to route this judgment to.
        raise HTTPException(
            status_code=503, detail=f"substrate unavailable: {exc}"
        ) from exc
    try:
        effect = route_judgment(judgment, substrate, permit=row)
    except RouteRefused as exc:
        # 409, not 500: the service worked exactly as designed by refusing.
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    _EFFECTS[effect.id] = effect
    return JSONResponse(
        status_code=202,
        content={"effect": effect.model_dump(), "mode": _mode()},
    )


@app.get("/effects/{id}", summary="Status of a route effect")
def get_effect(id: str) -> dict:
    effect = _EFFECTS.get(id)
    if effect is None:
        raise HTTPException(status_code=404, detail=f"unknown effect {id}")
    return {"effect": effect.model_dump(), "mode": _mode()}
