"""The divergence rule — deterministic, and shown line by line.

This is a rule, not a model. It has thresholds you can read, arithmetic you can
check by hand, and an evidence render that shows every check with its own
verdict — the pane in the wireframe:

    soc_delta(-6.8%) < -5%          ✓
    temp_slope(+1.8°C/10m) > 1.5    ✓
    charge_current collapse         ✓
                                    → DIVERGENCE

All three checks must pass. Two out of three is not a divergence, and the
evidence says which one held it back — an operator who disagrees with the
verdict can see exactly which number to argue with.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from .telemetry import Reading

#: Ten one-minute ticks: the window the thresholds are quoted against.
WINDOW_TICKS = 10

#: Thresholds, from the spec's evidence pane.
SOC_DELTA_MAX_PCT = -5.0        # state of charge must fall by more than this
TEMP_SLOPE_MIN_C = 1.5          # °C per 10 minutes
CHARGE_COLLAPSE_RATIO = 0.35    # current must fall below this share of baseline

#: The evidence pane's column, so the verdicts line up as in the wireframe.
_VERDICT_COLUMN = 34

PASS = "✓"
FAIL = "✗"


@dataclass(frozen=True)
class Check:
    """One line of the evidence pane."""

    name: str
    expression: str
    passed: bool
    value: Optional[float] = None
    threshold: Optional[float] = None

    def render(self) -> str:
        return f"{self.expression:<{_VERDICT_COLUMN}}{PASS if self.passed else FAIL}"


@dataclass(frozen=True)
class Metrics:
    """The three numbers, computed deterministically.

    Arithmetic is never delegated. Whichever substrate renders the verdict, the
    numbers under it are computed here, from the readings, in Python — so two
    substrates can disagree about the call while quoting identical evidence.
    """

    unit_id: str
    tick: int
    soc_delta: float
    temp_slope: float
    charge_ratio: float
    nominal_current: float
    window_ticks: int

    def as_dict(self) -> dict:
        return {
            "unit_id": self.unit_id, "tick": self.tick,
            "soc_delta": self.soc_delta, "temp_slope": self.temp_slope,
            "charge_ratio": self.charge_ratio,
            "nominal_current": self.nominal_current,
            "window_ticks": self.window_ticks,
        }


@dataclass(frozen=True)
class Evaluation:
    """The rule's verdict on one unit at one tick, with its working shown."""

    unit_id: str
    tick: int
    diverged: bool
    checks: list[Check] = field(default_factory=list)
    soc_delta: Optional[float] = None
    temp_slope: Optional[float] = None
    charge_ratio: Optional[float] = None
    nominal_current: Optional[float] = None
    reason: Optional[str] = None
    #: Set when a substrate other than the rule rendered the verdict. The three
    #: metric lines are facts either way; the verdict line says who called it.
    attribution: Optional[str] = None

    @property
    def divergence_type(self) -> Optional[str]:
        """Which divergence this is, in the contract's vocabulary.

        The contract offers ``soc`` and ``temperature``. State of charge is the
        headline when both moved; it is the number that decides load-shedding.
        """
        if not self.diverged:
            return None
        return "soc"

    @property
    def magnitude(self) -> Optional[float]:
        return abs(self.soc_delta) if self.soc_delta is not None else None

    def render_evidence(self) -> str:
        """The pane, exactly as the wireframe shows it.

        With an attribution set (a non-rule substrate rendered the verdict) the
        verdict line names it. The metric lines never change: they are computed
        arithmetic, whoever is judging.
        """
        lines = [check.render() for check in self.checks]
        verdict = "→ DIVERGENCE" if self.diverged else "→ no divergence"
        if self.attribution:
            verdict = f"{verdict}  [{self.attribution}]"
        lines.append(f"{'':<{_VERDICT_COLUMN}}{verdict}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "tick": self.tick,
            "diverged": self.diverged,
            "divergence_type": self.divergence_type,
            "magnitude": self.magnitude,
            "soc_delta": self.soc_delta,
            "temp_slope": self.temp_slope,
            "charge_ratio": self.charge_ratio,
            "nominal_current": self.nominal_current,
            "reason": self.reason,
            "attribution": self.attribution,
            "checks": [
                {"name": c.name, "expression": c.expression, "passed": c.passed,
                 "value": c.value, "threshold": c.threshold, "line": c.render()}
                for c in self.checks
            ],
            "evidence": self.render_evidence(),
        }


