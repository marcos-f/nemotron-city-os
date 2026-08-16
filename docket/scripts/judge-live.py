#!/usr/bin/env python3
"""Run REAL judgments against nemotron-3-super-120b and cache every response.

This is the SG3 step. It exists so that flipping docket from MOCK to real is a
single command rather than a code change:

    export NVIDIA_API_KEY=$(cd ~/source/git.infra/shared-secrets \
      && sops -d --extract '["env"]["NVIDIA_API_KEY"]' .secrets/nvidia-env.yaml)
    python3 scripts/judge-live.py --limit 12

Every hosted response lands in data/cache/ keyed by model+permit+attempt, so the
demo replays offline afterwards and keeps working if the key later expires.

Judgments are validated by exactly the same verbatim-quote validator the mock
path uses. A real model that paraphrases is rejected here too — that is the
point, and the summary reports how often it happened.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from docket import config, corpus  # noqa: E402
from docket.clients.nvidia import HostedJudgeClient, JudgeUnavailable  # noqa: E402
from docket.judge import judge_permit  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=12,
                    help="how many judgeable permits to run")
    ap.add_argument("--permit", action="append", default=[],
                    help="specific permitnum(s); repeatable")
    ap.add_argument("--include-thin", action="store_true",
                    help="also run permits below the evidence threshold")
    args = ap.parse_args(argv)

    if not config.api_key():
        print(
            "NVIDIA_API_KEY is not set — refusing to run.\n"
            "This script exists to exercise the REAL model; running it without a "
            "key would only prove the mock works.",
            file=sys.stderr,
        )
        return 2

    if args.permit:
        rows = [r for p in args.permit if (r := corpus.get(p)) is not None]
    else:
        rows = [
            r for r in corpus.permits()
            if args.include_thin
            or len((r.get("description") or "").strip()) >= config.EVIDENCE_MIN_CHARS
        ][: args.limit]

    if not rows:
        print("no permits selected", file=sys.stderr)
        return 1

    client = HostedJudgeClient()
    accepted = abstained = regenerated = uncited = 0

    for row in rows:
        permitnum = row.get("permitnum")
        try:
            judgment = judge_permit(row, client=client)
        except JudgeUnavailable as exc:
            print(f"  {permitnum}: model unavailable — {exc}", file=sys.stderr)
            return 1

        if judgment.abstained:
            abstained += 1
            if (judgment.abstain_reason or "").startswith("uncited"):
                uncited += 1
            print(f"  {permitnum}: ABSTAIN — {judgment.abstain_reason}")
        else:
            accepted += 1
            if judgment.attempts > 1:
                regenerated += 1
            print(
                f"  {permitnum}: accepted (attempts={judgment.attempts}, "
                f"conf={judgment.confidence:.2f})"
            )
            print(f"      quote: {judgment.quote[:110]}")

    cached = len(list(Path(config.CACHE_DIR).glob("judgment-*.json")))
    summary = {
        "model": config.JUDGMENT_MODEL,
        "permits_run": len(rows),
        "accepted": accepted,
        "abstained": abstained,
        "of_which_uncited": uncited,
        "regenerated_once_then_accepted": regenerated,
        "cached_responses": cached,
    }
    print("\n" + json.dumps(summary, indent=2))
    print(
        f"\n{cached} hosted responses cached under {config.CACHE_DIR} — "
        "the demo path now replays offline."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
