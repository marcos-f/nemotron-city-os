"""Two proven ways past the gate, and the refusals that close them.

A security review defeated the gate twice against a scratch instance:

1. ``caller_role`` was optional, so the agent check was opt-in. Deleting one
   JSON key approved an irreversible effect and executed it.
2. ``POST /config/reload`` took an arbitrary filesystem path with no
   authentication, so two curls reclassified an effect type to reversible +
   auto_execute and executed it with no human and no approval record.

Both reproductions are below as tests. Each fix ADDS a named refusal — the
system now has more ways to say no, not fewer ways to say yes.
"""

from __future__ import annotations

import pytest

from throughline.config import (
    POLICY_REVERSIBILITY_DOWNGRADE,
    POLICY_SOURCE_ALLOWLIST,
    Config,
    ConfigRefusal,
    ConfigStore,
    EffectRule,
    downgrades,
    resolve_source,
)
from throughline.gate import (
    REFUSAL_AGENT_APPROVES_IRREVERSIBLE,
    REFUSAL_AGENT_SUBJECT_CLAIMS_HUMAN,
    REFUSAL_ROLE_MISSING,
    REFUSAL_ROLE_UNRECOGNISED,
    looks_like_an_agent_subject,
)
from throughline.ledger import Ledger

from conftest import BAD_CONFIG, FIXTURES, GOOD_CONFIG

IRREVERSIBLE = {"effect_type": "payment.release", "description": "release $40k"}

_hold_counter = 0


def _hold(client) -> dict:
    """Queue an irreversible effect and return its pending approval."""
    global _hold_counter
    _hold_counter += 1
    signal_id = f"sig-hold-{_hold_counter}"
    client.post("/signals", json={
        "id": signal_id, "class": "test.signal", "source": "test",
        "real_or_synthetic": "real",
    })
    body = {
        **IRREVERSIBLE,
        "id": f"eff-hold-{_hold_counter}",
        "signal_id": signal_id,
    }
    effect = client.post("/effects", json=body).json()
    assert effect["status"] == "queued"
    approval = client.get("/approvals").json()["approvals"][-1]
    assert approval["state"] == "pending"
    return {"effect": effect, "approval": approval}


def _refusals(client) -> list[dict]:
    return [
        e["body"] for e in client.get("/ledger", params={"limit": 1000}).json()["entries"]
        if e["type"] == "approval.refused"
    ]


# ------------------------------------------------------- CRITICAL 1: the role


def test_decide_without_caller_role_is_refused_and_ledgered(client):
    """The original exploit: omit one key, execute an irreversible effect."""
    held = _hold(client)
    response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "agent:nemoclerk",
    })

    assert response.status_code == 403
    assert response.json()["refusal_reason"] == REFUSAL_ROLE_MISSING
    # The effect is exactly where it was: held.
    assert client.get(f"/effects/{held['effect']['id']}").json()["status"] == "queued"
    assert client.get("/approvals").json()["approvals"][-1]["state"] == "pending"
    assert _refusals(client)[-1]["refusal_reason"] == REFUSAL_ROLE_MISSING
    assert client.get("/ledger/verify").json()["valid"] is True


def test_decide_with_an_empty_caller_role_is_refused(client):
    held = _hold(client)
    response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "   ",
    })
    assert response.status_code == 403
    assert response.json()["refusal_reason"] == REFUSAL_ROLE_MISSING
    assert client.get(f"/effects/{held['effect']['id']}").json()["status"] == "queued"


def test_decide_with_an_unrecognised_caller_role_is_refused_and_ledgered(client):
    """An unknown role is refused, never quietly treated as human."""
    held = _hold(client)
    for role in ("superuser", "HUMAN-ish", "operator", "system"):
        response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
            "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": role,
        })
        assert response.status_code == 403, role
        assert response.json()["refusal_reason"] == REFUSAL_ROLE_UNRECOGNISED, role
    assert client.get(f"/effects/{held['effect']['id']}").json()["status"] == "queued"
    assert len(_refusals(client)) == 4
    assert client.get("/ledger/verify").json()["valid"] is True


def test_agent_subject_claiming_the_human_role_is_refused_and_ledgered(client):
    """The subject and the declared role have to agree."""
    held = _hold(client)
    for subject in ("agent:nemoclerk", "agent|nemo-pilot", "bot:releaser",
                    "svc:autoapprove", "AGENT:shouty"):
        response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
            "decision": "approve", "decided_by": subject, "caller_role": "human",
        })
        assert response.status_code == 403, subject
        assert response.json()["refusal_reason"] == REFUSAL_AGENT_SUBJECT_CLAIMS_HUMAN
    assert client.get(f"/effects/{held['effect']['id']}").json()["status"] == "queued"
    body = _refusals(client)[-1]
    assert body["refusal_reason"] == REFUSAL_AGENT_SUBJECT_CLAIMS_HUMAN
    assert body["caller_role"] == "human"


