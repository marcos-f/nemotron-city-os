"""Wire models. Field-for-field with contracts/openapi.yaml."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Signal(BaseModel):
    """A permit document entering the federation. Mirrors throughline's Signal."""

    id: str
    cls: str = Field(alias="class")
    source: str
    ingest_time: str
    real_or_synthetic: Literal["real", "synthetic"]
    payload_ref: str | None = None
    staleness: str | None = None

    model_config = {"populate_by_name": True}


class Judgment(BaseModel):
    """A judgment card, or an abstention.

    `finding` is prose; `quote` is the verbatim span it rests on. When
    `abstained` is true there is no quote and no route proposal — that is the
    whole point of the abstention path.
    """

    finding: str
    confidence: float = Field(ge=0.0, le=1.0)
    citations: list[str]
    abstained: bool

    id: str | None = None
    signal_id: str | None = None
    permitnum: str | None = None
    quote: str | None = None
    route_proposal: str | None = None
    abstain_reason: str | None = None
    mock: bool = False
    model: str | None = None
    produced_at: str | None = None
    quote_check: dict[str, Any] | None = None
    attempts: int = 1


class RouteEffect(BaseModel):
    """The reversible effect docket asks throughline to gate."""

    id: str
    reversibility: Literal["reversible", "irreversible"] = "reversible"
    status: str
    signal_id: str | None = None
    judgment_id: str | None = None
    description: str | None = None
    substrate: str = "mock"
    ledgered: bool = False


class PermitSummary(BaseModel):
    permitnum: str
    permitclass: str | None = None
    permittypedesc: str | None = None
    description: str = ""
    statuscurrent: str | None = None
    originaladdress1: str | None = None
    applieddate: str | None = None
    judgeable: bool = True
