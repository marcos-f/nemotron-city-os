"""helm — the FastAPI console on :8610.

Every console action is a documented API path. The HTML pages are thin: they
render what the same endpoints return, so ``/docs`` really is the whole
surface (``test://helm/openapi-surface-complete``).
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Literal

from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from helm import __version__
from helm.clients import Federation
from helm.config import Settings, load_settings
from helm.datasets_routes import router as datasets_router
from helm.console import Console
from helm.nemoclerk import NemoClerk, SessionStore, ToolRegistry
from helm.oidc import OidcClient, ProviderUnavailable, TokenInvalid, issuer_host
from helm.pages import TABS
from helm.signet import (
    GLOBAL_ROLES,
    ROLES,
    SESSION_COOKIE,
    Identity,
    Signet,
    attesting,
)
from helm.warrant.routes import register_warrant
from helm.warrant.tools import WarrantToolRegistry
from helm.views import PORTS, QUEUE_PAGE_SIZE, ViewBuilder

log = logging.getLogger("helm")

HERE = Path(__file__).parent

GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"


# --------------------------------------------------------------- schemas
class DecideRequest(BaseModel):
    """``decided_by`` is REQUIRED: a subject-less decision is a 422.

    test://signet/subject-required
    """

    decision: Literal["approve", "reject"]
    decided_by: str = Field(min_length=1, description="OIDC subject (signet) of the approver")
    rationale: str | None = None
    caller_role: Literal["human", "agent"] | None = None


class PrefsRequest(BaseModel):
    theme: Literal["light", "dark", "system"] | None = None
    feeds_collapsed: bool | None = None
    rail_collapsed: bool | None = None
    feeds_width: int | None = Field(default=None, ge=160, le=640)
    rail_width: int | None = Field(default=None, ge=200, le=720)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)
    feature_area: str = "helm"
    selected_event: str = ""


class RoleRequest(BaseModel):
    subject: str = Field(min_length=1)
    role: Literal["admin", "operator", "viewer", "agent"]


class GlobalRoleRequest(BaseModel):
    """A FEDERATION-level assignment, not a console role.

    ``global_role`` is typed as a plain string rather than a Literal so that
    an unknown value becomes a named 422 from signet's own validation
    instead of a schema rejection that says nothing about which roles exist.
    An empty string clears the assignment.
    """

    subject: str = Field(min_length=1)
    global_role: str = Field(
        default="",
        description="federation-steward, or '' to clear it",
    )


class ToolCallRequest(BaseModel):
    """An MCP tool call. The principal is what the gate judges.

    ``principal`` is a CLAIM, not an identity. Naming one declares "treat me
    as an agent" and helm honours that downward demotion — it never honours
    it as a NAME. The principal helm judges, and the only one that reaches
    the chain, is derived: the signet subject of the session for a human,
    nemoclerk's own principal for everything else.
    """

    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    principal: str = Field(
        default="",
        description=(
            "Declare the caller an agent principal. The string is NOT used as "
            "the approver; helm derives that from the session or from its own "
            "runtime."
        ),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()

    federation = Federation(settings.sibling_urls, settings.sibling_timeout)
    signet = Signet(
        settings.data_dir,
        settings.session_secret,
        auth_disabled=settings.auth_disabled,
    )
    oidc = OidcClient(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        scopes=settings.oidc_scope_tuple,
        timeout=settings.oidc_timeout,
        offline=settings.offline,
        ca_bundle=settings.oidc_ca_bundle,
    )
    console = Console(federation, signet)
    # warrant's tools are added by subclass: additive, nothing removed.
    registry = WarrantToolRegistry(federation, console)
    sessions = SessionStore()
    clerk = NemoClerk(registry, settings, sessions)
    views = ViewBuilder(console, signet, clerk, settings)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["tabs_list"] = TABS

    app = FastAPI(
        title="helm",
        version=__version__,
        description=(
            "Everything an agent operator can drive, except approving its own "
            "irreversible effect."
        ),
    )
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    app.state.settings = settings
    app.state.console = console
    app.state.signet = signet
    app.state.oidc = oidc
    app.state.clerk = clerk
    app.state.registry = registry
    app.state.federation = federation

    @app.on_event("startup")
    def _startup() -> None:
        # Probe the model ladder off the request path: no page load waits on
        # a GPU that may be busy or absent.
        clerk.ladder.start_probe()

    # ------------------------------------------------------------- ledger
    #: Every refusal path in helm, and how each one reaches the chain.
    #: Enumerated rather than assumed, because "every refusal is on the
    #: chain" was true of the showcase refusal and false of the ones a real
    #: unauthorised operator triggers — exactly backwards.
    REFUSAL_PATHS = (
        "agent-on-irreversible (forwarded to throughline, ledgered there)",
        "impersonation (attested_by != session subject)",
        "anonymous (no session)",
        "role (viewer may not decide)",
        "admin-only (non-admin reached an admin surface)",
        "substrate-unreachable (decision not attempted)",
    )

    def ledger_note(kind: str, body: dict[str, Any]) -> int | None:
        """Append an auditable helm record to throughline's chain.

        helm keeps no ledger of its own; anything that must be on the record
        is ingested as a signal so it lands in the one chain.
        """
        payload = dict(body)
        payload.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        reply = federation.throughline.signal(
            {
                "id": f"helm-{kind}-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "class": f"helm.{kind}",
                "source": "helm/signet",
                "ingest_time": payload["at"],
                "real_or_synthetic": "real",
                "payload_ref": json.dumps(payload, sort_keys=True)[:600],
            }
        )
        if not reply.ok:
            log.warning("could not ledger %s: %s", kind, reply.error)
            return None
        data = reply.data if isinstance(reply.data, dict) else {}
        seq = data.get("seq")
        if seq is None:
            # throughline echoes the Signal, not the ledger row, so read the
            # seq back from the chain rather than reporting "not ledgered"
            # for a record that WAS written.
            tail = console.ledger(limit=1).get("rows") or []
            seq = tail[0]["seq"] if tail else -1
        return seq

    def ledger_refusal(outcome: dict[str, Any], *, action: str, target: str) -> None:
        """A refusal that is not on the chain did not happen, evidentially."""
        if outcome.get("ledgered"):
            return  # throughline already recorded it (the agent path)
        seq = ledger_note(
            "refusal",
            {
                "action": action,
                "target": target,
                "kind": outcome.get("refusal_kind", "refused"),
                "principal": outcome.get("principal", "anonymous"),
                "claimed": outcome.get("claimed", ""),
                "actual": outcome.get("actual", ""),
                "reason": outcome.get("reason", "")[:300],
            },
        )
        if seq is not None:
            outcome["ledgered"] = True
            outcome["ledger_seq"] = seq

    # ------------------------------------------------------------ identity
    def current_identity(request: Request) -> Identity:
        return signet.identity_from_token(request.cookies.get(SESSION_COOKIE))

    def render(name: str, request: Request, ctx: dict[str, Any]) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name=name,
            context={"ctx": ctx, "boot": views.boot(ctx)},
        )

    def require_login(request: Request) -> Identity | RedirectResponse:
        identity = current_identity(request)
        if not identity.authenticated:
            return RedirectResponse("/login", status_code=303)
        return identity

    def forbidden(
        request: Request, identity: Identity, what: str, why: str, required: str
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="forbidden.html",
            context={
                "what": what,
                "why": why,
                "required": required,
                "subject": identity.subject,
                "role": identity.role,
                "boot": {
                    "page": "forbidden",
                    "subject": identity.subject,
                    "role": identity.role,
                    "authenticated": identity.authenticated,
                    "prefs": signet.prefs_for(identity.subject),
                },
                "ctx": {"prefs": signet.prefs_for(identity.subject)},
            },
            status_code=403,
        )

    # ================================================================ pages
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index(request: Request) -> Response:
        identity = current_identity(request)
        if not identity.authenticated:
            # A first-time anonymous visitor used to get a bare 303 to
            # /login with no body — six reviewers across two rounds could
            # not evaluate the product at all because of it. The landing
            # page now offers both real paths in: a genuine read-only guest
            # console (no account, no credential) or signing in to operate
            # it. Neither implies the visitor is an identified human.
            return templates.TemplateResponse(
                request=request,
                name="landing.html",
                context={
                    "boot": {"page": "landing", "subject": "", "role": "",
                             "authenticated": False, "prefs": signet.prefs_for("")},
                    "ctx": {"prefs": signet.prefs_for("")},
                },
            )
        cards = [
            {"path": "/console", "title": "helm — console overview", "coordinate": "feature-area://helm",
             "blurb": "Ledger tail, approval queue, signal-class status."},
            {"path": "/docket", "title": "docket — permit intake", "coordinate": "feature-area://docket",
             "blurb": "Quoted judgments and first-class abstentions."},
            {"path": "/breaker", "title": "breaker — microgrid telemetry", "coordinate": "feature-area://breaker",
             "blurb": "A dispatch held at the gate, with no timeout."},
            {"path": "/siren", "title": "siren — 911 incidents", "coordinate": "feature-area://siren",
             "blurb": "Hot-reload, and the config that was refused."},
            {"path": "/blindspot", "title": "blindspot — warehouse video", "coordinate": "feature-area://blindspot",
             "blurb": "Designed, not built: the tab renders its wireframe."},
            {"path": "/composed", "title": "composed — live gate-hold view", "coordinate": "scenario://helm/composed-state",
             "blurb": "Assembles itself when an irreversible effect is held."},
            {"path": "/approval-detail", "title": "approval record", "coordinate": "scenario://signet/subject-in-record",
             "blurb": "WHO, as verifiably as WHY — and the refused variant."},
            {"path": "/admin", "title": "admin — site configuration", "coordinate": "use-case://signet/bind-identity",
             "blurb": "RBAC, signal classes, the inert gate-disable control."},
            {"path": "/docs", "title": "/docs — the whole API", "coordinate": "scenario://helm/shell-tabs",
             "blurb": "Every console action, documented. No privileged path."},
        ]
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "cards": cards,
                "port": settings.port,
                "boot": {"page": "index", "subject": identity.subject,
                         "role": identity.role, "authenticated": True,
                         "prefs": signet.prefs_for(identity.subject)},
                "ctx": {"prefs": signet.prefs_for(identity.subject)},
            },
        )

    def _auth_providers(error: str = "") -> dict[str, Any]:
        """What the login page may honestly offer, and why.

        Only the ``configured`` question is answered here. Whether the
        provider is REACHABLE is not probed on page load: the demo runs
        offline, and a login page that blocks for a network timeout before
        it paints is its own outage. The unreachable case surfaces where it
        actually happens — on the button press — and lands back here with
        the reason in words.
        """
        mock_reason = ""
        if not oidc.configured:
            mock_reason = oidc.unconfigured_reason
        elif settings.offline:
            mock_reason = (
                f"HELM_OFFLINE=1 — helm will not call {oidc.host} in offline "
                "mode, so signet cannot bind a real subject on this run."
            )
        return {
            "oidc": oidc.configured and not settings.offline,
            "oidc_issuer": settings.oidc_issuer,
            "oidc_host": oidc.host,
            "github": bool(settings.github_client_id),
            # Why signet/OIDC is not the answer on this run. There is no
            # mock fallback any more: when this is set and neither OIDC nor
            # GitHub is configured, the honest answer is "no sign-in
            # available", not a fabricated identity.
            "mock_reason": mock_reason,
            "env": settings.env,
            "error": error,
        }

    def _hero_rungs() -> dict[str, Any]:
        """What the login hero may say about local NVIDIA silicon, and in
        what tense — read from the SAME observation ``/healthz`` and
        ``/nemoclerk/runtime`` read.

        Two fixes landed in the same session and reintroduced a divergence
        this codebase had already eliminated once: one made ``/healthz``
        and ``/nemoclerk/runtime`` derive "what served" from
        ``clerk.served()`` — the one observation of the turn that actually
        answered — while the other separately rewrote this paragraph to
        read ``clerk.ladder.rungs()`` instead, which answers a different
        question: what is REACHABLE. Reachable is not the same as served:
        with no turn served yet, the ladder can still show the GB10 rung
        "active" (it is up and would answer the next turn), so the hero
        asserted, present tense, that GB10 "is serving interactive turns"
        while ``/healthz`` reported ``served_source: "none"`` — a claim the
        product's own endpoint refuted.

        There is now one source of truth for whether a turn has served
        (``served()["source"] != "none"``) and one tense rule that follows
        from it: CONFIGURED/REACHABLE is described only when nothing has
        served, and never in the present continuous a live serve would
        earn.
        """
        served = clerk.served()
        has_served = served["source"] != "none"
        by_name = {r["name"]: r for r in clerk.ladder.rungs()}
        return {
            "has_served": has_served,
            "served": served,
            "dgx": by_name.get("dgx-spark"),
            "rtx": by_name.get("rtx-5090"),
        }

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request, error: str = "") -> HTMLResponse:
        stages = console.stage_states(None)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "stages": stages,
                "providers": _auth_providers(error),
                "rungs": _hero_rungs(),
                "boot": {"page": "login", "subject": "", "role": "", "authenticated": False,
                         "prefs": signet.prefs_for("")},
                "ctx": {"prefs": signet.prefs_for("")},
            },
        )

    @app.get("/guest", response_class=HTMLResponse, include_in_schema=False)
    def guest_page(request: Request) -> Response:
        """A genuine read-only console. No account, no credential, no login.

        Deliberately does NOT go through ``require_login``/``render`` — it
        renders the SAME data the public JSON API already serves
        (``/overview``, ``/ledger``, ``/approvals``, ``/feeds``), addresses
        withheld exactly as ``/healthz`` withholds them from an anonymous
        caller, and it carries no form, href or button that could reach
        the approval path. A guest who reaches this page is never treated
        as, or represented as, an identified human — this is not a mock
        login and it never signs anyone in.
        """
        ctx = views.guest()
        ctx["prefs"] = signet.prefs_for("")
        return templates.TemplateResponse(
            request=request,
            name="guest.html",
            context={
                "ctx": ctx,
                "boot": {"page": "guest", "subject": "", "role": "",
                         "authenticated": False, "prefs": ctx["prefs"]},
            },
        )

    @app.get("/help", response_class=HTMLResponse, include_in_schema=False)
    def help_page(request: Request) -> Response:
        ctx = views.help()
        ctx["prefs"] = signet.prefs_for("")
        return templates.TemplateResponse(
            request=request,
            name="help.html",
            context={
                "ctx": ctx,
                "boot": {"page": "help", "subject": "", "role": "",
                         "authenticated": False, "prefs": ctx["prefs"]},
            },
        )

    @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
    def console_page(
        request: Request, q: str = "", page: int = 1, per: str = ""
    ) -> Response:
        # ``q``/``page``/``per`` drive the approval queue's search and pager.
        # They are query parameters rather than client-side state so that a
        # particular view of the queue — "the irreversible ones", "page 12"
        # — is a URL an operator can bookmark, share, or be sent to.
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render(
            "helm.html",
            request,
            views.helm(identity, query=q, page=page, per=per or QUEUE_PAGE_SIZE),
        )

    @app.get("/docket", response_class=HTMLResponse, include_in_schema=False)
    def docket_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("docket.html", request, views.docket(identity))

    @app.get("/breaker", response_class=HTMLResponse, include_in_schema=False)
    def breaker_page(request: Request, effect: str = "") -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("breaker.html", request, views.breaker(identity, effect))

    @app.get("/siren", response_class=HTMLResponse, include_in_schema=False)
    def siren_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("siren.html", request, views.siren(identity))

    @app.get("/blindspot", response_class=HTMLResponse, include_in_schema=False)
    def blindspot_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("blindspot.html", request, views.blindspot(identity))

    # The datasets page and its JSON surface live entirely in
    # helm/datasets_routes.py and helm/templates/datasets.html — one include,
    # no closures borrowed, so it can move or be removed as a unit.
    app.include_router(datasets_router)

    @app.get("/composed", response_class=HTMLResponse, include_in_schema=False)
    def composed_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("composed.html", request, views.composed(identity))

    @app.get("/approval-detail", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/approval-detail/{approval_id}", response_class=HTMLResponse, include_in_schema=False)
    def approval_detail_page(request: Request, approval_id: str = "") -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("approval_detail.html", request, views.approval_detail(identity, approval_id))

    @app.get("/walk/{effect_id}", response_class=HTMLResponse, include_in_schema=False)
    def walk_page(request: Request, effect_id: str) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("walk.html", request, views.walk(identity, effect_id))

    @app.get("/profile", response_class=HTMLResponse, include_in_schema=False)
    def profile_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        return render("profile.html", request, views.profile(identity))

    @app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
    def admin_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        if not identity.is_admin:
            return forbidden(
                request,
                identity,
                "Site configuration is admin-only.",
                "Your signet identity is bound to a role that can operate the console "
                "but not reconfigure it. Roles, signal classes, gate policy and "
                "providers are changed by an admin, and every change is ledgered.",
                "role: admin",
            )
        return render("admin.html", request, views.admin(identity))

    @app.post("/admin/roles/form", include_in_schema=False)
    def admin_role_form(
        request: Request, subject: str = Form(...), role: str = Form(...)
    ) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        if not identity.is_admin:
            return forbidden(
                request, identity, "Role edits are admin-only.",
                "Changing who may approve is itself a governed act.", "role: admin"
            )
        change = _apply_role_change(subject, role, identity)
        return render("admin.html", request, views.admin(identity, change))

    # ================================================================== api
    @app.get("/healthz", tags=["health"], summary="Liveness, identity mode and sibling reachability")
    def healthz(request: Request) -> dict[str, Any]:
        # Sibling reachability is by NAME and STATE for anyone. The address
        # each sibling actually listens on is infrastructure detail, not a
        # health signal, and is withheld from an anonymous caller — see
        # ``Console.sibling_reachability``. An admin session may still ask.
        identity = current_identity(request)
        return {
            "component": "helm",
            "version": __version__,
            "port": settings.port,
            "env": settings.env,
            "auth": {
                # The mode names the strongest thing this deployment can
                # actually do, not the weakest thing it is allowed to. There
                # is no "mock" mode: a deployment with no real provider
                # configured honestly reports "none".
                "mode": (
                    "oidc" if settings.oidc_configured and not settings.offline
                    else "github-oauth" if settings.github_client_id
                    else "none"
                ),
                "oidc_configured": settings.oidc_configured,
                "oidc_issuer": settings.oidc_issuer,
                "oidc_host": issuer_host(settings.oidc_issuer),
                "auth_disabled": settings.auth_disabled,
                "github_configured": bool(settings.github_client_id),
            },
            "nemoclerk": clerk.pane_header(),
            "siblings": console.sibling_reachability(reveal_addresses=identity.is_admin),
        }

    @app.get("/overview", tags=["console"], summary="Federation overview: feed health, queue depth, ledger status")
    def get_overview(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        return console.overview(reveal_addresses=identity.is_admin)

    @app.get("/feeds", tags=["console"], summary="The signal feeds and which of them are live")
    def get_feeds(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        return {"feeds": console.feeds(reveal_addresses=identity.is_admin)}

    @app.get("/events", tags=["console"], summary="Event rows for one feed, read from the substrate ledger")
    def get_events(feed: str = "all", limit: int = 12) -> dict[str, Any]:
        return {"feed": feed, "events": console.events_for(feed, limit)}

    def _label_attestation(record: dict[str, Any]) -> dict[str, Any]:
        """Say IN WORDS whether an approval's approver was ever verified.

        The substrate stores ``issuer: null`` for a decision nobody vouched
        for, and null is the most dangerous value a record can have: read
        by a template it renders blank, and a blank field beside a verified
        one reads as agreement. Every approval this console serves therefore
        carries an explicit ``signet`` block, and its ``label`` is never
        empty. Records written before the issuer existed land here too, and
        are described as unverified rather than as unknown-and-probably-fine.
        """
        if not isinstance(record, dict):
            return record
        issuer = record.get("issuer") or ""
        decided_by = record.get("decided_by") or ""
        mode = record.get("auth_mode") or ("oidc" if issuer else "unattested")
        if not decided_by:
            label = "not decided — no approver yet"
        elif issuer:
            label = f"verified by {issuer_host(issuer)}"
        else:
            label = (
                "UNVERIFIED — no issuer vouched for this subject. The name "
                "is what the console recorded, not something a third party "
                "attested to."
            )
        record["signet"] = {
            "subject": decided_by,
            "issuer": issuer,
            "auth_mode": mode,
            "verified": bool(issuer) and mode == "oidc",
            "label": label,
        }
        return record

    @app.get("/approvals", tags=["approvals"], summary="List the approval queue (proxied from throughline)")
    def list_approvals(state: str | None = None) -> dict[str, Any]:
        """The queue, or an explicit admission that it cannot be read.

        This returned ``{"approvals": [], "pending": 0}`` when throughline
        was down, so an API consumer could not tell an empty queue from a
        blind one. Absence of evidence now says so.
        """
        reply = federation.throughline.approvals(state)
        if not reply.ok:
            last, at = console.last_seen.get("gate.pending")
            return {
                "known": False,
                "approvals": [],
                "pending": None,
                "detail": reply.error or "throughline is not responding",
                "last_seen": {"pending": last, "at": at},
            }
        approvals = [_label_attestation(a) for a in reply.data.get("approvals", [])]
        return {
            "known": True,
            "approvals": approvals,
            "pending": len([a for a in approvals if a.get("state") == "pending"]),
        }

    @app.get("/approvals/{approval_id}", tags=["approvals"], summary="One approval record")
    def get_approval(approval_id: str) -> dict[str, Any]:
        record = console.approval(approval_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no approval {approval_id!r}")
        return _label_attestation(record)

    @app.post(
        "/approvals/{approval_id}/decide",
        tags=["approvals"],
        summary="Decide a pending approval — role-gated here, schema-enforced by throughline",
        responses={
            403: {"description": "Refused: the caller's ROLE may not decide this effect"},
            404: {"description": "No such approval"},
            422: {"description": "Schema: decided_by (the signet subject) is required"},
        },
    )
    def decide_approval(
        approval_id: str, body: DecideRequest, request: Request
    ) -> JSONResponse:
        identity = current_identity(request)

        # The approver is the SESSION SUBJECT. body.decided_by is an
        # attestation the caller makes about itself; it is checked and then
        # discarded. It never reaches the ledger.
        role = "viewer"
        if body.caller_role == "agent" or identity.is_agent:
            role = "agent"
        elif identity.authenticated:
            role = identity.role

        # Bind WHO and WHICH AUTHORITY for the duration of this decision.
        # The substrate call picks it up at the last boundary, so the
        # approval record and its ledger entry carry the verified subject
        # AND the issuer that vouched for them — the difference between
        # asserting that a human approved and being able to show it.
        #
        # The approver itself comes from the SESSION, never from the body:
        # an issuer proves who a subject is, and this proves that the
        # subject in the record is the one that actually signed in.
        with attesting(identity):
            outcome = console.decide(
                approval_id=approval_id,
                decision=body.decision,
                approver=identity.subject,
                role=role,
                principal=(
                    (body.decided_by or "agent:unidentified")
                    if role == "agent"
                    else (identity.subject or "anonymous")
                ),
                rationale=body.rationale,
                attested_by=body.decided_by,
                authenticated=identity.authenticated,
            )
        outcome.setdefault("attestation", {
            "subject": identity.subject,
            "issuer": identity.issuer,
            "auth_mode": identity.auth_mode,
        })
        status = outcome.get("status", 200)
        if status == 200:
            return JSONResponse(outcome, status_code=200)
        if outcome.get("refused"):
            ledger_refusal(outcome, action="approvals.decide", target=approval_id)
        return JSONResponse({"detail": outcome}, status_code=status)

    @app.get("/effects", tags=["console"],
             summary="Every effect the substrate has typed, newest first")
    def list_effects(limit: int = 50) -> dict[str, Any]:
        """A list-effects view: the contract promised it and 405 answered."""
        return console.effects(limit=limit)

    @app.get("/ledger", tags=["ledger"], summary="Tail of the provenance ledger with verify badges")
    def get_ledger(request: Request, limit: int = 12) -> dict[str, Any]:
        # Decider identity is withheld from an anonymous caller by
        # throughline itself once helm asks as one — the same withholding
        # this whole session gave sibling addresses. An admin session helm
        # has verified may still ask with helm's own caller token and see
        # the real name.
        identity = current_identity(request)
        return console.ledger(limit=limit, reveal_identity=identity.is_admin)

    @app.get("/ledger/verify", tags=["ledger"], summary="Recompute and verify the whole hash chain")
    def get_ledger_verify() -> dict[str, Any]:
        return console.verify()

    @app.get("/walk/{effect_id}/json", tags=["ledger"], summary="Cause walk: effect → judgment → signal")
    def get_walk(effect_id: str) -> dict[str, Any]:
        return console.walk(effect_id)

    @app.get("/composed/state", tags=["console"],
             summary="The composed state — assembles on a gate-hold, not on a schedule")
    def get_composed() -> dict[str, Any]:
        return console.composed()

    @app.get("/auth/session", tags=["signet"], summary="The current signet identity")
    def auth_session(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        return {
            "identity": identity.to_dict(),
            "mock": identity.mock,
            "auth_disabled_reason": views._auth_disabled_reason(),
            # The badge the console shows, served from the same source the
            # console reads it from — so a screenshot and the API can never
            # disagree about whether this identity was really authenticated.
            "auth": {
                "mode": identity.auth_mode,
                "badge": identity.auth_badge,
                "issuer": identity.issuer,
                "verified": identity.auth_mode == "oidc",
            },
        }

    # ------------------------------------------------------------ real oidc
    @app.get(
        "/auth/oidc",
        tags=["signet"],
        summary="Begin the real OIDC round trip (authorization code + PKCE S256)",
        responses={
            303: {"description": "Redirect to the provider, or back to /login with a reason"}
        },
    )
    def auth_oidc() -> RedirectResponse:
        """Send the browser to the issuer, remembering state, nonce and verifier.

        A provider we cannot reach is not an error page: it is a redirect
        back to /login carrying the reason, where the LABELLED mock button
        is waiting. That is the offline demo's path, and it is a different
        outcome from a provider that answered and failed verification.
        """
        try:
            url, _pending = oidc.begin()
        except ProviderUnavailable as exc:
            log.info("oidc unavailable, offering the labelled fallback: %s", exc)
            return RedirectResponse(f"/login?error={quote(str(exc))}", status_code=303)
        except TokenInvalid as exc:
            return RedirectResponse(f"/login?error={quote(str(exc))}", status_code=303)
        return RedirectResponse(url, status_code=303)

    @app.get(
        "/auth/callback",
        tags=["signet"],
        summary="OIDC callback: verify the ID token and bind the signet subject",
        responses={
            303: {"description": "Verified — session issued, on to the console"},
            400: {"description": "REFUSED: state, nonce, signature, issuer, audience or expiry"},
            503: {"description": "The provider became unreachable mid-flow"},
        },
    )
    def auth_callback(
        code: str = "", state: str = "", error: str = "", error_description: str = ""
    ) -> Response:
        """The whole point: nobody gets a session without a verified token.

        Every refusal here is explicit and none of them degrade into the
        mock identity. A caller who fails verification is told why and sent
        back to /login as NOBODY — the one thing that must never happen is
        a mock subject wearing a real issuer's badge.
        """
        if error:
            detail = f"{oidc.host} refused the authorization request: {error}"
            if error_description:
                detail += f" — {error_description}"
            return RedirectResponse(f"/login?error={quote(detail)}", status_code=303)
        try:
            claims = oidc.complete(code=code, state=state)
        except TokenInvalid as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None

        session, identity = signet.issue_for_claims(claims)
        log.info(
            "signet bound %s via %s (role=%s)",
            identity.subject,
            identity.issuer,
            identity.role,
        )
        response = RedirectResponse("/console", status_code=303)
        response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax")
        return response

    @app.get("/auth/github", tags=["signet"], summary="Begin the GitHub OAuth round trip",
             include_in_schema=True)
    def auth_github() -> RedirectResponse:
        if not settings.github_client_id:
            return RedirectResponse(
                "/login?error="
                + quote("GitHub is not configured on this deployment."),
                status_code=303,
            )
        state = signet.new_oauth_state()
        url = (
            f"{GITHUB_AUTHORIZE}?client_id={settings.github_client_id}"
            f"&redirect_uri={settings.public_url}/auth/github/callback"
            f"&scope=read:user%20user:email&state={state}"
        )
        return RedirectResponse(url, status_code=303)

    @app.get("/auth/github/callback", tags=["signet"], summary="GitHub OAuth callback: bind the subject")
    def auth_github_callback(code: str = "", state: str = "") -> Response:
        if not signet.consume_oauth_state(state):
            raise HTTPException(status_code=400, detail="OAuth state did not match")
        try:
            token_reply = httpx.post(
                GITHUB_TOKEN,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": f"{settings.public_url}/auth/github/callback",
                },
                timeout=10.0,
            )
            token = token_reply.json().get("access_token", "")
            user = httpx.get(
                GITHUB_USER,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                timeout=10.0,
            ).json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=f"GitHub OAuth failed: {exc}") from exc
        subject = user.get("email") or f"github:{user.get('login', 'unknown')}"
        session = signet.issue(subject, "github", user.get("name") or user.get("login", ""))
        response = RedirectResponse("/console", status_code=303)
        response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite="lax")
        return response

    @app.get("/logout", tags=["signet"], summary="Sign out: clear the session and its NemoClerk transcripts")
    def logout(request: Request) -> Response:
        identity = current_identity(request)
        if identity.subject:
            sessions.clear_subject(identity.subject)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    @app.get("/prefs", tags=["signet"], summary="Read this subject's stored preferences")
    def get_prefs(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        return signet.prefs_for(identity.subject)

    @app.put("/prefs", tags=["signet"], summary="Persist theme and both rails' collapse state and widths")
    def put_prefs(body: PrefsRequest, request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.authenticated:
            raise HTTPException(status_code=401, detail="sign in to store preferences")
        return signet.set_prefs(identity.subject, body.model_dump(exclude_none=True))

    # ------------------------------------------------------------ nemoclerk
    @app.get("/nemoclerk/session", tags=["nemoclerk"],
             summary="The transcript for (subject, feature-area) — one session per page")
    def nemoclerk_session(request: Request, feature_area: str = "helm") -> dict[str, Any]:
        identity = current_identity(request)
        session = sessions.get(identity.subject, feature_area)
        return session.to_dict()

    @app.post("/nemoclerk/message", tags=["nemoclerk"],
              summary="Ask NemoClerk; the answer carries its tool chips or makes no claim")
    def nemoclerk_message(body: MessageRequest, request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        from helm.pages import PAGES

        page = PAGES.get(body.feature_area) or PAGES["helm"]
        answer = clerk.ask(
            body.message,
            subject=identity.subject or "anonymous",
            feature_area=body.feature_area,
            priming=page.about.priming(body.feature_area),
            selected_event=body.selected_event,
        )
        # The hardware claim is auditable from the chain rather than merely
        # printed: every turn records WHAT served it, on WHICH silicon.
        #
        # Every field below is an OBSERVATION of this turn, taken from
        # `_serving`. `silicon` used to come from pane_header(), which
        # describes the LADDER — the rung that happens to be up — so a turn
        # that consulted no model still notarised "NVIDIA GB10 Grace
        # Blackwell" beside a none@none principal. Because the chain then
        # proves that entry valid forever, it turned our best TRUE claim into
        # a permanently notarised false one.
        ledger_note(
            "nemoclerk.turn",
            {
                "principal": answer.principal,
                "model": answer.model,
                "tier": answer.tier,
                "silicon": answer.silicon,
                "source": answer.source,
                "grounded": answer.grounded,
                "refused": answer.refused,
                "tools": [c["name"] for c in answer.chips],
                "subject": identity.subject or "anonymous",
                # The page helm ACTUALLY primed the turn with. `body.feature_area`
                # is the caller's string and falls back to "helm" when it names
                # no page, so recording it would put an unresolvable label on
                # the chain for a turn that was primed as something else.
                "feature_area": page.feature_area,
            },
        )
        return answer.to_dict()

    @app.get("/nemoclerk/runtime", tags=["nemoclerk"],
             summary="The active runtime and model tier named in the rail header")
    def nemoclerk_runtime() -> dict[str, Any]:
        return clerk.pane_header()

    # ------------------------------------------------------------------ mcp
    @app.get("/mcp/tools", tags=["mcp"], summary="The MCP tool surface helm hosts")
    def mcp_tools() -> dict[str, Any]:
        return {"server": "helm", "tools": registry.schemas()}

    @app.post("/mcp/call", tags=["mcp"],
              summary="Call an MCP tool. approve_effect on an irreversible effect is REFUSED for agent principals")
    def mcp_call(body: ToolCallRequest, request: Request) -> JSONResponse:
        identity = current_identity(request)
        # DERIVED, never read off the body. `body.principal` used to be the
        # first term of this expression, which let a cookieless caller pick
        # the name that throughline then hash-chained forever.
        claimed = (body.principal or "").strip()
        principal = clerk.principal()
        arguments = dict(body.arguments)
        if body.name == "approve_effect":
            # An MCP caller is an agent principal unless it presents a human
            # session AND names itself as that human. Transport does not
            # promote a role; the role is what it is. A claim can only demote:
            # naming a principal keeps the caller an agent, and the name it
            # picked is discarded either way.
            role = "agent"
            if identity.authenticated and not identity.is_agent and not claimed:
                role = identity.role
                principal = identity.subject
            # The approver is the principal helm DERIVED — the signet subject
            # of the session for a human, nemoclerk's own principal for an
            # agent — never a name the caller supplied. This comment used to
            # say exactly that while the line below forwarded `body.principal`.
            arguments.update(
                {
                    "principal": principal,
                    "role": role,
                    "approver": identity.subject if role != "agent" else principal,
                    # Authenticity is a property of the SESSION. An agent role
                    # is the least authenticated caller there is; it must not
                    # be the thing that grants the flag.
                    "authenticated": identity.authenticated,
                }
            )
        with attesting(identity):
            result = registry.call(body.name, arguments)
        payload = result.to_dict()
        payload["principal"] = principal
        if result.refused:
            outcome = result.data if isinstance(result.data, dict) else {}
            ledger_refusal(
                dict(outcome, principal=principal),
                action=f"mcp.{body.name}",
                target=str(arguments.get("id", "")),
            )
            return JSONResponse(payload, status_code=403)
        return JSONResponse(payload, status_code=200 if result.ok else 502)

    # ---------------------------------------------------------------- admin
    def _apply_role_change(subject: str, role: str, identity: Identity) -> dict[str, Any]:
        try:
            change = signet.set_role(subject, role, identity.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no subject {subject!r}") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        ledger_seq = None
        reply = federation.throughline.signal(
            {
                "id": f"helm-role-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "class": "helm.admin.role_changed",
                "source": "helm/signet",
                "ingest_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "real_or_synthetic": "real",
                "payload_ref": (
                    f"subject={change.subject} {change.old_role}->{change.new_role} "
                    f"by={change.changed_by}"
                ),
            }
        )
        if reply.ok and isinstance(reply.data, dict):
            ledger_seq = reply.data.get("seq") or reply.data.get("ledger_seq")
            if ledger_seq is None:
                tail = console.ledger(limit=1).get("rows", [])
                ledger_seq = tail[0]["seq"] if tail else None
        return {
            "subject": change.subject,
            "old_role": change.old_role,
            "new_role": change.new_role,
            "changed_by": change.changed_by,
            "at": change.at,
            "ledger_seq": ledger_seq,
            "ledgered": bool(reply.ok),
        }

    # ------------------------------------------------- federation stewards
    def _steward_context(
        identity: Identity, change: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """The roster page's context, built on the admin page's own.

        Reusing ``views.admin`` keeps the shell — tabs, rails, badge — identical
        to every other page without teaching the view builder a new page.
        """
        holders = signet.global_role_holders("federation-steward")
        ctx = views.admin(identity)
        ctx.update(
            {
                "global_role": "federation-steward",
                "holders": holders,
                "candidates": [
                    row
                    for row in signet.role_table()
                    if not row.get("global_role")
                    and not row["subject"].startswith("agent:")
                ],
                # Two votes, from two distinct datasets. Stewards alone can
                # only supply that if there are at least two of them.
                "quorum_possible": len(holders) >= 2,
                "change": change,
            }
        )
        return ctx

    def _apply_global_role_change(
        subject: str, global_role: str, identity: Identity
    ) -> dict[str, Any]:
        """Assign or clear a federation role, and put the act in the chain.

        Gated on the console's ``admin`` — which is federation-level here,
        not per-dataset — and reachable from no dataset-scoped path at all.
        warrant's dataset admins have no route to this endpoint, which is
        the point: a dataset admin who could hand out the franchise could
        manufacture the very quorum that exists to check them.

        Ledgered under its own signal class, because widening who may vote
        in a break-glass is itself the kind of act break-glass is audited
        for.
        """
        try:
            change = signet.set_global_role(subject, global_role, identity.subject)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"no subject {subject!r}") from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        reply = federation.throughline.signal(
            {
                "id": f"helm-globalrole-{int(time.time() * 1000)}-{secrets.token_hex(3)}",
                "class": "helm.admin.global_role_changed",
                "source": "helm/signet",
                "ingest_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "real_or_synthetic": "real",
                "payload_ref": (
                    f"subject={change.subject} global_role="
                    f"{change.old_global_role or 'none'}->"
                    f"{change.new_global_role or 'none'} by={change.changed_by} "
                    f"auth_mode={identity.auth_mode} issuer={identity.issuer or 'none'}"
                ),
            }
        )
        ledger_seq = None
        if reply.ok and isinstance(reply.data, dict):
            ledger_seq = reply.data.get("seq") or reply.data.get("ledger_seq")
            if ledger_seq is None:
                tail = console.ledger(limit=1).get("rows", [])
                ledger_seq = tail[0]["seq"] if tail else None
        payload = change.to_dict()
        payload.update(
            {
                "ledger_seq": ledger_seq,
                "ledgered": bool(reply.ok),
                # Who widened the franchise, and whether anybody vouched for
                # them. A grant made by an unverified identity must never
                # read like one made by a verified identity.
                "changed_by_issuer": identity.issuer,
                "changed_by_auth_mode": identity.auth_mode,
                "changed_by_verified": identity.auth_mode == "oidc",
            }
        )
        return payload

    @app.get(
        "/admin/global-roles",
        tags=["admin"],
        summary="Federation-level roles and the NAMED people who hold them",
    )
    def admin_global_roles(request: Request) -> dict[str, Any]:
        """warrant reads this. It never writes it.

        ``holders`` may legitimately be empty, and empty IS the answer — not
        a reason to invent one. With fewer than two eligible voters a
        break-glass request refuses with ``WARRANT-BREAKGLASS-NO-QUORUM``,
        which is the correct outcome and must stay reachable.
        """
        identity = current_identity(request)
        if not identity.authenticated:
            raise HTTPException(status_code=401, detail="sign in first")
        holders = signet.global_role_holders("federation-steward")
        return {
            "global_roles": list(GLOBAL_ROLES),
            "federation-steward": {
                "grants": (
                    "the right to VOTE in another dataset's break-glass "
                    "quorum. No dataset authority of any kind."
                ),
                "holders": holders,
                "count": len(holders),
                "assignable_by": "helm admin only; never by a dataset admin",
                "no_holder_is_seeded": True,
                "note": (
                    "With nobody holding this, a break-glass on a small or "
                    "single-dataset federation refuses with "
                    "WARRANT-BREAKGLASS-NO-QUORUM rather than quietly "
                    "lowering the bar. That is the intended failure."
                ),
            },
        }

    @app.post(
        "/admin/global-roles",
        tags=["admin"],
        summary="Assign or clear a federation role — admin-only, and ledgered",
        responses={
            403: {"description": "REFUSED: not a dataset admin's to give"},
            404: {"description": "No such subject"},
            422: {"description": "Not a known federation role"},
        },
    )
    def admin_set_global_role(body: GlobalRoleRequest, request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": (
                        "federation roles are assigned at the federation "
                        "level. A dataset admin cannot grant the franchise "
                        "that votes on their own dataset's break-glass."
                    ),
                    "your_role": identity.role,
                    "required": "admin",
                },
            )
        return _apply_global_role_change(body.subject, body.global_role, identity)

    @app.get("/admin/stewards", response_class=HTMLResponse, include_in_schema=False)
    def stewards_page(request: Request) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        if not identity.is_admin:
            return forbidden(
                request,
                identity,
                "The federation steward roster is admin-only.",
                "Stewards may vote in another dataset's break-glass quorum. "
                "Who holds that franchise is a federation-level decision, so "
                "it is neither given out by nor rearranged by a dataset admin.",
                "role: admin",
            )
        return render("stewards.html", request, _steward_context(identity))

    @app.post("/admin/stewards/form", include_in_schema=False)
    def stewards_form(
        request: Request, subject: str = Form(...), global_role: str = Form("")
    ) -> Response:
        identity = require_login(request)
        if isinstance(identity, RedirectResponse):
            return identity
        if not identity.is_admin:
            return forbidden(
                request, identity,
                "Assigning a federation steward is admin-only.",
                "Widening who may vote in a break-glass is itself a governed act.",
                "role: admin",
            )
        change = _apply_global_role_change(subject, global_role, identity)
        return render("stewards.html", request, _steward_context(identity, change))

    @app.get("/admin/roles", tags=["admin"], summary="The RBAC role table")
    def admin_roles(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": "site configuration is admin-only",
                    "your_role": identity.role,
                    "required": "admin",
                },
            )
        return {"roles": signet.role_table(), "role_options": list(ROLES)}

    @app.post("/admin/roles", tags=["admin"], summary="Change a subject's role — the change is ledgered")
    def admin_set_role(body: RoleRequest, request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason": "role edits are admin-only",
                    "your_role": identity.role,
                    "required": "admin",
                },
            )
        return _apply_role_change(body.subject, body.role, identity)

    @app.get("/admin/classes", tags=["admin"], summary="Signal classes and their sources")
    def admin_classes(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(status_code=403, detail={"required": "admin"})
        return {"classes": views.admin(identity)["classes"]}

    @app.post("/admin/classes/{name}/reload", tags=["admin"],
              summary="Hot-reload a signal class without a redeploy")
    def admin_reload_class(name: str, request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(status_code=403, detail={"required": "admin"})
        outcome = console.reload_class(name)
        payload = outcome.to_dict()
        payload["ok"] = outcome.performed
        payload["chip"] = (
            f"tool: reload_signal_class {name} ✓"
            if outcome.performed
            else f"tool: reload_signal_class {name} ✗ did not run"
        )
        return payload

    @app.get("/admin/gate-policy", tags=["admin"],
             summary="Effect classes and their approval requirements. The gate cannot be disabled.")
    def admin_gate_policy(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(status_code=403, detail={"required": "admin"})
        return {
            "policy": views.admin(identity)["gate_policy"],
            "disable_gate": {
                "available": False,
                "reason": (
                    "cannot be disabled — refused at config load "
                    "(scenario://throughline/no-bypass). Not a permission an admin "
                    "lacks; a control the substrate does not possess."
                ),
            },
        }

    # -------------------------------------------------------------- warrant
    # Dataset-scoped ownership and delegation (feature-area://9). New routes,
    # new templates, new tools — mounted from one call so nothing here has to
    # be edited again.
    register_warrant(app, federation=federation, signet=signet, templates=templates)
    registry.warrant = app.state.warrant

    @app.get("/admin/providers", tags=["admin"], summary="Identity providers and the AUTH_DISABLED env gate")
    def admin_providers(request: Request) -> dict[str, Any]:
        identity = current_identity(request)
        if not identity.is_admin:
            raise HTTPException(status_code=403, detail={"required": "admin"})
        return {
            "providers": views.admin(identity)["providers"],
            "auth_flag": {
                "flag": "AUTH_DISABLED",
                "value": settings.auth_disabled,
                "env": settings.env,
                "honoured_in": ["dev", "test"],
                "note": (
                    "In staging or production, booting with AUTH_DISABLED set is "
                    "REFUSED and the flag is named in the refusal."
                ),
            },
        }

    @app.exception_handler(StarletteHTTPException)
    async def navigable_404(request: Request, exc: StarletteHTTPException) -> Response:
        """A stuck browser gets a way back in; a JSON API caller is unchanged.

        /help, /support, /contact, /about, /status-page and /faq used to
        404 with a bare ``{"detail":"Not Found"}`` and no way back into the
        app — a support reviewer filed it P0. A browser navigating here
        (``Accept: text/html``) now gets a page with links home, to the
        guest console, to help, and to /docs. Every other HTTPException —
        including the many deliberate JSON 404s this API raises for a
        missing approval, dataset or subject — is untouched: this only
        changes the RESPONSE FORMAT for a 404 raised on a route that does
        not exist at all, and only when the caller asked for HTML.
        """
        if exc.status_code == 404 and "text/html" in request.headers.get("accept", ""):
            return templates.TemplateResponse(
                request=request,
                name="not_found.html",
                context={
                    "path": request.url.path,
                    "boot": {"page": "not-found", "subject": "", "role": "",
                             "authenticated": False, "prefs": signet.prefs_for("")},
                    "ctx": {"prefs": signet.prefs_for("")},
                },
                status_code=404,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    return app


app = create_app
