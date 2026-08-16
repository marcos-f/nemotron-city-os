"""The model-backed substrate: deepseek-v4-flash-0731 on the DGX Spark.

Operator-authorised on 2026-08-16 as an exception to the entitled-models line.
These tests run with no endpoint: a fake transport for behaviour, and the
committed judgment cache for the demo path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from breaker.engine import GridWatch
from breaker.judge import SEED_CACHE, ModelJudge, prompt_for
from breaker.models import TelemetryReading
from breaker.rule import compute_metrics, evaluate
from breaker.substrates import load_registry
from breaker.telemetry import DIVERGENT_UNIT, fixture, reading_for
from breaker.throughline import MockThroughlineClient

from conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config" / "substrates.yaml"


def history(upto: int, unit: str = DIVERGENT_UNIT):
    return [reading_for(unit, tick) for tick in range(upto + 1)]


def reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def fake_transport(content: str, record: list | None = None):
    def call(url, body, timeout):
        if record is not None:
            record.append(body)
        return reply(content)

    return call


@pytest.fixture
def metrics():
    return compute_metrics(history(23))


@pytest.fixture
def model_registry():
    registry = load_registry(CONFIG)
    registry.select("dsv4")
    return registry


@pytest.fixture
def nemotron_registry():
    registry = load_registry(CONFIG)
    registry.select("nemotron")
    return registry


# --------------------------------------------------------------- the client

def test_the_model_is_never_asked_to_do_arithmetic(metrics):
    """It receives the numbers as facts; it does not compute them."""
    prompt = prompt_for(metrics)
    assert f"soc_delta = {metrics.soc_delta}%" in prompt
    assert f"temp_slope = {metrics.temp_slope}" in prompt
    assert f"charge_current = {metrics.charge_ratio} of nominal" in prompt
    assert "compute" not in prompt.lower()


def test_a_verdict_is_parsed(metrics, tmp_path):
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None,
                       transport=fake_transport(
                           '{"diverged": true, "rationale": "all three", "confidence": 0.9}'))
    verdict = judge.judge(metrics)
    assert verdict.diverged is True
    assert verdict.confidence == 0.9
    assert verdict.abstained is False
    assert verdict.model == "deepseek-v4-flash-0731"


def test_a_fenced_verdict_is_parsed(metrics, tmp_path):
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=fake_transport(
        '```json\n{"diverged": false, "rationale": "not yet", "confidence": 0.4}\n```'))
    assert judge.judge(metrics).diverged is False


def test_a_reasoning_traces_draft_json_does_not_shadow_the_real_verdict(metrics, tmp_path):
    """Found live against nemotron: its <think> trace routinely narrates a
    DRAFT verdict — its own braces and all — before the real one. Naive
    first-"{"-to-last-"}" slicing spliced the draft's opening brace to the
    real answer's closing brace, producing something that parses as
    nothing — so a verdict the model DID give correctly got reported as "not
    a verdict" and the judgment abstained despite an answer being right
    there. Only the text after the LAST </think> is the answer."""
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=fake_transport(
        'Given the numbers, soc_delta alone exceeds threshold. So answer: '
        '{"diverged": true, "rationale": "draft, ignore me", "confidence": 1.0}. '
        'Confidence high.\n</think>\n'
        '{"diverged": true, "rationale": "the real answer", "confidence": 0.87}'
    ))
    verdict = judge.judge(metrics)
    assert verdict.abstained is False
    assert verdict.diverged is True
    assert verdict.rationale == "the real answer"


def test_a_reply_that_is_not_a_verdict_abstains(metrics, tmp_path):
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None,
                       transport=fake_transport("I think maybe the battery is sad."))
    verdict = judge.judge(metrics)
    assert verdict.abstained is True
    assert verdict.diverged is False
    assert "not a verdict" in verdict.rationale


def test_an_unreachable_endpoint_abstains(metrics, tmp_path):
    def explode(url, body, timeout):
        raise OSError("connection refused")

    verdict = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=explode).judge(metrics)
    assert verdict.abstained is True
    assert "unreachable" in verdict.rationale


def test_responses_are_cached_and_replayed(metrics, tmp_path):
    calls: list = []
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=fake_transport(
        '{"diverged": true, "rationale": "x", "confidence": 1.0}', calls))

    first = judge.judge(metrics)
    second = judge.judge(metrics)
    assert first.cached is False and second.cached is True
    assert len(calls) == 1, "the second judgment hit the network"
    assert judge.cache_path(metrics).exists()


def test_offline_mode_never_touches_the_network(metrics, tmp_path):
    def explode(url, body, timeout):
        raise AssertionError("offline mode made a request")

    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=explode, offline=True)
    verdict = judge.judge(metrics)
    assert verdict.abstained is True
    assert "offline" in verdict.rationale


def test_the_committed_cache_serves_the_demo_path_offline():
    """The federation's offline rule: the demo replays from disk, no endpoint."""
    seeds = list(SEED_CACHE.glob("*.json"))
    assert seeds, "no committed judgments — the demo would need the endpoint"
    payload = json.loads(seeds[0].read_text(encoding="utf-8"))
    assert payload["choices"][0]["message"]["content"]


