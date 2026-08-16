"""A model-backed judgment substrate, over an OpenAI-compatible endpoint.

One client class serves every model-endpoint substrate in
``config/substrates.yaml`` — today ``dsv4`` (deepseek-v4-flash-0731, an
operator-authorised exception to the entitled-models line) and ``nemotron``
(the NVIDIA model that satisfies the bounty's CONDITION-slot requirement).
Each is registered as an alternate under the same judgment contract as the
rule; ``breaker.engine.GridWatch.judge`` builds one of these from whichever
substrate is currently ACTIVE, never from a substrate hardcoded elsewhere.

Four rules keep this honest:

1. **The model never does arithmetic.** ``soc_delta``, ``temp_slope`` and the
   charge ratio are computed in ``rule.compute_metrics`` and handed to the model
   as facts. The model supplies a verdict and a rationale, nothing else, so two
   substrates can disagree about the call while quoting identical numbers.
2. **It never silently becomes the rule.** If the endpoint is unreachable, slow,
   or answers with something that is not a verdict, the judgment **abstains** —
   and an abstention proposes nothing. Falling back to the rule and letting the
   screen keep the model's name on it would be the exact dishonesty this repo
   is built to avoid.
3. **The judgment names what SERVED it, not what was asked for.** The verdict's
   ``model`` is read back from the response body's own ``model`` field —
   what the endpoint says it ran — falling back to the configured name only
   when a response omits it. Reporting the configured name unconditionally
   would let a misrouted or mis-deployed endpoint serve one model while the
   judgment record kept claiming another; that gap between "served" and
   "configured" is the exact shape of two prior incidents.
4. **Every response is cached to disk**, keyed by the request, so the demo path
   replays offline. ``BREAKER_MODEL_OFFLINE=1`` refuses the network entirely and
   serves cache or abstains.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .rule import (
    CHARGE_COLLAPSE_RATIO,
    SOC_DELTA_MAX_PCT,
    TEMP_SLOPE_MIN_C,
    Metrics,
)

DEFAULT_ENDPOINT = os.environ.get(
    "BREAKER_MODEL_ENDPOINT", "http://dgx-spark.nemotron.example.com:18000/v1")
DEFAULT_MODEL = os.environ.get("BREAKER_MODEL", "deepseek-v4-flash-0731")
DEFAULT_CACHE = Path(os.environ.get("BREAKER_MODEL_CACHE", "data/model-cache"))
#: Judgments committed to the repo so the demo path replays with no endpoint at
#: all. The federation's convention is that every hosted response on the demo
#: path is cached to disk and the demo completes offline from those caches.
SEED_CACHE = Path(__file__).resolve().parents[1] / "fixtures" / "model-cache"

SYSTEM_PROMPT = (
    "You judge microgrid battery telemetry for a grid operator. You are given "
    "measurements that were computed deterministically; do not recompute or "
    "invent numbers. Divergence is defined for you, not for you to infer: the "
    "unit has diverged ONLY when ALL THREE of soc_delta, temp_slope and the "
    "charge ratio individually cross their operator threshold in the same "
    "window. Two out of three, or one out of three, is not a divergence — "
    "judge the data against that rule, do not substitute your own combination "
    "logic for it. Reply with ONLY a JSON object: "
    '{"diverged": bool, "rationale": string, "confidence": number}.'
)


@dataclass
class ModelVerdict:
    """One model judgment, or an abstention with the reason it abstained."""

    diverged: bool
    rationale: str
    confidence: float
    model: str
    endpoint: str
    abstained: bool = False
    cached: bool = False
    latency_ms: Optional[int] = None
    raw: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "diverged": self.diverged, "rationale": self.rationale,
            "confidence": self.confidence, "model": self.model,
            "endpoint": self.endpoint, "abstained": self.abstained,
            "cached": self.cached, "latency_ms": self.latency_ms,
        }


def prompt_for(metrics: Metrics) -> str:
    """The user turn. Deterministic, so the cache key is stable.

    States the combination rule explicitly (ALL THREE, matching
    breaker.rule's own "All three checks must pass" — see rule.py's module
    docstring, sourced from the evidence-pane spec) rather than leaving the
    model to infer AND-vs-OR from a bare threshold list. A judgment substrate
    should be judging the DATA against a stated rule, not reverse-engineering
    the rule itself from the thresholds alone.
    """
    return (
        f"unit {metrics.unit_id} at tick {metrics.tick}, over a "
        f"{metrics.window_ticks}-tick window:\n"
        f"  soc_delta = {metrics.soc_delta}%\n"
        f"  temp_slope = {metrics.temp_slope} C per 10 min\n"
        f"  charge_current = {metrics.charge_ratio} of nominal "
        f"({metrics.nominal_current} A)\n"
        f"Operator thresholds: soc_delta < {SOC_DELTA_MAX_PCT}, "
        f"temp_slope > {TEMP_SLOPE_MIN_C}, charge ratio < {CHARGE_COLLAPSE_RATIO}.\n"
        "Divergence requires ALL THREE thresholds to be crossed in this same "
        "window — not any one, not two of three.\n"
        "Has this unit diverged, by that rule?"
    )


def _http_post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    # NOTE: an HTTP read timeout, nothing else. It bounds one inference call; it
    # has no relationship to how long a queued dispatch may wait, which is
    # forever. See tests/test_proposal_flow.py::test_no_timeout_path_exists.
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


class ModelJudge:
    """Client for an OpenAI-compatible endpoint, with an on-disk cache."""

    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = DEFAULT_MODEL,
        cache_dir: Path | str = DEFAULT_CACHE,
        seed_dir: Optional[Path | str] = SEED_CACHE,
        request_timeout: float = 60.0,
        transport: Optional[Callable[[str, dict[str, Any], float], dict[str, Any]]] = None,
        offline: Optional[bool] = None,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.cache_dir = Path(cache_dir)
        self.seed_dir = Path(seed_dir) if seed_dir else None
        self.request_timeout = request_timeout
        self._transport = transport or _http_post
        self.offline = (
            offline if offline is not None
            else os.environ.get("BREAKER_MODEL_OFFLINE") == "1"
        )
        self.calls: list[str] = field(default_factory=list) if False else []

    # ----------------------------------------------------------------- cache

    def _request_body(self, metrics: Metrics) -> dict[str, Any]:
        return {
            "model": self.model,
            "temperature": 0,          # a judgment substrate that wanders is not one
            # A reasoning model's <think> trace plus its final JSON routinely
            # runs past 300 tokens once the prompt spells out the AND rule
            # explicitly (see prompt_for) — 300 was cutting the real verdict
            # off mid-string (finish_reason "length"), which is truncation,
            # not a model that failed to answer.
            "max_tokens": 600,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt_for(metrics)},
            ],
        }

    def cache_key(self, metrics: Metrics) -> str:
        payload = json.dumps(self._request_body(metrics), sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:24]

    def cache_path(self, metrics: Metrics) -> Path:
        return self.cache_dir / f"{self.cache_key(metrics)}.json"

    def _read_cache(self, metrics: Metrics) -> Optional[dict[str, Any]]:
        key = f"{self.cache_key(metrics)}.json"
        candidates = [self.cache_dir / key]
        if self.seed_dir is not None:
            candidates.append(self.seed_dir / key)
        for path in candidates:
            if not path.exists():
                continue
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
        return None

    def _write_cache(self, metrics: Metrics, payload: dict[str, Any]) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self.cache_path(metrics)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # A cache we cannot write is a slower demo, not a wrong one.
            pass

    # --------------------------------------------------------------- judging

    def _parse(self, content: str) -> Optional[dict[str, Any]]:
        text = content.strip()
        # A reasoning model's <think> trace routinely narrates a DRAFT verdict
        # — its own quoted JSON, braces and all — before delivering the real
        # one after the closing tag. find("{")..rfind("}") across the WHOLE
        # content splices the draft's opening brace to the real answer's
        # closing brace, producing text that is neither and parses as
        # nothing — a real, parseable verdict then gets reported as "not a
        # verdict" and the judgment abstains when the model did not actually
        # fail to answer. Only the text after the LAST </think> is the
        # answer, so cut there first when the tag is present.
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[1].strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict) or "diverged" not in parsed:
            return None
        return parsed

    def _abstain(self, reason: str) -> ModelVerdict:
        return ModelVerdict(
            diverged=False, rationale=reason, confidence=0.0, model=self.model,
            endpoint=self.endpoint, abstained=True,
        )

    def judge(self, metrics: Metrics) -> ModelVerdict:
        """Judge one window. Abstains rather than guessing or falling back."""
        cached = self._read_cache(metrics)
        if cached is not None:
            verdict = self._verdict_from(cached, cached_hit=True)
            if verdict is not None:
                return verdict

        if self.offline:
            return self._abstain(
                "offline mode and no cached judgment for this window")

        started = time.monotonic()
        try:
            response = self._transport(
                f"{self.endpoint}/chat/completions",
                self._request_body(metrics),
                self.request_timeout,
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return self._abstain(f"model endpoint unreachable: {exc}")
        latency_ms = int((time.monotonic() - started) * 1000)

        verdict = self._verdict_from(response, cached_hit=False, latency_ms=latency_ms)
        if verdict is None:
            return self._abstain("model reply was not a verdict")
        self._write_cache(metrics, response)
        return verdict

    def _verdict_from(
        self, response: dict[str, Any], cached_hit: bool,
        latency_ms: Optional[int] = None,
    ) -> Optional[ModelVerdict]:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        parsed = self._parse(content)
        if parsed is None:
            return None
        # The response's OWN "model" field says what actually served the
        # request. That is what the judgment names — not the configured
        # ``self.model`` the request asked for — because a judgment must
        # name what actually judged, not what was requested. Falls back to
        # the configured name only if a response omits the field entirely
        # (a non-vLLM endpoint, or a fixture built without it).
        served_model = response.get("model") or self.model
        return ModelVerdict(
            diverged=bool(parsed["diverged"]),
            rationale=str(parsed.get("rationale", "")),
            confidence=float(parsed.get("confidence", 0.0)),
            model=served_model,
            endpoint=self.endpoint,
            cached=cached_hit,
            latency_ms=latency_ms,
            raw=content,
        )

    def health(self) -> dict[str, Any]:
        """Is the endpoint serving the model we claim to be using?"""
        try:
            request = urllib.request.Request(f"{self.endpoint}/models")
            with urllib.request.urlopen(request, timeout=5) as response:
                served = [m["id"] for m in json.loads(response.read()).get("data", [])]
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"reachable": False, "detail": str(exc), "model_served": False}
        return {
            "reachable": True,
            "models": served,
            "model_served": self.model in served,
        }
