"""Wire models. Contract shapes from contracts/openapi.yaml, plus what the
evidence pane and the ledger walk need."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Signal(BaseModel):
    """The federation's signal envelope — identical across all four feeds."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    signal_class: str = Field(alias="class", default="microgrid.telemetry")
    source: str
    ingest_time: datetime = Field(default_factory=utcnow)
    real_or_synthetic: Literal["real", "synthetic"] = "synthetic"
    payload_ref: Optional[str] = None
    staleness: Optional[str] = None


class TelemetryReading(BaseModel):
    """A breaker telemetry row, as posted to /signals/telemetry.

    It is NOT spec-03 §6.2's row shape — §6.2 nests the readings under
    ``measurements`` and carries ``asset_id``, ``status`` and ``quality``.
    This flat shape is the one breaker actually enforces; the divergence is
    documented on ``telemetry.Reading`` and in README.md.
    """

    unit_id: str
    tick: int
    soc_pct: float
    temp_c: float
    charge_current_a: float
    voltage_v: Optional[float] = None
    feeder: Optional[str] = None
    ts: Optional[datetime] = None


class Judgment(BaseModel):
    """The one-contract judgment shape breaker shares with throughline."""

    id: str
    signal_id: str
    finding: str
    confidence: float
    citations: list[str] = Field(default_factory=list)
    abstained: bool = False
    substrate: str
    substrate_label: str
    produced_by: str = "breaker"


class Proposal(BaseModel):
    """A dispatch proposal. Contract fields first, provenance after."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    divergence_type: Literal["soc", "temperature"]
    status: Literal[
        "proposed", "waiting_at_gate", "approved", "rejected", "withdrawn"
    ]
    magnitude: Optional[float] = None

    unit_id: Optional[str] = None
    tick: Optional[int] = None
    description: Optional[str] = None
    evidence: Optional[str] = None
    checks: Optional[list[dict[str, Any]]] = None
    substrate: Optional[str] = None
    substrate_label: Optional[str] = None
    signal_id: Optional[str] = None
    judgment_id: Optional[str] = None
    effect_id: Optional[str] = None
    approval_id: Optional[str] = None
    gate_mode: Optional[str] = None
    held_since: Optional[datetime] = None
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    execution_count: int = 0

    #: The stable key this proposal was minted from: ``unit:tick:kind``. Two
    #: observations of the same anomaly for the same unit at the same tick
    #: derive the SAME key, and therefore the same proposal — see
    #: ``GridWatch._idempotency_key`` in engine.py. Re-observing does not
    #: mint a second proposal; it updates this one.
    idempotency_key: Optional[str] = None

    #: Set only when ``status`` becomes ``withdrawn``: a divergence that
    #: stopped diverging closed its own proposal rather than waiting forever
    #: for a human decision that was never coming. Distinct from
    #: ``decided_by``/``decided_at``, which record a HUMAN decision relayed
    #: through the gate — a self-closed proposal was decided by nobody.
    closed_reason: Optional[str] = None
    closed_at: Optional[datetime] = None
