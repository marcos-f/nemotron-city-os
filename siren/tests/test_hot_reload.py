"""test://siren/atomic-reload-invalid-config — the refusal, and what survives it.

The refusal is the pitch. A config that marks an irreversible effect
auto_execute is refused whole: not partially applied, not applied with the
offending rule dropped, not warned about. What was running keeps running.
"""

from __future__ import annotations

import pytest

from siren import reload as reload_mod
from siren.substrate import MockSubstrate, SubstrateError
from tests.conftest import write_config


def test_valid_config_walks_the_timeline(tmp_path):
    path = write_config(tmp_path / "ok.yaml", auto_execute_irreversible=False)
    hot = reload_mod.HotReload()

    result = hot.drop(path, MockSubstrate(), emitted=3)

    assert result["state"] == "registered"
    assert [s["step"] for s in result["steps"]] == ["validated", "registered", "flowing"]
    assert [s["state"] for s in result["steps"]] == ["ok", "ok", "ok"]
    assert result["redeploy_required"] is False
    assert hot.registered_classes == ["fire.incident"]


def test_flowing_is_not_green_until_signals_actually_went_out(tmp_path):
    path = write_config(tmp_path / "ok.yaml", auto_execute_irreversible=False)
    result = reload_mod.HotReload().drop(path, MockSubstrate(), emitted=0)
    assert result["steps"][2]["state"] == "pending"


def test_invalid_config_is_refused_and_the_previous_one_keeps_flowing(tmp_path):
    good = write_config(tmp_path / "ok.yaml", auto_execute_irreversible=False)
    bad = write_config(tmp_path / "bad.yaml", auto_execute_irreversible=True)
    substrate = MockSubstrate()
    hot = reload_mod.HotReload()

    hot.drop(good, substrate, emitted=2)
    before = list(hot.registered_classes)
    active_before = hot.active_config

    result = hot.drop(bad, substrate)

    assert result["state"] == "refused"
    assert result["refusal"]["rule"] == "dispatch_units"
    assert result["refusal"]["policy"] == "no-auto-execute-on-irreversible"
    assert hot.registered_classes == before, "the running class list is untouched"
    assert hot.active_config is active_before, "the running config is the same object"
    assert substrate.reloads == [good], "the refused config never reached the substrate"
    assert [s["state"] for s in result["steps"]] == ["refused", "skipped", "skipped"]


def test_refusal_names_file_rule_and_line():
    with pytest.raises(reload_mod.ConfigRefusal) as caught:
        reload_mod.validate_config_file("config/incident.invalid.yaml")
    detail = str(caught.value)
    assert "config/incident.invalid.yaml:28" in detail
    assert "dispatch_units" in detail
    assert "no-auto-execute-on-irreversible" in detail


@pytest.mark.parametrize("text,policy", [
    ("effects: [{id: a, effect_type: x, reversibility_class: maybe}]",
     reload_mod.POLICY_UNKNOWN_CLASS),
    ("effects: [{id: a, reversibility_class: reversible}]",
     reload_mod.POLICY_RULE_SHAPE),
    ("version: 1", reload_mod.POLICY_RULE_SHAPE),
    ("effects: 3", reload_mod.POLICY_RULE_SHAPE),
    ("- a\n- b", reload_mod.POLICY_RULE_SHAPE),
    ("effects: [[1,2]]", reload_mod.POLICY_RULE_SHAPE),
    ("effects: []\nversion: one", reload_mod.POLICY_RULE_SHAPE),
    ("effects: [\n", reload_mod.POLICY_RULE_SHAPE),
])
def test_malformed_registries_are_refused_by_policy(text, policy):
    with pytest.raises(reload_mod.ConfigRefusal) as caught:
        reload_mod.validate_config_text(text, source="candidate.yaml")
    assert caught.value.policy == policy


def test_unreadable_config_is_refused_not_crashed():
    with pytest.raises(reload_mod.ConfigRefusal) as caught:
        reload_mod.validate_config_file("config/does-not-exist.yaml")
    assert "unreadable config" in caught.value.message


def test_substrate_refusal_is_reported_as_the_substrates(tmp_path):
    """When throughline is the one refusing, siren reports THAT verdict —
    with throughline named as its author — not its own pre-flight guess."""
    path = write_config(tmp_path / "ok.yaml", auto_execute_irreversible=False)

    class Refusing(MockSubstrate):
        def reload_config(self, path):
            raise SubstrateError("refused", status=422, body={
                "refused": True, "file": path, "rule": "dispatch_units", "line": 28,
                "policy": "no-auto-execute-on-irreversible",
                "detail": "throughline said no",
            })

    result = reload_mod.HotReload().drop(path, Refusing())

    assert result["state"] == "refused"
    assert result["refusal"]["refused_by"] == "throughline"
    assert result["steps"][1]["state"] == "refused"


def test_unreachable_substrate_is_an_error_not_a_silent_pass(tmp_path):
    path = write_config(tmp_path / "ok.yaml", auto_execute_irreversible=False)

    class Down(MockSubstrate):
        def reload_config(self, path):
            raise SubstrateError("throughline unreachable at http://127.0.0.1:8600")

    result = reload_mod.HotReload().drop(path, Down())

    assert result["state"] == "error"
    assert result["refusal"]["policy"] == "substrate-unreachable"
    assert result["steps"][2]["state"] == "skipped"


def test_idle_timeline_claims_nothing():
    timeline = reload_mod.HotReload().timeline()
    assert timeline["state"] == "idle"
    assert {s["state"] for s in timeline["steps"]} == {"pending"}


def test_config_endpoint_shows_the_artifact_before_the_drop(client):
    body = client.get("/config/incident").json()
    assert body["valid"] is True
    assert body["config"]["signal_classes"] == ["fire.incident"]


def test_config_endpoint_refuses_an_invalid_artifact(client, monkeypatch):
    monkeypatch.setenv("SIREN_INCIDENT_CONFIG", "config/incident.invalid.yaml")
    response = client.get("/config/incident")
    assert response.status_code == 422
    assert response.json()["valid"] is False
    assert response.json()["rule"] == "dispatch_units"
