"""HTTP surface. Implements contracts/openapi.yaml on :8600."""

from __future__ import annotations

import hmac
import json
from typing import Any, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from . import __version__
from .approvals import AlreadyDecided, ApprovalNotFound
from .config import (
    POLICY_REVERSIBILITY_DOWNGRADE,
    POLICY_SOURCE_ALLOWLIST,
    ConfigRefusal,
)
from .datasets_api import attach_datasets
from .gate import EffectNotFound, PendingBacklogCapped, RoleRefused, UntraceableEffectRefused
from .ledger import LedgerUnwritable, digest
from .models import Decision, Effect, EffectProposal, Judgment, Signal
from .redact import scrub
from .service import Settings, Substrate
from .walk import walk_effect

#: throughline's read surface — the ledger, its verification, and the
#: approval queue — is deliberately public: public, third-party
#: verifiability of the hash chain is this product's differentiator, not an
#: oversight. A review found the surface anonymous and unlabelled and could
#: not tell a deliberate design from an accident; this is the label. It is
#: both machine-readable (every field below) and human-readable (the prose
#: in ``reason`` and ``message_ui``), and it is attached to the responses of
#: the endpoints it names rather than gating them — gating this surface
#: would remove the feature it describes.
PUBLIC_VERIFICATION_NOTICE: dict[str, Any] = {
    "public": True,
    "surface": ["/ledger", "/ledger/verify", "/approvals"],
    "reason": (
        "This endpoint is intentionally readable without authentication. "
        "throughline's hash-chained ledger and its approval history are "
        "meant to be checked by a third party — an auditor, a reviewer, "
        "another component in the federation — without an account. That is "
        "a deliberate design decision, not an oversight."
    ),
    "excluded_from_public_read": [
        "writes are authenticated where they change who may do what: "
        "POST /approvals/{id}/decide, an authz.* effect and a warrant.* "
        "signal all require the caller token (see /healthz -> gate)",
        "/admin/roles (role administration) is not part of this surface "
        "and requires authentication",
        "server-local filesystem paths and internal host:port topology are "
        "redacted from every unauthenticated JSON response THIS COMPONENT "
        "(throughline) serves — every route in its own table, not a sample "
        "of them. This guarantee is scoped to throughline's own process: it "
        "says nothing about any other component in the federation (docket, "
        "siren, breaker, blindspot, helm, ...). Each of those serves and "
        "secures its own endpoints, including its own /healthz, and "
        "throughline cannot speak for a response it did not produce.",
        "decider identity is excluded from the anonymous view: "
        "decided_by (and its mirror, approved_by) is pseudonymized on every "
        "unauthenticated JSON response THIS COMPONENT serves, and a bare "
        "email address is scrubbed wherever else it appears. This is "
        "deliberate, not a gap in the redaction above — the hash chain and "
        "its verification (whether a decision happened, when, and to what "
        "effect) are meant to be checked by anyone; WHO made a given "
        "decision is a different fact, is not required to check chain "
        "integrity, and is not published to a caller who presented no "
        "credential. The pseudonym is a stable, non-reversible token (a "
        "keyed hash of the real subject) rather than a blank: two entries "
        "decided by the same person still visibly share one pseudonym, so "
        "an auditor can see repetition without learning identity. An "
        "authenticated caller (the caller token) still sees the real "
        "subject.",
    ],
    "message_ui": "Public verification surface — readable by design, for "
                  "third-party audit. See 'excluded_from_public_read' for "
                  "what this deliberately does not cover.",
}

#: Header carrying the caller token on throughline's PRIVILEGED write surface.
CALLER_TOKEN_HEADER = "X-Throughline-Caller-Token"

#: Named refusal for a privileged write that presents no valid token.
REFUSAL_CALLER_TOKEN = "caller-token-required"