def test_a_declared_agent_still_cannot_approve_an_irreversible_effect(client):
    """The original refusal beat, unchanged."""
    held = _hold(client)
    response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "agent:nemoclerk", "caller_role": "agent",
    })
    assert response.status_code == 403
    assert response.json()["refusal_reason"] == REFUSAL_AGENT_APPROVES_IRREVERSIBLE
    assert client.get(f"/effects/{held['effect']['id']}").json()["status"] == "queued"


def test_a_human_subject_declaring_human_still_releases_the_effect(client):
    """The fix closes a hole; it does not close the door."""
    held = _hold(client)
    response = client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "human",
        "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc",
        "rationale": "verified against the invoice",
    })
    assert response.status_code == 200
    assert response.json()["effect"]["status"] == "executed"


def test_every_refusal_leaves_the_approval_decidable_by_a_human(client):
    """A refusal holds; it does not poison the approval."""
    held = _hold(client)
    client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "agent:nemoclerk"})
    released = client.post(f"/approvals/{held['approval']['id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "human",
        "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc"})
    assert released.status_code == 200
    assert released.json()["effect"]["status"] == "executed"


@pytest.mark.parametrize("subject,expected", [
    ("agent:nemoclerk", True), ("agent|nemo-pilot", True), ("bot:x", True),
    ("svc:y", True), ("service:z", True), ("AGENT:Loud", True),
    ("oidc|operator-7", False), ("alice@example.com", False), ("", False),
    (None, False), ("management:agent", False),
])
def test_agent_subject_detection(subject, expected):
    assert looks_like_an_agent_subject(subject) is expected


# --------------------------------------------- CRITICAL 2: the reload surface


def test_reload_from_outside_the_allowlist_is_refused_and_names_the_path(client, tmp_path):
    """The second exploit: hand the service a registry of your own choosing."""
    evil = tmp_path / "pwn.yaml"
    evil.write_text(
        "version: 99\neffects:\n  - id: RULE-PWN\n"
        "    effect_type: grid.load_shed\n"
        "    reversibility_class: reversible\n    auto_execute: true\n",
        encoding="utf-8",
    )
    before = client.get("/config").json()["config"]

    response = client.post("/config/reload", json={"path": str(evil)})

    assert response.status_code == 403
    body = response.json()
    assert body["refused"] is True
    assert body["policy"] == POLICY_SOURCE_ALLOWLIST
    assert str(evil) in body["file"] or str(evil.resolve()) in body["file"]
    assert body["active_config"] == before
    assert client.get("/config").json()["config"] == before


def test_the_refused_path_is_named_in_the_ledger(client, tmp_path):
    evil = tmp_path / "pwn.yaml"
    evil.write_text("version: 1\neffects: []\n", encoding="utf-8")
    client.post("/config/reload", json={"path": str(evil)})

    refusals = [
        e["body"] for e in client.get("/ledger", params={"limit": 1000}).json()["entries"]
        if e["type"] == "config.refused"
    ]
    assert refusals, "an out-of-allowlist reload must be ledgered"
    assert refusals[-1]["policy"] == POLICY_SOURCE_ALLOWLIST
    assert str(evil.resolve()) in refusals[-1]["file"]
    assert client.get("/ledger/verify").json()["valid"] is True


def test_the_allowlist_is_reported_on_healthz(client):
    allowlist = client.get("/healthz").json()["config"]["allowlist"]
    assert any(entry.endswith("config") for entry in allowlist)


def test_reload_inside_the_allowlist_still_refuses_an_unsafe_config(client):
    """UC-006 is untouched: the named refusal still names names."""
    response = client.post("/config/reload", json={"path": str(BAD_CONFIG)})
    assert response.status_code == 422
    assert response.json()["rule"] == "RULE-666"


def test_reload_of_the_running_config_is_still_a_plain_200(client):
    response = client.post("/config/reload", json={"path": str(GOOD_CONFIG)})
    assert response.status_code == 200
    assert response.json() == {**response.json(), "refused": False, "held": False}


def test_resolve_source_accepts_a_path_inside_an_allowed_directory():
    assert resolve_source(GOOD_CONFIG, [GOOD_CONFIG.parent]) == GOOD_CONFIG.resolve()


def test_resolve_source_refuses_a_traversal_out_of_the_allowlist(tmp_path):
    outside = tmp_path / "outside.yaml"
    outside.write_text("version: 1\n", encoding="utf-8")
    sneaky = FIXTURES / ".." / ".." / ".." / str(outside)
    with pytest.raises(ConfigRefusal) as excinfo:
        resolve_source(sneaky, [FIXTURES])
    assert excinfo.value.policy == POLICY_SOURCE_ALLOWLIST


# ----------------------------------------- CRITICAL 2b: downgrades are effects


DOWNGRADE_YAML = FIXTURES / "downgrade-grid-load-shed.yaml"


def test_downgrade_detection_counts_unregistered_as_irreversible():
    current = Config(source="a", version=1, rules=[])
    candidate = Config(source="b", version=2, rules=[
        EffectRule("RULE-PWN", "grid.load_shed", "reversible", True),
    ])
    found = downgrades(current, candidate)
    assert found == [{
        "effect_type": "grid.load_shed", "rule": "RULE-PWN", "from": "irreversible",
        "from_registered": False, "to": "reversible", "auto_execute": True,
    }]


