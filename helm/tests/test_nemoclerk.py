"""test://helm/nemoclerk-grounded and test://helm/nemoclerk-context.

These tests never contact a model endpoint. The model client is either
disabled (offline) or a stub, because what is under test is helm's
GROUNDING MECHANISM, not a model's manners.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from helm.config import Settings
from helm.nemoclerk.agent import ModelLadder, NemoClerk
from tests.conftest import sign_in


# ------------------------------------------- test://helm/nemoclerk-grounded
def test_a_data_question_yields_a_tool_chip(client: TestClient, substrate) -> None:
    substrate.propose("eff-ground")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "what's waiting for approval?", "feature_area": "helm"},
    ).json()
    assert answer["grounded"] is True
    assert len(answer["chips"]) >= 1
    assert answer["chips"][0]["name"] == "list_approvals"


def test_the_answers_facts_match_the_tool_result(client: TestClient, substrate) -> None:
    """No chip → no claim, and with a chip the claim IS the tool result."""
    substrate.propose("eff-facts", description="microgrid load-shed battery_4")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "what's waiting for approval?", "feature_area": "helm"},
    ).json()
    chip = answer["chips"][0]
    assert chip["summary"] in answer["text"] or "microgrid load-shed battery_4" in answer["text"]
    assert "1 approval(s)" in chip["summary"]
    # every id the answer mentions came from the tool
    assert "apr-eff-facts" in json.dumps(chip["data"])


def test_an_answer_with_no_chip_makes_no_claim(client: TestClient) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "who won the 1998 world cup?", "feature_area": "helm"},
    ).json()
    assert answer["chips"] == []
    assert answer["grounded"] is False
    assert "1998" not in answer["text"]
    assert "tool" in answer["text"].lower() or "API" in answer["text"]


def test_ledger_question_is_grounded_in_the_ledger(client: TestClient, substrate) -> None:
    substrate.propose("eff-led")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "show me the newest ledger entries", "feature_area": "helm"},
    ).json()
    names = {c["name"] for c in answer["chips"]}
    assert "read_ledger" in names
    chip = next(c for c in answer["chips"] if c["name"] == "read_ledger")
    # the chip's own count must match what the ledger held when it was read
    # (the turn itself is ledgered afterwards, which advances the chain)
    reported = int(chip["summary"].split("chain length ")[1].split(",")[0])
    assert reported <= len(substrate.entries)
    assert reported >= len(substrate.entries) - 2


def test_a_feeds_question_names_only_feeds_the_tool_reported(
    client: TestClient, substrate
) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message", json={"message": "which feeds are live?", "feature_area": "helm"}
    ).json()
    chip = next(c for c in answer["chips"] if c["name"] == "list_feeds")
    assert "offline: video" in chip["summary"]
    assert chip["summary"] in answer["text"] or "video" in answer["text"]


def test_the_refusal_is_a_tool_result_not_a_sentence(client: TestClient, substrate) -> None:
    substrate.propose("eff-clerk-refuse")
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message", json={"message": "approve it", "feature_area": "breaker"}
    ).json()
    assert answer["refused"] is True
    refusal_chip = next(c for c in answer["chips"] if c["refused"])
    assert refusal_chip["name"] == "approve_effect"
    assert "agent:nemoclerk" in answer["text"]
    assert substrate.approvals["apr-eff-clerk-refuse"]["state"] == "pending"


# -------------------------------------------- test://helm/nemoclerk-context
def test_sessions_are_keyed_subject_and_feature_area(client: TestClient, substrate) -> None:
    """A breaker transcript is absent from the docket session."""
    sign_in(client, "dana@nvidia-demo.example", "admin")
    client.post(
        "/nemoclerk/message",
        json={"message": "why is dispatch held?", "feature_area": "breaker"},
    )
    client.post(
        "/nemoclerk/message",
        json={"message": "show me an abstention", "feature_area": "docket"},
    )

    breaker = client.get("/nemoclerk/session", params={"feature_area": "breaker"}).json()
    docket = client.get("/nemoclerk/session", params={"feature_area": "docket"}).json()

    breaker_text = json.dumps(breaker["turns"])
    docket_text = json.dumps(docket["turns"])
    assert "why is dispatch held?" in breaker_text
    assert "why is dispatch held?" not in docket_text
    assert "show me an abstention" in docket_text
    assert "show me an abstention" not in breaker_text


def test_each_session_is_primed_with_its_module_about(client: TestClient) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    for area, marker in (
        ("breaker", "Grid Watch"),
        ("docket", "Permit Triage"),
        ("siren", "City Pulse"),
        ("blindspot", "Floor Watch"),
    ):
        client.get(f"/{area}")
        session = client.get("/nemoclerk/session", params={"feature_area": area}).json()
        assert marker in session["priming"], f"{area} session is not primed"
        assert session["feature_area"] == area


def test_sessions_are_keyed_by_subject_too(client: TestClient) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    client.post("/nemoclerk/message", json={"message": "overview", "feature_area": "helm"})
    sign_in(client, "ruiz@nvidia-demo.example", "operator")
    ruiz = client.get("/nemoclerk/session", params={"feature_area": "helm"}).json()
    assert ruiz["turns"] == []
    assert ruiz["subject"] == "ruiz@nvidia-demo.example"


def test_switching_tabs_switches_sessions_in_the_rail(client: TestClient, substrate) -> None:
    sign_in(client, "dana@nvidia-demo.example", "admin")
    client.post(
        "/nemoclerk/message",
        json={"message": "why is dispatch held?", "feature_area": "breaker"},
    )
    assert "why is dispatch held?" in client.get("/breaker").text
    assert "why is dispatch held?" not in client.get("/docket").text


# ------------------------------------------------------------- model ladder
def test_the_runtime_reports_no_turn_served_until_one_does(client: TestClient) -> None:
    """No question has been asked yet, so no rung has served anything.

    ``/nemoclerk/runtime`` used to name the ladder's ACTIVE rung here — a
    hope about what would answer NEXT — as if it were already serving. It
    must say plainly that nothing has served yet instead of reporting a
    configured default as if it were active.
    """
    header = client.get("/nemoclerk/runtime").json()
    assert header["runtime"] == "helm tool-loop"
    assert header["principal"].startswith("agent:nemoclerk(")
    assert header["tier"] == "no turn served yet"
    assert header["model"] == ""
    assert header["served_source"] == "none"
    # ...and it names every configured rung, with its silicon, so the
    # hardware claim is legible AND falsifiable rather than merely printed.
    rungs = header["rungs"]
    assert [r["name"] for r in rungs][-2:] == ["cache", "refuse"]
    # the fallback rungs are always named, and never as a live model
    assert rungs[-1]["state"] == "active"  # nothing else is answering here
    # the configured ladder names its silicon (see the dedicated test below)
    full = ModelLadder(Settings(nvidia_api_key="k")).rungs()
    assert any("Grace Blackwell" in r["silicon"] for r in full)
    assert any("RTX 5090" in r["silicon"] for r in full)


def test_health_reported_rung_can_never_diverge_from_the_ledgered_rung(
    app, substrate, settings
) -> None:
    """The exact defect a reviewer caught live: ``/healthz``/``/nemoclerk/runtime``
    named the ladder's ACTIVE, reachable GB10 rung while the concurrent
    turn's ledger entry recorded a different (no-model) rung for the SAME
    turn. Both readings must now come from the same observation — what
    actually served the last turn — so they can never disagree again, even
    when the ladder considers a rung reachable but that rung fails THIS turn.
    """
    from helm.nemoclerk.agent import Tier

    substrate.propose("eff-divergence")
    clerk: NemoClerk = app.state.clerk

    # The ladder considers GB10 ACTIVE and reachable...
    gb10 = Tier(
        1, "dgx-spark", "http://stub/v1", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        hardware="NVIDIA GB10 Grace Blackwell (DGX Spark) · vLLM",
        silicon="NVIDIA GB10 Grace Blackwell (DGX Spark)",
        supports_tools=False,
    )
    clerk.ladder._state = "done"
    clerk.ladder._active = gb10
    clerk.ladder._reachable_tiers = [gb10]
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    # ...but the CONCURRENT turn's endpoint call fails outright: GB10 never
    # actually answers this turn.
    clerk._post = lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("refused"))

    answer = clerk.ask("which feeds are live?", subject="dana", feature_area="helm").to_dict()
    # The ladder re-probes after a failure (``invalidate()``) and, in this
    # scenario, finds GB10 reachable again a moment later — exactly what the
    # live curl against ``/v1/models`` shows: the rung is genuinely up, it
    # just did not serve THIS turn. A health check landing right now must
    # still not borrow that reachability as if it had.
    clerk.ladder._state = "done"
    clerk.ladder._active = gb10
    clerk.ladder._reachable_tiers = [gb10]
    header = clerk.pane_header()

    # Neither reading may name GB10, or claim its hardware, for a turn GB10
    # never served.
    assert "GB10" not in json.dumps(answer)
    assert "GB10" not in json.dumps(header)
    # The ledgered fields and the health-reported fields are the SAME
    # observation now, so they cannot disagree.
    assert answer["model"] == header["model"]
    assert answer["silicon"] == header["silicon"]
    assert answer["hardware"] == header["hardware"]


def test_an_unreachable_model_yields_a_refusal_not_a_composed_answer(
    app, substrate, settings
) -> None:
    """The mock/stub model responder is gone from the product. When nothing
    can serve a turn, helm must say so in words rather than silently
    stringing tool summaries into a confident-sounding reply standing in for
    a model that was never consulted.
    """
    from helm.nemoclerk.agent import Tier

    substrate.propose("eff-refuse-no-model")
    clerk: NemoClerk = app.state.clerk
    tier = Tier(1, "rtx-5090", "http://stub/v1", "qwen3.6-27b")
    clerk.ladder._state = "done"
    clerk.ladder._active = tier
    clerk.ladder._reachable_tiers = [tier]
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    clerk._post = lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("refused"))

    answer = clerk.ask("which feeds are live?", subject="dana", feature_area="helm")
    assert answer.grounded is True  # the tools still ran; the raw facts are real
    assert "no model is reachable" in answer.text.lower()
    assert "video" in answer.text  # the real tool result, disclosed verbatim
    assert "MOCK NEMOCLERK" not in answer.text
    assert answer.honesty_label != "", "the refusal must still be disclosed, never silent"


def test_the_ledgered_principal_names_what_actually_served(app, settings) -> None:
    """A dead rung kept its name in the principal the chain notarises.

    Everywhere else a falsehood is visible to a human. Here it is NOTARISED:
    the chain verifies valid over an entry asserting a model was consulted
    when none was.
    """
    from helm.nemoclerk.agent import Tier

    clerk: NemoClerk = app.state.clerk
    tier = Tier(1, "rtx-5090", "http://stub/v1", "qwen3.6-27b", thinking_off=True)
    clerk.ladder._state = "done"
    clerk.ladder._active = tier
    clerk.ladder._reachable_tiers = [tier]
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})

    class Dead:
        def raise_for_status(self) -> None:
            raise httpx.ConnectError("connection refused")

    def dead_post(url: str, **kwargs: Any):
        raise httpx.ConnectError("connection refused")

    clerk._post = dead_post
    clerk._serving = ("model", tier)
    assert clerk.principal() == "agent:nemoclerk(qwen3.6-27b@rtx-5090)"

    # The endpoint dies. The next turn must NOT ledger the dead rung.
    answer = clerk.ask("which feeds are live?", subject="dana", feature_area="helm")
    assert clerk.principal() == "agent:nemoclerk(none@none)"
    assert "rtx-5090" not in clerk.principal()
    assert answer.grounded is True  # the tools still ran
    # ...and the dead rung was evicted rather than kept on the strength of a
    # probe that succeeded once, at boot, and was never revisited.
    assert tier.name not in {t.name for t in clerk.ladder._reachable_tiers}


def test_a_cached_turn_ledgers_cache_not_the_model(app, settings) -> None:
    from helm.nemoclerk.agent import Tier

    clerk: NemoClerk = app.state.clerk
    tier = Tier(1, "rtx-5090", "http://stub/v1", "qwen3.6-27b")
    clerk._serving = ("cache", tier)
    assert clerk.principal() == "agent:nemoclerk(cache@rtx-5090)"


def test_the_ladder_leads_with_nvidia_silicon_running_an_nvidia_model() -> None:
    """The only rung that makes the hardware claim without hedging.

    Measured: 0.6s for an 11-token turn with /no_think, so it is interactive
    AND it is Nemotron on a GB10 Grace Blackwell — not someone else's model
    on NVIDIA silicon.
    """
    ladder = ModelLadder(Settings(fleet_model_url="http://fleet/v1", nvidia_api_key="key"))
    tiers = ladder.tiers()
    assert [t.number for t in tiers] == [1, 2, 3, 4]

    spark = tiers[0]
    assert spark.name == "dgx-spark"
    assert "nemotron" in spark.model.lower()
    assert "GB10 Grace Blackwell" in spark.silicon
    assert spark.host == "dgx-spark"
    # it needs /no_think for the same reason qwen needs enable_thinking:false
    assert spark.system_prefix == "/no_think"
    # ...and it has no tool-call parser, which helm's grounding survives
    assert spark.supports_tools is False

    fast = tiers[1]
    assert fast.name == "rtx-5090"
    assert fast.model == "qwen3.6-27b"
    assert fast.thinking_off is True
    assert fast.supports_tools is True

    assert tiers[3].name == "hosted-nemotron"
    # the retired deepseek rung is gone: the GB10 cannot host both
    assert not any("deepseek" in t.model for t in tiers)


def test_a_rung_without_a_tool_parser_is_still_grounded(app, settings, substrate) -> None:
    """Grounding is helm's mechanism, not a capability it rents from a model."""
    from helm.nemoclerk.agent import Tier

    substrate.propose("eff-notools")
    sent: list[dict[str, Any]] = []

    class Reply:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"role": "assistant", "content": "One is held."}}]}

    clerk: NemoClerk = app.state.clerk
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    clerk._post = lambda url, **kw: (sent.append(kw["json"]), Reply())[1]
    tier = Tier(
        1, "dgx-spark", "http://stub/v1", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        system_prefix="/no_think", supports_tools=False,
    )
    clerk.ladder._state = "done"
    clerk.ladder._active = tier

    answer = clerk.ask("what is waiting?", subject="dana", feature_area="helm")
    assert answer.grounded is True
    assert answer.chips, "the router grounds the turn even with no tool parser"
    # no request ever offered tools to a server that cannot parse them
    assert all("tools" not in payload for payload in sent)
    # ...and every request carried the prefix the rung requires
    assert all(
        payload["messages"][0]["content"].startswith("/no_think") for payload in sent
    )


