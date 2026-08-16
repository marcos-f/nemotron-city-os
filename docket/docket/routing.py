"""Route effects.

An accepted judgment produces one reversible effect: route this permit to a
review desk. It auto-executes — no approval queue — and the ledger entry binds
effect -> judgment -> signal so the route can be walked back to the sentence it
came from.

An abstention produces NO effect. That asymmetry is the product.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .models import Judgment, RouteEffect, Signal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RouteRefused(RuntimeError):
    """Refused before it reached the substrate, with the reason named."""


def signal_for(permit: dict) -> Signal:
    """A permit document as a federation signal."""
    permitnum = permit.get("permitnum", "unknown")
    return Signal(
        id=f"sig-permit-{permitnum}",
        **{"class": "permit.document"},
        source="socrata:76t5-zqzr",
        ingest_time=_now(),
        real_or_synthetic="real",
        payload_ref=f"data/permits.json#{permitnum}",
        staleness="snapshot",
    )


def route_judgment(judgment: Judgment, client, permit: dict | None = None) -> RouteEffect:
    """Send the route effect for an accepted judgment to the substrate."""
    if judgment.abstained:
        raise RouteRefused(
            "abstention proposes no route — nothing to execute or reverse"
        )
    if not judgment.quote:
        raise RouteRefused("uncited judgment cannot be routed")

    signal_id = judgment.signal_id or (
        f"sig-permit-{judgment.permitnum}" if judgment.permitnum else None
    )

    if permit is not None:
        client.post_signal(signal_for(permit).model_dump(by_alias=True))

    ledgered_judgment = client.post_judgment(
        {
            "id": judgment.id,
            "signal_id": signal_id,
            "verdict": judgment.finding,
            "produced_by": judgment.model or "docket",
            "rationale": judgment.quote,
            "confidence": judgment.confidence,
            "real_or_synthetic": "synthetic" if judgment.mock else "real",
            "produced_at": judgment.produced_at,
        }
    )

    desk = judgment.route_proposal or "General review desk"
    raw = client.post_effect(
        {
            "id": f"eff-route-{uuid.uuid4().hex[:12]}",
            "reversibility": "reversible",
            "status": "proposed",
            "signal_id": signal_id,
            "judgment_id": ledgered_judgment.get("id", judgment.id),
            "description": f"route permit {judgment.permitnum} to {desk}",
        }
    )

    return RouteEffect(
        id=raw.get("id"),
        reversibility=raw.get("reversibility", "reversible"),
        status=raw.get("status", "proposed"),
        signal_id=raw.get("signal_id", signal_id),
        judgment_id=raw.get("judgment_id", judgment.id),
        description=raw.get("description"),
        substrate=raw.get("substrate", getattr(client, "substrate", "mock")),
        ledgered=bool(raw.get("ledgered", True)),
    )
