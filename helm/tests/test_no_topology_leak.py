"""helm#7 — no anonymous response may publish internal topology.

``GET /healthz`` and ``GET /feeds``, both unauthenticated and 200, used to
publish ``http://127.0.0.1:8600`` through ``:8604`` for every sibling —
throughline, docket, breaker, siren, blindspot — straight into the JSON
body. Four separate reviewers found it, and it was doubly damaging because
throughline's own public disclosure block claims internal host:port
topology is "redacted from every unauthenticated response, on this surface
and every other one" — so helm's leak made a sibling's published guarantee
false.

The fix reports sibling reachability by NAME and STATE only to an anonymous
caller; the address is withheld, not deleted — an authenticated admin
session may still ask for it. This file is the regression coverage: every
GET route this app answers with NO ``require_login``/role check ahead of
it, enumerated rather than sampled, so a newly added anonymous route that
starts leaking again fails here instead of waiting for a fifth reviewer.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

LOOPBACK_ADDRESS = re.compile(r"127\.0\.0\.1:860\d\b")
ANY_HTTP_ADDRESS = re.compile(r"https?://[^\s\"'<>]+")

# Every route this app serves via a plain ``@app.get``/``@app.post`` with no
# ``require_login`` (or admin) check ahead of it in helm/app.py — i.e.
# reachable and answering to a browser tab that never signed in. Sourced by
# reading every route registration in helm/app.py and keeping the ones with
# no ``require_login(...)`` / ``identity.is_admin`` guard before the return.
# Cross-checked against ``tests.test_console_api.CONSOLE_ACTIONS`` so the
# published surface and this enumeration cannot silently drift apart.
ANONYMOUS_GET_ROUTES = (
    "/",
    "/login",
    "/guest",
    "/help",
    "/healthz",
    "/overview",
    "/feeds",
    "/events",
    "/approvals",
    "/effects",
    "/ledger",
    "/ledger/verify",
    "/composed/state",
    "/auth/session",
    "/prefs",
    "/nemoclerk/session",
    "/nemoclerk/runtime",
    "/mcp/tools",
)


@pytest.fixture
def leaky_effect(substrate) -> tuple[str, str]:
    """An effect and its approval, so /walk and /approvals/{id} answer 200
    rather than a 404 that would prove nothing about what a REAL row leaks.
    """
    effect_id = "eff-topology-leak-check"
    substrate.propose(effect_id)
    return effect_id, f"apr-{effect_id}"


def test_no_anonymous_get_response_leaks_a_sibling_address_or_absolute_path(
    client: TestClient, substrate, tmp_path, leaky_effect
) -> None:
    """Every anonymous-reachable route, not a sample of them.

    Checked against BOTH the specific regression pattern (a sibling's
    loopback ``host:port``) and the general shape of an internal address or
    a filesystem path this run's own ``tmp_path`` would appear under —
    because the next leak will not necessarily use 127.0.0.1 to prove it is
    real.
    """
    effect_id, approval_id = leaky_effect
    routes = list(ANONYMOUS_GET_ROUTES) + [
        f"/walk/{effect_id}/json",
        f"/approvals/{approval_id}",
    ]
    leaking: list[tuple[str, str]] = []
    for path in routes:
        response = client.get(path, follow_redirects=False)
        body = response.text
        if LOOPBACK_ADDRESS.search(body):
            leaking.append((path, "loopback sibling address (127.0.0.1:860x)"))
        if str(tmp_path) in body:
            leaking.append((path, "absolute filesystem path (this run's tmp_path)"))
    assert not leaking, (
        "anonymous-reachable routes leaked internal topology or a path:\n  "
        + "\n  ".join(f"{path}: {why}" for path, why in leaking)
    )


def test_anonymous_healthz_reports_siblings_by_name_and_state_only(
    client: TestClient, substrate
) -> None:
    body = client.get("/healthz").json()
    assert set(body["siblings"]) == {
        "throughline", "docket", "breaker", "siren", "blindspot"
    }
    for name, sib in body["siblings"].items():
        assert "url" not in sib, (
            f"{name} exposes its address to an anonymous /healthz caller: {sib}"
        )
        assert not ANY_HTTP_ADDRESS.search(str(sib.get("detail", ""))), (
            f"{name}'s detail text carries an address anonymously: {sib['detail']!r}"
        )
        assert "online" in sib


def test_anonymous_feeds_report_no_sibling_address(
    client: TestClient, substrate
) -> None:
    feeds = client.get("/feeds").json()["feeds"]
    assert feeds, "the fixture produced no feeds — this test proves nothing"
    for feed in feeds:
        assert "url" not in feed, f"feed {feed.get('name')} exposes url anonymously: {feed}"
        assert not ANY_HTTP_ADDRESS.search(str(feed.get("detail", ""))), feed


def test_anonymous_overview_reports_no_sibling_address(
    client: TestClient, substrate
) -> None:
    services = client.get("/overview").json()["services"]
    for name, sib in services.items():
        assert "url" not in sib, f"{name} exposes url in anonymous /overview: {sib}"


def test_admin_may_still_see_sibling_addresses(admin: TestClient, substrate) -> None:
    """Withheld from anonymous callers, not deleted from the product.

    An operator with the right role can still debug a sibling by address —
    the fix narrows WHO sees it, it does not remove the capability.
    """
    healthz = admin.get("/healthz").json()
    assert any("url" in sib for sib in healthz["siblings"].values()), (
        "an admin session can no longer see any sibling's address at all"
    )

    feeds = admin.get("/feeds").json()["feeds"]
    assert any("url" in feed for feed in feeds), (
        "an admin session can no longer see any feed's address at all"
    )


def test_nemoclerk_runtime_principal_never_says_mock(
    client: TestClient, substrate
) -> None:
    """There is no mock login and no mock composer; the principal must not
    say there is. Regression for the ``agent:nemoclerk(mock@none)`` string
    a reviewer found live on an unauthenticated ``/nemoclerk/runtime``.
    """
    body = client.get("/nemoclerk/runtime").json()
    assert "mock" not in body.get("principal", ""), body["principal"]