def test_thinking_is_switched_off_for_the_reasoning_rung(app, settings) -> None:
    """Without enable_thinking:false the model answers with an empty string."""
    from helm.nemoclerk.agent import Tier

    sent: dict[str, Any] = {}

    class StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"role": "assistant", "content": "ok."}}]}

    clerk: NemoClerk = app.state.clerk
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})

    def stub_post(url: str, **kwargs: Any) -> StubResponse:
        sent.update(kwargs["json"])
        return StubResponse()

    clerk._post = stub_post
    tier = Tier(1, "rtx-5090", "http://stub/v1", "qwen3.6-27b", thinking_off=True)
    clerk._chat_one(tier, [{"role": "user", "content": "hi"}], with_tools=False)
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


def test_an_empty_model_reply_is_a_failure_not_a_blank_bubble(app, settings) -> None:
    from helm.nemoclerk.agent import Tier

    class StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"role": "assistant", "content": "   "}}]}

    clerk: NemoClerk = app.state.clerk
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    clerk._post = lambda url, **kw: StubResponse()
    tier = Tier(1, "rtx-5090", "http://stub/v1", "qwen3.6-27b", thinking_off=True)
    assert clerk._chat_one(tier, [{"role": "user", "content": "hi"}], with_tools=False) is None


def test_a_mock_turn_is_labelled_as_a_mock_turn(client: TestClient, substrate) -> None:
    """Honesty copy: a turn with no model in the loop says so — and, since
    the deterministic composer is no longer a product code path, says it as
    an explicit refusal rather than as a labelled-but-composed reply.
    """
    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message", json={"message": "overview please", "feature_area": "helm"}
    ).json()
    assert answer["source"] == "mock"
    assert "NO MODEL IN THE LOOP" in answer["honesty_label"]
    assert "MOCK NEMOCLERK" not in answer["honesty_label"]
    assert "no model is reachable" in answer["text"].lower()


