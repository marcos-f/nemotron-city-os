"""The substrate client: signals, judgments and route effects to throughline.

throughline gates every effect by reversibility class. Routing a permit to a
review desk is REVERSIBLE — it auto-executes without approval — but it is still
ledgered and hash-chained, which is what makes the route auditable after the
fact. docket never asks for an irreversible effect.

MockThroughlineClient keeps an in-process ledger with the same shape, so the
pipeline can be built and tested before the substrate exists. The real client
is chosen when the substrate answers on its port.
"""
from __future__ import annotations

import itertools
import uuid
from datetime import datetime, timezone

from .. import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SubstrateUnavailable(RuntimeError):
    """throughline could not be reached."""


class MockThroughlineClient:
    """In-process stand-in. Labelled mock in every record it returns."""

    name = "mock"
    substrate = "mock"

    def __init__(self):
        self.signals: list[dict] = []
        self.judgments: list[dict] = []
        self.effects: dict[str, dict] = {}
        self._seq = itertools.count(1)

    def post_signal(self, signal: dict) -> dict:
        self.signals.append(signal)
        return signal

    def post_judgment(self, judgment: dict) -> dict:
        record = dict(judgment)
        record.setdefault("id", f"jdg-mock-{next(self._seq)}")
        self.judgments.append(record)
        return record

    def post_effect(self, effect: dict) -> dict:
        record = dict(effect)
        record.setdefault("id", f"eff-mock-{uuid.uuid4().hex[:12]}")
        # Reversible effects auto-execute; that is the substrate's rule and the
        # mock must not be more permissive OR more cautious than the real one.
        record["status"] = (
            "executed" if record.get("reversibility") == "reversible" else "queued"
        )
        record["substrate"] = "mock"
        record["ledgered"] = True
        record.setdefault("ts", _now())
        self.effects[record["id"]] = record
        return record

    def get_effect(self, effect_id: str) -> dict | None:
        return self.effects.get(effect_id)

    def health(self) -> bool:
        return True


class RealThroughlineClient:
    """HTTP client against a running throughline."""

    name = "real"
    substrate = "real"

    def __init__(self, base_url: str | None = None, timeout: float = 15.0):
        self.base_url = (base_url or config.THROUGHLINE_URL).rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, body: dict) -> dict:
        import httpx

        try:
            resp = httpx.post(
                f"{self.base_url}{path}", json=body, timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise SubstrateUnavailable(f"POST {path}: {exc}") from exc

    def post_signal(self, signal: dict) -> dict:
        return self._post("/signals", signal)

    def post_judgment(self, judgment: dict) -> dict:
        return self._post("/judgments", judgment)

    def post_effect(self, effect: dict) -> dict:
        record = self._post("/effects", effect)
        record["substrate"] = "real"
        record["ledgered"] = True
        return record

    def get_effect(self, effect_id: str) -> dict | None:
        import httpx

        try:
            resp = httpx.get(
                f"{self.base_url}/effects/{effect_id}", timeout=self.timeout
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise SubstrateUnavailable(f"GET /effects/{effect_id}: {exc}") from exc

    def health(self) -> bool:
        import httpx

        try:
            return httpx.get(f"{self.base_url}/docs", timeout=5.0).status_code == 200
        except Exception:
            return False


def build_client(force_mock: bool | None = None):
    """Real substrate when it answers; otherwise refuse, never fabricate.

    Mock is selected ONLY by explicit request — MOCK_THROUGHLINE=1 or
    force_mock=True, an intentional fixture mode. If nothing forces mock and
    the real substrate does not answer, this raises SubstrateUnavailable. A
    demo that quietly degraded to a mock substrate on a health-check failure
    would be exactly the kind of unlabelled fiction this project refuses to
    ship: route effects would be ledgered in-process and reported "executed"
    while the real, durable substrate never saw them.
    """
    if force_mock is None:
        force_mock = config.mock_throughline()
    if force_mock:
        return MockThroughlineClient()

    client = RealThroughlineClient()
    if client.health():
        return client
    raise SubstrateUnavailable(
        f"real throughline unreachable at {client.base_url}; refusing to "
        "silently substitute a mock substrate for a real one"
    )
