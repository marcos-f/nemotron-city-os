"""test://throughline/verify-live and test://throughline/tamper-case.

The chain is only worth something if verification can fail, so the tamper
case is the load-bearing test here: corrupt one byte, verify must say so and
name the sequence number.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from throughline.ledger import GENESIS_PREV_HASH, Ledger, LedgerUnwritable


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.jsonl")


def test_first_entry_links_to_genesis(ledger: Ledger):
    entry = ledger.append("signal.ingested", {"id": "sig-1"})
    assert entry["seq"] == 1
    assert entry["prev_hash"] == GENESIS_PREV_HASH
    assert len(entry["sha256"]) == 64


def test_entries_chain_to_their_predecessor(ledger: Ledger):
    first = ledger.append("signal.ingested", {"id": "sig-1"})
    second = ledger.append("effect.proposed", {"id": "eff-1"})
    assert second["prev_hash"] == first["sha256"]
    assert second["seq"] == first["seq"] + 1


def test_verify_live(ledger: Ledger):
    """test://throughline/verify-live — verify passes over live appends."""
    for index in range(25):
        ledger.append("signal.ingested", {"id": f"sig-{index}", "n": index})
    result = ledger.verify()
    assert result == {"valid": True, "chain_length": 25, "broken_at": None, "detail": None}


def test_verify_live_over_a_reopened_ledger(tmp_path):
    """A restart resumes the chain rather than starting a second one."""
    path = tmp_path / "ledger.jsonl"
    Ledger(path).append("signal.ingested", {"id": "sig-1"})
    reopened = Ledger(path)
    entry = reopened.append("effect.proposed", {"id": "eff-1"})
    assert entry["seq"] == 2
    assert reopened.verify()["valid"] is True


def test_verify_empty_ledger_is_valid(ledger: Ledger):
    assert ledger.verify() == {
        "valid": True, "chain_length": 0, "broken_at": None, "detail": None,
    }


def test_tamper_case_names_seq(ledger: Ledger):
    """test://throughline/tamper-case — corrupt one byte, verify FAILS naming seq."""
    for index in range(5):
        ledger.append("signal.ingested", {"id": f"sig-{index}"})
    assert ledger.verify()["valid"] is True

    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    victim = json.loads(lines[2])
    assert victim["seq"] == 3
    # One byte: flip a single character of the recorded body.
    lines[2] = lines[2].replace('"sig-2"', '"sig-X"')
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = Ledger(ledger.path).verify()
    assert result["valid"] is False
    assert result["broken_at"] == "3"
    assert "sha256" in result["detail"]


def test_tamper_case_detects_a_rewritten_digest(ledger: Ledger):
    """Rewriting the digest too does not help: the link to seq 4 breaks."""
    for index in range(5):
        ledger.append("signal.ingested", {"id": f"sig-{index}"})
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[2])
    entry["body"]["id"] = "sig-X"
    from throughline.ledger import digest

    entry["sha256"] = digest(entry)
    lines[2] = json.dumps(entry, sort_keys=True, separators=(",", ":"))
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = Ledger(ledger.path).verify()
    assert result["valid"] is False
    assert result["broken_at"] == "4"
    assert "prev_hash" in result["detail"]


def test_tamper_case_detects_a_deleted_line(ledger: Ledger):
    for index in range(4):
        ledger.append("signal.ingested", {"id": f"sig-{index}"})
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    del lines[1]
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = Ledger(ledger.path).verify()
    assert result["valid"] is False
    assert result["broken_at"] == "2"


def test_tamper_case_detects_unparsable_line(ledger: Ledger):
    ledger.append("signal.ingested", {"id": "sig-1"})
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write("{not json at all}\n")
    result = Ledger(ledger.path).verify()
    assert result["valid"] is False
    assert result["broken_at"] == "2"


def test_ledger_fails_closed_when_the_path_is_unwritable(tmp_path):
    """An unwritable ledger raises — callers must not proceed to the effect.

    The obstruction is a path that cannot be a file rather than a permission
    bit, so the test means the same thing when CI runs as root.
    """
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("I am a file", encoding="utf-8")
    ledger = Ledger(blocker / "ledger.jsonl")
    with pytest.raises(LedgerUnwritable):
        ledger.append("effect.proposed", {"id": "eff-1"})


def test_ledger_refuses_to_start_on_an_unusable_path(tmp_path):
    """Unreadable at boot fails closed: never start a second chain."""
    directory = tmp_path / "ledger.jsonl"
    directory.mkdir()  # the ledger path is a directory: opening it must fail
    with pytest.raises(LedgerUnwritable):
        Ledger(directory)


