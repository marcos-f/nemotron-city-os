"""The /datasets page and its JSON surface — what data the federation uses.

Self-contained on purpose. Everything this module needs it pulls off
``app.state`` rather than closing over ``create_app``'s locals, so the page can
be mounted with a single ``app.include_router(...)`` and lives entirely in
files nobody else is editing.

The page answers one operator question — *what data feeds are we running on,
and where did each one come from* — and it answers it with the same honesty the
rest of the console uses:

* a component that is not serving is shown offline, with its declared entries
  labelled as declared rather than read;
* a dataset that is designed but not built is listed as unavailable, never
  hidden and never counted as if it were working;
* a licence nobody has established shows as **unknown**, not as blank and not
  as a guess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from helm.datasets import DESIGNED_NOT_BUILT, FederatedDatasets
from helm.pages import PAGES, TABS, About, Page
from helm.signet import SESSION_COOKIE
from helm.views import ViewBuilder

HERE = Path(__file__).resolve().parent

#: This page's own copy contract. Built here rather than added to
#: ``helm.pages.PAGES`` so the module stays self-contained; ``PAGES`` is
#: consulted only for the shared shell defaults.
DATASETS_PAGE = Page(
    slug="datasets",
    title="NemoCity — datasets",
    h1="datasets — what the federation is standing on",
    coordinate="scenario://helm/dataset-registry",
    feature_area="helm",
    footer="scenario://helm/dataset-registry · every feed, its licence and its provenance",
    about=About(
        what=(
            "Every dataset the federation consumes, declared by the component "
            "that consumes it: source, licence, provenance, and whether it is "
            "live, a cached snapshot, a fixture, or designed but not built."
        ),
        calls="GET /datasets on each of throughline, docket, breaker, siren and blindspot.",
        doing=(
            "Reading each component's own registry. A component that is not "
            "serving is reported offline with whatever it declared, never as "
            "zero datasets."
        ),
        why=(
            "A demo that cannot say where its data came from is a demo that is "
            "asking to be believed. The registry refuses a config that omits a "
            "licence or a provenance, so an entry on this page has evidence "
            "behind it or it does not load at all."
        ),
        wow=(
            "A cached snapshot cannot be declared without its as-of time, and a "
            "fixture cannot be labelled real. The loader refuses the file."
        ),
    ),
    chips=(
        "Which datasets have an unknown licence?",
        "What is cached rather than live, and how stale is it?",
        "Which datasets are declared but not built?",
    ),
    open_feed="document",
    component="helm",
)

router = APIRouter()


def _templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    # _components.tabs() reads this bare global. Omit it and the nav silently
    # renders with only the "helm" link — no error, just a wrong page.
    templates.env.globals["tabs_list"] = TABS
    return templates


def _federated(request: Request) -> FederatedDatasets:
    state = request.app.state
    if not hasattr(state, "federated_datasets"):
        state.federated_datasets = FederatedDatasets(state.federation)
    return state.federated_datasets


def _context(request: Request) -> dict[str, Any] | RedirectResponse:
    state = request.app.state
    identity = state.signet.identity_from_token(request.cookies.get(SESSION_COOKIE))
    if not identity.authenticated:
        return RedirectResponse("/login?next=/datasets", status_code=303)

    views = ViewBuilder(state.console, state.signet, state.clerk, state.settings)
    # Borrow the shared shell context, then swap in this page's copy. PAGES is
    # left untouched.
    ctx = views.base("helm", identity)
    ctx["page"] = DATASETS_PAGE

    summary = _federated(request).summary()
    ctx["datasets"] = summary
    ctx["honesty_note"] = DESIGNED_NOT_BUILT
    return ctx


@router.get("/datasets", response_class=HTMLResponse, include_in_schema=False)
def datasets_page(request: Request) -> Response:
    ctx = _context(request)
    if isinstance(ctx, RedirectResponse):
        return ctx
    templates = _templates()
    return templates.TemplateResponse(
        request=request, name="datasets.html",
        context={"ctx": ctx, "boot": _boot(ctx)})


def _boot(ctx: dict[str, Any]) -> dict[str, Any]:
    """Minimal boot payload; mirrors ViewBuilder.boot's contract loosely.

    Kept local so this module does not depend on the shape of a helper that
    another change may be reworking.
    """
    page = ctx["page"]
    return {
        "slug": page.slug,
        "featureArea": page.feature_area,
        "coordinate": page.coordinate,
        "chips": list(page.chips),
        "placeholder": page.placeholder,
        "theme": (ctx.get("prefs") or {}).get("theme", "system"),
    }


@router.get("/datasets/json", tags=["datasets"])
def datasets_json(request: Request) -> Response:
    """The federated dataset registry: every component, every dataset.

    Discovery is a READ. There is no route here that changes a dataset's
    source or mode — that is a config edit in the owning component, accepted
    or refused by its loader.

    Three-valued, like every other read in this console. A component that
    could not be reached and has no declaration on file carries
    ``datasets: null``, never ``[]`` — an empty list is a claim, and we do not
    have one to make. When NO component could be read, the whole response is a
    **503** with ``total: null``: a caller must not be able to read "the
    federation is unreachable" as "the federation uses no data".
    """
    summary = _federated(request).summary()
    status = 503 if summary["status"] == "unknown" else 200
    return JSONResponse(status_code=status, content=summary)


def read_through_authority(request: Request, dataset_id: str) -> dict[str, Any]:
    """Who holds what on this dataset — READ THROUGH to warrant, never stored.

    The registry deliberately persists no owner. Ownership is a projection of
    throughline's chain, and a copy kept here would go stale the moment a
    grant, revoke, delegation or break-glass changed anything — and would keep
    answering confidently while wrong. That is the failure this registry
    refuses in other components' data, so it does not commit it in its own.

    warrant's three-valued contract is carried through unchanged: when the
    substrate is unreachable or the chain does not verify, ``known`` is False
    and ``people`` / ``grants`` are **null**. That is "we could not find out",
    which is not "nobody has access", and this function never softens one into
    the other.
    """
    warrant = getattr(request.app.state, "warrant", None)
    if warrant is None:
        # warrant is not mounted. Say so; do not imply the dataset is unowned.
        return {
            "dataset_id": dataset_id, "known": False,
            "reason": "warrant-not-mounted",
            "detail": "this console has no authority surface mounted",
            "people": None, "grants": None, "claimed": None,
            "message_ui": "Who may do what to this dataset is UNKNOWN — no "
                          "authority surface is mounted. This is not a "
                          "statement that nobody has access.",
        }
    # dataset_id is an opaque string to warrant, so the coupling is one call.
    return warrant.snapshot().authority(dataset_id).to_dict()


@router.get("/datasets/json/{dataset_id}", tags=["datasets"])
def dataset_json(dataset_id: str, request: Request) -> Response:
    """One dataset's source, licence, provenance and as-of time.

    Carries an ``authority`` block read through from warrant. When authority
    is unknown the response is **503** — the provenance half is still true and
    still returned, but a consumer must not read a missing owner as an absent
    one.
    """
    from fastapi import HTTPException

    lookup = _federated(request).locate(dataset_id)
    if not lookup.found:
        if not lookup.known:
            # A 404 ASSERTS the dataset does not exist. We did not establish
            # that: a component registry went unread and declared nothing, so
            # the honest answer is 503 "cannot tell" — the same distinction
            # this very response already makes about authority one field down.
            return JSONResponse(
                {**lookup.as_dict(), "reason": "dataset-registry-unread",
                 "honesty_note": DESIGNED_NOT_BUILT},
                status_code=503,
            )
        raise HTTPException(status_code=404,
                            detail=f"no dataset {dataset_id!r} is declared "
                                   "by any component")
    entry = lookup.entry
    if entry.get("availability") == "unavailable":
        entry = {**entry, "unavailable_reason": DESIGNED_NOT_BUILT}

    authority = read_through_authority(request, dataset_id)
    body = {**entry, "authority": authority,
            "authority_href": f"/datasets/{dataset_id}/people"}
    return JSONResponse(body, status_code=200 if authority.get("known") else 503)
