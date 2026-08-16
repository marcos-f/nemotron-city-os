"""Judgment generation.

Two implementations behind one call signature:

  MockJudgeClient   — fixtures from data/fixtures/, labelled MOCK everywhere.
  HostedJudgeClient — nemotron-3-super-120b over integrate.api.nvidia.com,
                      with every response cached to data/cache/ so the demo
                      path replays offline.

The mock is not a stub that returns success. It carries fixtures that FAIL the
quote validator on purpose, because the uncited-rejection path is a feature we
must be able to demonstrate, not an error branch we hope never runs.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .. import config

SYSTEM_PROMPT = (
    "You triage Seattle building permits for a human reviewer. "
    "Return STRICT JSON with keys: finding, quote, confidence, route_proposal. "
    "'quote' MUST be a span copied character-for-character from the permit "
    "description given to you. Never paraphrase, never fix spelling, never "
    "shorten with ellipses. If the description does not support a finding, "
    "set finding to \"\" and quote to \"\"."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JudgeUnavailable(RuntimeError):
    """The hosted model could not be reached or refused."""


class MockJudgeClient:
    """Fixture-backed judgments. Every payload it touches is labelled MOCK."""

    name = "mock"

    def __init__(self, fixtures_dir: Path | None = None):
        self.fixtures_dir = Path(fixtures_dir or config.FIXTURES_DIR)

    @property
    def model(self) -> str:
        return f"MOCK:{config.JUDGMENT_MODEL}"

    def _fixtures(self) -> dict:
        path = self.fixtures_dir / "judgments.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def judge(self, permit: dict, attempt: int = 1) -> dict:
        """Return a raw judgment dict for a permit.

        Fixtures are keyed by permitnum, then by attempt, so a fixture can
        model "cited wrong the first time, cited correctly on regenerate" —
        the exact behaviour the regenerate-once rule exists for.
        """
        fixtures = self._fixtures()
        entry = fixtures.get(permit.get("permitnum", ""))

        if entry is None:
            raw = self._derive(permit)
        elif isinstance(entry, list):
            raw = entry[min(attempt, len(entry)) - 1]
        else:
            raw = entry

        raw = dict(raw)
        raw["mock"] = True
        raw["model"] = self.model
        raw["produced_at"] = _now()
        return raw

    def _derive(self, permit: dict) -> dict:
        """A default fixture for permits with no hand-written entry.

        It quotes the leading clause of the real description, so the mock
        exercises the same validator the hosted path does rather than routing
        around it.
        """
        desc = (permit.get("description") or "").strip()
        if not desc:
            return {"finding": "", "quote": "", "confidence": 0.0,
                    "route_proposal": None}
        clause = desc.split(".")[0].strip() or desc
        clause = clause[:240].rstrip()
        kind = permit.get("permittypedesc") or permit.get("permitclass") or "review"
        return {
            "finding": f"{kind} — routed on the description's leading clause.",
            "quote": clause,
            "confidence": 0.72,
            "route_proposal": f"{permit.get('permitclass') or 'General'} desk",
        }


class HostedJudgeClient:
    """nemotron-3-super-120b, cached to disk on the way past."""

    name = "hosted"

    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None):
        self.api_key = api_key if api_key is not None else config.api_key()
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)

    @property
    def model(self) -> str:
        return config.JUDGMENT_MODEL

    def _cache_path(self, permit: dict, attempt: int) -> Path:
        key = hashlib.sha256(
            json.dumps(
                {
                    "model": self.model,
                    "permitnum": permit.get("permitnum"),
                    "description": permit.get("description"),
                    "attempt": attempt,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]
        return self.cache_dir / f"judgment-{key}.json"

    def _prompt(self, permit: dict, attempt: int) -> str:
        base = (
            f"Permit {permit.get('permitnum')} "
            f"({permit.get('permittypedesc') or permit.get('permitclass')}).\n"
            f"Description:\n{permit.get('description')}\n"
        )
        if attempt > 1:
            base += (
                "\nYour previous answer was REJECTED: the quote was not found "
                "verbatim in the description above. Copy an exact span this time."
            )
        return base

    def judge(self, permit: dict, attempt: int = 1) -> dict:
        cache = self._cache_path(permit, attempt)
        if cache.exists():
            raw = json.loads(cache.read_text())
            raw["cached"] = True
            return raw

        if not self.api_key:
            raise JudgeUnavailable("NVIDIA_API_KEY is not set")

        import httpx

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._prompt(permit, attempt)},
            ],
            "temperature": 0.2 if attempt == 1 else 0.0,
            "max_tokens": 700,
        }
        try:
            resp = httpx.post(
                f"{config.NVIDIA_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=90.0,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # network, auth, quota — all one story here
            raise JudgeUnavailable(str(exc)) from exc

        raw = self._parse(body)
        raw.update(
            {"mock": False, "model": self.model, "produced_at": _now(),
             "cached": False}
        )

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(raw, indent=1, sort_keys=True) + "\n")
        return raw

    @staticmethod
    def _parse(body: dict) -> dict:
        content = body["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise JudgeUnavailable(f"model did not return JSON: {exc}") from exc
        return {
            "finding": parsed.get("finding", ""),
            "quote": parsed.get("quote", ""),
            "confidence": float(parsed.get("confidence") or 0.0),
            "route_proposal": parsed.get("route_proposal"),
        }


def build_client() -> MockJudgeClient | HostedJudgeClient:
    """Pick the client the environment actually supports."""
    return MockJudgeClient() if config.mock_judgment() else HostedJudgeClient()
