"""spec-03 microgrid telemetry: the fixture and its generator.

The fixture is **deterministic**: nine units over forty one-minute ticks, 360
records, so the divergence lands on the same tick in CI as on the demo machine.
Nothing here samples the wall clock or the OS random source.

Determinism comes from a **closed-form sine of the indices, not a seeded
generator** — ``random`` is never imported and there is no seed. That is worth
stating precisely, because "fixed seed" (which this docstring used to say)
implies something you could reseed to get a different fixture. You cannot.
This fixture varies only by changing the formula in ``_wobble``.

``spec-03`` is a REAL document: ``specs/03_Grid_Guardian.md`` in the
``nvidia-hackathon-system`` governance catalog, a peer repository this
checkout does not contain. Cite its sections precisely, because they say
different things:

* **§3.2 Controlled demonstration sequence** — the demo BEAT. Its 0:25–0:55
  row reads "Battery anomaly / Detect SOC/current divergence / Peer
  comparison", which is what this fixture stages: battery_4's state of charge
  falls, its temperature rises, and its charge current collapses, while the
  other units drift the way healthy units drift. That is what makes the
  divergence a divergence rather than a threshold trip.
* **§4.1 FR-01** — the ingest REQUIREMENT ("time-stamped V, A, W, Wh,
  temperature, SOC, status, alarms").
* **§6.2 Telemetry schema** — the spec's ROW SHAPE, and this module does NOT
  implement it. See the ``Reading`` docstring below.
* **§3.1 Hero scenario** — specifies FOUR battery modules; this fixture
  generates nine. The divergent unit is ``battery_4`` in both, so the beat is
  faithful and the peer set is simply wider than specified.

Earlier revisions of this docstring cited "§3.2" for the row shape, which is
the wrong section, and README.md once said the document does not exist at
all. Both are corrected; see the *Data provenance* section of README.md.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

#: The fixture's unit set. spec-03 §3.1 specifies FOUR battery modules; this
#: generates nine, so the peer set is wider than the spec's. ``battery_4`` is
#: the divergent unit in both, so the §3.2 beat is faithful.
UNITS = tuple(f"battery_{n}" for n in range(1, 10))
TICKS = 40
TICK_SECONDS = 60
DIVERGENT_UNIT = "battery_4"

#: The tick at which battery_4 begins to go wrong. The rule needs a full
#: window after this before it can see it — see rule.WINDOW_TICKS.
ONSET_TICK = 18

#: Fixture epoch. A fixed timestamp, so two runs produce identical records.
EPOCH = datetime(2026, 8, 15, 2, 0, 0, tzinfo=timezone.utc)

FEEDER = "feeder-7"


@dataclass(frozen=True)
class Reading:
    """One telemetry record — this repository's row shape, NOT spec-03 §6.2's.

    The divergence is real and is stated rather than papered over. spec-03
    §6.2 *Telemetry schema* specifies::

        {"timestamp": ..., "asset_id": "battery_4",
         "measurements": {"voltage_v": ..., "current_a": ...,
                          "soc_pct": ..., "temperature_c": ...},
         "status": "charging", "quality": "good"}

    This dataclass is flat and differently named: ``unit_id`` not
    ``asset_id``, ``ts`` not ``timestamp``, the four readings hoisted out of
    ``measurements``, ``temp_c`` not ``temperature_c``, ``charge_current_a``
    not ``current_a``, plus ``tick`` and ``feeder`` that §6.2 does not have,
    and no ``status`` or ``quality`` at all. It therefore satisfies §4.1
    FR-01 only in part: V, A, SOC and temperature are ingested; W, Wh, status
    and alarms are not.

    This shape — not §6.2's — is what ``GET /telemetry/fixture`` returns and
    what ``/signals/telemetry`` accepts. The contract of record is
    ``contracts/openapi.yaml#/components/schemas/Reading``. Aligning to §6.2
    is a real piece of work and has not been done; do not read the citations
    above as a claim that it has.
    """

    unit_id: str
    tick: int
    ts: str
    soc_pct: float
    temp_c: float
    charge_current_a: float
    voltage_v: float
    feeder: str = FEEDER

    def as_dict(self) -> dict:
        return asdict(self)


def _wobble(unit_index: int, tick: int, scale: float) -> float:
    """A repeatable, unit-specific wobble.

    Deterministic by construction — a sine of the indices, not a random draw —
    so the fixture is byte-identical everywhere it runs.
    """
    return scale * math.sin((tick * 0.7) + unit_index * 1.9)


def reading_for(unit: str, tick: int) -> Reading:
    """The reading for one unit at one tick."""
    index = UNITS.index(unit)
    ts = (EPOCH + timedelta(seconds=TICK_SECONDS * tick)).isoformat()

    # Healthy behaviour: a slow discharge, a steady temperature, a stable feed.
    soc = 82.0 - index * 0.6 - tick * 0.15 + _wobble(index, tick, 0.10)
    temp = 27.5 + index * 0.2 + _wobble(index, tick, 0.15)
    current = 40.0 - index * 0.3 + _wobble(index, tick, 0.40)

    if unit == DIVERGENT_UNIT and tick >= ONSET_TICK:
        elapsed = tick - ONSET_TICK
        soc -= 1.10 * elapsed          # state of charge falls away
        temp += 0.35 * elapsed         # temperature climbs
        current -= 5.80 * elapsed      # charge current collapses
        current = max(current, 0.4)

    voltage = 48.0 + (soc - 80.0) * 0.05 + _wobble(index, tick, 0.05)

    return Reading(
        unit_id=unit,
        tick=tick,
        ts=ts,
        soc_pct=round(soc, 2),
        temp_c=round(temp, 2),
        charge_current_a=round(current, 2),
        voltage_v=round(voltage, 2),
    )


def tick_readings(tick: int) -> list[Reading]:
    return [reading_for(unit, tick) for unit in UNITS]


def fixture(ticks: int = TICKS) -> list[Reading]:
    """The whole fixture: ``ticks`` × 9 records, oldest first."""
    return [reading for tick in range(ticks) for reading in tick_readings(tick)]


def stream(ticks: int = TICKS, unit: Optional[str] = None) -> Iterator[Reading]:
    """Yield the fixture tick by tick. The caller decides the pacing."""
    for tick in range(ticks):
        for reading in tick_readings(tick):
            if unit is None or reading.unit_id == unit:
                yield reading


def series(unit: str, field: str, ticks: int = TICKS) -> list[float]:
    """One field of one unit across the fixture — the sparkline's data."""
    return [getattr(reading_for(unit, tick), field) for tick in range(ticks)]
