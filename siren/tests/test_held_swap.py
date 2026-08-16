"""A swap the gate HELD is not a swap the gate accepted.

throughline treats a reversibility downgrade as an irreversible effect, so
the config swap itself now waits at the gate: it answers

    {"refused": false, "held": true, "policy": "...", "approval_id": "apr-..."}

— accepted, and NOT applied, with the previous config still running.

siren read only ``refused`` and therefore announced "the substrate accepted
the swap", marked the class registered and drew ``flowing`` green, while the
running config was untouched and an approval sat pending. That is green on
intent, which is the one thing this timeline exists to never be. Reproduced
against a real throughline on a scratch port before the fix.

These tests use a fake substrate rather than the live gate so CI, which has
no substrate at all, still holds the line.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from siren.reload import HotReload
from siren.service import create_app


class HoldingSubstrate:
    """A substrate that answers exactly as throughline does today."""

    name = "real"
    validates_config = True

    def __init__(self) -> None:
        self.reloads: list[str] = []

    def post_signal(self, signal: Any) -> dict[str, Any]:
        return signal.wire()

    def reload_config(self, path: str) -> dict[str, Any]:
        self.reloads.append(path)
        return {
            "refused": False,
            "held": True,
            "policy": "reversibility-downgrade-must-pass-the-gate",
            "effect_id": "eff-49e623fbc9b7",
            "approval_id": "apr-09232a1da9cc",
            "downgrades": [{"effect_type": "incident.notify", "rule": "incident_notify",
                            "from": "irreversible", "to": "reversible",
                            "auto_execute": True}],
            "candidate_config": {"source": path, "version": 1, "effects": []},
        }

    def health(self) -> dict[str, Any]:
        return {"substrate": "real", "reachable": True}


class ApplyingSubstrate(HoldingSubstrate):
    """The other legitimate answer: no downgrade, so the swap applies."""

    def reload_config(self, path: str) -> dict[str, Any]:
        self.reloads.append(path)
        return {"refused": False, "accepted": True, "path": path}


@pytest.fixture
def config_path() -> str:
    return "config/incident.yaml"


def test_a_held_swap_is_reported_as_held_not_registered(config_path):
    hot = HotReload()

    result = hot.drop(config_path, HoldingSubstrate(), emitted=0)

    assert result["state"] == "held", (
        "the defect in one assertion: this said 'registered' while the "
        "running config was untouched"
    )
    assert result["held"]["held_by"] == "throughline"
    assert result["held"]["approval_id"] == "apr-09232a1da9cc"
    assert result["held"]["policy"] == "reversibility-downgrade-must-pass-the-gate"
    assert result["held"]["downgrades"], "and it names what it is holding, and why"
    assert result["refusal"] is None, "held is not refused; it is accepted and waiting"


def test_a_held_swap_registers_no_class_and_flows_nothing(config_path):
    hot = HotReload()

    result = hot.drop(config_path, HoldingSubstrate(), emitted=3)

    assert result["registered_classes"] == [], (
        "what is registered is what is RUNNING, and the candidate is not"
    )
    assert result["emitted"] == 0, "nothing flows for a class that is not running"
    states = {step["step"]: step["state"] for step in result["steps"]}
    assert states["validated"] == "ok"
    assert states["registered"] == "held"
    assert states["flowing"] == "skipped"


def test_a_held_swap_leaves_a_previously_registered_class_alone(config_path):
    """The held drop must not deregister what was already flowing."""
    hot = HotReload()
    hot.drop(config_path, ApplyingSubstrate(), emitted=2)
    assert hot.registered_classes == ["fire.incident"]

    hot.drop(config_path, HoldingSubstrate(), emitted=2)

    assert hot.registered_classes == ["fire.incident"], (
        "the previous config keeps running while the swap waits"
    )


def test_an_applied_swap_is_still_reported_as_registered(config_path):
    """Nothing is taken away: a gate that applies the swap still says so."""
    hot = HotReload()

    result = hot.drop(config_path, ApplyingSubstrate(), emitted=2)

    assert result["state"] == "registered"
    assert result["registered_classes"] == ["fire.incident"]
    assert result["emitted"] == 2


def test_the_endpoint_answers_202_for_a_held_swap(offline, data_dir, seeded_snapshot):
    """202 Accepted: not 200 (it did not happen) and not 422 (not refused)."""
    with TestClient(create_app()) as client:
        client.app.state.substrate = HoldingSubstrate()

        response = client.post("/hot-reload", json={"path": "config/incident.yaml"})

        assert response.status_code == 202
        assert response.json()["state"] == "held"

        health = client.get("/healthz").json()
        assert health["hot_reload"]["state"] == "held", (
            "the monitoring surface says held too, or the pane is the only "
            "place the truth appears"
        )
