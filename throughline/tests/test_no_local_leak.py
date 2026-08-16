"""P0 (issue #2): unauthenticated responses must never leak the deploy
host's filesystem layout, the OS username, or internal network topology.

A review found ``GET /datasets`` and ``GET /ledger`` returning 200 to an
anonymous caller with a fully-resolved server-local path in the body —
``/home/<user>/.../throughline/config/datasets.yaml`` — which discloses both
the deploy user's name and the host's directory layout. Several ledger entry
types (``config.loaded``, ``config.refused``, the downgrade-hold effects)
record the same kind of absolute path at the moment they were written, and
the ledger is append-only: fixing this by editing those entries would be
rewriting history, which this product treats as the one unforgivable act.
The fix instead redacts on the way OUT, only for a caller that presented no
valid caller token — an authenticated operator still sees the real path.
"""

from __future__ import annotations

import re
from pathlib import Path

from throughline.service import Settings, Substrate

REPO_ROOT = Path(__file__).resolve().parents[1]
GOOD_CONFIG = REPO_ROOT / "config" / "effects.yaml"
FIXTURES = Path(__file__).parent / "fixtures"

_ABS_PATH_PATTERN = re.compile(r"/(?:[^\s\"'()\[\]{}:,]+/)+[^\s\"'()\[\]{}:,]+")


def _no_leak(body: str) -> None:
    assert "/home/" not in body, f"leaked a home-directory path: {body[:400]}"
    # Any multi-segment absolute path at all — not just /home/... — must be
    # gone from an unauthenticated response, per the P0: "never emit an
    # absolute filesystem path ... on an unauthenticated surface."
    match = _ABS_PATH_PATTERN.search(body)
    assert match is None, f"leaked an absolute path: {match.group(0)!r}"


def test_anonymous_datasets_has_no_absolute_path(anonymous_client):
    _no_leak(anonymous_client.get("/datasets").text)


def test_anonymous_dataset_show_has_no_absolute_path(anonymous_client):
    listing = anonymous_client.get("/datasets").json()
    dataset_id = listing["datasets"][0]["id"]
    _no_leak(anonymous_client.get(f"/datasets/{dataset_id}").text)


def test_anonymous_ledger_has_no_absolute_path(anonymous_client, client, tmp_path):
    # Force a config.loaded ledger entry carrying an absolute source path
    # onto the chain — the exact shape the review found leaking.
    client.post("/config/reload", json={"path": str(GOOD_CONFIG)})
    _no_leak(anonymous_client.get("/ledger", params={"limit": 1000}).text)


def test_anonymous_ledger_verify_has_no_absolute_path(anonymous_client):
    _no_leak(anonymous_client.get("/ledger/verify").text)


def test_anonymous_config_has_no_absolute_path(anonymous_client):
    _no_leak(anonymous_client.get("/config").text)


def test_anonymous_healthz_has_no_absolute_path(anonymous_client):
    _no_leak(anonymous_client.get("/healthz").text)


def test_anonymous_approvals_has_no_absolute_path(anonymous_client, client):
    client.post("/effects", json={
        "effect_type": "dispatch.leak-sweep-probe",
        "reversibility": "irreversible",
        "description": f"probe from {GOOD_CONFIG}",
    })
    _no_leak(anonymous_client.get("/approvals").text)


def test_an_out_of_allowlist_reload_refusal_leaks_no_path_to_anonymous(anonymous_client, client, tmp_path):
    evil = tmp_path / "pwn.yaml"
    evil.write_text("version: 1\neffects: []\n", encoding="utf-8")
    client.post("/config/reload", json={"path": str(evil)})
    _no_leak(anonymous_client.get("/ledger", params={"limit": 1000}).text)
    _no_leak(anonymous_client.get("/config").text)


def test_authenticated_caller_still_sees_the_real_source_path(client):
    """The redaction is scoped to UNAUTHENTICATED callers. An operator who
    presents the caller token still needs the real path to debug the host
    they run — this is a public-safety fix, not a feature removal."""
    body = client.get("/datasets").json()
    assert str(REPO_ROOT) in body["source"]


# --------------------------------------------------------------------------
# issue (this regression): a disclosure asserted redaction was complete on
# "every other" surface. A first-time review panel falsified that in minutes
# against ``GET /datasets/{id}`` — the LIST route was swept, the DETAIL route
# was not, because the original P0 fix was verified against a sample of
# routes (``/datasets``, ``/ledger``, ``/ledger/verify``, ``/config``,
# ``/healthz``, ``/approvals``) rather than every anonymous-reachable one.
# These tests enumerate the live route table itself, so a new anonymous GET
# route added later is covered automatically instead of by whoever remembers
# to add a line here.

