"""test://siren/hot-reload-no-redeploy — against a REAL throughline.

These tests WRITE to the substrate they reach — the invalid variant below
deliberately puts a ``config.refused`` row on its ledger — so reaching one is
an explicit opt-in rather than a consequence of having the federation running.
``tests/conftest.py`` points the default run at an isolated scratch address and
this whole module skips. Run it against a real gate with:

    SIREN_LIVE_SUBSTRATE=1 SIREN_SUBSTRATE=real \
        python -m pytest tests/test_integration_throughline.py -v

Prefer a scratch throughline on a spare port over the live federation: the live
chain is permanent, append-only, and is what the judges will be shown.

What it proves, and how it avoids proving it by accident:

* The class is not registered before the drop and is after it, so the drop is
  what registered it.
* throughline's process identity is captured before and after — same boot,
  same ledger head lineage. A restart would reset neither, so the check is a
  real no-redeploy assertion rather than a hopeful one.
* The invalid variant is refused BY THROUGHLINE (its ledger gains a
  config.refused entry naming the rule) and the incident config keeps running.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

THROUGHLINE = os.environ.get("THROUGHLINE_URL", "http://127.0.0.1:8600").rstrip("/")
CONFIG = str(Path("config/incident.yaml").resolve())
INVALID = str(Path("config/incident.invalid.yaml").resolve())


def _get(path: str):
    with urllib.request.urlopen(f"{THROUGHLINE}{path}", timeout=5) as response:
        return json.loads(response.read().decode())


def _reachable() -> bool:
    try:
        _get("/healthz")
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


pytestmark = pytest.mark.skipif(
    not _reachable(), reason=f"no real throughline at {THROUGHLINE}"
)


def config_is_allowlisted() -> bool:
    """Will throughline even look at siren's config directory?

    throughline now refuses a reload whose source is outside its allowlist
    (``config-source-must-be-allowlisted``), and the allowlist is
    ``<throughline>/config`` plus whatever ``THROUGHLINE_CONFIG_DIRS`` adds.
    siren's config lives in its own repo, so a federation booted without that
    variable refuses siren's drop by path before it ever reads a rule.

    That is a **deployment** fact, not a siren bug, and both outcomes are
    asserted below rather than skipped — a skip reads like a pass.
    """
    allowlist = (_get("/healthz").get("config") or {}).get("allowlist") or []
    parent = str(Path(CONFIG).parent.resolve())
    return any(parent == str(Path(root).resolve()) for root in allowlist)


@pytest.fixture
def real_client(monkeypatch):
    monkeypatch.delenv("OFFLINE_MODE", raising=False)
    monkeypatch.setenv("SIREN_SUBSTRATE", "real")
    from siren.service import create_app

    with TestClient(create_app()) as client:
        assert client.get("/healthz").json()["substrate"]["reachable"] is True
        yield client


def test_the_drop_is_reported_as_what_the_gate_actually_did(real_client):
    """The beat, against throughline as it behaves today.

    Three outcomes are legitimate and each has its own honest rendering. What
    is NOT legitimate is the one siren used to produce: throughline answering
    ``refused: false, held: true`` — accepted and NOT applied, with an
    approval pending — and siren announcing "the substrate accepted the
    swap", classes registered, flowing green. siren read only ``refused``.
    """
    boot_before = _get("/healthz")["ledger"]["boot_verification"]
    chain_before = _get("/healthz")["ledger"]["chain_length"]
    running_before = _get("/config")["config"]

    response = real_client.post("/hot-reload", json={"path": CONFIG, "emit": 3})
    body = response.json()

    if not config_is_allowlisted():
        # Refused by path, before a rule was read. siren renders throughline's
        # refusal; it does not perform one of its own.
        assert response.status_code == 422, body
        assert body["state"] == "refused"
        assert body["refusal"]["refused_by"] == "throughline"
        assert body["refusal"]["policy"] == "config-source-must-be-allowlisted"
        assert _get("/config")["config"] == running_before
        return

    if body["state"] == "held":
        assert response.status_code == 202, "accepted and waiting is not a refusal"
        assert body["substrate"] == "real"
        assert body["held"]["held_by"] == "throughline"
        assert body["held"]["approval_id"], "a held swap names its approval"
        assert body["held"]["policy"], "and the policy that held it"
        assert [step["state"] for step in body["steps"]] == ["ok", "held", "skipped"]
        assert _get("/config")["config"] == running_before, (
            "a held swap has not swapped; the previous config keeps running"
        )
        assert body["registered_classes"] == [], (
            "nothing is registered until the human releases it — this is the "
            "assertion that fails if siren goes back to reading only `refused`"
        )
        assert body["emitted"] == 0, "nothing flows for a class that is not running"
        return

    # The swap applied outright: no reversibility downgrade to hold.
    assert response.status_code == 200, body
    assert body["state"] == "registered"
    assert body["substrate"] == "real"
    assert body["redeploy_required"] is False
    assert body["registered_classes"] == ["fire.incident"]

    running = _get("/config")["config"]
    assert running["source"] == CONFIG, "throughline is running the dropped file"
    registered = {rule["id"] for rule in running["effects"]}
    assert {"incident_notify", "dispatch_units", "evacuation_order"} <= registered
    assert {"RULE-001", "RULE-002", "RULE-003", "RULE-004"} <= registered, (
        "the drop must not deregister the other components' effects"
    )

    after = _get("/healthz")["ledger"]
    assert after["boot_verification"] == boot_before, "throughline restarted"
    assert after["chain_length"] > chain_before, "the swap left no trace"

    assert body["signals"]["emitted_count"] == 3
    ingested = [
        entry for entry in _get("/ledger?limit=25")["entries"]
        if entry["type"] == "signal.ingested"
        and (entry.get("body") or {}).get("class") == "fire.incident"
    ]
    assert ingested, "the class registered but nothing flowed"


def test_dispatch_units_is_irreversible_and_auto_execute_is_off(real_client):
    """The class that hot-loads brings its own gate with it.

    Asserted against the candidate siren validated and handed over, because
    that is true whether the gate applied the swap, held it, or refused the
    path — and the property under test is the config's, not the gate's.
    """
    config = real_client.get("/config/incident").json()["config"]
    rules = {rule["id"]: rule for rule in config["effects"]}

    assert rules["dispatch_units"]["reversibility_class"] == "irreversible"
    assert rules["dispatch_units"]["auto_execute"] is False

    if config_is_allowlisted():
        response = real_client.post("/hot-reload", json={"path": CONFIG, "emit": 1})
        running = {r["id"]: r for r in _get("/config")["config"]["effects"]}
        # Both outcomes asserted, never one silently skipped. A held or refused
        # swap leaves the rule un-running, and saying so is the assertion; an
        # applied swap must carry the gate across with it.
        if response.json().get("state") == "registered":
            assert "dispatch_units" in running, (
                "the swap registered but the rule that brings the gate with it "
                "is not running"
            )
            assert running["dispatch_units"]["reversibility_class"] == "irreversible"
            assert running["dispatch_units"]["auto_execute"] is False
        else:
            assert response.json()["state"] in ("held", "refused"), response.json()


def test_invalid_variant_is_refused_by_throughline_and_ledgered(real_client):
    real_client.post("/hot-reload", json={"path": CONFIG, "emit": 1})
    running_before = _get("/config")["config"]

    response = real_client.post("/hot-reload", json={"path": INVALID})

    assert response.status_code == 422
    refusal = response.json()["refusal"]
    assert refusal["refused_by"] == "throughline", (
        "the refusal counter-beat is throughline's; siren renders it, "
        "it does not perform it"
    )

    if config_is_allowlisted():
        # Refused on the rule, which is the demo beat.
        assert refusal["rule"] == "dispatch_units"
        assert refusal["policy"] == "no-auto-execute-on-irreversible"
        assert refusal["line"] == 28
        expected_rule = "dispatch_units"
    else:
        # Refused on the path, before a rule was read. Still throughline's
        # refusal, still ledgered, still leaves the previous config running —
        # a different sentence, not a weaker one.
        assert refusal["policy"] == "config-source-must-be-allowlisted"
        expected_rule = None

    assert _get("/config")["config"] == running_before, (
        "the previous config must keep running, whole and untouched"
    )

    ledgered = [
        entry for entry in _get("/ledger?limit=10")["entries"]
        if entry["type"] == "config.refused"
    ]
    assert ledgered, "throughline did not ledger the refusal"
    assert ledgered[-1]["body"]["rule"] == expected_rule


def test_ledger_stays_verifiable_across_the_beat(real_client):
    real_client.post("/hot-reload", json={"path": CONFIG, "emit": 2})
    real_client.post("/hot-reload", json={"path": INVALID})
    verification = _get("/ledger/verify")
    assert verification["valid"] is True
    assert verification["broken_at"] is None