def _caller_token_refusal(substrate, request: Request, *, act: str, detail: dict):
    """Authenticate the caller on the privileged half of the write surface.

    throughline holds no session and cannot authenticate a *person*; helm
    does that. What it can authenticate is the CALLER — the process on the
    other end of the socket — and until it did, the honest identity work
    helm performs could be skipped by not going through helm. A review
    proved both halves of that: a decide forging
    ``auth_mode: oidc, issuer: https://git.nemotron.example.com`` mints an approval
    the console will render as verified, and an ``authz.grant`` posted
    directly mints an administrator. Attestation refusal alone does not stop
    either, because a caller who can invent a subject can invent an issuer
    beside it. This is the half that does.

    Scoped, deliberately, to the acts where forging one changes who may do
    what — a decide, and the ``authz.*`` / ``warrant.*`` records the
    permission graph is projected from. Ordinary signal, judgment and effect
    traffic (docket's routes, siren's alerts, breaker's probes) is
    untouched, which is what keeps this deployable without a flag day.

    The token comes from ``THROUGHLINE_CALLER_TOKEN`` or, failing that, from
    a 0600 file the substrate mints for itself at
    ``<data_dir>/caller-token``. There is no way to turn the check off: a
    substrate with no token configured makes one, and a caller that can read
    it is a caller with access to the substrate's own data directory.
    """
    required = substrate.settings.resolved_caller_token
    presented = request.headers.get(CALLER_TOKEN_HEADER, "")
    if presented and hmac.compare_digest(presented, required):
        return None
    substrate.ledger.append("write.refused", {
        "act": act,
        "reason": (
            "this act changes who may do what, and the caller presented no "
            "valid caller token; throughline authenticates the caller on its "
            "privileged write surface"
        ),
        "refusal_reason": REFUSAL_CALLER_TOKEN,
        "presented_a_token": bool(presented),
        **detail,
    })
    return JSONResponse(status_code=403, content={
        "detail": (
            "caller token required: this act changes who may do what. Present "
            f"{CALLER_TOKEN_HEADER} (see /healthz -> gate.caller_token)."
        ),
        "refused": True,
        "refusal_reason": REFUSAL_CALLER_TOKEN,
        "act": act,
        **detail,
        "message_ui": "Refused — the caller is not authenticated to this substrate.",
    })


def _is_privileged_effect(effect_type: Optional[str]) -> bool:
    """``authz.*`` effects ARE the permission graph. Anything else is not."""
    return (effect_type or "").startswith("authz.")


def _is_privileged_signal(signal_class: Optional[str]) -> bool:
    """warrant's structured authz records ride in on signals of this class."""
    return (signal_class or "").startswith("warrant.")


DESCRIPTION = (
    "signals in, gated effects out, every consequence traceable to its cause. "
    "Irreversible effects are never auto-executed: they are queued and wait, "
    "without a deadline, for a human decision."
)