def test_a_model_turn_uses_the_returned_tool_calls(app, substrate, settings) -> None:
    """With a stub endpoint, the loop executes the model's tool_calls for real."""
    substrate.propose("eff-loop")
    calls: list[dict[str, Any]] = []

    class StubResponse:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._payload

    def stub_post(url: str, **kwargs: Any) -> StubResponse:
        payload = kwargs["json"]
        calls.append(payload)
        if len(calls) == 1:
            return StubResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "list_approvals",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )
        return StubResponse(
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "One irreversible dispatch is held at the gate.",
                        }
                    }
                ]
            }
        )

    clerk: NemoClerk = app.state.clerk
    clerk._post = stub_post
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    clerk.ladder._probed = True
    from helm.nemoclerk.agent import Tier

    clerk.ladder._state = "done"
    clerk.ladder._active = Tier(1, "dgx-spark", "http://stub/v1", "deepseek-v4-flash-0731")

    answer = clerk.ask("what is waiting?", subject="dana", feature_area="helm")

    assert calls, "the model was never called"
    assert calls[0]["tool_choice"] == "auto"
    assert calls[0]["max_tokens"] <= 250, "answers must stay short on a 5 tok/s endpoint"
    assert any(m["role"] == "tool" for m in calls[1]["messages"]), "tool result not fed back"
    assert answer.grounded is True
    assert [c["name"] for c in answer.chips] == ["list_approvals"]
    assert answer.text == "One irreversible dispatch is held at the gate."
    assert answer.source == "model"


