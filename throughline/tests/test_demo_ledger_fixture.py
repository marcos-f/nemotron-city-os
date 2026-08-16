"""A chain that does not move, so recorded evidence stays true.

The live ledger grew from ~100 to over 1000 entries during one review, which
made every recorded claim of the form "agent refusal at seq 96" false almost
immediately. ``fixtures/demo-ledger.jsonl`` is a pinned chain with a recorded
head hash: a demo can be served against it with ``--ledger`` (or
``THROUGHLINE_LEDGER``), and an evidence artifact can cite both a sequence
number and the head hash it was true for.

The live ledger is unaffected — this is an addition, not a replacement, and
``test_the_default_settings_still_use_the_live_ledger`` holds that line.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from throughline.ledger import Ledger
from throughline.service import Settings, Substrate

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "fixtures" / "demo-ledger.jsonl"
HEAD_FILE = REPO_ROOT / "fixtures" / "demo-ledger.head.json"
PINNED = json.loads(HEAD_FILE.read_text(encoding="utf-8"))


def test_the_pinned_chain_verifies():
    verification = Ledger(FIXTURE).verify()
    assert verification["valid"] is True
    assert verification["chain_length"] == PINNED["chain_length"]


def test_the_pinned_head_hash_matches_the_recorded_one():
    """If this fails, the fixture moved and every citation of it is stale."""
    assert Ledger(FIXTURE).head == PINNED["head"]


def test_every_recorded_beat_sits_at_its_pinned_sequence_number():
    entries = {e["seq"]: e for e in Ledger(FIXTURE).read()}
    expected = {
        "signal_ingested": "signal.ingested",
        "judgment_recorded": "judgment.recorded",
        "reversible_executed": "effect.executed",
        "effect_queued": "effect.queued",
        "agent_refused": "approval.refused",
        "config_refused": "config.refused",
        "config_outside_allowlist_refused": "config.refused",
        "downgrade_held": "config.downgrade_held",
        "human_released": "approval.decided",
        "irreversible_executed": "effect.executed",
    }
    for beat, event_type in expected.items():
        seq = PINNED["beats"][beat]
        assert entries[seq]["type"] == event_type, f"{beat} moved off seq {seq}"


def test_the_refusal_beats_are_actually_refusals():
    entries = {e["seq"]: e for e in Ledger(FIXTURE).read()}
    refusal = entries[PINNED["beats"]["agent_refused"]]["body"]
    assert refusal["refusal_reason"]
    assert refusal["effect_id"] == "eff-demo-cancel"

    outside = entries[PINNED["beats"]["config_outside_allowlist_refused"]]["body"]
    assert outside["policy"] == "config-source-must-be-allowlisted"

    held = entries[PINNED["beats"]["downgrade_held"]]["body"]
    assert held["downgrades"][0]["effect_type"] == "grid.load_shed"


def test_the_release_follows_the_refusals_in_the_chain():
    """The order is the argument: refused, refused, then a human releases."""
    beats = PINNED["beats"]
    assert beats["agent_refused"] < beats["human_released"]
    assert beats["human_released"] < beats["irreversible_executed"]


def test_serving_against_the_pinned_ledger_restores_its_effects(tmp_path):
    """``--ledger`` points the substrate at a chain and it comes back to life."""
    copy = tmp_path / "pinned.jsonl"
    shutil.copy(FIXTURE, copy)
    substrate = Substrate(Settings(
        data_dir=tmp_path / "data",
        config_path=REPO_ROOT / "config" / "effects.yaml",
        ledger_file=copy,
    ))
    assert substrate.settings.ledger_path == copy
    assert substrate.boot_verification["valid"] is True
    assert substrate.boot_verification["chain_length"] == PINNED["chain_length"]
    # The gate and the queue rebuilt themselves from that chain.
    assert substrate.gate.get("eff-demo-cancel").status == "executed"
    assert substrate.gate.get("eff-demo-notify").status == "executed"
    downgrade = [a for a in substrate.queue.all()
                 if a.effect_type == "config.reversibility_downgrade"]
    assert downgrade and downgrade[0].state == "pending"


def test_the_ledger_env_var_selects_the_same_chain(tmp_path, monkeypatch):
    copy = tmp_path / "pinned.jsonl"
    shutil.copy(FIXTURE, copy)
    monkeypatch.setenv("THROUGHLINE_LEDGER", str(copy))
    assert Settings().ledger_path == copy


def test_the_default_settings_still_use_the_live_ledger(tmp_path, monkeypatch):
    """Nothing about the live path changed."""
    monkeypatch.delenv("THROUGHLINE_LEDGER", raising=False)
    settings = Settings(data_dir=tmp_path / "data")
    assert settings.ledger_path == tmp_path / "data" / "ledger.jsonl"
    assert settings.ledger_file is None


def test_the_cli_exposes_the_ledger_flag():
    from throughline.cli import build_parser

    args = build_parser().parse_args(["serve", "--ledger", "fixtures/demo-ledger.jsonl"])
    assert args.ledger == "fixtures/demo-ledger.jsonl"


@pytest.mark.parametrize("field", ["head", "chain_length", "beats", "path"])
def test_the_head_file_records_what_an_artifact_needs_to_cite(field):
    assert PINNED[field]
