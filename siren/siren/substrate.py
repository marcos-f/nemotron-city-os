"""The throughline client, in two honest halves.

``MockSubstrate`` runs in-process and opens no socket. ``RealSubstrate``
talks to throughline on :8600. Which one is running is reported on every
payload that reaches a pane, because a mock that does not announce itself is
the failure mode this whole demo exists to argue against.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Optional

from .models import Signal

DEFAULT_THROUGHLINE_URL = "http://127.0.0.1:8600"


def throughline_url() -> str:
    return os.environ.get("THROUGHLINE_URL", DEFAULT_THROUGHLINE_URL).rstrip("/")


def substrate_choice() -> str:
    """mock unless explicitly told otherwise; offline always mocks.

    The switch is one environment variable so the demo can flip substrates
    without a rebuild, and so the offline path can never reach a socket by
    inheriting a stale setting.

    "Offline" here means ``feed.offline_mode()``, which is the environment
    variable OR the runtime switch. Reading the variable directly, as this
    did, meant a siren flipped offline at runtime kept a real substrate and
    kept a socket with it.
    """
    from .feed import offline_mode  # local: feed imports models, not this

    if offline_mode():
        return "mock"
    return "real" if os.environ.get("SIREN_SUBSTRATE", "mock").lower() == "real" else "mock"


class SubstrateError(RuntimeError):
    """The substrate was reached and refused, or could not be reached."""

    def __init__(self, message: str, *, status: Optional[int] = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class MockSubstrate:
    """Records what would have been sent. No network, no pretending."""

    name = "mock"
    #: The mock is not a config authority. Offline, siren's pre-flight is the
    #: only verdict there is, and it says so rather than borrowing authority.
    validates_config = False

    def __init__(self) -> None:
        self.signals: list[dict[str, Any]] = []
        self.reloads: list[str] = []

    def post_signal(self, signal: Signal) -> dict[str, Any]:
        wire = signal.wire()
        self.signals.append(wire)
        return wire

    def reload_config(self, path: str) -> dict[str, Any]:
        """The mock does not validate for throughline — siren validates
        locally first (see ``reload.py``), and this records the drop."""
        self.reloads.append(path)
        return {"accepted": True, "substrate": "mock", "path": path}

    def health(self) -> dict[str, Any]:
        return {"substrate": "mock", "reachable": True, "note": "in-process mock; no substrate contacted"}


class RealSubstrate:
    """HTTP against a running throughline."""

    name = "real"
    #: throughline owns the loader, the atomic swap and the refusal ledger.
    #: When it is reachable, its verdict on a config is the authoritative one.
    validates_config = True

    def __init__(self, url: Optional[str] = None, timeout: float = 6.0) -> None:
        self.url = (url or throughline_url()).rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"{self.url}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                text = response.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"detail": raw}
            raise SubstrateError(
                f"throughline {method} {path} returned {exc.code}",
                status=exc.code,
                body=parsed,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SubstrateError(f"throughline unreachable at {self.url}: {exc}") from exc

    def post_signal(self, signal: Signal) -> dict[str, Any]:
        return self._request("POST", "/signals", signal.wire())

    def reload_config(self, path: str) -> dict[str, Any]:
        return self._request("POST", "/config/reload", {"path": path})

    def health(self) -> dict[str, Any]:
        try:
            body = self._request("GET", "/healthz")
        except SubstrateError as exc:
            return {"substrate": "real", "reachable": False, "url": self.url, "error": str(exc)}
        return {"substrate": "real", "reachable": True, "url": self.url, "throughline": body}


def build_substrate() -> Any:
    return RealSubstrate() if substrate_choice() == "real" else MockSubstrate()