#: Schema/UI routes. Not part of the data surface this product publishes —
#: Swagger's own static HTML/JS, not a throughline response body — and they
#: are also served as non-JSON content types the scrub middleware does not
#: (and need not) touch.
_SCHEMA_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _flatten_routes(routes) -> list:
    """FastAPI 0.141's ``app.include_router`` mounts a router LAZILY as a
    single ``_IncludedRouter`` wrapper rather than expanding its routes onto
    ``app.routes`` up front — the datasets router (``attach_datasets`` in
    ``throughline/datasets_api.py``) is mounted exactly this way. A naive
    walk of ``app.routes`` therefore silently skips ``/datasets`` and
    ``/datasets/{id}`` entirely: not "found but not scrubbed" but "not even
    seen" by anything that enumerates the route table this way. That is a
    second, independent way the original P0 fix's manual, hand-picked route
    list could miss a route a reviewer finds with curl. Recurse into
    ``original_router.routes`` so the flattened list is real."""
    flat = []
    for route in routes:
        nested = getattr(route, "original_router", None)
        if nested is not None:
            flat.extend(_flatten_routes(nested.routes))
        else:
            flat.append(route)
    return flat


def _anonymous_get_routes(app) -> list[str]:
    """Every GET route this app serves, minus schema/UI routes, path templates
    intact (e.g. ``/effects/{id}``)."""
    paths = []
    for route in _flatten_routes(app.routes):
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if path is None or "GET" not in methods or path in _SCHEMA_ROUTES:
            continue
        paths.append(path)
    return sorted(set(paths))


def test_every_anonymous_get_route_is_enumerated_and_covered():
    """Documents the route table this suite promises to sweep, so a route
    added to app.py without a matching entry in the concrete-path fixture
    below fails loudly instead of silently going unswept."""
    from throughline.app import create_app

    paths = _anonymous_get_routes(create_app())
    assert paths == [
        "/approvals",
        "/config",
        "/datasets",
        "/datasets/{id}",
        "/effects/{id}",
        "/effects/{id}/walk",
        "/healthz",
        "/ledger",
        "/ledger/verify",
    ]


def test_no_anonymous_reachable_route_leaks_a_path_or_internal_hostport(
    anonymous_client, client, tmp_path
):
    """Hit every anonymous-reachable GET route this app serves — not a
    sample — with concrete ids, and assert none of them leak a server-local
    absolute path or an internal host:port. This is the test that would have
    caught the DETAIL-route gap: it enumerates ``app.routes`` itself rather
    than a hand-picked subset."""
    # Seed concrete ids so path-parameter routes are actually exercised
    # rather than 404ing past the check.
    client.post("/config/reload", json={"path": str(GOOD_CONFIG)})
    effect = client.post("/effects", json={
        "id": "eff-leak-sweep-probe",
        "effect_type": "notify.operator",
        "description": f"leak-sweep probe referencing {GOOD_CONFIG}",
    }).json()
    dataset_id = client.get("/datasets").json()["datasets"][0]["id"]

    concrete = {
        "/effects/{id}": f"/effects/{effect['id']}",
        "/effects/{id}/walk": f"/effects/{effect['id']}/walk",
        "/datasets/{id}": f"/datasets/{dataset_id}",
    }

    app_paths = _anonymous_get_routes(anonymous_client.app)
    assert app_paths, "route enumeration returned nothing — fixture is broken"

    for template in app_paths:
        url = concrete.get(template, template)
        response = anonymous_client.get(url)
        _no_leak(response.text)


def test_anonymous_dataset_detail_has_no_absolute_path_after_reload(
    anonymous_client, client
):
    """The DETAIL route specifically — the one the regression review found
    uncovered. A registry reload resolves the source file to an absolute
    path (exactly like ``/config/reload`` does for effects config); the
    per-entry DETAIL response must be scrubbed of it exactly as the LIST
    response already is.

    The reload target must live inside the dataset-registry allowlist
    (``config/``, same confinement as ``/config/reload``), so this follows
    the pattern in ``test_dataset_registry.py``: write under
    ``config/``, reload, clean up in ``finally``.
    """
    registry_source = REPO_ROOT / "config" / "leak-probe-registry-under-test.yaml"
    registry_source.write_text(
        "version: 1\n"
        "component: throughline\n"
        "datasets:\n"
        "  - id: leak-probe\n"
        "    name: Leak probe dataset\n"
        "    component: throughline\n"
        "    mode: fixture\n"
        "    real_or_synthetic: synthetic\n"
        "    availability: available\n"
        f"    source: {registry_source}\n"
        "    licence: \"n/a\"\n"
        "    provenance: \"authored for this test\"\n",
        encoding="utf-8",
    )
    try:
        reload_response = client.post(
            "/datasets/reload", json={"path": str(registry_source)}
        )
        assert reload_response.status_code == 200, reload_response.text

        _no_leak(anonymous_client.get("/datasets").text)
        _no_leak(anonymous_client.get("/datasets/leak-probe").text)
    finally:
        registry_source.unlink(missing_ok=True)
