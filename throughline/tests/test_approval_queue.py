"""test://throughline/hold-release — held effects wait for a human, forever.

The queue's defining property is negative: there is no timeout path. That is
asserted against the source below, so it cannot be reintroduced quietly.
"""

from __future__ import annotations

import io
import re
import threading
import tokenize
from pathlib import Path

import pytest
from pydantic import ValidationError

from throughline.approvals import AlreadyDecided, ApprovalNotFound, ApprovalQueue
from throughline.models import Decision
from throughline.service import Settings, Substrate

PACKAGE = Path(__file__).resolve().parents[1] / "throughline"

IRREVERSIBLE = {
    "id": "eff-hold", "reversibility": "irreversible", "status": "proposed",
    "effect_type": "payment.release", "description": "release $40k",
    "signal_id": "sig-hold",
}

HOLD_SIGNAL = {
    "id": "sig-hold", "class": "fire.incident", "source": "test",
    "real_or_synthetic": "real",
}


def test_hold_release(client):
    """test://throughline/hold-release — held, then executed only on release."""
    client.post("/signals", json=HOLD_SIGNAL)
    proposed = client.post("/effects", json=IRREVERSIBLE)
    assert proposed.status_code == 202
    effect = proposed.json()
    assert effect["status"] == "queued"
    assert effect["reversibility_class"] == "irreversible"

    # It is held. Nothing executes it while it waits.
    assert client.get(f"/effects/{effect['id']}").json()["status"] == "queued"
    ledger_types = [e["type"] for e in client.get("/ledger").json()["entries"]]
    assert "effect.executed" not in ledger_types

    approval = client.get("/approvals").json()["approvals"][0]
    assert approval["state"] == "pending"
    assert approval["effect_id"] == effect["id"]

    decided = client.post(f"/approvals/{approval['id']}/decide", json={
        "decision": "approve", "decided_by": "oidc|operator-7", "caller_role": "human",
        "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc",
        "rationale": "verified",
    })
    assert decided.status_code == 200
    assert decided.json()["effect"]["status"] == "executed"
    assert client.get(f"/effects/{effect['id']}").json()["status"] == "executed"

    # The release is provable: decision then execution, in that order, chained.
    entries = client.get("/ledger").json()["entries"]
    types = [e["type"] for e in entries]
    assert types.index("approval.decided") < types.index("effect.executed")
    decision_entry = next(e for e in entries if e["type"] == "approval.decided")
    assert decision_entry["body"]["decided_by"] == "oidc|operator-7"
    assert client.get("/ledger/verify").json()["valid"] is True


def test_rejected_approval_never_executes(client):
    client.post("/signals", json=HOLD_SIGNAL)
    effect = client.post("/effects", json=IRREVERSIBLE).json()
    approval = client.get("/approvals").json()["approvals"][0]
    client.post(f"/approvals/{approval['id']}/decide", json={
        "decision": "reject", "decided_by": "oidc|operator-7", "caller_role": "human"})
    assert client.get(f"/effects/{effect['id']}").json()["status"] == "rejected"
    types = [e["type"] for e in client.get("/ledger").json()["entries"]]
    assert "effect.executed" not in types


def test_decision_without_a_subject_is_rejected_at_the_schema_level():
    """An approval without a subject is not an approval."""
    with pytest.raises(ValidationError):
        Decision(decision="approve")
    with pytest.raises(ValidationError):
        Decision(decision="approve", decided_by="")


def test_decide_endpoint_requires_a_subject(client):
    client.post("/signals", json=HOLD_SIGNAL)
    client.post("/effects", json=IRREVERSIBLE)
    approval = client.get("/approvals").json()["approvals"][0]
    for body in ({"decision": "approve"}, {"decision": "approve", "decided_by": ""}):
        response = client.post(f"/approvals/{approval['id']}/decide", json=body)
        assert response.status_code == 422, body
    assert client.get("/approvals").json()["approvals"][0]["state"] == "pending"


