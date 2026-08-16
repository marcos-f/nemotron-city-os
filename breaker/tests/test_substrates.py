"""The substrate contract: nemotron active, rule registered, cuOpt honest."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from breaker.substrates import (
    Substrate,
    SubstrateRegistry,
    SubstrateUnavailable,
    load_registry,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "substrates.yaml"
REPO_ROOT = CONFIG.parents[1]


def test_nemotron_is_the_active_substrate_by_default():
    """The bounty requirement: an NVIDIA model in the CONDITION slot, live.

    load_registry() with no override is what production actually boots —
    BreakerService and GridWatch both call it with no args. Before this, it
    defaulted to "rule", so live judgment.recorded traffic carried a
    deterministic rule while the product's claim was an LLM in the loop.
    """
    registry = load_registry(CONFIG)
    assert registry.active.id == "nemotron"
    assert registry.active.available is True
    assert registry.active.kind == "model-endpoint"
    assert registry.active.model == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"


def test_nemotron_is_registered_on_the_gb10_rung():
    registry = load_registry(CONFIG)
    nemotron = registry.get("nemotron")
    assert nemotron.endpoint == "http://dgx-spark.nemotron.example.com:8000/v1"
    assert nemotron.model == "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    assert nemotron.kind == "model-endpoint"
    assert nemotron.available is True


def test_dsv4_is_registered_but_not_active():
    """dsv4 is DeepSeek, not Nemotron — it must not be what the bounty sees."""
    registry = load_registry(CONFIG)
    assert registry.active.id != "dsv4"
    assert registry.get("dsv4").kind == "model-endpoint"


def test_the_rule_is_still_registered_and_selectable():
    """A deterministic control is genuinely valuable and stays available —
    it just is not the thing quietly doing all the work by default."""
    registry = load_registry(CONFIG)
    rule = registry.get("rule")
    assert rule.available is True
    assert rule.kind == "deterministic-rule"
    registry.select("rule")
    assert registry.active.id == "rule"


def test_cuopt_is_registered_as_an_unavailable_alternate():
    registry = load_registry(CONFIG)
    cuopt = registry.get("cuopt")
    assert cuopt.available is False
    assert cuopt.label == "registered alternate — unavailable without GPU"
    assert cuopt.requires["gpu"] is True


def test_selecting_cuopt_is_refused_rather_than_faked():
    registry = load_registry(CONFIG)
    with pytest.raises(SubstrateUnavailable, match="registered alternate"):
        registry.select("cuopt")
    assert registry.active.id == "nemotron"      # unchanged


def test_selecting_an_unknown_substrate_raises():
    with pytest.raises(KeyError):
        load_registry(CONFIG).select("magic")


def test_an_unavailable_substrate_cannot_be_made_active():
    with pytest.raises(SubstrateUnavailable):
        SubstrateRegistry(
            [Substrate(id="cuopt", kind="gpu-solver", available=False, label="no gpu")],
            active="cuopt",
        )


def test_cuopt_is_never_a_dependency():
    """Never install cuopt: GPU-only, ~10GB, and the PyPI name is a squatted stub."""
    for name in ("pyproject.toml", "requirements.txt"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8").lower()
        assert "cuopt" not in text, f"{name} must not depend on cuopt"


def test_no_import_of_cuopt_anywhere_in_the_package():
    for module in (REPO_ROOT / "breaker").glob("*.py"):
        source = module.read_text(encoding="utf-8")
        assert "import cuopt" not in source
        assert "from cuopt" not in source


def test_the_config_labels_the_alternate_wherever_it_names_it():
    """Honesty copy: cuOpt is never named without its unavailability."""
    document = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    entry = next(s for s in document["substrates"] if s["id"] == "cuopt")
    assert "unavailable" in entry["label"].lower()
    assert entry["available"] is False


def test_judgments_name_the_substrate_that_produced_them(held):
    """No screen can attribute the rule's verdict to a solver."""
    watch, proposal = held
    assert proposal.substrate == "rule"
    assert proposal.substrate_label == "deterministic rule"
    assert watch.client.judgments[proposal.judgment_id]["substrate"] == "rule"


def test_healthz_shows_the_registered_alternate_with_its_label(client):
    registered = client.get("/healthz").json()["substrate"]["registered"]
    cuopt = next(s for s in registered if s["id"] == "cuopt")
    assert cuopt["available"] is False
    assert "unavailable without GPU" in cuopt["label"]


def test_selecting_cuopt_over_http_is_a_409(client):
    response = client.post("/substrates/cuopt")
    assert response.status_code == 409
    assert "registered alternate" in response.json()["detail"]
    assert client.get("/substrates").json()["active"] == "rule"
