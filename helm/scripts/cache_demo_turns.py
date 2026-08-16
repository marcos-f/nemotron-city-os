#!/usr/bin/env python3
"""Record every scripted demo turn to disk so the demo replays offline.

Each prompt chip on each page — the buttons the operator actually clicks on
stage — is asked once against the live model, and the PHRASING is written to
``data/model-cache/scripted/``. With the network off, NemoClerk replays that
phrasing over freshly executed tool calls, and labels the turn as cached.

The facts are never cached: the tool calls run for real every time. Only the
sentence that wraps them is replayed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from helm.pages import PAGES  # noqa: E402

EXTRA_TURNS = {
    "helm": ["what is waiting for approval?", "is the ledger intact?"],
    "breaker": ["approve it", "what happens if nobody approves?"],
    "siren": ["what changed in the config?"],
    "composed": ["why did this compose?"],
    "approval-detail": ["who signed this?"],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8610")
    args = parser.parse_args()

    recorded: list[dict[str, str]] = []
    for slug, page in PAGES.items():
        prompts = list(page.chips) + EXTRA_TURNS.get(slug, [])
        for prompt in prompts:
            started = time.time()
            try:
                response = httpx.post(
                    f"{args.base}/nemoclerk/message",
                    json={"message": prompt, "feature_area": slug},
                    timeout=180,
                )
                answer = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                print(f"  [skip] {slug}: {prompt!r}: {exc}")
                continue
            elapsed = time.time() - started
            recorded.append(
                {
                    "page": slug,
                    "prompt": prompt,
                    "text": answer.get("text", ""),
                    "source": answer.get("source", ""),
                    "tier": answer.get("tier", ""),
                    "chips": [c["chip"] for c in answer.get("chips", [])],
                    "seconds": round(elapsed, 2),
                }
            )
            print(
                f"  [{answer.get('source', '?'):5}] {elapsed:5.1f}s {slug:16} "
                f"{prompt[:44]:44} -> {len(answer.get('chips', []))} chip(s)"
            )

    # Write the phrasings into the scripted cache the offline path reads.
    from helm.app import create_app
    from helm.config import load_settings

    app = create_app(load_settings())
    clerk = app.state.clerk
    # Only a MODEL-sourced phrasing is worth banking. Caching the mock
    # composer's own sentence and replaying it as "cached" would dress a
    # fallback up as a recording.
    banked = 0
    for turn in recorded:
        if turn["text"] and turn["source"] == "model":
            clerk.scripted_cache_write(turn["prompt"], turn["text"])
            banked += 1
    if not banked:
        print(
            "\nNo model rung answered: nothing banked. The offline demo still "
            "runs on the labelled MOCK composer over live tool results.\n"
            "Re-run this when a model endpoint is reachable."
        )

    out = ROOT / "artifacts" / "demo-turns.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    # A JSON OBJECT, so scripts/evidence can stamp it with the chain head.
    # Sequence numbers go stale within minutes on a live chain; the head hash
    # is what makes an artifact checkable later.
    out.write_text(
        json.dumps(
            {
                "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "banked_from_a_live_model": banked,
                "turns": recorded,
            },
            indent=2,
        )
    )
    scripted = ROOT / "data" / "model-cache" / "scripted"
    cached = len(list(scripted.glob("*.json"))) if scripted.exists() else 0
    print(
        f"\n{len(recorded)} turns recorded, {banked} banked from a live model, "
        f"{cached} scripted phrasings on disk"
    )
    print(f"written to {out}")
    return 0 if recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