def _signed(value: float, unit: str = "") -> str:
    return f"{value:+.1f}{unit}"


def compute_metrics(history: Sequence[Reading]) -> Optional[Metrics]:
    """The window's three numbers, or None when there is not enough history."""
    if len(history) <= WINDOW_TICKS:
        return None

    window = history[-(WINDOW_TICKS + 1):]
    first, last = window[0], window[-1]
    opening = [r.charge_current_a for r in history[: WINDOW_TICKS + 1]]
    nominal = sorted(opening)[len(opening) // 2]

    return Metrics(
        unit_id=last.unit_id,
        tick=last.tick,
        soc_delta=round(last.soc_pct - first.soc_pct, 2),
        temp_slope=round(last.temp_c - first.temp_c, 2),
        charge_ratio=round((last.charge_current_a / nominal) if nominal > 0 else 1.0, 3),
        nominal_current=round(nominal, 2),
        window_ticks=WINDOW_TICKS,
    )


def render_checks(metrics: Metrics, verdicts: Sequence[bool]) -> list[Check]:
    """The three evidence lines for a set of metrics and per-check verdicts."""
    return [
        Check(
            name="soc_delta",
            expression=f"soc_delta({_signed(metrics.soc_delta, '%')}) < {SOC_DELTA_MAX_PCT:g}%",
            passed=verdicts[0], value=metrics.soc_delta, threshold=SOC_DELTA_MAX_PCT,
        ),
        Check(
            name="temp_slope",
            expression=(f"temp_slope({_signed(metrics.temp_slope, '°C/10m')}) "
                        f"> {TEMP_SLOPE_MIN_C:g}"),
            passed=verdicts[1], value=metrics.temp_slope, threshold=TEMP_SLOPE_MIN_C,
        ),
        Check(
            name="charge_current",
            expression="charge_current collapse",
            passed=verdicts[2], value=metrics.charge_ratio,
            threshold=CHARGE_COLLAPSE_RATIO,
        ),
    ]


def evaluate(history: Sequence[Reading]) -> Evaluation:
    """Evaluate the rule over one unit's readings, oldest first.

    ``history`` must be for a single unit. Fewer than ``WINDOW_TICKS + 1``
    readings is not a negative verdict — it is *not enough evidence yet*, and
    the evaluation says so rather than quietly returning False.
    """
    if not history:
        raise ValueError("evaluate() needs at least one reading")
    units = {reading.unit_id for reading in history}
    if len(units) != 1:
        raise ValueError(f"evaluate() takes one unit's history, got {sorted(units)}")

    latest = history[-1]
    if len(history) <= WINDOW_TICKS:
        return Evaluation(
            unit_id=latest.unit_id,
            tick=latest.tick,
            diverged=False,
            checks=[Check("window", f"window({len(history)}/{WINDOW_TICKS + 1} ticks)",
                          passed=False)],
            reason=(f"insufficient history: {len(history)} readings, "
                    f"{WINDOW_TICKS + 1} needed"),
        )

    # Collapse is measured against the unit's NOMINAL charge current — the
    # median of its opening window — not against a sliding baseline. A sliding
    # baseline drifts down with the fault and stops calling it a collapse a few
    # ticks after it starts, which would let a diverging battery quietly fall
    # back to "ok" while it is still on fire. The cost of the fixed reference is
    # stated plainly: a unit that is already collapsed when breaker first sees
    # it has a collapsed nominal, and this check will not fire for it.
    metrics = compute_metrics(history)
    soc_delta, temp_slope, charge_ratio = (
        metrics.soc_delta, metrics.temp_slope, metrics.charge_ratio)
    nominal = metrics.nominal_current

    checks = render_checks(metrics, [
        soc_delta < SOC_DELTA_MAX_PCT,
        temp_slope > TEMP_SLOPE_MIN_C,
        charge_ratio < CHARGE_COLLAPSE_RATIO,
    ])

    diverged = all(check.passed for check in checks)
    held_back = [check.name for check in checks if not check.passed]

    return Evaluation(
        unit_id=latest.unit_id,
        tick=latest.tick,
        diverged=diverged,
        checks=checks,
        soc_delta=round(soc_delta, 2),
        temp_slope=round(temp_slope, 2),
        charge_ratio=round(charge_ratio, 3),
        nominal_current=round(nominal, 2),
        reason=None if diverged else f"held back by: {', '.join(held_back)}",
    )