# --------------------------------------------------------------- the engine

def test_nemotron_stays_the_default_substrate():
    """See tests/test_substrates.py for the fuller nemotron-default contract."""
    assert load_registry(CONFIG).active.id == "nemotron"


def test_the_model_is_only_consulted_once_a_check_trips(model_registry, tmp_path):
    """Nine healthy units x 40 ticks must not become 360 inference calls."""
    calls: list = []
    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=fake_transport(
        '{"diverged": true, "rationale": "x", "confidence": 1.0}', calls))
    watch = GridWatch(client=MockThroughlineClient(), registry=model_registry, judge=judge)

    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            break
    assert len(calls) == 1
    assert calls[0]["temperature"] == 0


def test_the_substrates_disagree_by_one_tick_and_the_pane_shows_it(model_registry, tmp_path):
    """Pinned from the real endpoint's judgment, replayed from the committed cache.

    The rule fires at tick 23. The model calls it at 22, while two of the three
    checks are still failing — so the pane shows two crosses above a DIVERGENCE
    verdict, attributed to the model. That disagreement is the point of a
    swappable substrate, and the gate holds the dispatch either way.
    """
    judge = ModelJudge(cache_dir=tmp_path, offline=True)   # seed cache only
    watch = GridWatch(client=MockThroughlineClient(), registry=model_registry, judge=judge)

    proposal = None
    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            proposal = outcome["proposal"]
            break

    assert proposal is not None, "the model substrate proposed nothing"
    assert proposal["tick"] == 22
    assert evaluate(history(23)).diverged is True
    assert evaluate(history(22)).diverged is False       # the rule would not have

    lines = proposal["evidence"].splitlines()
    assert lines[0].rstrip().endswith("✓")
    assert lines[1].rstrip().endswith("✗")
    assert lines[2].rstrip().endswith("✗")
    assert "→ DIVERGENCE" in lines[3]
    assert "deepseek-v4-flash-0731" in lines[3]
    assert "cached" in lines[3]

    # The dispatch is still irreversible and still held. The substrate changed;
    # the gate did not.
    assert proposal["status"] == "waiting_at_gate"
    assert proposal["substrate"] == "dsv4"


def test_the_numbers_are_identical_whichever_substrate_judges(model_registry, tmp_path):
    judge = ModelJudge(cache_dir=tmp_path, offline=True)
    watch = GridWatch(client=MockThroughlineClient(), registry=model_registry, judge=judge)
    for record in fixture():
        if record.unit_id == DIVERGENT_UNIT and record.tick <= 22:
            watch.ingest(TelemetryReading(**record.as_dict()))

    model_view = watch.evaluation_for(DIVERGENT_UNIT)
    rule_view = evaluate(history(22))
    assert model_view.soc_delta == rule_view.soc_delta
    assert model_view.temp_slope == rule_view.temp_slope
    assert model_view.charge_ratio == rule_view.charge_ratio
    assert [c.expression for c in model_view.checks] == [c.expression for c in rule_view.checks]


def test_an_abstention_proposes_nothing(model_registry, tmp_path):
    """No silent fallback to the rule: an abstention is an abstention."""
    def explode(url, body, timeout):
        raise OSError("endpoint down")

    judge = ModelJudge(cache_dir=tmp_path, seed_dir=None, transport=explode)
    watch = GridWatch(client=MockThroughlineClient(), registry=model_registry, judge=judge)

    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        assert outcome["proposal"] is None

    assert watch.abstentions, "the abstention was not recorded"
    assert watch.proposals == {}
    evaluation = watch.evaluation_for(DIVERGENT_UNIT)
    assert "ABSTAINED" in evaluation.attribution
    assert evaluation.diverged is False


def test_the_judgment_carries_the_models_confidence(model_registry, tmp_path):
    judge = ModelJudge(cache_dir=tmp_path, offline=True)
    watch = GridWatch(client=MockThroughlineClient(), registry=model_registry, judge=judge)
    proposal = None
    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            proposal = outcome["proposal"]
            break

    judgment = watch.client.judgments[proposal["judgment_id"]]
    assert judgment["substrate"] == "dsv4"
    assert "deepseek-v4-flash-0731" in judgment["substrate_label"]
    assert judgment["confidence"] == 0.95           # what the model actually said
    assert any("verdict:" in citation for citation in judgment["citations"])