def test_effects_do_not_execute_when_the_ledger_fails(client, monkeypatch):
    """Fail closed end to end: 503, and no effect ran."""

    def refuse(*args, **kwargs):
        raise LedgerUnwritable("simulated disk failure")

    monkeypatch.setattr(client.substrate.ledger, "append", refuse)
    response = client.post("/effects", json={
        "reversibility": "reversible", "status": "proposed",
        "effect_type": "notify.operator",
    })
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "ledger_unwritable"
    assert body["effect_executed"] is False


def test_verify_live_endpoint(client):
    """The contract path reports the same verdict as the library."""
    for index in range(3):
        response = client.post("/signals", json={
            "id": f"sig-{index}", "class": "demo.signal", "source": "pytest",
            "real_or_synthetic": "synthetic",
        })
        assert response.status_code == 201
    body = client.get("/ledger/verify").json()
    assert body["valid"] is True
    assert body["chain_length"] >= 3


def test_tamper_case_endpoint(client):
    """test://throughline/tamper-case over HTTP: /ledger/verify names the seq."""
    client.post("/signals", json={
        "id": "sig-1", "class": "demo.signal", "source": "pytest",
        "real_or_synthetic": "synthetic",
    })
    path = client.substrate.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    victim = next(i for i, line in enumerate(lines) if '"pytest"' in line)
    seq = json.loads(lines[victim])["seq"]
    lines[victim] = lines[victim].replace('"pytest"', '"forged"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    body = client.get("/ledger/verify").json()
    assert body["valid"] is False
    assert body["broken_at"] == str(seq)
    assert "altered" in body["detail"]


def test_two_instances_extend_one_chain(tmp_path):
    """A second Ledger on the same file must not fork the chain."""
    path = tmp_path / "ledger.jsonl"
    first, second = Ledger(path), Ledger(path)
    for index in range(5):
        first.append("signal.ingested", {"id": f"a-{index}"})
        second.append("signal.ingested", {"id": f"b-{index}"})

    result = Ledger(path).verify()
    assert result["valid"] is True
    assert result["chain_length"] == 10


def test_concurrent_processes_do_not_break_the_chain(tmp_path):
    """The failure that started this: two services, one ledger, forked seqs."""
    import subprocess
    import sys

    path = tmp_path / "ledger.jsonl"
    program = (
        "import sys; sys.path.insert(0, %r);"
        "from throughline.ledger import Ledger;"
        "l = Ledger(%r);"
        "[l.append('signal.ingested', {'id': f'{sys.argv[1]}-{i}'}) for i in range(20)]"
    ) % (str(Path(__file__).resolve().parents[1]), str(path))

    workers = [
        subprocess.Popen([sys.executable, "-c", program, f"w{n}"]) for n in range(4)
    ]
    for worker in workers:
        assert worker.wait(60) == 0

    result = Ledger(path).verify()
    assert result["valid"] is True, result
    assert result["chain_length"] == 80


def test_appending_onto_an_unreadable_tail_fails_closed(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append("signal.ingested", {"id": "sig-1"})
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{corrupt\n")

    with pytest.raises(LedgerUnwritable):
        Ledger(path).append("effect.proposed", {"id": "eff-1"})


def test_boot_raises_the_alarm_on_a_broken_chain(tmp_path):
    """A broken chain is announced at boot, never quietly repaired."""
    from throughline.service import Settings, Substrate

    settings = Settings(data_dir=tmp_path / "data", config_path=Path("config/effects.yaml"))
    first = Substrate(settings)
    first.ledger.append("signal.ingested", {"id": "sig-1"})

    lines = first.ledger.path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1].replace('"sig-1"', '"sig-X"')
    first.ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    second = Substrate(settings)
    assert second.boot_verification["valid"] is False
    alarms = [e for e in second.ledger.read() if e.get("type") == "ledger.alarm"]
    assert alarms and alarms[-1]["body"]["broken_at"] == second.boot_verification["broken_at"]
    # The break is still there — the alarm records it, it does not erase it.
    assert second.ledger.verify()["valid"] is False


def test_healthz_reports_a_broken_chain(client):
    client.post("/signals", json={
        "id": "sig-health", "class": "demo.signal", "source": "pytest",
        "real_or_synthetic": "synthetic"})
    assert client.get("/healthz").json()["ledger"]["valid"] is True
    path = client.substrate.ledger.path
    path.write_text(path.read_text(encoding="utf-8").replace("sig-health", "sig-forged"),
                    encoding="utf-8")
    assert client.get("/healthz").json()["ledger"]["valid"] is False