def test_a_model_answer_with_no_tool_call_is_grounded_by_the_backstop(
    app, substrate, settings
) -> None:
    """A model that skips its tools does not get to make a claim unaided."""
    substrate.propose("eff-backstop")

    class StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"choices": [{"message": {"role": "assistant", "content": "Nothing at all."}}]}

    clerk: NemoClerk = app.state.clerk
    clerk._post = lambda url, **kw: StubResponse()
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    from helm.nemoclerk.agent import Tier

    clerk.ladder._state = "done"
    clerk.ladder._active = Tier(1, "dgx-spark", "http://stub/v1", "m")

    answer = clerk.ask("how many approvals are waiting?", subject="dana", feature_area="helm")
    assert answer.grounded is True
    assert answer.chips, "the backstop did not run a tool"
    assert "1 approval(s)" in answer.chips[0]["summary"]


def test_scripted_turns_replay_from_cache_offline(app, substrate) -> None:
    """The demo turns must survive the network being off — and say they are cached."""
    substrate.propose("eff-cached")
    clerk: NemoClerk = app.state.clerk
    question = "what's waiting for approval?"
    clerk.scripted_cache_write(question, "One irreversible dispatch is held at the gate.")
    answer = clerk.ask(question, subject="dana", feature_area="helm")
    assert answer.source == "cache"
    assert answer.text == "One irreversible dispatch is held at the gate."
    assert "CACHED RESPONSE" in answer.honesty_label
    assert answer.chips, "a cached phrasing still carries live tool chips"