def create_app(settings: Optional[Settings] = None, substrate: Optional[Substrate] = None) -> FastAPI:
    app = FastAPI(title="throughline", version=__version__, description=DESCRIPTION)
    app.state.substrate = substrate or Substrate(settings)

    def sub(request: Request) -> Substrate:
        return request.app.state.substrate

    def _is_authenticated(request: Request) -> bool:
        substrate = sub(request)
        presented = request.headers.get(CALLER_TOKEN_HEADER, "")
        return bool(presented) and hmac.compare_digest(
            presented, substrate.settings.resolved_caller_token
        )

    @app.middleware("http")
    async def _scrub_unauthenticated_responses(request: Request, call_next):
        """Never let a filesystem path or an internal host:port leave this
        process on a response an unauthenticated caller can read.

        The ledger is append-only and several historical entries already
        recorded a fully-resolved config path at load time — that cannot be
        edited without rewriting history, which this product treats as the
        one unforgivable act. So the redaction happens here, at the
        serialization boundary, on the way OUT, leaving the ledger file
        itself untouched. An authenticated caller (one presenting a valid
        caller token) is exempt: they are already trusted with the write
        surface and an operator debugging locally still needs the real path.
        """
        response = await call_next(request)
        if _is_authenticated(request):
            return response
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response
        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return Response(
                content=body, status_code=response.status_code,
                headers=dict(response.headers), media_type=response.media_type,
            )
        scrubbed = scrub(
            payload,
            identity_key=sub(request).settings.resolved_caller_token.encode("utf-8"),
        )
        new_body = json.dumps(scrubbed).encode("utf-8")
        headers = dict(response.headers)
        headers["content-length"] = str(len(new_body))
        return Response(
            content=new_body, status_code=response.status_code,
            headers=headers, media_type=response.media_type,
        )

    @app.exception_handler(LedgerUnwritable)
    async def _ledger_unwritable(request: Request, exc: LedgerUnwritable):
        # Fail closed: an unwritable ledger halts effects, it does not skip them.
        return JSONResponse(
            status_code=503,
            content={"error": "ledger_unwritable", "detail": str(exc),
                     "effect_executed": False},
        )

    # ----------------------------------------------------------- contract

    @app.post("/signals", status_code=201, response_model=Signal, tags=["signals"])
    async def post_signal(signal: Signal, request: Request):
        """Ingest a signal and record it in the ledger.

        A ``warrant.*`` signal carries the structured record an authorization
        effect is projected from, so it is part of the permission graph and
        requires an authenticated caller. Every other class does not.
        """
        substrate = sub(request)
        if _is_privileged_signal(signal.signal_class):
            refusal = _caller_token_refusal(
                substrate, request, act="signal.ingest",
                detail={"signal_id": signal.id, "signal_class": signal.signal_class},
            )
            if refusal is not None:
                return refusal
        substrate.ledger.append("signal.ingested", signal.model_dump(mode="json", by_alias=True))
        return signal

    @app.post("/judgments", status_code=201, response_model=Judgment, tags=["signals"])
    async def post_judgment(judgment: Judgment, request: Request) -> Judgment:
        """Record a judgment about a signal — the walk's middle hop."""
        sub(request).ledger.append("judgment.recorded", judgment.model_dump(mode="json"))
        return judgment

    @app.post("/effects", status_code=202, response_model=Effect, tags=["effects"])
    async def post_effect(proposal: EffectProposal, request: Request):
        """Propose an effect. Irreversible effects are queued, never executed.

        An ``authz.*`` effect IS a change to who may do what, so it requires
        an authenticated caller. Ordinary effects do not, and route exactly
        as they always have.
        """
        substrate = sub(request)
        if _is_privileged_effect(proposal.effect_type):
            refusal = _caller_token_refusal(
                substrate, request, act="effect.propose",
                detail={"effect_id": proposal.id, "effect_type": proposal.effect_type},
            )
            if refusal is not None:
                return refusal
        try:
            return await substrate.gate.submit(proposal)
        except UntraceableEffectRefused as exc:
            # Ledgered already, inside the gate. No id is minted, nothing is
            # queued: this proposal never becomes an entry to lose track of.
            return JSONResponse(status_code=422, content={
                "detail": str(exc),
                "refused": True,
                "refusal_reason": exc.reason,
                "effect_id": exc.effect_id,
                "message_ui": (
                    "Refused — this effect cannot be traced to its cause "
                    f"({exc.reason}). throughline does not mint an identity "
                    "for it."
                ),
            })
        except PendingBacklogCapped as exc:
            # Ledgered already, inside the gate. The effect stays refused;
            # nothing already queued or decided is touched.
            return JSONResponse(status_code=429, content={
                "detail": str(exc),
                "refused": True,
                "refusal_reason": "pending-duplicate-cap",
                "effect_id": exc.effect_id,
                "cap": exc.cap,
                "duplicate_count": exc.duplicate_count,
                "message_ui": (
                    "Refused — a near-identical proposal is already pending "
                    f"{exc.duplicate_count} times over; throughline will not "
                    "grow that backlog further."
                ),
            })

    # Path parameters are named exactly as the contract names them.
    @app.get("/effects/{id}", response_model=Effect, tags=["effects"])
    async def get_effect(id: str, request: Request) -> Effect:
        try:
            return sub(request).gate.get(id)
        except EffectNotFound:
            raise HTTPException(status_code=404, detail=f"unknown effect {id}")

    @app.get("/effects/{id}/walk", tags=["effects"])
    async def walk(id: str, request: Request) -> dict[str, Any]:
        """effect -> judgment -> signal, <=3 hops, each hash verified live."""
        result = walk_effect(sub(request).ledger, id)
        if result is None:
            raise HTTPException(status_code=404, detail=f"unknown effect {id}")
        return result

    @app.get("/ledger/verify", tags=["ledger"])
    async def verify_ledger(request: Request) -> dict[str, Any]:
        """Walk the hash chain. Returns valid=false naming the broken seq."""
        return {
            **sub(request).ledger.verify(),
            "public_verification": PUBLIC_VERIFICATION_NOTICE,
        }

    @app.post("/approvals/{id}/decide", tags=["approvals"])
    async def decide_approval(id: str, decision: Decision, request: Request) -> dict[str, Any]:
        """Record a human decision. This is the only path to execution.

        Releasing an irreversible effect additionally requires an
        ATTESTATION on the decision — see
        :meth:`throughline.gate.Gate._check_attestation`. An unattested
        approve is refused (403), ledgered, and the effect stays held.
        """
        substrate = sub(request)
        refusal = _caller_token_refusal(
            substrate, request, act="approval.decide", detail={"approval_id": id},
        )
        if refusal is not None:
            return refusal
        try:
            return await substrate.gate.decide(id, decision)
        except ApprovalNotFound:
            raise HTTPException(status_code=404, detail=f"unknown approval {id}")
        except RoleRefused as exc:
            # Same ``detail`` shape as any other refusal, plus the named
            # reason so a console can render *which* rule held the line.
            return JSONResponse(status_code=403, content={
                "detail": str(exc),
                "refused": True,
                "refusal_reason": exc.reason,
                "approval_id": id,
                "message_ui": "Approval refused — the effect is still held.",
            })
        except AlreadyDecided as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except EffectNotFound as exc:
            raise HTTPException(status_code=404, detail=f"unknown effect {exc}")

    # ------------------------------------------------ additive read surface

    @app.get("/approvals", tags=["approvals"])
    async def list_approvals(
        request: Request, state: Optional[str] = Query(default=None)
    ) -> dict[str, Any]:
        queue = sub(request).queue
        items = queue.snapshot()
        if state:
            items = [i for i in items if i["state"] == state]
        return {"approvals": items, "pending": len(queue.pending()),
                "health": queue.health,
                "public_verification": PUBLIC_VERIFICATION_NOTICE}

    @app.get("/ledger", tags=["ledger"])
    async def read_ledger(
        request: Request,
        limit: int = Query(default=50, ge=1, le=1000),
        offset: Optional[int] = Query(
            default=None, ge=0,
            description=(
                "Read a WINDOW from the head of the chain instead of its tail. "
                "Omitted, the response is the last `limit` entries, exactly as "
                "before. Supplied, the response is entries [offset, offset+limit) "
                "in chain order, so a consumer that needs the WHOLE chain can "
                "page through it — `limit` stays the per-page cap it always was."
            ),
        ),
    ):
        """The tail by default; the whole chain by paging.

        ``limit`` is capped at 1000 because an unbounded read of an
        append-only chain is a denial of service against this service. That
        cap is a PER-PAGE cap, not a ceiling on what a consumer may know:
        with ``offset`` it walks the chain in pages and reaches the head.

        A consumer that needs the complete history reads from ``offset=0``
        and keeps going while ``has_more`` is true. ``offset`` is echoed back
        so a caller can tell a paging server from an older one that ignored
        the parameter and handed back a suffix.
        """
        ledger = sub(request).ledger
        if offset is None:
            entries = ledger.tail(limit)
            window: dict[str, Any] = {}
        else:
            # ``read()`` is the whole chain; the slice is what we serve.
            all_entries = ledger.read()
            entries = all_entries[offset:offset + limit]
            returned = len(entries)
            next_offset = offset + returned
            more = next_offset < len(all_entries)
            window = {
                "offset": offset,
                "limit": limit,
                "returned": returned,
                # ``total_entries`` counts LINES, which is what a pager needs;
                # ``chain_length`` stays the sequence counter it has always
                # been. They agree on a healthy chain and disagree loudly on
                # one with a corrupt line, which is information, not noise.
                "total_entries": len(all_entries),
                # Truthful about the chain as read a moment ago. An append
                # that lands after this line simply shows up on a later page.
                "has_more": more,
                "next_offset": next_offset if more else None,
            }
        for entry in entries:
            # Per-row verification, recomputed now: the tick in the UI can go red.
            entry["verified"] = "_raw" not in entry and digest(entry) == entry.get("sha256")
        return {"entries": entries, "chain_length": ledger.length, "head": ledger.head,
                "public_verification": PUBLIC_VERIFICATION_NOTICE, **window}

    @app.get("/config", tags=["config"])
    async def get_config(request: Request) -> dict[str, Any]:
        substrate = sub(request)
        return {"config": substrate.config.as_dict(), "refusal": substrate.boot_refusal}

    @app.post("/config/reload", tags=["config"])
    async def reload_config(
        request: Request, body: dict[str, Any] = Body(default_factory=dict)
    ):
        """Reload atomically. A refusal names file, rule and line, and the
        previously loaded config keeps running untouched.

        Two guards sit in front of the swap, because the registry is what the
        gate consults and is therefore part of the gate:

        * the source must be inside the allowlisted config directory — an
          arbitrary path is refused (403) and the path is named in the ledger;
        * a candidate that would reclassify any effect type down to
          ``reversible`` is not applied. It is proposed as an irreversible
          effect and held for a human (202), and the running registry keeps
          classifying that effect type exactly as it did a moment ago.
        """
        substrate = sub(request)
        path = body.get("path") or substrate.settings.config_path
        try:
            candidate = substrate.stage_config(path)
        except ConfigRefusal as refusal:
            payload = refusal.as_dict()
            outside = payload["policy"] == POLICY_SOURCE_ALLOWLIST
            substrate.boot_refusal = payload
            return JSONResponse(status_code=403 if outside else 422, content={
                **payload,
                "allowlist": [str(p) for p in substrate.settings.config_allowlist],
                "active_config": substrate.config.as_dict(),
                "message_ui": (
                    "Configuration refused — source is outside the allowlisted "
                    "config directory. Previous configuration still running."
                ) if outside else
                "Configuration refused — previous configuration still running.",
            })

        found = substrate.downgrades_against_running(candidate)
        if found:
            effect = await substrate.hold_config_downgrade(candidate, found)
            return JSONResponse(status_code=202, content={
                "refused": False,
                "held": True,
                "policy": POLICY_REVERSIBILITY_DOWNGRADE,
                "effect_id": effect.id,
                "approval_id": effect.approval_id,
                "downgrades": found,
                "candidate_config": candidate.as_dict(),
                "active_config": substrate.config.as_dict(),
                "message_ui": (
                    "Reversibility downgrade held for a human — this reload "
                    "would make an irreversible effect type auto-executable. "
                    "The previous configuration is still running."
                ),
            })

        config = substrate.config_store.apply(candidate)
        substrate.boot_refusal = None
        return {"refused": False, "held": False, "config": config.as_dict()}

    @app.get("/healthz", tags=["ops"])
    async def healthz(request: Request) -> dict[str, Any]:
        substrate = sub(request)
        verification = substrate.ledger.verify()
        queue_health = substrate.queue.health
        alarms: list[dict[str, Any]] = []
        if not verification["valid"]:
            alarms.append({"signal": "ledger_invalid", **verification})
        if queue_health.get("degraded"):
            alarms.append({"signal": "approvals_degraded", **queue_health})
        return {
            "component": "throughline",
            "version": __version__,
            # An operator reading one field must be able to tell whether to
            # trust the rest of this payload.
            "status": "degraded" if alarms else "ok",
            "degraded": bool(alarms),
            "alarms": alarms,
            "ledger": {"chain_length": substrate.ledger.length, "head": substrate.ledger.head,
                       "path": str(substrate.ledger.path),
                       "valid": verification["valid"],
                       "boot_verification": substrate.boot_verification},
            "approvals_pending": len(substrate.queue.pending()),
            "approvals": {
                "pending": len(substrate.queue.pending()),
                "cache_path": str(substrate.queue.path),
                "health": queue_health,
            },
            # What it takes to RELEASE an irreversible effect here. An
            # operator reading this can tell a substrate that only accepts
            # verified identities from one widened for a mock demo, without
            # reading the code or the environment.
            "gate": {
                "attested_auth_modes": list(substrate.gate.attested_modes),
                "irreversible_requires_attestation": True,
                "caller_token": {
                    # The token itself is never published — only where a
                    # local caller should look for it, and which acts need it.
                    "required_on": ["approval.decide", "authz.* effects",
                                    "warrant.* signals"],
                    "source": substrate.settings.caller_token_source,
                    "path": str(substrate.settings.caller_token_path),
                    "header": CALLER_TOKEN_HEADER,
                },
            },
            "config": {"source": substrate.config.source, "version": substrate.config.version,
                       "rules": len(substrate.config.rules),
                       "refusal": substrate.boot_refusal,
                       "allowlist": [str(p) for p in substrate.settings.config_allowlist]},
            "relay": substrate.mirror.status(),
        }

    # Dataset discovery: a read surface over the declared registry. Lives in
    # its own module so provenance discovery can be added to a component
    # without reopening its route table.
    attach_datasets(
        app,
        component="throughline",
        ledger=app.state.substrate.ledger,
        component_env="THROUGHLINE_DATASETS",
    )

    return app


def __getattr__(name: str):
    """Build the default app only when something actually asks for it.

    ``uvicorn throughline.app:app`` still works, but merely *importing* this
    module no longer constructs a Substrate — which opened a second ledger and
    wrote a config.loaded entry every time a tool imported it for its type
    hints. Importing must not touch the ledger.
    """
    if name == "app":
        global _default_app
        try:
            return _default_app
        except NameError:
            _default_app = create_app()
            return _default_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
