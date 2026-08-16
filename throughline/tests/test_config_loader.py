"""test://throughline/refusal-named — the loader refuses, and names names.

Covers UC-006: refusal-named, no-bypass and atomic-reload.
"""

from __future__ import annotations

import pytest

from throughline.config import (
    POLICY_IRREVERSIBLE_AUTO_EXECUTE,
    POLICY_RULE_SHAPE,
    POLICY_UNKNOWN_CLASS,
    ConfigRefusal,
    ConfigStore,
    load_config,
    parse_config,
)
from throughline.ledger import Ledger

from conftest import BAD_CONFIG, GOOD_CONFIG

UNKNOWN_CLASS = BAD_CONFIG.parent / "unknown-class.yaml"


def test_good_config_loads(tmp_path):
    config = load_config(GOOD_CONFIG)
    assert {rule.effect_type for rule in config.rules} >= {
        "notify.operator", "payment.release"
    }
    assert config.by_effect_type["payment.release"].reversibility_class == "irreversible"
    assert config.by_effect_type["payment.release"].auto_execute is False


def test_refusal_named(tmp_path):
    """test://throughline/refusal-named — file, rule id and line, by name."""
    with pytest.raises(ConfigRefusal) as excinfo:
        load_config(BAD_CONFIG)

    refusal = excinfo.value
    assert refusal.file == str(BAD_CONFIG)
    assert refusal.rule == "RULE-666"
    assert refusal.line == 10  # the `auto_execute: true` line itself
    assert refusal.policy == POLICY_IRREVERSIBLE_AUTO_EXECUTE
    assert "irreversible" in refusal.message

    # The named coordinates survive into the wire payload the UI renders.
    payload = refusal.as_dict()
    assert payload["refused"] is True
    assert payload["rule"] == "RULE-666"
    assert payload["line"] == 10
    assert str(BAD_CONFIG) in payload["detail"]


def test_refusal_rejects_the_whole_config_not_just_the_bad_rule(tmp_path):
    """The safe rule in the same file does not survive the refusal."""
    with pytest.raises(ConfigRefusal):
        load_config(BAD_CONFIG)
    store = ConfigStore()
    with pytest.raises(ConfigRefusal):
        store.reload(BAD_CONFIG)
    assert store.current.rules == []  # nothing from that file became active


def test_refusal_is_ledgered(tmp_path):
    """The refusal is a ledger event, not just a log line."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = ConfigStore(ledger=ledger)
    with pytest.raises(ConfigRefusal):
        store.reload(BAD_CONFIG)

    refusals = [e for e in ledger.read() if e["type"] == "config.refused"]
    assert len(refusals) == 1
    body = refusals[0]["body"]
    assert body["rule"] == "RULE-666"
    assert body["line"] == 10
    assert body["policy"] == POLICY_IRREVERSIBLE_AUTO_EXECUTE
    assert ledger.verify()["valid"] is True


def test_atomic_reload_leaves_the_previous_config_untouched(tmp_path):
    """scenario://throughline/atomic-reload — all or nothing."""
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = ConfigStore(ledger=ledger)
    good = store.reload(GOOD_CONFIG)
    before = [rule.as_dict() for rule in store.current.rules]

    with pytest.raises(ConfigRefusal):
        store.reload(BAD_CONFIG)

    assert store.current is good
    assert [rule.as_dict() for rule in store.current.rules] == before


def test_unknown_reversibility_class_is_refused():
    with pytest.raises(ConfigRefusal) as excinfo:
        load_config(UNKNOWN_CLASS)
    assert excinfo.value.policy == POLICY_UNKNOWN_CLASS
    assert excinfo.value.rule == "RULE-777"
    assert excinfo.value.line == 5


def test_valid_yaml_that_violates_the_registry_is_refused():
    """Valid YAML is not the bar; the effect registry is."""
    text = "version: 1\neffects:\n  - id: RULE-010\n    reversibility_class: reversible\n"
    with pytest.raises(ConfigRefusal) as excinfo:
        parse_config(text, source="inline.yaml")
    assert excinfo.value.policy == POLICY_RULE_SHAPE
    assert excinfo.value.rule == "RULE-010"


def test_malformed_yaml_is_refused():
    with pytest.raises(ConfigRefusal) as excinfo:
        parse_config("effects: [oops\n", source="broken.yaml")
    assert "not valid YAML" in excinfo.value.message


def test_unreadable_config_is_refused(tmp_path):
    with pytest.raises(ConfigRefusal):
        load_config(tmp_path / "absent.yaml")


def test_effects_must_be_a_list():
    with pytest.raises(ConfigRefusal):
        parse_config("version: 1\neffects: nope\n", source="inline.yaml")


def test_version_must_be_an_integer():
    with pytest.raises(ConfigRefusal):
        parse_config("version: 'one'\neffects: []\n", source="inline.yaml")


def test_reload_endpoint_names_the_refusal_and_keeps_running(client):
    """The API surface carries the same names to the screen."""
    before = client.get("/config").json()["config"]
    response = client.post("/config/reload", json={"path": str(BAD_CONFIG)})

    assert response.status_code == 422
    body = response.json()
    assert body["refused"] is True
    assert body["rule"] == "RULE-666"
    assert body["line"] == 10
    assert body["file"] == str(BAD_CONFIG)
    assert "previous configuration still running" in body["message_ui"]
    # ...and the previously loaded rules are still the active ones.
    assert body["active_config"] == before
    assert client.get("/config").json()["config"] == before


def test_reload_endpoint_accepts_a_good_config(client):
    response = client.post("/config/reload", json={"path": str(GOOD_CONFIG)})
    assert response.status_code == 200
    assert response.json()["refused"] is False
    assert client.get("/healthz").json()["config"]["refusal"] is None


def test_boot_refusal_runs_with_no_rules(tmp_path):
    """Refused at boot => zero rules => every effect type is irreversible."""
    from throughline.service import Settings, Substrate

    substrate = Substrate(Settings(data_dir=tmp_path / "data", config_path=BAD_CONFIG))
    assert substrate.boot_refusal["rule"] == "RULE-666"
    assert substrate.config.rules == []