@pytest.mark.live
def test_live_local_gpu_endpoint_answers(app, substrate) -> None:
    """Optional live smoke against the local GPU rung. Skips when unreachable."""
    import httpx

    from helm.nemoclerk.agent import Tier

    url = "http://rtx-workstation.nemotron.example.com:9997/v1"
    try:
        if not httpx.get(f"{url}/models", timeout=3).is_success:
            pytest.skip("local GPU endpoint not reachable")
    except httpx.HTTPError:
        pytest.skip("local GPU endpoint not reachable")

    substrate.propose("eff-live")
    clerk: NemoClerk = app.state.clerk
    clerk.settings = Settings(**{**clerk.settings.__dict__, "offline": False})
    clerk.ladder._state = "done"
    clerk.ladder._active = Tier(
        1, "rtx-5090", url, "qwen3.6-27b", hardware="NVIDIA RTX 5090", thinking_off=True
    )
    answer = clerk.ask("what is waiting for approval?", subject="dana", feature_area="helm")
    assert answer.grounded is True
    assert answer.chips



def test_every_turn_records_the_silicon_that_served_it(client: TestClient, substrate) -> None:
    """The hardware claim must be auditable from the chain, not just printed."""
    sign_in(client, "dana@nvidia-demo.example", "admin")
    before = len(substrate.entries)
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "which feeds are live?", "feature_area": "helm"},
    ).json()
    assert len(substrate.entries) > before
    note = substrate.entries[-1]
    assert note["body"]["class"] == "helm.nemoclerk.turn"
    payload = note["body"]["payload_ref"]
    assert answer["principal"] in payload
    assert answer["source"] in payload
    assert "list_feeds" in payload


