"""issues/5 — idempotency, resolution, and traceability of the proposal queue.

A review found the approval queue never drains: ~216 near-identical
"Load-shed dispatch for battery_4" proposals for the same telemetry tick
(tick 23), and 207 of 452 approvals carrying null effect_type, description
and signal_id. This module pins the three fixes:

1. IDEMPOTENCY — re-observing the same anomaly for the same unit at the same
   tick maps to the SAME proposal (GridWatch._idempotency_key /
   GridWatch._observe_divergence), not a new one.
2. RESOLUTION — a divergence that stops diverging closes its own proposal,
   with the reason recorded, rather than waiting forever for a human
   decision that never arrives (GridWatch._maybe_resolve).
3. TRACEABILITY — every effect breaker raises names its causing signal. The
   null-signal effects found in the live system do not originate here (see
   issues/5 and README/issue evidence); this module pins the positive claim
   breaker CAN make: its own effects are never missing a signal_id.
"""

from __future__ import annotations

from breaker.engine import GridWatch
from breaker.models import TelemetryReading
from breaker.telemetry import DIVERGENT_UNIT, UNITS, fixture, reading_for
from breaker.throughline import MockThroughlineClient

from conftest import drive, rule_registry

SUBJECT = "oidc|ruiz@nvidia-demo.example"


def _reading(unit: str, tick: int, soc: float, temp: float, current: float) -> TelemetryReading:
    return TelemetryReading(
        unit_id=unit, tick=tick, soc_pct=soc, temp_c=temp,
        charge_current_a=current, voltage_v=48.0, feeder="feeder-7",
    )


# --------------------------------------------------------- 1. idempotency

def test_repeat_observation_of_same_unit_tick_maps_to_the_same_proposal(watch):
    """test://breaker/idempotent-reobservation

    This is the root cause the review found: re-observing battery_4's tick-23
    divergence used to mint a fresh signal/judgment/effect/proposal every
    time (a random uuid4 stamp), which is how 216 near-identical proposals
    piled up in the live queue. It must now map to the one proposal already
    open for this (unit, tick, kind).
    """
    proposal = drive(watch)
    assert proposal is not None and proposal.tick == 23

    ledger_before = list(watch.client.ledger)

    # Re-observe the EXACT same divergence: same unit, same tick.
    reading = reading_for(DIVERGENT_UNIT, 23)
    outcome = watch.ingest(TelemetryReading(**reading.as_dict()))

    assert outcome["proposal"] is not None
    assert outcome["proposal"]["id"] == proposal.id, "a repeat observation must update the SAME proposal"
    assert len(watch.all_proposals()) == 1, "a repeat observation must not mint a second proposal"

    # And nothing new was posted to the substrate — the flood is not merely
    # bounded, its cause (re-posting on every observation) is removed.
    assert watch.client.ledger == ledger_before, (
        "a repeat observation must not re-post signal/judgment/effect"
    )

    reobserved = watch.get(proposal.id)
    assert reobserved.effect_id == proposal.effect_id
    assert reobserved.signal_id == proposal.signal_id
    assert reobserved.idempotency_key == f"{DIVERGENT_UNIT}:23:soc"


def test_idempotency_key_is_derived_from_unit_tick_and_kind(watch):
    proposal = drive(watch)
    assert proposal.idempotency_key == f"{proposal.unit_id}:{proposal.tick}:{proposal.divergence_type}"


def test_repeated_fixture_drive_does_not_regrow_the_queue():
    """test://breaker/no-regrowth-on-replay — the incident, reproduced and closed.

    Driving the WHOLE fixture twice against the same GridWatch (which is
    what a re-driven ``POST /fixture/run`` did against a live service) must
    not multiply battery_4's proposal.
    """
    watch = GridWatch(client=MockThroughlineClient(), registry=rule_registry())
    for _ in range(2):
        for record in fixture():
            watch.ingest(TelemetryReading(**record.as_dict()))

    proposals = [p for p in watch.all_proposals() if p.unit_id == DIVERGENT_UNIT]
    assert len(proposals) == 1, (
        f"expected exactly one battery_4 proposal after two full replays, got "
        f"{len(proposals)}: {[p.id for p in proposals]}"
    )


# ---------------------------------------------------------- 2. resolution

def test_a_cleared_divergence_closes_its_own_proposal():
    """test://breaker/self-resolving-proposal

    Today a proposal leaves ``pending`` only via a human decision that never
    arrives. This is the other exit: the condition itself stops diverging,
    and the proposal closes itself with the reason recorded, rather than
    waiting forever.
    """
    watch = GridWatch(client=MockThroughlineClient(), registry=rule_registry())
    unit = "battery_test"

    # 11 ticks: soc falling, temp rising, current collapsing in the tail —
    # a divergence by all three checks, same shape as the fixture's.
    for tick in range(1, 12):
        soc = 80 - (tick - 1)
        temp = 25 + 0.3 * (tick - 1)
        current = 40.0 if tick <= 8 else 5.0
        watch.ingest(_reading(unit, tick, soc, temp, current))

    proposal = watch._open_proposal_for(unit)
    assert proposal is not None
    assert proposal.status == "waiting_at_gate"
    assert proposal.tick == 11

    # Recovery: current back to nominal, soc/temp flat. Ticks strictly after
    # the one that raised the proposal, exactly as real monotonic telemetry
    # would report a recovery.
    for tick in range(12, 23):
        watch.ingest(_reading(unit, tick, 70.0, 28.0, 40.0))

    closed = watch.get(proposal.id)
    assert closed.status == "withdrawn"
    assert closed.closed_reason is not None and "divergence cleared" in closed.closed_reason
    assert closed.closed_at is not None
    # It is still held nowhere at the gate — no dispatch, no decision.
    assert watch.dispatches == []

    withdrawals = [e for e in watch.ledger if e["type"] == "proposal.withdrawn"]
    assert len(withdrawals) == 1
    assert withdrawals[0]["body"]["proposal_id"] == proposal.id
    assert withdrawals[0]["body"]["reason"]


