#!/usr/bin/env python3
"""Build the pinned demo ledger fixture.

Demo evidence used to cite sequence numbers against the LIVE chain, which
grows every time anyone touches the system: a recorded claim like "agent
refusal at seq 96" was false within the hour. The fix is a chain that does
not move. This script produces one by driving the real substrate — the same
gate, the same ledger, the same refusals — and writing the result to
``fixtures/demo-ledger.jsonl`` together with ``fixtures/demo-ledger.head.json``,
which records the head hash, the chain length and the sequence number of every
beat worth citing.

Run it only when the demo script itself changes:

    python3 scripts/make-demo-ledger.py --regenerate

Nothing in the service calls this. The live ledger is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from throughline.models import Decision, EffectProposal, Judgment, Signal  # noqa: E402
from throughline.service import Settings, Substrate  # noqa: E402

FIXTURES = ROOT / "fixtures"
LEDGER = FIXTURES / "demo-ledger.jsonl"
HEAD = FIXTURES / "demo-ledger.head.json"


async def build(substrate: Substrate) -> dict[str, int]:
    """Run the demo beats against a real substrate. Returns beat -> seq."""
    beats: dict[str, int] = {}

    def seq_of(event_type: str) -> int:
        return max(e["seq"] for e in substrate.ledger.read() if e["type"] == event_type)

    substrate.ledger.append("signal.ingested", Signal(
        id="sig-demo-0001", **{"class": "grid.telemetry"},
        source="breaker.substation-4", real_or_synthetic="synthetic",
        payload_ref="demo://grid/battery_4/window-10",
    ).model_dump(mode="json", by_alias=True))
    beats["signal_ingested"] = seq_of("signal.ingested")

    substrate.ledger.append("judgment.recorded", Judgment(
        id="jdg-demo-0001", signal_id="sig-demo-0001", verdict="degrading",
        produced_by="breaker.rule", confidence=0.91, real_or_synthetic="synthetic",
        rationale="soc_delta -7.2%, temp_slope +1.9C, charge ratio 0.28",
    ).model_dump(mode="json"))
    beats["judgment_recorded"] = seq_of("judgment.recorded")

    # Beat: a reversible effect executes on the spot.
    await substrate.gate.submit(EffectProposal(
        id="eff-demo-notify", effect_type="notify.operator",
        signal_id="sig-demo-0001", judgment_id="jdg-demo-0001",
        description="Post the degrading-battery finding to the operator console.",
    ))
    beats["reversible_executed"] = seq_of("effect.executed")

    # Beat: an irreversible effect is held.
    held = await substrate.gate.submit(EffectProposal(
        id="eff-demo-cancel", effect_type="order.cancel",
        signal_id="sig-demo-0001", judgment_id="jdg-demo-0001",
        description="Cancel downstream order 7741. Not undoable.",
    ))
    beats["effect_queued"] = seq_of("effect.queued")

    # Beat: an agent tries to release it and is refused, by declaration...
    for decision in (
        Decision(decision="approve", decided_by="agent:nemoclerk", caller_role="agent"),
        # ...and with the role omitted entirely, which used to work.
        Decision(decision="approve", decided_by="agent:nemoclerk"),
        # ...and while claiming to be a person.
        Decision(decision="approve", decided_by="agent:nemoclerk", caller_role="human"),
        # ...and as a plausible person nobody authenticated, which is the
        # beat the round-2 review added: an irreversible effect is not
        # released on a decision carrying no attestation.
        Decision(decision="approve", decided_by="dana@nvidia-demo.example",
                 caller_role="human"),
    ):
        try:
            await substrate.gate.decide(held.approval_id, decision)
        except Exception:
            pass
    beats["agent_refused"] = seq_of("approval.refused")

    # Beat: an unsafe config is refused whole, naming file, rule and line.
    try:
        substrate.config_store.reload(Path("tests/fixtures/unsafe-auto-execute.yaml"))
    except Exception:
        pass
    beats["config_refused"] = seq_of("config.refused")

    # Beat: a reload from outside the allowlist is refused, naming the path.
    try:
        substrate.stage_config(Path(tempfile.gettempdir()) / "not-allowlisted.yaml")
    except Exception:
        pass
    beats["config_outside_allowlist_refused"] = seq_of("config.refused")

    # Beat: a reversibility downgrade is held at the gate.
    candidate = substrate.stage_config(
        Path("tests/fixtures/downgrade-grid-load-shed.yaml"))
    await substrate.hold_config_downgrade(
        candidate, substrate.downgrades_against_running(candidate))
    beats["downgrade_held"] = seq_of("config.downgrade_held")

    # Beat: a human releases the held effect, and only then does it execute.
    await substrate.gate.decide(held.approval_id, Decision(
        decision="approve", decided_by="oidc|operator-7", caller_role="human",
        issuer="https://git.nemotron.example.com", auth_mode="oidc",
        rationale="confirmed with the substation lead"))
    beats["human_released"] = seq_of("approval.decided")
    beats["irreversible_executed"] = seq_of("effect.executed")
    return beats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--regenerate", action="store_true",
                        help="overwrite the committed fixture")
    args = parser.parse_args()

    if LEDGER.exists() and not args.regenerate:
        print(f"{LEDGER} exists; pass --regenerate to rebuild it")
        return 1

    work = Path(tempfile.mkdtemp(prefix="throughline-demo-ledger-"))
    import os

    os.chdir(ROOT)  # keep repo-relative paths out of the chain's provenance
    try:
        substrate = Substrate(Settings(
            data_dir=work, config_path=Path("config/effects.yaml"),
            # The demo operator allowlists the fixture directory so the
            # downgrade beat has a candidate to hold; /tmp stays outside it,
            # which is what the out-of-allowlist refusal beat demonstrates.
            extra_config_dirs=[Path("tests/fixtures")]))
        beats = asyncio.run(build(substrate))
        verification = substrate.ledger.verify()
        if not verification["valid"]:
            print(f"refusing to pin a broken chain: {verification}")
            return 1

        FIXTURES.mkdir(parents=True, exist_ok=True)
        shutil.copy(substrate.ledger.path, LEDGER)
        HEAD.write_text(json.dumps({
            "note": (
                "Pinned demo chain. Cite sequence numbers against THIS file "
                "and record `head` alongside the claim, so a reader can tell "
                "which chain state the claim was true for. Serve it with "
                "`throughline serve --ledger fixtures/demo-ledger.jsonl` or "
                "THROUGHLINE_LEDGER."
            ),
            "path": "fixtures/demo-ledger.jsonl",
            "head": substrate.ledger.head,
            "chain_length": verification["chain_length"],
            "beats": beats,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {LEDGER} ({verification['chain_length']} entries)")
        print(f"head {substrate.ledger.head}")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