def test_deciding_twice_is_a_conflict(client):
    client.post("/signals", json=HOLD_SIGNAL)
    client.post("/effects", json=IRREVERSIBLE)
    approval = client.get("/approvals").json()["approvals"][0]
    payload = {"decision": "approve", "decided_by": "oidc|operator-7",
               "caller_role": "human",
               "issuer": "https://git.nemotron.example.com", "auth_mode": "oidc"}
    assert client.post(f"/approvals/{approval['id']}/decide", json=payload).status_code == 200
    assert client.post(f"/approvals/{approval['id']}/decide", json=payload).status_code == 409


def test_queue_is_durable_across_a_restart(tmp_path):
    """A restart mid-wait resumes with the same pending items."""
    settings = Settings(data_dir=tmp_path / "data",
                        config_path=Path("config/effects.yaml"))
    first = Substrate(settings)
    queue_path = first.queue.path
    approval = first.queue.enqueue("eff-1", "irreversible", effect_type="payment.release")

    reopened = ApprovalQueue(queue_path)
    restored = reopened.get(approval.id)
    assert restored.state == "pending"
    assert restored.effect_id == "eff-1"

    reopened.decide(approval.id, "approve", "oidc|operator-7")
    assert ApprovalQueue(queue_path).get(approval.id).state == "approved"


def test_wait_blocks_until_a_human_decides(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    approval = queue.enqueue("eff-1", "irreversible")
    released = threading.Event()

    def waiter():
        queue.wait(approval.id)
        released.set()

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    # Nothing releases it on its own — no deadline exists to expire.
    assert not released.wait(0.4)

    queue.decide(approval.id, "approve", "oidc|operator-7")
    assert released.wait(5)
    thread.join(5)
    assert queue.wait(approval.id).state == "approved"


def test_wait_returns_immediately_once_decided(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    approval = queue.enqueue("eff-1", "irreversible")
    queue.decide(approval.id, "reject", "oidc|operator-7", "not today")
    assert queue.wait(approval.id).state == "rejected"


def test_unknown_approval_raises(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    with pytest.raises(ApprovalNotFound):
        queue.get("apr-nope")


def test_decide_requires_a_subject_at_the_queue_too(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    approval = queue.enqueue("eff-1", "irreversible")
    with pytest.raises(ValueError):
        queue.decide(approval.id, "approve", "")


def test_already_decided_raises(tmp_path):
    queue = ApprovalQueue(tmp_path / "approvals.json")
    approval = queue.enqueue("eff-1", "irreversible")
    queue.decide(approval.id, "approve", "oidc|operator-7")
    with pytest.raises(AlreadyDecided):
        queue.decide(approval.id, "approve", "oidc|operator-7")


def test_a_malformed_persisted_row_does_not_take_the_queue_down(tmp_path):
    path = tmp_path / "approvals.json"
    path.write_text('[{"id": "apr-1"}]', encoding="utf-8")
    assert ApprovalQueue(path).all() == []
    path.write_text("not json", encoding="utf-8")
    assert ApprovalQueue(path).all() == []


def test_no_timeout_code_path_exists():
    """A queue that drains itself is an auto-executor with extra steps.

    Fails if any timeout, deadline, expiry or auto-approval mechanism appears
    on the hold path.
    """
    banned = re.compile(
        r"^(timeout|deadline|expires?|expiry|expire_at|ttl|auto_approve|"
        r"auto_release|max_wait|sleep)$",
        re.IGNORECASE,
    )
    offenders = []
    for module in ("approvals.py", "gate.py"):
        source = (PACKAGE / module).read_text(encoding="utf-8")
        # Tokenize so prose about not having a timeout is not mistaken for one.
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.NAME and banned.match(token.string):
                offenders.append(f"{module}:{token.start[0]}: {token.line.strip()}")
    assert not offenders, "timeout path on the hold path: " + "; ".join(offenders)


def test_wait_takes_no_timeout_argument():
    import inspect

    signature = inspect.signature(ApprovalQueue.wait)
    assert list(signature.parameters) == ["self", "approval_id"]
