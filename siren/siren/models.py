"""Wire types. These mirror contracts/openapi.yaml exactly — the contract is
the source of truth and the tests assert the two never drift apart."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class Signal(BaseModel):
    """The federation-wide envelope. Identical in every component; that
    sameness is the point siren exists to prove — incident is not telemetry,
    yet the envelope does not change."""

    id: str
    signal_class: str = Field(alias="class")
    source: str
    ingest_time: str
    real_or_synthetic: Literal["real", "synthetic"]
    payload_ref: Optional[str] = None
    staleness: Optional[str] = None

    model_config = {"populate_by_name": True}

    def wire(self) -> dict:
        """Serialize under the contract's field names (``class``, not
        ``signal_class``), dropping unset optionals."""
        return self.model_dump(by_alias=True, exclude_none=True)


class Incident(BaseModel):
    """One map-ready 911 record.

    The timestamp is carried three ways on purpose. ``reported_at`` is the
    instant in UTC, ``reported_at_local`` is the same instant on the Seattle
    wall clock the source actually wrote, and ``tz`` names the zone the
    conversion went through. A reader can check the arithmetic instead of
    trusting it — which matters, because the arithmetic was wrong by seven
    hours and nothing downstream could tell: the values were well-formed.

    ``reported_at`` is nullable, and null is the honest answer when the source
    row carried no usable timestamp. ``reported_at_missing`` says so
    explicitly, so a pane renders "time unknown" rather than an invented one.
    """

    id: str
    incident_type: str
    lat: float
    lon: float
    reported_at: Optional[str] = None
    reported_at_local: Optional[str] = None
    tz: str = "America/Los_Angeles"
    reported_at_missing: bool = False
    address: Optional[str] = None


class FeedStatus(BaseModel):
    """The as-of label the UI is contractually required to render.

    ``source`` is never inferred by the caller: when siren serves cache it
    says so here and sets ``label`` to the words the pane displays.
    """

    source: Literal["live", "snapshot"]
    label: str
    as_of: str
    fetched_at: Optional[str] = None
    incident_count: int
    mode: Literal["live", "offline"]
    substrate: Literal["mock", "real"]
    stale_seconds: Optional[int] = None

    #: The zone the source writes its wall clocks in, named so a reader can
    #: check the conversion instead of trusting it.
    tz: str = "America/Los_Angeles"
    #: The freshest incident's UTC timestamp, and its age against ``as_of``.
    #: A negative or wildly large age means the conversion has regressed —
    #: this pair is what made the seven-hour bug visible.
    newest_reported_at: Optional[str] = None
    newest_age_seconds: Optional[int] = None
    #: Rows the source gave no usable timestamp for. They are served with
    #: ``reported_at: null``, never with an invented time.
    undated_incidents: int = 0
    #: True when the snapshot being served was repaired on read from a
    #: pre-timezone-fix cache rather than re-cut from the source.
    snapshot_migrated: bool = False


class Pulse(BaseModel):
    """City pulse: the incident list bound to the as-of label that qualifies
    it. One payload so a pane can never render rows without their provenance."""

    status: FeedStatus
    incidents: list[Incident]
