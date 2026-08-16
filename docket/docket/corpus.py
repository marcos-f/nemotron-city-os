"""The offline permit corpus.

Loaded once from data/permits.json, which scripts/snapshot_permits.py wrote
from Socrata. Nothing here touches the network — after the snapshot, the demo
runs with the cable unplugged.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import config


class CorpusMissing(RuntimeError):
    """The snapshot has not been taken. Says how to fix it."""


@lru_cache(maxsize=4)
def _load(path_str: str) -> dict:
    path = Path(path_str)
    if not path.exists():
        raise CorpusMissing(
            f"permit snapshot missing at {path} — run scripts/snapshot_permits.py"
        )
    return json.loads(path.read_text())


def snapshot(path: Path | None = None) -> dict:
    return _load(str(path or config.PERMITS_PATH))


def permits(path: Path | None = None) -> list[dict]:
    return snapshot(path)["permits"]


def count(path: Path | None = None) -> int:
    return len(permits(path))


def get(permitnum: str, path: Path | None = None) -> dict | None:
    for row in permits(path):
        if row.get("permitnum") == permitnum:
            return row
    return None


def description_of(permit: dict) -> str:
    return (permit.get("description") or "").strip()


def reset_cache() -> None:
    """Tests point config at fixture corpora; the cache must not outlive that."""
    _load.cache_clear()
