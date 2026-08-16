"""The cause walk: effect -> judgment -> signal, in at most three hops.

Each hop is a real ledger entry, and each hop's digest is recomputed at read
time rather than trusted from the file. helm renders these hops with a tick per
verified hash; a tick that cannot go red would be decoration, so
``verified: false`` is a first-class outcome here.
"""

from __future__ import annotations

from typing import Any, Optional

from .ledger import Ledger, digest

MAX_HOPS = 3


def _hop(kind: str, entry: dict[str, Any]) -> dict[str, Any]:
    sha = entry.get("sha256")
    try:
        recomputed = digest(entry)
    except (KeyError, TypeError):
        recomputed = None
    return {
        "kind": kind,
        "seq": entry.get("seq"),
        "type": entry.get("type"),
        "ts": entry.get("ts"),
        "id": (entry.get("body") or {}).get("id"),
        "sha256": sha,
        "hash_prefix": (sha or "")[:8],
        "prev_hash": entry.get("prev_hash"),
        "verified": recomputed is not None and recomputed == sha,
        "body": entry.get("body"),
    }


def walk_effect(ledger: Ledger, effect_id: str) -> Optional[dict[str, Any]]:
    """Resolve an effect back to its cause. ``None`` if the effect is unknown."""
    entries = [e for e in ledger.read() if "_raw" not in e]

    effect_entries = [
        e for e in entries
        if str(e.get("type", "")).startswith("effect.")
        and (e.get("body") or {}).get("id") == effect_id
    ]
    if not effect_entries:
        return None

    # The proposal is the hop that names the cause; later entries only restate it.
    origin = next(
        (e for e in effect_entries if e["type"] == "effect.proposed"), effect_entries[0]
    )
    body = origin.get("body") or {}
    hops = [_hop("effect", origin)]
    missing: list[str] = []

    judgment_id = body.get("judgment_id")
    signal_id = body.get("signal_id")

    if judgment_id:
        judgment = next(
            (e for e in entries
             if e.get("type") == "judgment.recorded"
             and (e.get("body") or {}).get("id") == judgment_id),
            None,
        )
        if judgment is None:
            missing.append(f"judgment {judgment_id}")
        else:
            hops.append(_hop("judgment", judgment))
            signal_id = signal_id or (judgment.get("body") or {}).get("signal_id")

    if signal_id:
        signal = next(
            (e for e in entries
             if e.get("type") == "signal.ingested"
             and (e.get("body") or {}).get("id") == signal_id),
            None,
        )
        if signal is None:
            missing.append(f"signal {signal_id}")
        else:
            hops.append(_hop("signal", signal))
    else:
        missing.append("signal (the effect names none)")

    return {
        "effect_id": effect_id,
        "hops": hops,
        "hop_count": len(hops),
        "complete": not missing and any(h["kind"] == "signal" for h in hops),
        "missing": missing,
        "all_hops_verified": all(hop["verified"] for hop in hops),
        "latest_status": (effect_entries[-1].get("body") or {}).get("status"),
    }
