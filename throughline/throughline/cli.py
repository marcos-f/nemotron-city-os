"""The `throughline` command line, implementing contracts/opencli.yaml.

Every command in that contract exists here under the same name and with the
same flags; ``tests/test_cli.py`` asserts that against the contract file, so
the two cannot drift apart quietly.

The CLI is a client, not a second implementation: it drives the HTTP API, so
the gate, the ledger and the refusal behave identically whether a human, an
agent or a script is calling. `ledger verify` exits non-zero on a broken chain
— a verify that cannot fail the build is not verification.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

DEFAULT_URL = os.environ.get(
    "THROUGHLINE_URL", f"http://127.0.0.1:{os.environ.get('THROUGHLINE_PORT', '8600')}"
)

Transport = Callable[[str, str, Optional[dict[str, Any]]], tuple[int, Any]]


def caller_token() -> str:
    """The caller token this terminal can present, or ``""``.

    ``THROUGHLINE_CALLER_TOKEN`` first, then the substrate's own
    ``<data-dir>/caller-token`` file — which is how a local operator
    authenticates to a local substrate without a secret in shell history.
    Reading the file is the authentication: it is mode 0600, so being able
    to read it means being the user the substrate runs as.

    Empty when neither exists, in which case privileged acts (a decide, an
    ``authz.*`` effect) are refused. Everything else still works.
    """
    from_env = os.environ.get("THROUGHLINE_CALLER_TOKEN")
    if from_env:
        return from_env
    data_dir = os.environ.get("THROUGHLINE_DATA_DIR", "data")
    try:
        with open(os.path.join(data_dir, "caller-token"), encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def http_transport(base_url: str) -> Transport:
    def call(method: str, path: str, body: Optional[dict[str, Any]] = None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        token = caller_token()
        if token:
            headers["X-Throughline-Caller-Token"] = token
        request = urllib.request.Request(
            base_url + path, data=data, method=method, headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload or b"null")
            except json.JSONDecodeError:
                return exc.code, {"detail": payload.decode(errors="replace")}
        except urllib.error.URLError as exc:
            return 0, {"detail": f"cannot reach throughline at {base_url}: {exc.reason}"}

    return call


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="throughline",
        description="signals in, gated effects out, every consequence traceable to its cause.",
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="service base URL")
    groups = parser.add_subparsers(dest="group", required=True)

    serve = groups.add_parser("serve", help="Run the service")
    serve.add_argument("--host", default=os.environ.get("THROUGHLINE_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("THROUGHLINE_PORT", 8600)))
    serve.add_argument(
        "--data-dir", default=os.environ.get("THROUGHLINE_DATA_DIR", "data"),
        help="directory holding the ledger and the approvals cache",
    )
    serve.add_argument(
        "--ledger", default=os.environ.get("THROUGHLINE_LEDGER") or None,
        help="serve against a specific ledger file instead of "
             "<data-dir>/ledger.jsonl. Point this at fixtures/demo-ledger.jsonl "
             "to run a demo against the pinned chain, whose head hash is "
             "recorded in fixtures/demo-ledger.head.json, so recorded evidence "
             "keeps citing sequence numbers that are still true. Omit it and "
             "the live ledger behaves exactly as before.",
    )

    signal = groups.add_parser("signal", help="Signal commands").add_subparsers(
        dest="command", required=True)
    ingest = signal.add_parser("ingest", help="Ingest a signal into throughline")
    ingest.add_argument("--class", dest="signal_class", required=True)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--payload-ref")
    ingest.add_argument("--id")
    ingest.add_argument("--staleness")
    ingest.add_argument("--real-or-synthetic", choices=("real", "synthetic"),
                        default="synthetic")

    effect = groups.add_parser("effect", help="Effect commands").add_subparsers(
        dest="command", required=True)
    propose = effect.add_parser("propose", help="Propose a reversibility-typed effect")
    propose.add_argument("--signal-id")
    propose.add_argument("--reversibility", choices=("reversible", "irreversible"))
    propose.add_argument("--description")
    propose.add_argument("--effect-type")
    propose.add_argument("--judgment-id")
    propose.add_argument("--id")
    get = effect.add_parser("get", help="Get an effect's status")
    get.add_argument("--id", required=True)
    walk = effect.add_parser("walk", help="Walk an effect back to its cause")
    walk.add_argument("--id", required=True)

    ledger = groups.add_parser("ledger", help="Ledger commands").add_subparsers(
        dest="command", required=True)
    ledger.add_parser("verify", help="Verify the hash-chained provenance ledger")

    dataset = groups.add_parser("dataset", help="Dataset registry commands").add_subparsers(
        dest="command", required=True)
    dataset.add_parser("list", help="List every dataset this component consumes")
    show = dataset.add_parser("show", help="Show one dataset's provenance and licence")
    show.add_argument("--id", required=True)
    validate = dataset.add_parser(
        "validate", help="Validate a dataset registry file without loading it")
    validate.add_argument("--path", default=None,
                          help="registry file to validate (default: the running one)")

    approval = groups.add_parser("approval", help="Approval commands").add_subparsers(
        dest="command", required=True)
    decide = approval.add_parser("decide", help="Record a human decision on a queued approval")
    decide.add_argument("--id", required=True)
    decide.add_argument("--decision", required=True, choices=("approve", "reject"))
    decide.add_argument("--decided-by", required=True,
                        help="OIDC subject of the human approver")
    decide.add_argument(
        "--caller-role", choices=("human", "agent"),
        default=os.environ.get("THROUGHLINE_CALLER_ROLE") or "human",
        help="who is deciding. The service refuses a decision that does not "
             "declare one, and refuses an agent approving an irreversible "
             "effect. Defaults to 'human' because a person at a terminal is "
             "the CLI's caller; pass --caller-role agent from automation.",
    )
    decide.add_argument("--rationale")
    approval.add_parser("list", help="List the approval queue")

    return parser


def main(argv: Optional[list[str]] = None, transport: Optional[Transport] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.group == "serve":
        import uvicorn

        from .app import create_app
        from .service import Settings

        from pathlib import Path

        settings = Settings(
            host=args.host,
            port=args.port,
            data_dir=Path(args.data_dir),
            ledger_file=Path(args.ledger) if args.ledger else None,
        )
        print(f"throughline serving on {settings.host}:{settings.port} "
              f"ledger={settings.ledger_path}")
        uvicorn.run(create_app(settings), host=settings.host, port=settings.port)
        return 0

    # `dataset validate` runs against a FILE, not the service, so an operator
    # can check a registry before dropping it in — and so a refusal is visible
    # without a running substrate. It exits non-zero on a refusal: a validate
    # that cannot fail the build is not validation.
    if args.group == "dataset" and args.command == "validate":
        from .config import ConfigRefusal
        from .datasets import load_registry
        from .datasets_api import registry_path

        path = args.path or registry_path()
        try:
            registry = load_registry(path)
        except ConfigRefusal as refusal:
            _emit(refusal.as_dict())
            return 1
        _emit({"refused": False, **registry.as_dict()})
        return 0

    call = transport or http_transport(args.url)

    if args.group == "signal":
        body = {
            "id": args.id or f"sig-{os.urandom(6).hex()}",
            "class": args.signal_class,
            "source": args.source,
            "real_or_synthetic": args.real_or_synthetic,
        }
        if args.payload_ref:
            body["payload_ref"] = args.payload_ref
        if args.staleness:
            body["staleness"] = args.staleness
        status, payload = call("POST", "/signals", body)

    elif args.group == "effect" and args.command == "propose":
        body: dict[str, Any] = {}
        for key, value in (
            ("id", args.id), ("signal_id", args.signal_id),
            ("reversibility", args.reversibility), ("description", args.description),
            ("effect_type", args.effect_type), ("judgment_id", args.judgment_id),
        ):
            if value:
                body[key] = value
        status, payload = call("POST", "/effects", body)

    elif args.group == "effect" and args.command == "get":
        status, payload = call("GET", f"/effects/{args.id}", None)

    elif args.group == "effect" and args.command == "walk":
        status, payload = call("GET", f"/effects/{args.id}/walk", None)

    elif args.group == "ledger":
        status, payload = call("GET", "/ledger/verify", None)
        _emit(payload)
        # A broken chain is a failing exit code, not a printed shrug.
        return 0 if status == 200 and payload.get("valid") else 1

    elif args.group == "dataset" and args.command == "list":
        status, payload = call("GET", "/datasets", None)

    elif args.group == "dataset" and args.command == "show":
        status, payload = call("GET", f"/datasets/{args.id}", None)

    elif args.group == "approval" and args.command == "decide":
        body = {"decision": args.decision, "decided_by": args.decided_by,
                "caller_role": args.caller_role}
        if args.rationale:
            body["rationale"] = args.rationale
        status, payload = call("POST", f"/approvals/{args.id}/decide", body)

    else:  # approval list
        status, payload = call("GET", "/approvals", None)

    _emit(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
