"""helm's CLI — the console's actions, without a browser.

Every command here calls the same documented API path the console calls.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

from helm import __version__


def _base(args: argparse.Namespace) -> str:
    return str(args.url).rstrip("/")


def _print(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="helm", description="helm console CLI")
    parser.add_argument("--url", default="http://127.0.0.1:8610", help="helm base URL")
    parser.add_argument("--version", action="version", version=f"helm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("overview", help="Federation overview")
    sub.add_parser("feeds", help="Feed health")
    sub.add_parser("health", help="helm's own health")

    approvals = sub.add_parser("approvals", help="List the approval queue")
    approvals.add_argument("--state", default=None)

    decide = sub.add_parser("decide", help="Decide an approval")
    decide.add_argument("id")
    decide.add_argument("--decision", default="approve", choices=("approve", "reject"))
    decide.add_argument("--subject", required=True, help="signet subject (required)")
    decide.add_argument("--rationale", default=None)
    decide.add_argument("--as-agent", action="store_true", help="declare an agent caller")

    ledger = sub.add_parser("ledger", help="Tail the ledger")
    ledger.add_argument("--limit", type=int, default=12)

    walk = sub.add_parser("walk", help="Walk an effect back to its cause")
    walk.add_argument("effect_id")

    sub.add_parser("verify", help="Verify the hash chain")

    sub.add_parser("datasets", help="Every dataset the federation consumes")
    dataset = sub.add_parser("dataset", help="Inspect one dataset")
    dataset.add_argument("--id", required=True)

    sub.add_parser("composed", help="The composed state")

    ask = sub.add_parser("ask", help="Ask NemoClerk")
    ask.add_argument("message")
    ask.add_argument("--area", default="helm")

    # ------------------------------------------------------- warrant (datasets)
    # Command names are flat because contracts/opencli.yaml maps one name to
    # one documented path, and the contract test compares that set to argparse's
    # subcommand choices exactly.
    authority = sub.add_parser(
        "dataset-authority",
        help="Who may do what to a dataset, and who gave them the power to")
    authority.add_argument("dataset_id")
    authority.add_argument("--subject", default=None, help="narrow to one subject")
    authority.add_argument("--action", default=None, choices=("reader", "steward", "admin"))

    claim = sub.add_parser("dataset-claim",
                           help="Onboard a dataset; the caller becomes its first administrator")
    claim.add_argument("dataset_id")

    grant = sub.add_parser(
        "dataset-grant",
        help="Grant a role on a dataset. Refused if it would leave no administrator")
    grant.add_argument("dataset_id")
    grant.add_argument("--subject", required=True)
    grant.add_argument("--role", required=True, choices=("reader", "steward", "admin"))

    revoke = sub.add_parser("dataset-revoke", help="Withdraw a role from a subject")
    revoke.add_argument("dataset_id")
    revoke.add_argument("--subject", required=True)
    revoke.add_argument("--rationale", default=None)

    delegate = sub.add_parser(
        "dataset-delegate",
        help="Lend administrative authority with a MANDATORY expiry")
    delegate.add_argument("dataset_id")
    delegate.add_argument("--subject", required=True)
    delegate.add_argument("--role", default="admin", choices=("reader", "steward", "admin"))
    delegate.add_argument("--until", required=True,
                          help="ISO-8601 instant. Required: authority that never lapses is a grant.")

    bg_request = sub.add_parser(
        "breakglass-request",
        help="Ask a quorum of OTHER datasets' administrators to open a time-boxed window")
    bg_request.add_argument("dataset_id")
    bg_request.add_argument("--reason", required=True)
    bg_request.add_argument("--minutes", type=int, default=60)

    bg_vote = sub.add_parser("breakglass-vote", help="Vote to open a break-glass window")
    bg_vote.add_argument("dataset_id")
    bg_vote.add_argument("--request", required=True)

    bg_exit = sub.add_parser("breakglass-exit", help="Close a break-glass window")
    bg_exit.add_argument("dataset_id")
    bg_exit.add_argument("--request", required=True)
    bg_exit.add_argument("--reason", default="closed by hand")

    args = parser.parse_args(argv)
    base = _base(args)

    try:
        if args.command == "overview":
            _print(httpx.get(f"{base}/overview", timeout=15).json())
        elif args.command == "feeds":
            _print(httpx.get(f"{base}/feeds", timeout=15).json())
        elif args.command == "health":
            _print(httpx.get(f"{base}/healthz", timeout=15).json())
        elif args.command == "approvals":
            params = {"state": args.state} if args.state else None
            _print(httpx.get(f"{base}/approvals", params=params, timeout=15).json())
        elif args.command == "decide":
            body = {
                "decision": args.decision,
                "decided_by": args.subject,
                "caller_role": "agent" if args.as_agent else "human",
            }
            if args.rationale:
                body["rationale"] = args.rationale
            response = httpx.post(
                f"{base}/approvals/{args.id}/decide", json=body, timeout=30
            )
            _print(response.json())
            return 0 if response.is_success else 3
        elif args.command == "ledger":
            _print(httpx.get(f"{base}/ledger", params={"limit": args.limit}, timeout=15).json())
        elif args.command == "walk":
            _print(httpx.get(f"{base}/walk/{args.effect_id}/json", timeout=15).json())
        elif args.command == "datasets":
            _print(httpx.get(f"{base}/datasets/json", timeout=15).json())
        elif args.command == "dataset":
            response = httpx.get(f"{base}/datasets/json/{args.id}", timeout=15)
            _print(response.json())
            # An unknown dataset is a failure, not an empty print.
            return 0 if response.is_success else 3
        elif args.command == "verify":
            _print(httpx.get(f"{base}/ledger/verify", timeout=15).json())
        elif args.command == "composed":
            _print(
                httpx.get(
                    f"{base}/composed/state", timeout=15
                ).json()
            )
        elif args.command == "ask":
            _print(
                httpx.post(
                    f"{base}/nemoclerk/message",
                    json={"message": args.message, "feature_area": args.area},
                    timeout=120,
                ).json()
            )
        elif args.command == "dataset-authority":
            params = {}
            if args.subject:
                params["subject"] = args.subject
            if args.action:
                params["action"] = args.action
            response = httpx.get(
                f"{base}/datasets/{args.dataset_id}/authority",
                params=params or None, timeout=15)
            _print(response.json())
            # 503 is UNKNOWN, and it exits non-zero on purpose: a script that
            # cannot find out must not proceed as though it had.
            return 0 if response.is_success else (4 if response.status_code == 503 else 3)
        elif args.command in _WARRANT_POSTS:
            path, body = _warrant_post(args)
            response = httpx.post(f"{base}{path}", json=body, timeout=30)
            _print(response.json())
            return 0 if response.is_success else (4 if response.status_code == 503 else 3)
    except httpx.HTTPError as exc:
        print(f"helm: {exc}", file=sys.stderr)
        return 2
    return 0


_WARRANT_POSTS = frozenset({
    "dataset-claim", "dataset-grant", "dataset-revoke", "dataset-delegate",
    "breakglass-request", "breakglass-vote", "breakglass-exit",
})


def _warrant_post(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """One place that knows which documented path each command calls."""
    dataset = args.dataset_id
    if args.command == "dataset-claim":
        return f"/datasets/{dataset}/claim", {}
    if args.command == "dataset-grant":
        return f"/datasets/{dataset}/grants", {"subject": args.subject, "role": args.role}
    if args.command == "dataset-revoke":
        body: dict[str, Any] = {"subject": args.subject}
        if args.rationale:
            body["rationale"] = args.rationale
        return f"/datasets/{dataset}/revocations", body
    if args.command == "dataset-delegate":
        return f"/datasets/{dataset}/delegations", {
            "subject": args.subject, "role": args.role, "expires_at": args.until}
    if args.command == "breakglass-request":
        return f"/datasets/{dataset}/breakglass", {
            "reason": args.reason, "window_minutes": args.minutes}
    if args.command == "breakglass-vote":
        return f"/datasets/{dataset}/breakglass/{args.request}/vote", {}
    return f"/datasets/{dataset}/breakglass/{args.request}/exit", {"reason": args.reason}


if __name__ == "__main__":
    raise SystemExit(main())
