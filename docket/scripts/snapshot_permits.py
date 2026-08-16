#!/usr/bin/env python3
"""Snapshot Seattle building permits (Socrata 76t5-zqzr) to data/permits.json.

Run ONCE with network. Everything downstream reads the snapshot, so the demo
path is offline from here on. Re-running overwrites the snapshot.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DATASET = "76t5-zqzr"
ENDPOINT = f"https://data.seattle.gov/resource/{DATASET}.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "permits.json"

# Fields the judgment pipeline and the queue actually use. Everything else is
# dropped so the snapshot stays a demo corpus, not a data dump.
KEEP = (
    "permitnum",
    "permitclass",
    "permitclassmapped",
    "permittypemapped",
    "permittypedesc",
    "description",
    "statuscurrent",
    "originaladdress1",
    "originalcity",
    "originalstate",
    "originalzip",
    "applieddate",
    "issueddate",
    "estprojectcost",
    "housingunits",
    "latitude",
    "longitude",
)


def fetch(limit: int, timeout: int = 60) -> list[dict]:
    # Newest applications first: a reviewer's queue is not a random sample.
    query = urllib.parse.urlencode(
        {"$limit": str(limit), "$order": "applieddate DESC"}
    )
    req = urllib.request.Request(
        f"{ENDPOINT}?{query}", headers={"Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def slim(row: dict) -> dict:
    out = {k: row[k] for k in KEEP if k in row}
    # Socrata omits description entirely when blank; the abstention path needs
    # the field to exist so "empty" is a value rather than a KeyError.
    out.setdefault("description", "")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    rows = [slim(r) for r in fetch(args.limit)]
    rows = [r for r in rows if r.get("permitnum")]

    snapshot = {
        "dataset": DATASET,
        "source": ENDPOINT,
        "snapshot_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "real_or_synthetic": "real",
        "count": len(rows),
        "permits": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")

    empty = sum(1 for r in rows if not (r.get("description") or "").strip())
    print(f"snapshot: {len(rows)} permits -> {args.out}")
    print(f"snapshot: {empty} with empty description (abstention path)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