def test_a_turn_that_consulted_no_model_names_no_model_and_no_silicon(
    client: TestClient, substrate
) -> None:
    """The agent-refusal beat consults no model. The chain must say so.

    With a rung ACTIVE but never called, this turn used to record
    source:"model" and the rung's GB10 silicon beside a none@none
    principal — a single entry contradicting itself, verified valid forever.
    """
    from helm.nemoclerk.agent import Tier

    substrate.propose("eff-no-model", reversibility="irreversible")
    clerk: NemoClerk = client.app.state.clerk
    clerk.ladder._state = "done"
    clerk.ladder._active = Tier(
        1, "dgx-spark", "http://stub/v1", "nemotron-x",
        hardware="dgx-spark", silicon="NVIDIA GB10 Grace Blackwell (DGX Spark)",
    )

    sign_in(client, "dana@nvidia-demo.example", "admin")
    answer = client.post(
        "/nemoclerk/message",
        json={"message": "approve it", "feature_area": "helm"},
    ).json()

    assert answer["refused"] is True, "the beat is the ATTEMPT and its refusal"
    assert answer["source"] == "mock", "no model was consulted on this turn"
    assert answer["silicon"] == ""
    assert answer["principal"] == "agent:nemoclerk(none@none)"

    payload = substrate.entries[-1]["body"]["payload_ref"]
    assert "GB10" not in payload, payload
    assert "none@none" in payload


def test_a_turn_served_by_a_stub_does_not_get_to_name_gb10(
    app, substrate, settings
) -> None:
    """`silicon` is configuration bound to a rung SLOT, not to the endpoint.

    Point a rung at something that is not what the slot says it is, and the
    silicon claim is withdrawn rather than notarised.
    """
    from helm.nemoclerk.agent import Tier

    substrate.propose("eff-stub")

    class StubResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {
                # A 40-line stub answering on the rung's port, honest about
                # what it is.
                "model": "stub-echo",
                "choices": [{"message": {"role": "assistant", "content": "Fine."}}],
            }

    clerk: NemoClerk = app.state.clerk
    clerk._post = lambda url, **kw: StubResponse()
    clerk.settings = Settings(**{**settings.__dict__, "offline": False})
    clerk.ladder._state = "done"
    clerk.ladder._active = Tier(
        1, "dgx-spark", "http://127.0.0.1:18999/v1", "nemotron-x",
        hardware="dgx-spark", silicon="NVIDIA GB10 Grace Blackwell (DGX Spark)",
    )

    answer = clerk.ask("how many approvals are waiting?", subject="dana", feature_area="helm")
    assert "GB10" not in answer.silicon, answer.silicon
    assert "unverified" in answer.silicon
    assert answer.model == "stub-echo", "the ledger names what ANSWERED"
