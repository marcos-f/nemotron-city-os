"""warrant's HTTP surface, and the console page that renders it.

Discovery of who can do what is a **GET**, and the read path has no writer
behind it — the projection is built from the ledger and cannot mutate it.

Revocation is a POST to a ``revocations`` collection rather than a DELETE,
because it carries a rationale and because its refusal must come back with a
named rule in a body. It is not a soft delete.

Every route is registered from one call in ``helm/app.py`` so that this file,
its templates and its tests are additive.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from helm.signet import SESSION_COOKIE, attesting
from helm.warrant.model import (
    BREAKGLASS_DEFAULT_MINUTES,
    BREAKGLASS_MAX_MINUTES,
    GLOBAL_STEWARD_ROLE,
    ROLE_BLURB,
    ROLES,
    RULES,
    Refused,
    Unknown,
    unknown_message_ui,
    unknown_reason_text,
    utcnow,
)
from helm.warrant.service import Warrant

TAG = "datasets"


# ------------------------------------------------------------------ schemas
class ClaimRequest(BaseModel):
    """No body fields: the claimant is the authenticated subject, never a
    name typed into a form. Onboarding confers ownership on whoever actually
    onboarded."""


class GrantRequest(BaseModel):
    subject: str = Field(min_length=1, description="OIDC subject of the grantee")
    role: str = Field(description=f"one of {', '.join(ROLES)}")


class RevokeRequest(BaseModel):
    subject: str = Field(min_length=1)
    rationale: str | None = None


class DelegateRequest(BaseModel):
    subject: str = Field(min_length=1)
    role: str = "admin"
    expires_at: str = Field(
        min_length=1,
        description="ISO-8601 instant. REQUIRED — authority that never lapses is a grant.",
    )


class BreakGlassRequest(BaseModel):
    reason: str = Field(min_length=1, description="Goes on the record, permanently.")
    window_minutes: int = Field(default=BREAKGLASS_DEFAULT_MINUTES, ge=1,
                                le=BREAKGLASS_MAX_MINUTES)


class ExitRequest(BaseModel):
    reason: str = "closed by hand"


def register_warrant(app: FastAPI, *, federation: Any, signet: Any, templates: Any) -> Warrant:
    """Mount warrant on ``app``. Returns the service for reuse by MCP tools."""

    warrant = Warrant(federation, signet)
    app.state.warrant = warrant

    def identity_of(request: Request):
        return signet.identity_from_token(request.cookies.get(SESSION_COOKIE))

    def actor_of(request: Request):
        return warrant.actor_from_identity(identity_of(request))

    def write(request: Request, run):
        """Run a warrant write inside the caller's attestation.

        signet binds the verified identity for the duration, so the issuer and
        auth_mode that reach throughline come from the session that was
        actually verified — not from anything a layer in between could supply.
        WHICH AUTHORITY vouched is part of the grant, not decoration on it.
        """
        denial = require_subject(request)
        if denial is not None:
            return denial
        identity = identity_of(request)
        with attesting(identity):
            try:
                return JSONResponse(run(warrant.actor_from_identity(identity)),
                                    status_code=201)
            except Refused as exc:
                return refused_response(exc)

    def refused_response(exc: Refused) -> JSONResponse:
        body = exc.as_dict()
        body["message_ui"] = (
            f"Cannot say — {exc.detail or exc.reason}"
            if isinstance(exc, Unknown)
            else f"Refused — {exc.reason}"
        )
        return JSONResponse(body, status_code=exc.status)

    def require_subject(request: Request) -> JSONResponse | None:
        identity = identity_of(request)
        if not identity.authenticated or not identity.subject:
            return JSONResponse(
                {
                    "refused": True,
                    "rule": "WARRANT-ANONYMOUS",
                    "reason": "the identity provider has not vouched for anyone here",
                    "detail": (
                        "authorization needs a verified subject. The provider "
                        "authenticates; warrant authorizes; neither works alone."
                    ),
                },
                status_code=401,
            )
        return None

    # =================================================================== read
    @app.get(
        "/datasets/{dataset_id}/authority",
        tags=[TAG],
        summary="Who may do what to this dataset, and who gave them the power to",
    )
    def get_authority(dataset_id: str, request: Request,
                      subject: str | None = None,
                      action: str | None = None) -> JSONResponse:
        """A pure read. There is no writer behind this path.

        Returns ``known: false`` — never an empty list — when the substrate is
        unreachable or the chain does not verify. "We could not find out" and
        "nobody has access" are different facts with different shapes.
        """
        snap = warrant.snapshot()
        authority = snap.authority(dataset_id)
        now = utcnow()
        payload = authority.to_dict(now)
        payload["roles"] = [{"role": role, "may": ROLE_BLURB[role]} for role in reversed(ROLES)]
        if subject:
            held = authority.capability(subject, now) if authority.known else None
            if not authority.known:
                decision = "unknown"
            elif action:
                decision = "allow" if authority.may(subject, action, now) else "deny"
            else:
                decision = "allow" if held else "deny"
            payload["decision"] = {
                "subject": subject,
                "action": action,
                "decision": decision,
                "held": held,
                "rule": None if decision != "deny" else (
                    "WARRANT-NOT-ADMIN" if action == "admin" else "WARRANT-NO-AUTHORITY"
                ),
                "detail": (
                    payload.get("detail") or authority.detail
                    if decision == "unknown" else ""
                ),
            }
        return JSONResponse(payload, status_code=200 if authority.known else 503)

    @app.get("/datasets/{dataset_id}/breakglass", tags=[TAG],
             summary="Break-glass windows on this dataset, active and historical")
    def get_breakglass(dataset_id: str, request: Request) -> JSONResponse:
        snap = warrant.snapshot()
        authority = snap.authority(dataset_id)
        if not authority.known:
            body = authority.to_dict()
            body["windows"] = None
            return JSONResponse(body, status_code=503)
        now = utcnow()
        return JSONResponse({
            "dataset_id": dataset_id,
            "known": True,
            "as_of": authority.as_of,
            "windows": [w.to_dict(now) for w in authority.breakglass.values()],
            "active": [w.to_dict(now) for w in authority.open_breakglass(now)],
            "eligible_voters": sorted(warrant.eligible_voters(dataset_id, snap)),
            "global_role": GLOBAL_STEWARD_ROLE,
        })

    @app.get("/authority/mine", tags=[TAG],
             summary="Datasets the caller holds authority over, with its provenance")
    def my_authority(request: Request) -> JSONResponse:
        actor = actor_of(request)
        snap = warrant.snapshot()
        if not snap.known:
            return JSONResponse({
                "subject": actor.subject,
                "known": False,
                "reason": snap.reason,
                "detail": snap.detail,
                "datasets": None,
                # Each unknown states ITS cause. "The substrate is not
                # answering" was printed even when throughline had replied in
                # milliseconds and the read had merely fallen short of the
                # head — a false claim about a running service, made right
                # next to the ``detail`` that said what actually happened.
                "reason_text": unknown_reason_text(snap.reason),
                "message_ui": unknown_message_ui(snap.reason, snap.detail),
            }, status_code=503)
        return JSONResponse({
            # The identity half comes from the Actor WITH its provenance, and
            # is never re-derived here from the two strings beside it. This is
            # the caller's own live session, so it is the one surface where
            # the verdict can honestly be `verified`; a mock session renders
            # `unverified`, and anything whose only evidence is a record's own
            # word renders `unknown`.
            **actor.to_dict(),
            "known": True,
            "as_of": snap.as_of,
            "datasets": snap.datasets_with_authority(actor.subject),
            "administers": snap.datasets_administered_by(actor.subject),
            "global_role": actor.global_role,
        })

    @app.get("/authority/rules", tags=[TAG],
             summary="Every refusal warrant can name, and what each one means")
    def authority_rules() -> dict[str, Any]:
        return {
            "rules": [{"rule": rule, "reason": reason} for rule, reason in sorted(RULES.items())],
            "roles": [{"role": role, "may": ROLE_BLURB[role]} for role in reversed(ROLES)],
            "global_role": {
                "role": GLOBAL_STEWARD_ROLE,
                "note": (
                    "A named role held by people, listed in the UI. It grants no "
                    "dataset authority by itself — it may only vote in a "
                    "break-glass quorum. It is deliberately not a superuser."
                ),
            },
        }

    # ================================================================= writes
    @app.post("/datasets/{dataset_id}/claim", status_code=201, tags=[TAG],
              summary="Onboard a dataset: the verified subject becomes its first administrator")
    def claim(dataset_id: str, body: ClaimRequest, request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.claim(dataset_id, actor))

    @app.post("/datasets/{dataset_id}/grants", status_code=201, tags=[TAG],
              summary="Grant a role on this dataset. Administrators only. "
                      "Refused if it would leave no administrator.")
    def grant(dataset_id: str, body: GrantRequest, request: Request) -> JSONResponse:
        """A grant can REDUCE authority as well as widen it.

        Granting an existing administrator a lesser role is a downgrade, and a
        sole administrator granting ITSELF ``reader`` orphans the dataset —
        which is why the last-administrator invariant is enforced here and not
        only on the revocation path.
        """
        return write(request, lambda actor: warrant.grant(
            dataset_id, body.subject, body.role, actor))

    @app.post("/datasets/{dataset_id}/revocations", status_code=201, tags=[TAG],
              summary="Withdraw a role. Refused if it would leave no administrator.")
    def revoke(dataset_id: str, body: RevokeRequest, request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.revoke(
            dataset_id, body.subject, actor, body.rationale or ""))

    @app.post("/datasets/{dataset_id}/delegations", status_code=201, tags=[TAG],
              summary="Lend administrative authority with a mandatory expiry")
    def delegate(dataset_id: str, body: DelegateRequest, request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.delegate(
            dataset_id, body.subject, body.role, body.expires_at, actor))

    @app.post("/datasets/{dataset_id}/delegations/{delegation_id}/revoke",
              status_code=201, tags=[TAG], summary="End a delegation early")
    def end_delegation(dataset_id: str, delegation_id: str, request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.revoke_delegation(
            dataset_id, delegation_id, actor))

    @app.post("/datasets/{dataset_id}/breakglass", status_code=201, tags=[TAG],
              summary="Ask a quorum of OTHER datasets' administrators to open a time-boxed window")
    def request_breakglass(dataset_id: str, body: BreakGlassRequest,
                           request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.request_breakglass(
            dataset_id, body.reason, actor, body.window_minutes))

    @app.post("/datasets/{dataset_id}/breakglass/{request_id}/vote",
              status_code=201, tags=[TAG],
              summary="Vote to open a break-glass window. The Nth vote releases it through the gate.")
    def vote_breakglass(dataset_id: str, request_id: str, request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.vote_breakglass(
            dataset_id, request_id, actor))

    @app.post("/datasets/{dataset_id}/breakglass/{request_id}/exit",
              status_code=201, tags=[TAG],
              summary="Close a break-glass window. Never blocked, always ledgered.")
    def exit_breakglass(dataset_id: str, request_id: str, body: ExitRequest,
                        request: Request) -> JSONResponse:
        return write(request, lambda actor: warrant.exit_breakglass(
            dataset_id, request_id, actor, body.reason))

    @app.post("/authority/sweep", status_code=200, tags=[TAG],
              summary="Close out delegations and windows the clock has already ended")
    def sweep(request: Request) -> JSONResponse:
        """Enforcement never waited for this — it only closes the audit trail.

        Expiry is evaluated against the clock on every read, so a delegation
        stops working whether or not this ever runs. Exposed so a demo can
        show the ledger catching up, and so a scheduler has something honest
        to call.
        """
        return JSONResponse(warrant.sweep())

    # ================================================================ console
    @app.get("/datasets/{dataset_id}/people", response_class=HTMLResponse,
             include_in_schema=False)
    def people_page(dataset_id: str, request: Request):
        identity = identity_of(request)
        if not identity.authenticated:
            return RedirectResponse("/login", status_code=303)
        snap = warrant.snapshot()
        authority = snap.authority(dataset_id)
        now = utcnow()
        view = authority.to_dict(now)
        actor = warrant.actor_from_identity(identity)
        return templates.TemplateResponse(
            request=request,
            name="warrant_people.html",
            context={
                "dataset_id": dataset_id,
                "a": view,
                "roles": [{"role": r, "may": ROLE_BLURB[r]} for r in reversed(ROLES)],
                "viewer": actor.to_dict(),
                "may_administer": authority.known and authority.may(actor.subject, "admin", now),
                "global_role": GLOBAL_STEWARD_ROLE,
                "boot": {
                    "page": "dataset-people",
                    "subject": identity.subject,
                    "role": identity.role,
                    "authenticated": True,
                    "prefs": signet.prefs_for(identity.subject),
                },
                "ctx": {"prefs": signet.prefs_for(identity.subject)},
            },
        )

    return warrant


__all__ = ["register_warrant"]
