"""test://docket/route-ledgered — an accepted judgment produces a route effect
the substrate accepts, and the ledger binds effect -> judgment -> signal.

scenario://docket/route-ledger — routing is REVERSIBLE, so it auto-executes
without approval; it is ledgered and hash-chained regardless.

The class at the bottom runs against a REAL throughline on :8600. It skips when
the substrate is not up, unless DOCKET_REQUIRE_REAL_SUBSTRATE=1 — which CI sets,
so an un-run integration is a red pipeline rather than a quiet skip.
"""
from __future__ import annotations

import os

import pytest

from docket import config
from docket.clients.throughline import (
    MockThroughlineClient,
    RealThroughlineClient,
    SubstrateUnavailable,
    build_client,
)
from docket.judge import judge_permit
from docket.routing import RouteRefused, route_judgment, signal_for
from tests.conftest import CITED, CITED_ALT, EMPTY_DESC, UNCITED


def _real_substrate_up() -> bool:
    return RealThroughlineClient().health()


REQUIRE_REAL = os.environ.get("DOCKET_REQUIRE_REAL_SUBSTRATE", "").strip() in {
    "1", "true", "yes", "on",
}


class TestSignalShape:
    def test_permit_becomes_a_federation_signal(self, permit):
        signal = signal_for(permit(CITED))
        assert signal.id == f"sig-permit-{CITED}"
        assert signal.cls == "permit.document"
        assert signal.source == "socrata:76t5-zqzr"
        assert signal.real_or_synthetic == "real", "the permit corpus is real data"

    def test_signal_serializes_under_the_contract_alias(self, permit):
        body = signal_for(permit(CITED)).model_dump(by_alias=True)
        assert body["class"] == "permit.document"
        for field in ("id", "class", "source", "ingest_time", "real_or_synthetic"):
            assert field in body

    def test_payload_ref_points_at_the_offline_snapshot(self, permit):
        assert signal_for(permit(CITED)).payload_ref.startswith("data/permits.json#")


class TestRouteEffectMock:
    def test_accepted_judgment_produces_a_reversible_effect(self, permit, substrate):
        row = permit(CITED)
        judgment = judge_permit(row)
        effect = route_judgment(judgment, substrate, permit=row)
        assert effect.reversibility == "reversible"

    def test_reversible_effect_auto_executes_without_approval(self, permit, substrate):
        row = permit(CITED)
        effect = route_judgment(judge_permit(row), substrate, permit=row)
        assert effect.status == "executed", "reversible effects need no approval"

    def test_effect_is_ledgered(self, permit, substrate):
        row = permit(CITED)
        effect = route_judgment(judge_permit(row), substrate, permit=row)
        assert effect.ledgered is True

    def test_ledger_binds_effect_to_judgment_to_signal(self, permit, substrate):
        row = permit(CITED)
        judgment = judge_permit(row)
        effect = route_judgment(judgment, substrate, permit=row)

        assert effect.judgment_id == judgment.id
        assert effect.signal_id == f"sig-permit-{CITED}"
        # All three hops actually reached the substrate.
        assert any(s["id"] == effect.signal_id for s in substrate.signals)
        assert any(j["id"] == effect.judgment_id for j in substrate.judgments)
        assert substrate.get_effect(effect.id) is not None

    def test_ledgered_judgment_carries_the_quote_as_rationale(self, permit, substrate):
        row = permit(CITED)
        judgment = judge_permit(row)
        route_judgment(judgment, substrate, permit=row)
        assert substrate.judgments[-1]["rationale"] == judgment.quote

    def test_mock_judgment_is_ledgered_as_synthetic(self, permit, substrate):
        row = permit(CITED)
        route_judgment(judge_permit(row), substrate, permit=row)
        assert substrate.judgments[-1]["real_or_synthetic"] == "synthetic"

    def test_effect_description_names_the_desk(self, permit, substrate):
        row = permit(CITED_ALT)
        judgment = judge_permit(row)
        effect = route_judgment(judgment, substrate, permit=row)
        assert CITED_ALT in effect.description
        assert judgment.route_proposal in effect.description

    def test_docket_never_requests_an_irreversible_effect(self, permit, substrate):
        for permitnum in (CITED, CITED_ALT):
            row = permit(permitnum)
            route_judgment(judge_permit(row), substrate, permit=row)
        assert all(
            e["reversibility"] == "reversible" for e in substrate.effects.values()
        )


class TestAbstentionIsNotRouted:
    """The asymmetry that makes abstention meaningful: no effect at all."""

    def test_abstention_refuses_to_route(self, permit, substrate):
        judgment = judge_permit(permit(EMPTY_DESC))
        with pytest.raises(RouteRefused):
            route_judgment(judgment, substrate, permit=permit(EMPTY_DESC))

    def test_uncited_abstention_refuses_to_route(self, permit, substrate):
        judgment = judge_permit(permit(UNCITED))
        with pytest.raises(RouteRefused):
            route_judgment(judgment, substrate, permit=permit(UNCITED))

    def test_refusal_emits_no_effect_at_all(self, permit, substrate):
        judgment = judge_permit(permit(EMPTY_DESC))
        with pytest.raises(RouteRefused):
            route_judgment(judgment, substrate)
        assert substrate.effects == {}


