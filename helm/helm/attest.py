"""The one principle every layer above the ledger must obey.

throughline never claims what it cannot prove. For a while the layers above
it did: the approve path took the caller's word for who was approving, the
gate display printed a confident ``0 waiting`` when it could not reach the
gate at all, the stage labels said "human-signed" over an effect that
auto-executed, and an admin action reported a class "flowing" while the
component that would have flowed it was down.

Those were five patches waiting to happen. They are one rule instead:

    **A value has THREE states, never two: known-true, known-false, and
    UNKNOWN. Unknown may never render as a confident zero, empty, green or
    success.**

``Reading`` carries that third state through every layer, and ``LastSeen``
remembers the last value that WAS known, so an outage can say "last seen 6
held at 06:12:41Z" instead of either lying or going blank.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class Reading:
    """A value read from a component, and whether we actually know it."""

    known: bool
    value: Any = None
    source: str = ""
    detail: str = ""
    as_of: str = ""
    last_known: Any = None
    last_seen_at: str = ""

    # ------------------------------------------------------------ builders
    @classmethod
    def seen(cls, value: Any, source: str = "") -> "Reading":
        return cls(known=True, value=value, source=source, as_of=_now())

    @classmethod
    def unknown(
        cls,
        source: str = "",
        detail: str = "",
        last_known: Any = None,
        last_seen_at: str = "",
    ) -> "Reading":
        return cls(
            known=False,
            value=None,
            source=source,
            detail=detail or f"{source} is not responding",
            last_known=last_known,
            last_seen_at=last_seen_at,
        )

    # ------------------------------------------------------------- reading
    @property
    def has_history(self) -> bool:
        return not self.known and self.last_known is not None

    def or_none(self) -> Any:
        """The value, or None. Never a substitute figure."""
        return self.value if self.known else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "known": self.known,
            "value": self.value,
            "source": self.source,
            "detail": self.detail,
            "as_of": self.as_of,
            "last_known": self.last_known,
            "last_seen_at": self.last_seen_at,
        }

    # -------------------------------------------------------------- phrase
    def phrase(self, unit: str = "", *, unknown_label: str = "unknown") -> str:
        """Plain words for a reader, including when the answer is unknown."""
        if self.known:
            return f"{self.value}{(' ' + unit) if unit else ''}"
        if self.has_history:
            return (
                f"{unknown_label} — {self.source} not responding; last seen "
                f"{self.last_known}{(' ' + unit) if unit else ''}"
                + (f" at {self.last_seen_at}" if self.last_seen_at else "")
            )
        return f"{unknown_label} — {self.detail}"


class LastSeen:
    """The last value that was actually known, persisted across restarts.

    An outage is the moment an operator most needs a number, and the moment
    the system is least able to produce one. Remembering the last KNOWN
    value — with its timestamp — is the honest middle between inventing a
    figure and showing a blank.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def record(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data[key] = {"value": value, "at": _now()}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
                os.replace(tmp, self.path)
            except OSError:
                pass  # a cache that cannot persist is still useful in memory

    def get(self, key: str) -> tuple[Any, str]:
        with self._lock:
            row = self._load().get(key)
        if not isinstance(row, dict):
            return None, ""
        return row.get("value"), str(row.get("at", ""))

    def reading(self, key: str, source: str, detail: str = "") -> Reading:
        """An UNKNOWN reading enriched with whatever was last true."""
        value, at = self.get(key)
        return Reading.unknown(
            source=source, detail=detail, last_known=value, last_seen_at=at
        )

    def observe(self, key: str, value: Any, source: str = "") -> Reading:
        """Record a value that IS known, and return it as a Reading."""
        self.record(key, value)
        return Reading.seen(value, source=source)


@dataclass
class Step:
    """One step of a multi-step action, which may not have happened."""

    name: str
    state: str  # "ok" | "skipped" | "refused" | "unknown"
    detail: str = ""

    @property
    def happened(self) -> bool:
        return self.state == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {"step": self.name, "state": self.state, "detail": self.detail}


@dataclass
class ActionResult:
    """The outcome of an action, which reports only what actually occurred.

    An action is the sharpest form of the rule: a display that overstates is
    misleading, but an action that reports an effect which did not happen is
    a false record of the world.
    """

    action: str
    performed: bool
    target: str = ""
    substrate: str = ""
    detail: str = ""
    steps: list[Step] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def summary(self) -> str:
        if self.performed:
            head = f"{self.action} {self.target}".strip()
            tail = "; ".join(f"{k}: {v}" for k, v in self.counts.items())
            if self.substrate and self.substrate != "real":
                head += f" (substrate: {self.substrate})"
            return f"{head}{' — ' + tail if tail else ''}"
        return f"{self.action} {self.target} DID NOT RUN — {self.detail}".strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "performed": self.performed,
            "target": self.target,
            "substrate": self.substrate,
            "detail": self.detail,
            "steps": [s.to_dict() for s in self.steps],
            "counts": self.counts,
            "summary": self.summary,
        }
