"""Append-only, hash-chained JSONL provenance ledger.

Every signal, judgment, effect, approval and refusal appends one line:

    {"seq": 1, "prev_hash": "...", "ts": "...", "type": "...",
     "body": {...}, "sha256": "..."}

Two properties carry the pitch and are therefore non-negotiable:

1. **Writes happen before effects.** Callers append the intent, then act.
   If the append fails, the effect does not run — see ``LedgerUnwritable``.
2. **Verification can fail.** ``verify()`` recomputes every digest and every
   link. Corrupting a single byte of any line makes it return
   ``valid: False`` naming the sequence number. A verify that cannot fail is
   not verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

try:  # POSIX only; the demo and CI both run on Linux.
    import fcntl

    LOCKING_AVAILABLE = True
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None
    LOCKING_AVAILABLE = False

GENESIS_PREV_HASH = "0" * 64

#: Fields covered by the digest. ``sha256`` itself is excluded, obviously.
_DIGESTED_FIELDS = ("seq", "prev_hash", "ts", "type", "body")


class LedgerUnwritable(RuntimeError):
    """The ledger could not be appended to.

    Raised so callers fail closed: no ledger line, no effect.
    """


def canonical(entry: dict[str, Any]) -> str:
    """Canonical serialization of the digested fields, stable across runs."""
    payload = {field: entry[field] for field in _DIGESTED_FIELDS}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def digest(entry: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(entry).encode("utf-8")).hexdigest()


class Ledger:
    """An append-only hash chain over a JSONL file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._seq, self._head = self._read_tail()

    # ------------------------------------------------------------------ read

    def _read_tail(self) -> tuple[int, str]:
        """Resume the chain from whatever is already on disk."""
        seq, head = 0, GENESIS_PREV_HASH
        if not self.path.exists():
            return seq, head
        try:
            handle = self.path.open("r", encoding="utf-8")
        except OSError as exc:
            # Unreadable at boot is fail-closed too: refuse to run rather than
            # start a second chain beside one we cannot see.
            raise LedgerUnwritable(f"ledger at {self.path} is unusable: {exc}") from exc
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A corrupt tail must not be silently overwritten; keep the
                    # counter moving so verify() can name the broken seq.
                    seq += 1
                    continue
                seq = int(entry.get("seq", seq + 1))
                head = str(entry.get("sha256", head))
        return seq, head

    def read(self) -> list[dict[str, Any]]:
        """All entries, parse errors included as ``{"_raw": ...}`` markers."""
        entries: list[dict[str, Any]] = []
        if not self.path.exists():
            return entries
        try:
            handle = self.path.open("r", encoding="utf-8")
        except OSError as exc:
            raise LedgerUnwritable(f"ledger at {self.path} is unusable: {exc}") from exc
        with handle:
            for lineno, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    entries.append({"_raw": line, "_lineno": lineno, "_error": str(exc)})
        return entries

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        entries = self.read()
        return entries[-limit:] if limit > 0 else entries

    @property
    def length(self) -> int:
        return self._seq

    @property
    def head(self) -> str:
        return self._head

    # ----------------------------------------------------------------- write

    def _tail_under_lock(self, handle: TextIO) -> tuple[int, str]:
        """Authoritative (seq, head) read from the file we hold the lock on.

        The in-memory counters are a cache; another process may have appended
        since we last looked. Trusting the cache is how a chain grows two
        branches with the same sequence numbers.
        """
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return 0, GENESIS_PREV_HASH

        window = min(size, 65536)
        handle.seek(size - window)
        chunk = handle.read()
        lines = [line for line in chunk.splitlines() if line.strip()]
        if not lines:
            return 0, GENESIS_PREV_HASH
        try:
            last = json.loads(lines[-1])
            return int(last["seq"]), str(last["sha256"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            # We cannot chain onto something we cannot read. Fail closed rather
            # than start a second branch.
            raise LedgerUnwritable(
                f"ledger tail at {self.path} is unreadable; refusing to append: {exc}"
            ) from exc

    def append(self, event_type: str, body: dict[str, Any]) -> dict[str, Any]:
        """Append one entry and return it. Raises ``LedgerUnwritable``.

        The write is flushed and fsynced before returning, so a caller that
        sees this return knows the line is durable — which is the whole point
        of writing before the effect runs.

        The whole read-chain-write is done under an exclusive file lock, so a
        second process (a restart racing a running instance, an operator who
        starts the service twice) extends the same chain instead of forking it.
        """
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a+", encoding="utf-8") as handle:
                    if LOCKING_AVAILABLE:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        seq, head = self._tail_under_lock(handle)
                        entry = {
                            "seq": seq + 1,
                            "prev_hash": head,
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "type": event_type,
                            "body": body,
                        }
                        entry["sha256"] = digest(entry)
                        line = json.dumps(
                            entry, sort_keys=True, separators=(",", ":"), default=str
                        )
                        handle.seek(0, os.SEEK_END)
                        handle.write(line + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    finally:
                        if LOCKING_AVAILABLE:
                            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                raise LedgerUnwritable(
                    f"ledger append failed for {event_type} at {self.path}: {exc}"
                ) from exc
            self._seq = entry["seq"]
            self._head = entry["sha256"]
            return entry

    # ---------------------------------------------------------------- verify

    def verify(self) -> dict[str, Any]:
        """Walk the chain. Returns the contract's verification object."""
        prev = GENESIS_PREV_HASH
        expected_seq = 0
        checked = 0

        for entry in self.read():
            expected_seq += 1
            if "_raw" in entry:
                return self._broken(
                    checked,
                    str(expected_seq),
                    f"line {entry['_lineno']} is not valid JSON: {entry['_error']}",
                )

            seq = entry.get("seq")
            if seq != expected_seq:
                return self._broken(
                    checked, str(expected_seq), f"sequence break: found seq={seq!r}"
                )
            if entry.get("prev_hash") != prev:
                return self._broken(
                    checked, str(seq), "prev_hash does not match the previous entry"
                )
            try:
                recomputed = digest(entry)
            except KeyError as exc:
                return self._broken(checked, str(seq), f"entry missing field {exc}")
            if recomputed != entry.get("sha256"):
                return self._broken(
                    checked, str(seq), "sha256 mismatch: entry content was altered"
                )

            prev = entry["sha256"]
            checked += 1

        return {"valid": True, "chain_length": checked, "broken_at": None, "detail": None}

    @staticmethod
    def _broken(checked: int, seq: str, detail: str) -> dict[str, Any]:
        return {
            "valid": False,
            "chain_length": checked,
            "broken_at": seq,
            "detail": detail,
        }

    # ------------------------------------------------------------- utilities

    def by_type(self, *types: str) -> Iterable[dict[str, Any]]:
        wanted = set(types)
        return (e for e in self.read() if e.get("type") in wanted)