class TestRouteOverHttp:
    def test_judge_then_route_returns_202(self, client):
        judgment = client.post(f"/permits/{CITED}/judge").json()["judgment"]
        resp = client.post(f"/judgments/{judgment['id']}/route")
        assert resp.status_code == 202
        effect = resp.json()["effect"]
        assert effect["reversibility"] == "reversible"
        assert effect["ledgered"] is True

    def test_routing_an_abstention_is_refused_with_409(self, client):
        judgment = client.post(f"/permits/{EMPTY_DESC}/judge").json()["judgment"]
        resp = client.post(f"/judgments/{judgment['id']}/route")
        assert resp.status_code == 409, "a refusal is correct behaviour, not a 500"

    def test_effect_is_retrievable_after_routing(self, client):
        judgment = client.post(f"/permits/{CITED}/judge").json()["judgment"]
        effect = client.post(f"/judgments/{judgment['id']}/route").json()["effect"]
        got = client.get(f"/effects/{effect['id']}")
        assert got.status_code == 200
        assert got.json()["effect"]["judgment_id"] == judgment["id"]

    def test_routing_an_unknown_judgment_is_404(self, client):
        assert client.post("/judgments/jdg-nope/route").status_code == 404

    def test_route_refuses_with_503_when_substrate_unavailable(self, client, monkeypatch):
        """When the real substrate cannot be reached, /route must refuse —
        never fabricate a route effect against an ephemeral mock ledger."""
        from docket import app as app_module

        judgment = client.post(f"/permits/{CITED}/judge").json()["judgment"]

        def _unavailable():
            raise SubstrateUnavailable("real throughline unreachable at test")

        monkeypatch.setattr(app_module, "_substrate_client", None)
        monkeypatch.setattr(app_module, "build_substrate_client", _unavailable)

        resp = client.post(f"/judgments/{judgment['id']}/route")
        assert resp.status_code == 503
        assert "unavailable" in resp.json()["detail"]

    def test_mode_reports_substrate_unavailable_rather_than_faking_mock(
        self, client, monkeypatch
    ):
        from docket import app as app_module

        def _unavailable():
            raise SubstrateUnavailable("real throughline unreachable at test")

        monkeypatch.setattr(app_module, "_substrate_client", None)
        monkeypatch.setattr(app_module, "build_substrate_client", _unavailable)

        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["mode"]["substrate"] == "unavailable"


class TestClientSelection:
    def test_forced_mock_returns_the_mock_client(self):
        assert isinstance(build_client(force_mock=True), MockThroughlineClient)

    def test_unreachable_substrate_refuses_rather_than_faking_it(self, monkeypatch):
        """An unreachable real substrate must never be silently swapped for a
        mock — that would ledger route effects nobody durable ever saw. See
        the operator directive filed as this repo's issue #2."""
        monkeypatch.setattr(config, "THROUGHLINE_URL", "http://127.0.0.1:1")
        monkeypatch.setenv("THROUGHLINE_URL", "http://127.0.0.1:1")
        with pytest.raises(SubstrateUnavailable):
            build_client(force_mock=False)

    def test_real_client_raises_rather_than_returning_a_fake_effect(self, monkeypatch):
        monkeypatch.setenv("THROUGHLINE_URL", "http://127.0.0.1:1")
        real = RealThroughlineClient(base_url="http://127.0.0.1:1")
        with pytest.raises(SubstrateUnavailable):
            real.post_effect({"id": "x", "reversibility": "reversible",
                              "status": "proposed"})


@pytest.mark.skipif(
    not REQUIRE_REAL and not _real_substrate_up(),
    reason="real throughline not reachable on :8600 (set "
           "DOCKET_REQUIRE_REAL_SUBSTRATE=1 to make this a failure)",
)
class TestRouteLedgeredAgainstRealThroughline:
    """int+ — the effect is accepted by the REAL substrate and walks back."""

    @pytest.fixture
    def real(self) -> RealThroughlineClient:
        client = RealThroughlineClient()
        assert client.health(), (
            f"throughline unreachable at {client.base_url}; int+ cannot pass"
        )
        return client

    def test_real_substrate_accepts_the_route_effect(self, permit, real):
        row = permit(CITED)
        judgment = judge_permit(row)
        effect = route_judgment(judgment, real, permit=row)
        assert effect.id
        assert effect.reversibility == "reversible"
        assert effect.substrate == "real"

    def test_reversible_effect_is_not_queued_for_approval(self, permit, real):
        row = permit(CITED)
        effect = route_judgment(judge_permit(row), real, permit=row)
        assert effect.status != "queued", (
            "a reversible route must auto-execute, never wait on a human"
        )

    def test_effect_is_readable_back_from_the_real_substrate(self, permit, real):
        row = permit(CITED)
        effect = route_judgment(judge_permit(row), real, permit=row)
        fetched = real.get_effect(effect.id)
        assert fetched is not None
        assert fetched["id"] == effect.id

    def test_cause_walk_binds_effect_judgment_signal(self, permit, real):
        """The three-hop walk throughline promises, exercised end to end."""
        import httpx

        row = permit(CITED)
        judgment = judge_permit(row)
        effect = route_judgment(judgment, real, permit=row)

        walk = httpx.get(f"{real.base_url}/effects/{effect.id}/walk", timeout=15.0)
        assert walk.status_code == 200
        body = walk.json()
        assert body["effect_id"] == effect.id
        kinds = [hop["kind"] for hop in body["hops"]]
        assert "effect" in kinds and "judgment" in kinds and "signal" in kinds
        assert body["hop_count"] <= 3
        assert body.get("all_hops_verified") is True, "ledger digests must verify"

    def test_ledger_stays_valid_after_our_appends(self, real):
        import httpx

        resp = httpx.get(f"{real.base_url}/ledger/verify", timeout=15.0)
        assert resp.status_code == 200
        assert resp.json()["valid"] is True
