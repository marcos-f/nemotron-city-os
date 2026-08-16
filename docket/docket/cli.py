"""The `docket` command line.

Small on purpose. It implements the dataset-registry commands declared in
contracts/opencli.yaml — ``dataset list``, ``dataset show`` and
``dataset validate`` — plus ``serve``, which is what the console script used to
do unconditionally.

The listing commands are CLIENTS of the HTTP API rather than a second reader of
the registry file, so the CLI and the service can never disagree about what
docket is standing on. ``dataset validate`` is the exception, deliberately: it
runs against a FILE, so an operator can check a registry before dropping it in
and a refusal is visible without a running service. It exits non-zero on a
refusal, because a validate that cannot fail the build is not validation.

Known drift, reported rather than hidden: contracts/opencli.yaml also declares
``signal document`` and ``judgment get``, which have never been implemented
here. They predate this module. They are left in the contract so the gap stays
auditable instead of being erased by deleting the evidence.
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
    "DOCKET_URL", f"http://127.0.0.1:{os.environ.get('DOCKET_PORT', '8601')}"
)

Transport = Callable[[str, str, Optional[dict[str, Any]]], tuple[int, Any]]


def http_transport(base_url: str) -> Transport:
    def call(method: str, path: str, body: Optional[dict[str, Any]] = None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            base_url + path, data=data, method=method,
            headers={"Content-Type": "application/json"},
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
            return 0, {"detail": f"cannot reach docket at {base_url}: {exc.reason}"}

    return call


def _emit(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docket",
        description="The reviewer that quotes its sources or shuts up.",
    )
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"base URL of a running docket (default {DEFAULT_URL})")
    groups = parser.add_subparsers(dest="group", required=True)

    serve = groups.add_parser("serve", help="Run the HTTP service")
    serve.add_argument("--host", default=os.environ.get("DOCKET_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=None)

    dataset = groups.add_parser(
        "dataset", help="Dataset registry commands").add_subparsers(
        dest="command", required=True)
    dataset.add_parser(
        "list", help="List every dataset this component consumes, "
                     "with licence and provenance")
    show = dataset.add_parser(
        "show", help="Show one dataset's source, licence, provenance and as-of time")
    show.add_argument("--id", required=True)
    validate = dataset.add_parser(
        "validate", help="Validate a dataset registry file; "
                         "exits non-zero on a refusal")
    validate.add_argument("--path", default=None,
                          help="registry file to validate (default: docket's own)")

    return parser


def main(argv: Optional[list[str]] = None, transport: Optional[Transport] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.group == "serve":
        from . import config
        from .__main__ import main as serve_main

        if args.port is not None:
            os.environ["DOCKET_PORT"] = str(args.port)
            config.PORT = args.port
        os.environ["DOCKET_HOST"] = args.host
        serve_main()
        return 0

    if args.group == "dataset" and args.command == "validate":
        from throughline.config import ConfigRefusal
        from throughline.datasets import load_registry

        from . import config

        path = args.path or config.datasets_path()
        try:
            registry = load_registry(path)
        except ConfigRefusal as refusal:
            _emit(refusal.as_dict())
            return 1
        _emit({"refused": False, **registry.as_dict()})
        return 0

    call = transport or http_transport(args.url)

    if args.command == "list":
        status, payload = call("GET", "/datasets", None)
    else:  # show
        status, payload = call("GET", f"/datasets/{args.id}", None)

    _emit(payload)
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