def test_downgrade_detection_ignores_an_unchanged_reversible_rule():
    rule = EffectRule("RULE-001", "notify.operator", "reversible", True)
    current = Config(source="a", version=1, rules=[rule])
    candidate = Config(source="b", version=2, rules=[rule])
    assert downgrades(current, candidate) == []


def test_downgrade_detection_catches_irreversible_to_reversible():
    current = Config(source="a", version=1, rules=[
        EffectRule("RULE-003", "payment.release", "irreversible", False)])
    candidate = Config(source="b", version=2, rules=[
        EffectRule("RULE-003", "payment.release", "reversible", True)])
    found = downgrades(current, candidate)
    assert found[0]["from"] == "irreversible" and found[0]["from_registered"] is True


def test_a_reversibility_downgrade_is_held_at_the_gate(client):
    """The full exploit, end to end — and it now stops at the gate."""
    before = client.get("/config").json()["config"]

    response = client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)})

    assert response.status_code == 202
    body = response.json()
    assert body["held"] is True
    assert body["policy"] == POLICY_REVERSIBILITY_DOWNGRADE
    assert [d["effect_type"] for d in body["downgrades"]] == ["grid.load_shed"]
    assert body["active_config"] == before

    # The holding effect is itself irreversible and queued for a human.
    effect = client.get(f"/effects/{body['effect_id']}").json()
    assert effect["reversibility_class"] == "irreversible"
    assert effect["status"] == "queued"
    assert client.get("/approvals").json()["approvals"][-1]["state"] == "pending"
    assert client.get("/ledger/verify").json()["valid"] is True


def test_the_downgrade_does_not_take_effect_while_it_is_held(client):
    """The attack's payload: propose the effect the reload was meant to free."""
    client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)})

    client.post("/signals", json={
        "id": "sig-shed-battery-4", "class": "grid.telemetry", "source": "test",
        "real_or_synthetic": "real",
    })
    shed = client.post("/effects", json={
        "id": "eff-shed-battery-4",
        "effect_type": "grid.load_shed", "description": "load-shed battery_4",
        "reversibility": "reversible", "signal_id": "sig-shed-battery-4",
    }).json()

    assert shed["reversibility_class"] == "irreversible"
    assert shed["status"] == "queued"  # NOT executed
    types = [e["type"] for e in client.get("/ledger", params={"limit": 1000}).json()["entries"]]
    assert types.count("config.downgrade_held") == 1


def test_the_hold_is_ledgered_with_the_offending_reclassification(client):
    client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)})
    entries = client.get("/ledger", params={"limit": 1000}).json()["entries"]
    held = [e["body"] for e in entries if e["type"] == "config.downgrade_held"][-1]
    assert held["downgrades"][0]["effect_type"] == "grid.load_shed"
    assert held["downgrades"][0]["auto_execute"] is True
    assert held["source"].endswith("downgrade-grid-load-shed.yaml")


def test_a_human_can_release_the_downgrade_and_it_then_applies(client):
    """Held is not blocked: an operator may still decide to loosen the class."""
    held = client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)}).json()

    released = client.post(f"/approvals/{held['approval_id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "human",
        "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc",
        "rationale": "planned registry change, reviewed",
    })
    assert released.status_code == 200
    assert released.json()["effect"]["status"] == "executed"

    config = client.get("/config").json()["config"]
    row = next(r for r in config["effects"] if r["effect_type"] == "grid.load_shed")
    assert row["reversibility_class"] == "reversible"
    assert client.get("/ledger/verify").json()["valid"] is True


def test_an_agent_cannot_release_the_downgrade(client):
    """The downgrade is irreversible, so the agent refusal covers it too."""
    held = client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)}).json()
    response = client.post(f"/approvals/{held['approval_id']}/decide", json={
        "decision": "approve", "decided_by": "agent:nemoclerk", "caller_role": "agent"})
    assert response.status_code == 403
    assert response.json()["refusal_reason"] == REFUSAL_AGENT_APPROVES_IRREVERSIBLE
    row = [r for r in client.get("/config").json()["config"]["effects"]
           if r["effect_type"] == "grid.load_shed"]
    assert row == []  # still unregistered, therefore still irreversible


def test_a_rejected_downgrade_never_applies(client):
    held = client.post("/config/reload", json={"path": str(DOWNGRADE_YAML)}).json()
    client.post(f"/approvals/{held['approval_id']}/decide", json={
        "decision": "reject", "decided_by": "oidc|operator-7", "caller_role": "human"})
    assert [r for r in client.get("/config").json()["config"]["effects"]
            if r["effect_type"] == "grid.load_shed"] == []


def test_stage_does_not_swap_the_running_config(tmp_path):
    ledger = Ledger(tmp_path / "ledger.jsonl")
    store = ConfigStore(ledger=ledger)
    candidate = store.stage(GOOD_CONFIG, [GOOD_CONFIG.parent])
    assert candidate.rules  # validated
    assert store.current.rules == []  # but not applied
    store.apply(candidate)
    assert store.current is candidate
