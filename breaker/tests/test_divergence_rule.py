"""test://breaker/divergence-fires — the rule, its tick, and its working.

The rule is deterministic, so these are exact assertions: the same tick, the
same numbers, the same rendered pane, every run and every machine.
"""

from __future__ import annotations

import pytest

from breaker.rule import (
    CHARGE_COLLAPSE_RATIO,
    SOC_DELTA_MAX_PCT,
    TEMP_SLOPE_MIN_C,
    WINDOW_TICKS,
    evaluate,
)
from breaker.telemetry import (
    DIVERGENT_UNIT,
    ONSET_TICK,
    TICKS,
    UNITS,
    fixture,
    reading_for,
    series,
)

#: The tick the fixture's divergence fires on. Pinned: if the generator or the
#: thresholds move, this test says so instead of quietly tracking them.
FIXTURE_DIVERGENCE_TICK = 23


def history(unit: str, upto: int):
    return [reading_for(unit, tick) for tick in range(upto + 1)]


def test_fixture_shape_is_spec_03():
    records = fixture()
    assert len(records) == 360           # 9 units x 40 ticks
    assert len(UNITS) == 9
    assert TICKS == 40
    assert DIVERGENT_UNIT in UNITS


def test_fixture_is_deterministic():
    assert [r.as_dict() for r in fixture()] == [r.as_dict() for r in fixture()]


def test_divergence_fires_at_the_fixtures_known_tick():
    """test://breaker/divergence-fires."""
    fired = [
        tick for tick in range(TICKS)
        if evaluate(history(DIVERGENT_UNIT, tick)).diverged
    ]
    assert fired, "the fixture never diverged"
    assert fired[0] == FIXTURE_DIVERGENCE_TICK
    # ...and it stays diverged once it has: the battery does not recover.
    assert fired == list(range(FIXTURE_DIVERGENCE_TICK, TICKS))


def test_no_healthy_unit_ever_fires():
    for unit in UNITS:
        if unit == DIVERGENT_UNIT:
            continue
        for tick in range(TICKS):
            evaluation = evaluate(history(unit, tick))
            assert not evaluation.diverged, f"{unit} fired at tick {tick}"


def test_divergence_cannot_fire_before_the_onset():
    for tick in range(ONSET_TICK):
        assert not evaluate(history(DIVERGENT_UNIT, tick)).diverged


def test_evidence_renders_line_by_line_like_the_wireframe():
    evaluation = evaluate(history(DIVERGENT_UNIT, FIXTURE_DIVERGENCE_TICK))
    lines = evaluation.render_evidence().splitlines()

    assert len(lines) == 4
    assert lines[0].startswith("soc_delta(-7.1%) < -5%")
    assert lines[1].startswith("temp_slope(+1.7°C/10m) > 1.5")
    assert lines[2].startswith("charge_current collapse")
    assert all(line.rstrip().endswith("✓") for line in lines[:3])
    assert lines[3].strip() == "→ DIVERGENCE"
    # The verdicts line up in one column, as they do in the pane.
    assert len({line.index("✓") for line in lines[:3]}) == 1


def test_evidence_shows_which_check_held_it_back():
    """Two out of three is not a divergence, and the pane says which failed."""
    evaluation = evaluate(history(DIVERGENT_UNIT, 22))
    assert evaluation.diverged is False
    lines = evaluation.render_evidence().splitlines()
    assert lines[0].rstrip().endswith("✓")     # soc had already fallen
    assert lines[1].rstrip().endswith("✗")     # temperature had not yet
    assert lines[2].rstrip().endswith("✗")     # nor had current collapsed
    assert lines[3].strip() == "→ no divergence"
    assert "temp_slope" in evaluation.reason and "charge_current" in evaluation.reason


def test_all_three_checks_are_required():
    """Each check is load-bearing: relax one number, the verdict flips."""
    evaluation = evaluate(history(DIVERGENT_UNIT, FIXTURE_DIVERGENCE_TICK))
    for check in evaluation.checks:
        assert check.passed
    assert evaluation.soc_delta < SOC_DELTA_MAX_PCT
    assert evaluation.temp_slope > TEMP_SLOPE_MIN_C
    assert evaluation.charge_ratio < CHARGE_COLLAPSE_RATIO


def test_a_unit_that_only_loses_charge_does_not_diverge():
    """SOC falling alone is a discharge, not a divergence."""
    from dataclasses import replace

    readings = history(DIVERGENT_UNIT, FIXTURE_DIVERGENCE_TICK)
    steady = [replace(r, temp_c=28.0, charge_current_a=39.0) for r in readings]
    evaluation = evaluate(steady)
    assert evaluation.diverged is False
    assert evaluation.checks[0].passed is True
    assert evaluation.checks[1].passed is False


def test_insufficient_history_is_not_a_negative_verdict():
    evaluation = evaluate(history(DIVERGENT_UNIT, WINDOW_TICKS - 1))
    assert evaluation.diverged is False
    assert "insufficient history" in evaluation.reason


def test_evaluate_rejects_mixed_units():
    mixed = [reading_for("battery_1", 0), reading_for("battery_2", 1)]
    with pytest.raises(ValueError, match="one unit"):
        evaluate(mixed)


def test_evaluate_rejects_an_empty_history():
    with pytest.raises(ValueError):
        evaluate([])


def test_magnitude_and_type_match_the_contract_vocabulary():
    evaluation = evaluate(history(DIVERGENT_UNIT, FIXTURE_DIVERGENCE_TICK))
    assert evaluation.divergence_type == "soc"
    assert evaluation.magnitude == pytest.approx(abs(evaluation.soc_delta))


def test_series_feeds_the_sparkline():
    soc = series(DIVERGENT_UNIT, "soc_pct")
    temp = series(DIVERGENT_UNIT, "temp_c")
    assert len(soc) == len(temp) == TICKS
    assert soc[-1] < soc[ONSET_TICK]      # falling
    assert temp[-1] > temp[ONSET_TICK]    # rising


def test_divergence_persists_while_the_fault_persists():
    """A collapse measured against a sliding baseline stops looking like a
    collapse a few ticks in. The nominal reference is what keeps the verdict
    true for as long as the battery is bad."""
    evaluation = evaluate(history(DIVERGENT_UNIT, TICKS - 1))
    assert evaluation.diverged is True
    assert evaluation.charge_ratio < CHARGE_COLLAPSE_RATIO
    assert evaluation.nominal_current > 30.0    # the healthy opening current


def test_a_unit_already_collapsed_when_first_seen_is_reported_honestly():
    """The stated cost of the fixed reference, asserted rather than assumed."""
    from dataclasses import replace

    readings = [replace(r, charge_current_a=0.4)
                for r in history(DIVERGENT_UNIT, FIXTURE_DIVERGENCE_TICK)]
    evaluation = evaluate(readings)
    assert evaluation.nominal_current == pytest.approx(0.4)
    charge_check = evaluation.checks[2]
    assert charge_check.passed is False
    assert evaluation.diverged is False
