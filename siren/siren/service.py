"""The service: contract paths first, then the panes helm renders.

Every payload that carries incident rows carries the as-of label with them.
There is no path here that returns a list of incidents stripped of the
provenance that says whether they are live or cached.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from throughline.datasets_api import attach_datasets

from . import __version__, feed, reload as reload_mod
from .models import Incident, Pulse, Signal
from .substrate import SubstrateError, build_substrate, substrate_choice


def signal_for(incident: Incident, state: feed.FeedState) -> Signal:
    """One incident -> one federation Signal.

    ``real_or_synthetic`` follows the data, not the mood: rows that came off
    the live poll are real; rows replayed from cache are still real 911
    records, so they stay real and carry their age in ``staleness`` instead.
    """
    status = state.status(substrate_choice())
    staleness = f"PT{status.stale_seconds or 0}S"
    return Signal(
        id=f"siren-incident-{incident.id}",
        **{"class": reload_mod.INCIDENT_SIGNAL_CLASS},
        source="seattle.fire.911",
        ingest_time=state.as_of,
        real_or_synthetic="real",
        payload_ref=f"https://data.seattle.gov/resource/{feed.SOCRATA_DATASET}.json"
                    f"?incident_number={incident.id}",
        staleness=staleness,
    )


def _ascii_header(value: str) -> str:
    """Header-safe rendering of a label written for human eyes."""
    return (value.replace("\u00b7", "|").replace("\u2014", "-")
            .encode("ascii", "replace").decode("ascii"))


def create_app() -> FastAPI:
    app = FastAPI(
        title="siren",
        version=__version__,
        description="The class that hot-loads mid-demo from one config file.",
    )

    state = feed.FeedState()
    hot = reload_mod.HotReload()
    substrate = build_substrate()
    # Serve something real from the first request onward: a snapshot read is
    # local, so this costs no network even in OFFLINE_MODE.
    feed.refresh(state) if not feed.offline_mode() else feed._serve_snapshot(
        state, feed.OFFLINE_LABEL, error=None
    )

    app.state.feed = state
    app.state.hot = hot
    app.state.substrate = substrate

    def pulse() -> Pulse:
        return Pulse(status=app.state.feed.status(app.state.substrate.name),
                     incidents=app.state.feed.incidents)

    # ---------------- contract paths ----------------

    @app.post("/signals/incident", status_code=201)
    def post_incident_signal(signal: Signal) -> dict[str, Any]:
        """Accept an incident signal and relay it to throughline.

        A relay failure is reported as one: the signal is echoed back with
        ``relayed: false`` and the substrate's error, never as a success.
        """
        body = signal.wire()
        try:
            app.state.substrate.post_signal(signal)
            body["relayed"] = True
        except SubstrateError as exc:
            body["relayed"] = False
            body["relay_error"] = str(exc)
        body["substrate"] = app.state.substrate.name
        return body

    @app.get("/incidents", response_model=list[Incident])
    def list_incidents(response: Response) -> list[Incident]:
        """Map-ready rows. The as-of label rides along in headers so even
        this bare contract array cannot be rendered without its provenance;
        /pulse carries the same facts in the body."""
        status = app.state.feed.status(app.state.substrate.name)
        response.headers["X-Siren-As-Of"] = status.as_of
        response.headers["X-Siren-Source"] = status.source
        # HTTP headers are latin-1 on the wire; the label is written for a
        # pane and carries em dashes and middots. Fold it, don't mangle it.
        response.headers["X-Siren-Label"] = _ascii_header(status.label)
        # The zone the rows were converted through, so even the bare contract
        # array cannot be read as "some timestamps, presumably UTC".
        response.headers["X-Siren-Tz"] = status.tz
        return app.state.feed.incidents

    # ---------------- panes ----------------

    @app.get("/pulse", response_model=Pulse)
    def get_pulse() -> Pulse:
        """City pulse: rows bound to the label that qualifies them."""
        return pulse()

    @app.get("/feed/status")
    def feed_status() -> dict[str, Any]:
        status = app.state.feed.status(app.state.substrate.name)
        return {**status.model_dump(), "last_error": app.state.feed.last_error,
                "dataset": feed.SOCRATA_DATASET}

    @app.post("/feed/refresh")
    def feed_refresh() -> dict[str, Any]:
        """Poll now (or, offline, re-read the cache). Never raises: a failed
        poll degrades to snapshot, which is cut order #2 by design."""
        feed.refresh(app.state.feed) if not feed.offline_mode() else feed._serve_snapshot(
            app.state.feed, feed.OFFLINE_LABEL, error=None
        )
        status = app.state.feed.status(app.state.substrate.name)
        return {**status.model_dump(), "last_error": app.state.feed.last_error}

    def _mode_body() -> dict[str, Any]:
        return {
            **feed.offline_state(),
            "substrate": app.state.substrate.name,
            "feed": app.state.feed.status(app.state.substrate.name).model_dump(),
        }

    @app.get("/feed/mode")
    def get_feed_mode() -> dict[str, Any]:
        """Is siren offline right now, and on whose say-so."""
        return _mode_body()

    @app.post("/feed/mode")
    def set_feed_mode(body: dict[str, Any] | None = None) -> Any:
        """Switch a RUNNING siren between offline and live.

        ``OFFLINE_MODE=1`` is read from the environment, which means it can
        only be set before the process starts. A scripted demo that arms a
        socket guard in its own process therefore could not stop siren
        polling Socrata beside it: ``demo.py --offline`` was offline for the
        demo and not for siren. This endpoint is the missing half.

        Offline is a guarantee, not a preference. Flipping it re-serves the
        cached snapshot with its own as-of label and, by default, rebuilds
        the substrate as the in-process mock so a stale
        ``SIREN_SUBSTRATE=real`` cannot keep a socket open behind the switch.
        Live mode is untouched: this adds a promise, it does not replace one.
        ``{"mode": "env"}`` clears the override and hands the decision back
        to the environment.

        ``{"substrate": "keep"}`` keeps the configured substrate instead.
        That exists because the orchestrator boots siren with
        ``SIREN_SUBSTRATE=real`` and throughline is on **loopback**: the
        refusal beat is only ledgered by throughline in that mode, and a
        scripted offline run allows loopback while forbidding the internet.
        Dropping to the mock would take the demo's refusal off the ledger to
        prevent a connection the run was never going to make. The 911 feed is
        snapshot-only either way — ``keep`` buys a loopback substrate, never
        an outbound poll.
        """
        payload = body or {}
        raw = payload.get("mode", payload.get("offline"))
        if isinstance(raw, bool):
            value: Any = raw
        elif isinstance(raw, str):
            token = raw.strip().lower()
            if token in ("offline", "1", "true", "yes", "on"):
                value = True
            elif token in ("live", "0", "false", "no", "off"):
                value = False
            elif token in ("env", "default", "auto"):
                value = None
            else:
                return JSONResponse(status_code=422, content={
                    "error": "unknown mode",
                    "detail": f"mode must be offline, live or env; got {raw!r}",
                })
        else:
            return JSONResponse(status_code=422, content={
                "error": "mode is required",
                "detail": 'send {"mode": "offline"}, {"mode": "live"} or {"mode": "env"}',
            })

        substrate_mode = str(payload.get("substrate", "follow")).strip().lower()
        if substrate_mode not in ("follow", "keep"):
            return JSONResponse(status_code=422, content={
                "error": "unknown substrate option",
                "detail": f"substrate must be follow or keep; got {payload['substrate']!r}",
            })

        feed.set_offline(value)
        if substrate_mode == "follow":
            # The substrate follows the switch, or offline is only half true.
            app.state.substrate = build_substrate()
        if feed.offline_mode():
            feed._serve_snapshot(app.state.feed, feed.OFFLINE_LABEL, error=None)
        else:
            feed.refresh(app.state.feed)
        return {**_mode_body(), "substrate_option": substrate_mode}

    @app.post("/feed/emit")
    def feed_emit(limit: int = 5) -> dict[str, Any]:
        """Emit the freshest incidents as signals into the substrate."""
        emitted, failures = [], []
        for incident in app.state.feed.incidents[: max(0, limit)]:
            signal = signal_for(incident, app.state.feed)
            try:
                app.state.substrate.post_signal(signal)
                emitted.append(signal.wire())
            except SubstrateError as exc:
                failures.append({"id": signal.id, "error": str(exc)})
        status = app.state.feed.status(app.state.substrate.name)
        return {
            "emitted": emitted,
            "emitted_count": len(emitted),
            "failures": failures,
            "substrate": app.state.substrate.name,
            "signal_class": reload_mod.INCIDENT_SIGNAL_CLASS,
            "as_of": status.as_of,
            "source": status.source,
            "label": status.label,
        }

    @app.get("/hot-reload/timeline")
    def hot_reload_timeline() -> dict[str, Any]:
        return {**app.state.hot.timeline(), "substrate": app.state.substrate.name}

    @app.post("/hot-reload")
    def hot_reload(body: dict[str, Any] | None = None) -> Any:
        """Drop a config into throughline and start the class flowing.

        A refused config comes back 422 with the refusal named by file, rule
        and line — the refusal panel renders this verbatim — and the previous
        config keeps running.
        """
        payload = body or {}
        path = payload.get("path") or os.environ.get(
            "SIREN_INCIDENT_CONFIG", reload_mod.DEFAULT_INCIDENT_CONFIG
        )
        emit = int(payload.get("emit", 3))

        result = app.state.hot.drop(path, app.state.substrate, emitted=0)
        if result["state"] == "held":
            # 202, not 422: the drop was ACCEPTED and is waiting for a human.
            # throughline treats a reversibility downgrade as an irreversible
            # effect, so the swap itself queues at the gate. Calling that a
            # refusal would be as wrong as calling it a success — and siren
            # used to call it a success, because it read only `refused`.
            return JSONResponse(status_code=202, content=result)
        if result["state"] != "registered":
            return JSONResponse(status_code=422, content=result)

        emitted = feed_emit(limit=emit)
        result = app.state.hot.drop(path, app.state.substrate,
                                    emitted=emitted["emitted_count"])
        return {**result, "signals": emitted}

    @app.get("/config/incident")
    def incident_config() -> dict[str, Any]:
        """The drop artifact as siren validates it — what the demo is about
        to hand throughline, before it hands it over."""
        path = os.environ.get("SIREN_INCIDENT_CONFIG", reload_mod.DEFAULT_INCIDENT_CONFIG)
        try:
            return {"path": path, "valid": True, "config": reload_mod.validate_config_file(path)}
        except reload_mod.ConfigRefusal as refusal:
            return JSONResponse(status_code=422,
                                content={"path": path, "valid": False, **refusal.as_dict()})

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        status = app.state.feed.status(app.state.substrate.name)
        return {
            "component": "siren",
            "version": __version__,
            "mode": status.mode,
            "offline": feed.offline_state(),
            "feed": {
                "source": status.source,
                "label": status.label,
                "as_of": status.as_of,
                "incident_count": status.incident_count,
                "dataset": feed.SOCRATA_DATASET,
                "last_error": app.state.feed.last_error,
                "tz": status.tz,
                "newest_reported_at": status.newest_reported_at,
                "newest_age_seconds": status.newest_age_seconds,
                "undated_incidents": status.undated_incidents,
                "snapshot_migrated": status.snapshot_migrated,
            },
            "hot_reload": {
                "state": app.state.hot.timeline()["state"],
                "registered_classes": app.state.hot.registered_classes,
                "refusal": app.state.hot.refusal,
            },
            "substrate": app.state.substrate.health(),
        }

    # ---------------- dataset registry ----------------
    #
    # Mounted from throughline's loader rather than reimplemented here: one
    # registry idiom across the federation, so a siren dataset entry and a
    # throughline one are refused by the same rules and read the same on the
    # wire. Nothing about siren's data is special enough to earn a second
    # implementation.
    #
    # ledger=None on purpose, and not by oversight: siren keeps NO local
    # ledger — it posts its signals to throughline over HTTP (see
    # siren/substrate.py), and there is no Ledger class in this package to
    # wire one to. A dataset refusal here is therefore visible on the wire
    # (422 from POST /datasets/reload, and the refusal on GET /datasets) but
    # is not hash-chained; the ledgered version of the same refusal is the one
    # throughline records when a registry is reloaded there.
    attach_datasets(app, component="siren", ledger=None,
                    component_env="SIREN_DATASETS")

    return app


app = create_app()
