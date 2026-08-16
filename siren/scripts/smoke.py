#!/usr/bin/env python3
"""Smoke every path in contracts/openapi.yaml against a RUNNING siren.

Reads the contract rather than a hand-kept list, so a path added to the
contract and forgotten in the service fails here instead of on stage.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
BASE = os.environ.get(
    "SIREN_URL", f"http://127.0.0.1:{os.environ.get('SIREN_PORT', '8603')}"
).rstrip("/")

#: Bodies and expectations per operation. Anything not listed is a plain GET
#: expected to return 200.
CASES: dict[tuple[str, str], dict] = {
    ("/signals/incident", "post"): {
        "body": {
            "id": "siren-smoke-F000000",
            "class": "fire.incident",
            "source": "seattle.fire.911",
            "ingest_time": "2026-08-16T00:00:00Z",
            "real_or_synthetic": "synthetic",
            "staleness": "PT0S",
        },
        "expect": (201,),
    },
    ("/feed/refresh", "post"): {"body": {}, "expect": (200,)},
    # Switched OFFLINE, deliberately. The BVT boots with OFFLINE_MODE=1 on a
    # runner with no route to data.seattle.gov, and the smoke must not be the
    # thing that puts a running siren back on the network.
    ("/feed/mode", "post"): {"body": {"mode": "offline"}, "expect": (200,)},
    ("/feed/emit", "post"): {"body": {}, "expect": (200,), "query": "?limit=2"},
    ("/hot-reload", "post"): {
        "body": {"path": str(ROOT / "config" / "incident.yaml"), "emit": 2},
        "expect": (200,),
    },
    # A templated path cannot be fetched literally; smoke a real registry id.
    ("/datasets/{id}", "get"): {"path": "/datasets/siren.seattle-fire-911"},
    ("/datasets/reload", "post"): {"body": {}, "expect": (200,)},
}


def call(method: str, path: str, body=None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        f"{BASE}{path}", data=data, method=method.upper(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
            raw = response.read().decode()
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                # /docs serves HTML. A non-JSON body is a fact about the
                # path, not a failure of the smoke.
                return response.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw


def main() -> int:
    spec = yaml.safe_load((ROOT / "contracts" / "openapi.yaml").read_text())
    failures: list[str] = []

    for path, item in sorted(spec["paths"].items()):
        for method in item:
            case = CASES.get((path, method), {})
            expected = case.get("expect", (200,))
            target = case.get("path", path) + case.get("query", "")
            status, body = call(method, target, case.get("body"))
            ok = status in expected
            print(f"{'ok  ' if ok else 'FAIL'} {method.upper():5} {path} -> {status}")
            if not ok:
                failures.append(f"{method.upper()} {path} -> {status}: {body}")

    status, docs = call("get", "/docs")
    print(f"{'ok  ' if status == 200 else 'FAIL'} GET   /docs -> {status}")
    if status != 200:
        failures.append("/docs did not serve")

    # The refusal is part of the contract surface: an invalid drop must come
    # back 422 with the offending rule named, and the class must keep flowing.
    status, refusal = call("post", "/hot-reload",
                           {"path": str(ROOT / "config" / "incident.invalid.yaml")})
    named = isinstance(refusal, dict) and (refusal.get("refusal") or {}).get("rule")
    print(f"{'ok  ' if status == 422 and named else 'FAIL'} POST  /hot-reload (invalid) "
          f"-> {status} rule={named}")
    if status != 422 or not named:
        failures.append(f"invalid config was not refused by name: {status} {refusal}")

    status, timeline = call("get", "/hot-reload/timeline")
    still = isinstance(timeline, dict) and timeline.get("registered_classes")
    print(f"{'ok  ' if still else 'FAIL'} previous config still flowing: {still}")
    if not still:
        failures.append("the refused drop dropped the running class")

    if failures:
        print("\nSMOKE FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
