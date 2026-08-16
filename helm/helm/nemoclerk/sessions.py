"""Contextual sessions, keyed (signet subject, feature-area).

Each page is a DIFFERENT conversation, primed with that module's About card
and the currently selected event. Switching tabs switches sessions;
transcripts never bleed (``test://helm/nemoclerk-context``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

FEATURE_AREAS = (
    "helm",
    "docket",
    "breaker",
    "siren",
    "blindspot",
    "composed",
    "approval-detail",
    "admin",
)


@dataclass
class Turn:
    role: str  # "you" | "nemoclerk"
    text: str
    chips: list[dict[str, Any]] = field(default_factory=list)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "text": self.text, "chips": self.chips, "at": self.at}


@dataclass
class Session:
    subject: str
    feature_area: str
    priming: str = ""
    turns: list[Turn] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.subject, self.feature_area)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "feature_area": self.feature_area,
            "priming": self.priming,
            "turns": [t.to_dict() for t in self.turns],
        }


class SessionStore:
    """In-memory sessions. A session is (subject, feature-area) — nothing else."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, str], Session] = {}
        self._lock = threading.Lock()

    def get(self, subject: str, feature_area: str, priming: str = "") -> Session:
        key = (subject, feature_area)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = Session(
                    subject=subject, feature_area=feature_area, priming=priming
                )
                self._sessions[key] = session
            elif priming and not session.priming:
                session.priming = priming
            return session

    def append(self, subject: str, feature_area: str, turn: Turn) -> Session:
        session = self.get(subject, feature_area)
        with self._lock:
            session.turns.append(turn)
            # keep transcripts bounded; the rail shows the recent exchange
            if len(session.turns) > 40:
                del session.turns[:-40]
        return session

    def clear_subject(self, subject: str) -> None:
        """Sign-out clears every session belonging to that subject."""
        with self._lock:
            for key in [k for k in self._sessions if k[0] == subject]:
                del self._sessions[key]

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return sorted(self._sessions)
