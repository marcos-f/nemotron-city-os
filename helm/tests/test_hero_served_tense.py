"""helm#6 — the login hero and /healthz + /nemoclerk/runtime must never
disagree about whether a turn has served, and the hero must never use the
present continuous for a rung that has not answered one.

Two fixes landed in the same session and reintroduced exactly the
divergence one of them existed to eliminate: one made ``/healthz`` and
``/nemoclerk/runtime`` derive "what served" from ``clerk.served()`` — the
one observation of the turn that actually answered — while the other
separately rewrote the login hero paragraph to render from
``clerk.ladder.rungs()`` instead, which answers "what is REACHABLE", a
different question. Live, the login page asserted, present tense, that "an
NVIDIA GB10 Grace Blackwell (DGX Spark) is serving interactive turns" while
``GET /healthz`` reported ``served_source: "none"`` — the page's own claim
of being "checkable rather than decorative" failed the moment anyone
checked it.

There is now one source of truth for "has a turn served"
(``clerk.served()["source"] != "none"``), read by the hero, ``/healthz``'s
``nemoclerk`` block, and ``/nemoclerk/runtime`` alike — this file pins that
down as a regression rather than a design note.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def _tier():
    from helm.nemoclerk.agent import Tier

    return Tier(
        1,
        "dgx-spark",
        "http://stub/v1",
        "nemotron-x",
        hardware="NVIDIA GB10 Grace Blackwell (DGX Spark) · vLLM",
        silicon="NVIDIA GB10 Grace Blackwell (DGX Spark)",
    )


def test_before_any_turn_the_hero_and_runtime_agree_nothing_has_served(
    client: TestClient, substrate
) -> None:
    runtime = client.get("/nemoclerk/runtime").json()
    assert runtime["served_source"] == "none"

    login = client.get("/login").text
    assert "no turn has served yet" in login, (
        "the hero must say plainly that nothing has served yet when "
        "served_source is none"
    )
    # The literal defect: asserting a rung IS serving, present continuous,
    # while nothing has served this run.
    assert "is serving interactive turns" not in login


def test_after_a_turn_serves_the_hero_and_runtime_agree_on_what_served(
    client: TestClient, substrate
) -> None:
    tier = _tier()
    clerk = client.app.state.clerk
    clerk.ladder._state = "done"
    clerk.ladder._active = tier
    clerk.ladder._reachable_tiers = [tier]
    clerk._serving = ("model", tier)
    clerk._serving_reported = tier.model

    runtime = client.get("/nemoclerk/runtime").json()
    assert runtime["served_source"] == "model"
    assert runtime["silicon"] == "NVIDIA GB10 Grace Blackwell (DGX Spark)"

    login = client.get("/login").text
    assert "no turn has served yet" not in login, (
        "a turn served this run — the hero must not still claim none has"
    )
    assert "NVIDIA GB10 Grace Blackwell (DGX Spark)" in login, (
        "the hero must name what actually served, once something has"
    )


def test_a_cached_turn_is_not_described_as_silicon_serving(
    client: TestClient, substrate
) -> None:
    """served_source == cache is a real 'has served' state, but no
    silicon served THIS turn — the hero must not claim hardware for a
    replay.
    """
    tier = _tier()
    clerk = client.app.state.clerk
    clerk.ladder._state = "done"
    clerk.ladder._active = tier
    clerk.ladder._reachable_tiers = [tier]
    clerk._serving = ("cache", tier)

    runtime = client.get("/nemoclerk/runtime").json()
    assert runtime["served_source"] == "cache"

    login = client.get("/login").text
    assert "no turn has served yet" not in login
    assert "cached response" in login
    assert "is serving interactive turns" not in login