def test_ledger_entries_are_appended_not_edited(watch):
    """Existing ledger entries are immutable; closure is a NEW entry."""
    proposal = drive(watch)
    ledger_before = [dict(e) for e in watch.ledger]

    watch.decide(proposal.id, "reject", SUBJECT)
    # A rejection is a human decision via the gate, not a self-closure, so it
    # does not append to breaker's own ledger — but nothing already appended
    # may have changed underneath it either way.
    for before, after in zip(ledger_before, watch.ledger):
        assert before == after, "an existing ledger entry was mutated"
    assert watch.ledger[: len(ledger_before)] == ledger_before, (
        "existing ledger entries must never be edited or removed"
    )


def test_a_replayed_earlier_tick_does_not_falsely_close_a_later_open_proposal():
    """test://breaker/no-false-resolution-from-out-of-order-replay

    A non-diverging window for a tick AT OR BEFORE the one that raised the
    open proposal is not a recovery — it is old or out-of-order data (a
    retried batch, a re-driven fixture). Closing on it would immediately let
    a fresh proposal be raised for the next diverging tick, regrowing the
    exact backlog this fix exists to stop.
    """
    watch = GridWatch(client=MockThroughlineClient(), registry=rule_registry())
    unit = "battery_test"
    for tick in range(1, 12):
        soc = 80 - (tick - 1)
        temp = 25 + 0.3 * (tick - 1)
        current = 40.0 if tick <= 8 else 5.0
        watch.ingest(_reading(unit, tick, soc, temp, current))

    proposal = watch._open_proposal_for(unit)
    assert proposal is not None and proposal.tick == 11

    # Replay an EARLIER, non-diverging tick (e.g. tick 1 resubmitted).
    watch.ingest(_reading(unit, 1, 80.0, 25.0, 40.0))

    still_open = watch.get(proposal.id)
    assert still_open.status == "waiting_at_gate", (
        "a non-diverging window at or before the proposing tick must not "
        "close the open proposal"
    )
    assert len(watch.all_proposals()) == 1


# -------------------------------------------------------- 3. traceability

def test_every_effect_breaker_raises_names_its_signal(watch):
    """test://breaker/every-effect-names-its-signal

    46% of the live approval queue's effects carry a null signal_id. This
    pins the positive claim breaker can make: every effect ITS OWN code
    raises always names the signal that caused it — GridWatch._propose is
    the only place breaker calls post_effect, and it always sets signal_id.
    """
    watch2 = GridWatch(client=MockThroughlineClient(), registry=rule_registry())
    fired = []
    for record in fixture():
        outcome = watch2.ingest(TelemetryReading(**record.as_dict()))
        if outcome["proposal"]:
            fired.append(outcome["proposal"])

    assert fired, "the fixture must raise at least one proposal to test this"
    for proposal in fired:
        effect = watch2.client.get_effect(proposal["effect_id"])
        assert effect.get("signal_id"), f"effect {effect.get('id')} has no signal_id"
        assert effect.get("effect_type"), f"effect {effect.get('id')} has no effect_type"
        assert effect.get("description"), f"effect {effect.get('id')} has no description"
        assert effect["signal_id"] == proposal["signal_id"]
        assert effect["signal_id"] in watch2.client.signals, (
            "the effect's signal_id must resolve to a real, posted signal"
        )


def test_breaker_has_exactly_one_call_site_that_posts_an_effect():
    """Structural pin: the only place breaker can raise an untraceable effect
    from is GridWatch._propose, and it is checked never to omit signal_id.

    This is the evidence for issues/5 item 3: if breaker only ever posts an
    effect from one place, and that place always sets signal_id, then
    breaker does not originate the null-signal effects found in the live
    queue — something else does.
    """
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "breaker"
    call_sites = []
    for path in package.glob("*.py"):
        if path.name == "throughline.py":
            continue  # defines post_effect; does not call it
        source = path.read_text(encoding="utf-8")
        call_sites.extend(
            f"{path.name}:{i + 1}" for i, line in enumerate(source.splitlines())
            if "post_effect(" in line and ".post_effect(" in line
        )
    assert len(call_sites) == 1, f"expected exactly one post_effect call site, found {call_sites}"
    assert call_sites[0].startswith("engine.py:"), call_sites