# ------------------------------------------------------------- nemotron

def test_nemotron_active_by_default_uses_the_gb10_endpoint_not_dsv4(nemotron_registry):
    """engine.py's judge property once hardcoded "dsv4" for every model
    substrate. That is the exact quiet-substitution defect: selecting a
    different model substrate must build a client pointed at the endpoint
    and model actually active — not silently keep talking to DeepSeek while
    the judgment record claims Nemotron judged."""
    watch = GridWatch(client=MockThroughlineClient(), registry=nemotron_registry)
    assert watch.judge.endpoint == "http://dgx-spark.nemotron.example.com:8000/v1"
    assert watch.judge.model == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def test_an_unreachable_nemotron_abstains_rather_than_becoming_the_rule(
    nemotron_registry, tmp_path
):
    """The core honesty requirement: an unreachable Nemotron must never let
    the product keep claiming an LLM judged by quietly running the rule."""
    def explode(url, body, timeout):
        raise OSError("connection refused")

    judge = ModelJudge(
        endpoint=nemotron_registry.get("nemotron").endpoint,
        model=nemotron_registry.get("nemotron").model,
        cache_dir=tmp_path, seed_dir=None, transport=explode,
    )
    watch = GridWatch(client=MockThroughlineClient(), registry=nemotron_registry, judge=judge)

    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        assert outcome["proposal"] is None

    assert watch.abstentions, "the abstention was not recorded"
    assert watch.proposals == {}, "no proposal may be raised from an abstention"
    evaluation = watch.evaluation_for(DIVERGENT_UNIT)
    assert "ABSTAINED" in evaluation.attribution
    assert evaluation.diverged is False


def test_a_recorded_nemotron_judgment_names_nemotron_and_the_served_model(
    nemotron_registry, tmp_path
):
    """The judgment record must name what ACTUALLY judged: the substrate id
    is "nemotron", and the model is read back from the response's own
    "model" field (what was served) rather than only the configured name."""
    def serve(url, body, timeout):
        return {
            "choices": [{"message": {"content":
                '{"diverged": true, "rationale": "all three", "confidence": 0.87}'}}],
            "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
        }

    judge = ModelJudge(
        endpoint=nemotron_registry.get("nemotron").endpoint,
        model=nemotron_registry.get("nemotron").model,
        cache_dir=tmp_path, seed_dir=None, transport=serve,
    )
    watch = GridWatch(client=MockThroughlineClient(), registry=nemotron_registry, judge=judge)

    proposal = None
    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            proposal = outcome["proposal"]
            break

    assert proposal is not None, "nemotron proposed nothing"
    assert proposal["substrate"] == "nemotron"
    judgment = watch.client.judgments[proposal["judgment_id"]]
    assert judgment["substrate"] == "nemotron"
    assert "nemotron" in judgment["substrate_label"].lower()
    assert "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning" in outcome["evaluation"]["attribution"]


def test_the_rule_pane_is_unchanged_when_the_rule_judges(watch):
    """No attribution, byte-identical to the wireframe."""
    for record in fixture():
        outcome = watch.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            evidence = outcome["proposal"]["evidence"]
            assert evidence.splitlines()[3].strip() == "→ DIVERGENCE"
            assert outcome["evaluation"]["attribution"] is None
            return
    pytest.fail("the rule produced no proposal")


# ------------------------------------------------------------------- the API

def test_substrate_health_is_probed_not_asserted(client):
    body = client.get("/substrates/cuopt/health").json()
    assert body["health"]["reachable"] is False
    assert "squatted stub" in body["health"]["detail"]

    assert client.get("/substrates/rule/health").json()["health"]["reachable"] is True
    assert client.get("/substrates/nope/health").status_code == 404


def test_the_registry_lists_all_four_substrates(client):
    substrates = {s["id"]: s for s in client.get("/substrates").json()["substrates"]}
    assert set(substrates) == {"rule", "dsv4", "nemotron", "cuopt"}
    assert substrates["dsv4"]["available"] is True
    assert substrates["dsv4"]["model"] == "deepseek-v4-flash-0731"
    assert substrates["nemotron"]["available"] is True
    assert substrates["nemotron"]["model"] == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    assert substrates["cuopt"]["available"] is False


@pytest.mark.skipif(
    os.environ.get("BREAKER_MODEL_LIVE") != "1",
    reason="set BREAKER_MODEL_LIVE=1 to hit the DGX Spark endpoint",
)
def test_the_live_endpoint_serves_the_model_we_name():
    health = ModelJudge().health()
    assert health["reachable"] is True
    assert health["model_served"] is True
