"""NemoClerk's runtime: a real OpenAI-compatible tool-calling loop.

Grounding is a MECHANISM here, not a prompt instruction. The model chooses
tools; helm executes them against the real substrate; the answer is composed
from the tool RESULTS and every result becomes a visible chip. If a data
question produced no tool call, NemoClerk makes no claim
(``test://helm/nemoclerk-grounded``).

Model ladder — the active rung is named in the rail header. The rungs are
built by ``Ladder.tiers()`` below and are conditional on configuration, so
this list describes the DEFAULT configuration in ``helm/config.py``; the
authority at runtime is ``GET /nemoclerk/runtime``, which reports every rung
and its state:

  1. **DGX Spark (GB10)** — ``NEMOCLERK_BASE_URL``, default
     ``http://dgx-spark.nemotron.example.com:8000/v1`` running
     ``nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`` on vLLM on NVIDIA GB10
     Grace Blackwell. An NVIDIA model on NVIDIA silicon, locally: it needs no
     internet. It is also slow (~5 tok/s, ``--enforce-eager``) and admits
     only two concurrent sequences, so calls are SERIALIZED and answers are
     kept short. It has no tool-call parser, so it is driven with
     ``/no_think`` and grounded by this module's router rather than by the
     model's own tool choice.
  2. **RTX 5090** — ``NEMOCLERK_FALLBACK_BASE_URL``, default
     ``http://rtx-workstation.nemotron.example.com:9997/v1`` running ``qwen3.6-27b`` on
     llama.cpp. The only rung that can also CHOOSE tools.
  3. fleet-internal qwen (``FLEET_MODEL_URL``) — unset by default, so it does
     not appear in the ladder unless configured.
  4. entitled hosted Nemotron (``NVIDIA_API_KEY``) — likewise unset by
     default; the only rung that needs the network.
  5. **cache** — every completed turn is written to disk, so the scripted
     demo replays with the network off

There is no rung after cache. helm used to carry a sixth, in-process rung —
a deterministic composer, labelled "MOCK NEMOCLERK", that strung tool
summaries into prose and stood in for a model. It was a stub model
responder wearing a disclosure label, and it is a PRODUCT bug for a stub
model path to exist at all: a reviewer caught it serving a live,
unauthenticated turn while ``/healthz`` simultaneously claimed the GB10
rung was answering. There is no way to make that composer's OUTPUT honest
enough to keep, because the dishonesty was never the label — it was
answering at all. So it is gone from every production code path. When no
rung above is reachable and no cached turn covers the question, helm
REFUSES: it says plainly that no model is reachable, attaches whatever raw
tool results it already grounded, and composes no prose to stand in for a
model that was never consulted. The cache rung is not this — a cached turn
replays a real model's real prior answer and is labelled as a replay, not
manufactured now. Composing a stand-in answer is a legitimate technique in
tests/ (a stub HTTP client, a fixed tool result) — it must never run in
front of a real user again.

There is no longer a ``deepseek-v4-flash-0731`` rung. That two-node job on
``dgx-spark:18000`` was stopped so the GB10's 121 GiB could host the
Nemotron above; the port refuses connections. This docstring described it as
rung 1 for longer than it was true, which is exactly the failure mode
``invalidate()`` below exists to prevent at runtime.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from helm.nemoclerk.sessions import SessionStore, Turn
from helm.nemoclerk.tools import ToolRegistry, ToolResult

log = logging.getLogger("helm.nemoclerk")

PRINCIPAL_FMT = "agent:nemoclerk({model}@{tier})"

MAX_TOOL_ROUNDS = 2

SYSTEM_PROMPT = """You are NemoClerk, the assistant inside helm, the operator \
console of the NemoCity federation.

Rules you must not break:
1. Call a tool for every factual question. Never state a number, id, hash,
   timestamp or status that did not come from a tool result.
2. If the tools cannot answer, say what is missing.
3. You are an agent principal. Approving an irreversible effect requires a
   human signet role and the gate refuses you by role. Never claim you
   approved anything.
4. Answer in at most three short sentences. Plain words, no markdown.
"""

# The deterministic backstop router. When the model declines to call a tool
# on a data question, helm calls one anyway rather than let a claim through
# ungrounded.
INTENTS: tuple[tuple[str, str], ...] = (
    (r"\bwho (can|may|approved|signed)\b|\bsigned\b|\bsignet\b|\brole\b|"
     r"\broles\b|\bapprover", "list_roles"),
    (r"\bapprov|\bwaiting\b|\bqueue\b|\bgate\b|\bhold\b|\bpending\b", "list_approvals"),
    (r"\bwalk\b|\bchain\b|\bcause\b|\bprovenance\b|\bhop", "walk_effect"),
    (r"\bverif|\btamper|\bintact\b|\bvalid\b", "verify_ledger"),
    (r"\bledger\b|\bseq\b|\bhash\b|\bentr(y|ies)\b|\brecent\b|\bnewest\b", "read_ledger"),
    (r"\bpermit\b|\broute\b|\bquote\b|\babstention\b|\bsource\b|\bdocument\b", "get_permit"),
    (r"\bincident|\b911\b|\bfire\b|\bmedical\b|\bcollision\b", "list_incidents"),
    (r"\bsafe\b|\bbattery\b|\bdispatch\b|\bdiverg|\bneeds me\b|\bright now\b|"
     r"\burgent\b|\bnobody\b", "list_approvals"),
    (r"\bnear.?miss\b|\bframe\b|\bcamera\b|\balmost happened\b|\bcaption|"
     r"\bvideo\b|\bfresh\b", "list_feeds"),
    (r"\bconfig\b|\brefus|\bpolicy\b|\brule", "get_config"),
    (r"\bfeed|\blive\b|\bsignal class|\bclasses\b|\bonline\b|\boffline\b", "list_feeds"),
    (r"\bcompose|\ball four\b|\bwhole system\b|\bthis view\b|\bappear", "get_composed"),
    (r"\breload\b|\bhot.?load", "reload_signal_class"),
    (r"\boverview\b|\bstatus\b|\bhealth\b|\bsummar", "get_overview"),
)

DATA_QUESTION = re.compile(
    r"\b(approv|waiting|queue|gate|hold|ledger|hash|seq|verif|walk|chain|cause|"
    r"feed|live|online|offline|status|overview|health|composed|effect|"
    r"judgment|signal|incident|permit|dispatch|reload|how many|what is|"
    r"what's|which|when|who|why|show|list|tell me|source|quote|route|"
    r"abstention|role|config|policy)\b",
    re.IGNORECASE,
)

APPROVE_REQUEST = re.compile(r"\bapprove it\b|\brelease it\b|\bsign off\b|\bjust do it\b", re.I)


@dataclass
class Tier:
    number: int
    name: str
    base_url: str
    model: str
    api_key: str = ""
    hardware: str = ""
    host: str = ""
    silicon: str = ""
    #: A system prefix the rung REQUIRES. Nemotron-3-nano-omni needs
    #: "/no_think" for the same reason qwen3.6 needs enable_thinking:false —
    #: without it the reasoning budget eats the answer and content is empty.
    system_prefix: str = ""
    #: Whether the SERVER can choose tools. helm's grounding never depends on
    #: this: the deterministic router runs the tools either way and the model
    #: only phrases from their results. A rung without a tool-call parser is
    #: still a first-class rung here.
    supports_tools: bool = True
    vision: bool = False
    #: Qwen3-class reasoning models burn the whole budget on hidden thinking
    #: and return an EMPTY content string unless thinking is switched off.
    thinking_off: bool = False

    @property
    def label(self) -> str:
        return f"tier {self.number} · {self.name}"


@dataclass
class Answer:
    text: str
    chips: list[dict[str, Any]] = field(default_factory=list)
    runtime: str = "helm tool-loop"
    tier: str = "none"
    model: str = ""
    hardware: str = ""
    #: The silicon that served THIS turn, or "" when no model was consulted.
    #: Never the configured silicon of a rung that merely happened to be up.
    silicon: str = ""
    principal: str = "agent:nemoclerk(none@none)"
    source: str = "none"  # model | cache | none (no model consulted — refused)
    honesty_label: str = ""
    grounded: bool = False
    refused: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "chips": self.chips,
            "runtime": self.runtime,
            "tier": self.tier,
            "model": self.model,
            "hardware": self.hardware,
            "silicon": self.silicon,
            "principal": self.principal,
            "source": self.source,
            "honesty_label": self.honesty_label,
            "grounded": self.grounded,
            "refused": self.refused,
        }


class ModelLadder:
    """Probe the rungs in order, in the BACKGROUND, and never block a page."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self._active: Tier | None = None
        self._reachable_tiers: list[Tier] = []
        self._state = "unprobed"  # unprobed | probing | done
        self._note = "probing the model ladder"
        self._lock = threading.Lock()

    def rungs(self) -> list[dict[str, Any]]:
        """Every rung by name, with its silicon and whether it is answering.

        At an event called NVIDIA Spark Hack, the truest high-value fact
        this project owns is that its assistant runs on local NVIDIA
        silicon — and the console never said so. It says so now, and marks
        the rungs that are NOT answering rather than implying all of them.
        """
        with self._lock:
            active = self._active.name if self._active else ""
            reachable = {t.name for t in self._reachable_tiers}
            probed = self._state == "done"
        out: list[dict[str, Any]] = []
        for tier in self.tiers():
            if not probed:
                state = "probing"
            elif tier.name == active:
                state = "active"
            elif tier.name in reachable:
                state = "standby"
            else:
                state = "unreachable"
            out.append(
                {
                    "number": tier.number,
                    "name": tier.name,
                    "model": tier.model,
                    "host": tier.host,
                    "silicon": tier.silicon,
                    "state": state,
                    "chooses_tools": tier.supports_tools,
                    "vision": tier.vision,
                }
            )
        out.append(
            {
                "number": len(out) + 1,
                "name": "cache",
                "model": "recorded turns",
                "host": "on disk",
                "silicon": "replayed, labelled on screen",
                "state": "standby",
            }
        )
        # There is no rung after cache. helm used to carry a seventh,
        # in-process "mock" rung that composed prose from tool summaries and
        # stood in for a model — a stub model responder in the product path.
        # It has been removed: when nothing above is reachable and no cache
        # entry covers the question, helm refuses outright rather than
        # naming a rung that never answered.
        out.append(
            {
                "number": len(out) + 1,
                "name": "refuse",
                "model": "",
                "host": "in-process",
                "silicon": "no model — helm refuses rather than composing an answer",
                "state": "active" if not any(r["state"] == "active" for r in out) else "standby",
            }
        )
        return out

    def reachable_tiers(self) -> list[Tier]:
        """Every rung that answered, in ladder order — used to degrade mid-turn."""
        self.active()
        with self._lock:
            return list(self._reachable_tiers)

    def tiers(self) -> list[Tier]:
        s = self.settings
        out: list[Tier] = []
        number = 0
        if s.agent_model_url:
            number += 1
            out.append(
                Tier(
                    number,
                    "dgx-spark",
                    s.agent_model_url,
                    s.agent_model_name,
                    hardware="NVIDIA GB10 Grace Blackwell (DGX Spark) · vLLM",
                    host="dgx-spark",
                    silicon="NVIDIA GB10 Grace Blackwell (DGX Spark)",
                    system_prefix="/no_think",
                    supports_tools=False,
                    vision=True,
                )
            )
        if getattr(s, "fallback_model_url", ""):
            number += 1
            out.append(
                Tier(
                    number,
                    "rtx-5090",
                    s.fallback_model_url,
                    s.fallback_model_name,
                    hardware="NVIDIA RTX 5090 · llama.cpp, speculative decoding",
                    host="rtx-workstation",
                    silicon="NVIDIA RTX 5090",
                    thinking_off=True,
                )
            )
        if s.fleet_model_url:
            number += 1
            out.append(Tier(number, "fleet-qwen", s.fleet_model_url, s.fleet_model_name))
        if s.nvidia_api_key:
            number += 1
            out.append(
                Tier(
                    number,
                    "hosted-nemotron",
                    s.hosted_model_url,
                    s.hosted_model_name,
                    s.nvidia_api_key,
                    hardware="entitled hosted inference (needs the network)",
                    host="integrate.api.nvidia.com",
                    silicon="NVIDIA hosted",
                )
            )
        return out

    def invalidate(self, tier: "Tier | None" = None, reason: str = "") -> None:
        """Evict a rung that just failed and force the ladder to re-probe.

        The probe used to run once, at boot, and nothing ever reset it. A
        rung that died afterwards kept its name in ``principal()`` — the
        exact string stamped into the ledger — so the chain notarised a
        model that was never consulted. Every other falsehood in this system
        is visible to a human; that one verifies as true forever. A rung
        earns its name per turn now.
        """
        with self._lock:
            if tier is not None:
                self._reachable_tiers = [
                    t for t in self._reachable_tiers if t.name != tier.name
                ]
                if self._active is not None and self._active.name == tier.name:
                    self._active = self._reachable_tiers[0] if self._reachable_tiers else None
            self._state = "unprobed"
            self._note = (
                f"re-probing: {tier.name if tier else 'a rung'} failed"
                + (f" ({reason})" if reason else "")
            )

    def start_probe(self) -> None:
        """Kick the probe off at startup; page loads never wait for it."""
        with self._lock:
            if self._state != "unprobed":
                return
            self._state = "probing"
        threading.Thread(target=self._probe, name="nemoclerk-probe", daemon=True).start()

    def _probe(self) -> None:
        if self.settings.offline:
            with self._lock:
                self._active = None
                self._note = "offline mode — cached turns only, live model not contacted"
                self._state = "done"
            return
        tried: list[str] = []
        found: Tier | None = None
        reachable: list[Tier] = []
        for tier in self.tiers():
            if self._reachable(tier):
                reachable.append(tier)
                if found is None:
                    found = tier
            else:
                tried.append(f"tier {tier.number} {tier.name} unreachable")
        with self._lock:
            self._reachable_tiers = reachable
            self._active = found
            if found is not None:
                self._note = f"{found.label} · {found.model}"
                if found.hardware:
                    self._note += f" · {found.hardware}"
            else:
                self._note = (
                    "; ".join(tried) or "no model endpoint configured"
                ) + " — serving cached turns, then labelled MOCK"
            self._state = "done"

    def active(self, wait: float = 0.0) -> Tier | None:
        """Non-blocking by default; ``wait`` lets a chat turn pause briefly."""
        self.start_probe()
        deadline = time.time() + wait
        while True:
            with self._lock:
                if self._state == "done":
                    return self._active
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    @property
    def note(self) -> str:
        with self._lock:
            return self._note

    @property
    def probed(self) -> bool:
        with self._lock:
            return self._state == "done"

    def _reachable(self, tier: Tier) -> bool:
        headers = {"Authorization": f"Bearer {tier.api_key}"} if tier.api_key else {}
        try:
            response = httpx.get(
                f"{tier.base_url.rstrip('/')}/models",
                headers=headers,
                timeout=self.settings.model_probe_timeout,
            )
        except httpx.HTTPError as exc:
            log.info("model tier %s unreachable: %s", tier.name, exc)
            return False
        return response.is_success


class NemoClerk:
    """The assistant. Tool-grounded, contextual, and refused by role."""

    def __init__(
        self,
        registry: ToolRegistry,
        settings: Any,
        sessions: SessionStore | None = None,
        *,
        http_post: Any = None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.sessions = sessions or SessionStore()
        self.ladder = ModelLadder(settings)
        self.cache_dir = Path(settings.cache_dir)
        # The GB10 deployment admits two concurrent sequences. helm takes one.
        self._call_lock = threading.Lock()
        self._post = http_post or httpx.post
        #: What actually served the CURRENT turn: ("model"|"cache", Tier) or
        #: None when nothing served it (refused). The ledgered principal is
        #: derived from this, never from what the ladder hoped was up.
        self._serving: tuple[str, Tier] | None = None
        #: The model name the ENDPOINT reported for the current turn, which is
        #: the only claim about the far side that came from the far side. When
        #: it disagrees with the rung's configured model, the rung's silicon
        #: stops being evidence — see ``served()``.
        self._serving_reported: str = ""

    # ------------------------------------------------------------- header
    def pane_header(self) -> dict[str, Any]:
        """What actually served the most recent turn — the SAME observation
        the ledger notarises, not a hope about which rung is up.

        ``/healthz`` and ``/nemoclerk/runtime`` both render this dict, and a
        reviewer once caught them naming the GB10 rung — the ladder's
        ACTIVE, reachable rung — while the concurrent turn on the public
        ledger recorded a mock/deterministic turn instead. The two can never
        disagree again because they are now the same read: ``served()``,
        derived from ``self._serving``, which is set only when a call
        (or a cache hit) actually happened. The ladder's rung-by-rung
        reachability is still reported under ``rungs`` — that answers "what
        COULD serve", a genuinely different and still-useful question from
        "what DID serve" — but the top-level ``tier``/``model``/``hardware``/
        ``silicon`` fields answer the second question exclusively, and say
        so explicitly when nothing has served yet.
        """
        served = self.served()
        rungs = self.ladder.rungs()
        if served["source"] == "none":
            return {
                "runtime": "helm tool-loop",
                "tier": "no turn served yet",
                "model": "",
                "hardware": "",
                "silicon": "",
                "principal": self.principal(),
                "served_source": "none",
                "note": self.ladder.note,
                "rungs": rungs,
            }
        return {
            "runtime": "helm tool-loop",
            "tier": served["tier"],
            "model": served["model"],
            "hardware": served["hardware"],
            "silicon": served["silicon"],
            "principal": self.principal(),
            "served_source": served["source"],
            "note": self.ladder.note,
            "rungs": rungs,
        }

    def principal(self) -> str:
        """The principal for the turn being served RIGHT NOW.

        This string is stamped into ``approve_effect`` and hash-chained by
        throughline, so it must describe what actually answered — a cached
        turn ledgers ``cache@<tier>`` and a turn that consulted no model at
        all ledgers ``none@none``. Naming a rung that did not serve would
        notarise a claim the chain then proves "valid" forever. This used to
        read ``mock@none`` — a leftover of the mock/dev sign-in and the
        in-process mock composer, both removed as product capabilities — and
        a reviewer flagged it as still asserting a "mock" mode that no
        longer exists anywhere in this deployment.
        """
        serving = self._serving
        if serving is None:
            return PRINCIPAL_FMT.format(model="none", tier="none")
        kind, tier = serving
        if kind == "cache":
            return PRINCIPAL_FMT.format(model="cache", tier=tier.name)
        # What ANSWERED, by its own report, falling back to what helm asked
        # for. A rung slot's configured model name is a constant; the name in
        # the response body is the endpoint's own statement about itself.
        return PRINCIPAL_FMT.format(
            model=self._serving_reported or tier.model, tier=tier.name
        )

    def served(self) -> dict[str, str]:
        """What ACTUALLY served this turn — model, rung, hardware, silicon.

        The same observation ``principal()`` is derived from, in the shape the
        ledger writes. The rail header may describe the whole LADDER, because
        that is what a header is for; a ledger entry describes ONE TURN, and
        must not borrow the active rung's hardware for a turn that rung never
        answered.

        ``silicon`` is the sharp end. It is configuration — a string bound to
        a rung SLOT — so the most it can ever mean is "this rung served, and
        this is what that rung is configured to be". Two things keep that
        honest here: it is empty unless a model call really happened, and it
        is withdrawn when the endpoint answers as a model the rung is not
        configured for, which is what a stub or a swapped deployment looks
        like from this side.
        """
        blank = {"source": "none", "model": "", "tier": "", "hardware": "", "silicon": ""}
        serving = self._serving
        if serving is None:
            return blank
        kind, tier = serving
        if kind == "cache":
            return {
                "source": "cache",
                "model": tier.model,
                "tier": tier.label,
                "hardware": "",
                # A replay computed nothing now, so no silicon served it.
                "silicon": "replayed from disk — no silicon in this turn",
            }
        reported = (self._serving_reported or "").strip()
        mismatch = bool(reported) and reported != tier.model
        return {
            "source": "model",
            "model": reported or tier.model,
            "tier": tier.label,
            "hardware": tier.hardware,
            "silicon": (
                # The configured silicon is deliberately NOT repeated here:
                # a withdrawn claim that still prints the claim is the same
                # string in the ledger, and greppable as if it were true.
                # ``tier.base_url`` is deliberately NOT interpolated either:
                # this string reaches an anonymous caller through both
                # /nemoclerk/runtime and /nemoclerk/message, and it is
                # helm's own internal address for the rung, not something an
                # unauthenticated caller is owed.
                f"unverified — the {tier.name} rung answered as {reported!r}, "
                f"not {tier.model!r}, so this turn is no evidence for what "
                f"the {tier.name} rung is configured to be"
                if mismatch
                else tier.silicon
            ),
        }

    # ---------------------------------------------------------------- ask
    def ask(
        self,
        question: str,
        *,
        subject: str,
        feature_area: str,
        priming: str = "",
        selected_event: str = "",
    ) -> Answer:
        session = self.sessions.get(subject, feature_area, priming)
        self.sessions.append(subject, feature_area, Turn(role="you", text=question))

        # Nothing has served this turn yet, so the principal is none@none
        # until something actually answers. A rung earns its name per turn.
        self._serving = None
        self._serving_reported = ""
        tier = self.ladder.active(wait=self.settings.model_probe_timeout)
        if APPROVE_REQUEST.search(question) or re.match(r"\s*approve\b", question, re.I):
            # "approve it" is not a question to reason about. helm ATTEMPTS it,
            # so the refusal is a real gate decision on the real ledger rather
            # than a model's description of one.
            results = self._router_tools(question)
            answer = self._answer(
                self._refusal_text(results)
                if any(r.refused for r in results)
                # No approval was pending, so there was nothing to refuse or
                # to approve. This states the real tool result plainly; it is
                # not a model's phrasing and never claims to be one.
                else self._facts_only(results),
                results,
                tier,
                "mock" if tier is None else "model",
            )
        elif tier is not None:
            answer = self._model_turn(tier, question, session.priming, selected_event)
        else:
            answer = self._offline_turn(question, session.priming, selected_event)

        self.sessions.append(
            subject,
            feature_area,
            Turn(role="nemoclerk", text=answer.text, chips=answer.chips),
        )
        return answer

    # ------------------------------------------------- the tool-call loop
    def _model_turn(
        self, tier: Tier, question: str, priming: str, selected_event: str
    ) -> Answer:
        context = priming or ""
        if selected_event:
            context += f"\nCurrently selected event: {selected_event}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT + ("\n" + context if context else "")},
            {"role": "user", "content": question},
        ]
        results: list[ToolResult] = []
        text = ""
        source = "model"

        rounds = MAX_TOOL_ROUNDS if tier.supports_tools else 0
        for _ in range(rounds):
            reply = self._chat(tier, messages, with_tools=True)
            if reply is None:
                break
            source = reply.get("_source", "model")
            message = reply.get("message") or {}
            calls = message.get("tool_calls") or []
            if not calls:
                text = self._clean(message.get("content") or "")
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                result = self._execute(name, arguments)
                results.append(result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", name),
                        "content": json.dumps(
                            {"ok": result.ok, "refused": result.refused, "result": result.summary}
                        ),
                    }
                )

        # Backstop: a data question that produced no tool call is grounded
        # here rather than answered on the model's word.
        if not results and DATA_QUESTION.search(question):
            results = self._router_tools(question)
            if results:
                text = ""

        if not results:
            return self._ungrounded(question, tier)

        if any(r.refused for r in results):
            text = self._refusal_text(results)
            source = source if source == "cache" else source
        elif not text:
            phrased = self._phrase_with_model(tier, question, results, context)
            if phrased:
                text, source = phrased
            else:
                # The model this turn hoped to phrase with did not answer.
                # Refuse rather than compose a stand-in reply in its place.
                text, source = self._refusal_no_model(results), "mock"

        return self._answer(text, results, tier, source)

    def _offline_turn(self, question: str, priming: str, selected_event: str) -> Answer:
        """No live model: run the router, then cache, then REFUSE.

        There used to be a rung after cache — a deterministic composer that
        strung tool summaries into prose. It is gone from this path. A cache
        hit replays a real model's real prior answer to this exact question,
        which is honest; a cache miss with no model reachable gets a refusal,
        not a manufactured phrasing standing in for one.
        """
        results = self._router_tools(question)
        if not results:
            return self._ungrounded(question, None)
        if any(r.refused for r in results):
            return self._answer(self._refusal_text(results), results, None, "mock")
        cached = self._scripted_cache_read(question)
        if cached is not None:
            return self._answer(cached, results, None, "cache")
        return self._answer(self._refusal_no_model(results), results, None, "mock")

    # ----------------------------------------------------------- helpers
    def _execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if name == "approve_effect":
            arguments = dict(arguments)
            arguments.setdefault("id", self._first_pending())
            arguments["principal"] = self.principal()
            arguments["role"] = "agent"
        return self.registry.call(name, arguments)

    def _first_pending(self) -> str:
        result = self.registry.list_approvals(state="pending")
        pending = result.data or []
        return pending[0]["id"] if pending else ""

    def _router_tools(self, question: str) -> list[ToolResult]:
        lowered = question.lower()
        if APPROVE_REQUEST.search(lowered) or re.search(r"^\s*approve\b", lowered):
            listing = self.registry.list_approvals(state="pending")
            target = (listing.data or [{}])[0].get("id", "") if listing.data else ""
            out = [listing]
            if target:
                out.append(self._execute("approve_effect", {"id": target}))
            return out
        chosen: list[str] = []
        for pattern, tool in INTENTS:
            if re.search(pattern, lowered) and tool not in chosen:
                chosen.append(tool)
        # No fallback tool: a question outside the console's vocabulary gets
        # no chip, and therefore no claim. Guessing a tool would manufacture
        # a citation for an answer nobody asked the substrate for.
        return [self.registry.call(name, {}) for name in chosen[:2]]

    def _ungrounded(self, question: str, tier: Tier | None) -> Answer:
        if DATA_QUESTION.search(question):
            text = (
                "I can only answer that from a tool result, and none of my tools "
                "covers it. Ask about the approval queue, the feeds, the ledger "
                "or a cause walk."
            )
        else:
            text = (
                "I answer questions about this console from its own API — the "
                "approval queue, the feeds, the ledger, and the cause walk "
                "behind any effect."
            )
        return self._answer(text, [], tier, "mock" if tier is None else "model")

    def _refusal_text(self, results: list[ToolResult]) -> str:
        refused = next(r for r in results if r.refused)
        target = (refused.data or {}).get("approval", {}).get("description", "")
        return (
            "I can't. Approving an irreversible effect requires a human signet "
            f"role and I am {self.principal()}. The gate refused me by role, and "
            "the refusal is on the ledger with my principal"
            + (f" — {target}." if target else ".")
        )

    @staticmethod
    def _facts_only(results: list[ToolResult]) -> str:
        """The tools' own results, joined verbatim — never a model's words.

        Used where helm never intends to consult a model at all (an
        approve-gate decision is a real tool call, not a question to reason
        about), so this is not a refusal-for-lack-of-a-model; it is a plain
        disclosure of what a real tool call returned.
        """
        parts = [r.summary for r in results if r.summary]
        if not parts:
            return "The tools returned nothing to report."
        return " ".join(p.rstrip(".") + "." for p in parts)

    def _refusal_no_model(self, results: list[ToolResult]) -> str:
        """No model is reachable, and none was consulted. REFUSE, in words.

        helm used to compose a plausible-sounding answer here — stringing
        tool summaries into prose behind a "MOCK NEMOCLERK" label — and stand
        it in for a model that was never asked. That composer is gone. This
        method never manufactures a claim a model would have made; it states
        the refusal plainly and, when the tools already ran, discloses their
        raw results rather than a fabricated phrasing of them.
        """
        parts = [r.summary for r in results if r.summary]
        if not parts:
            return (
                "No model is reachable to answer this, and no tool result is "
                "available either. I am not answering."
            )
        facts = " ".join(p.rstrip(".") + "." for p in parts)
        return (
            "No model is reachable to phrase an answer, so I am not composing "
            f"one. Here is exactly what the tools returned: {facts}"
        )

    def _answer(
        self,
        text: str,
        results: list[ToolResult],
        tier: Tier | None,
        source: str,
    ) -> Answer:
        # ``tier`` is the rung the ladder has ACTIVE — a hope about the next
        # call. ``_serving`` is the observation that a call was actually made
        # and by whom, and it is the only thing allowed to put a model name, a
        # rung label or a piece of NVIDIA silicon on this record. These three
        # fields used to come off the header, i.e. off the active rung, so a
        # turn that consulted no model at all still reported source "model"
        # and named GB10 silicon — and the ledger notarised it, permanently.
        # `principal()` was migrated to `_serving` and these were not.
        served = self.served()
        if served["source"] == "none" and source == "model":
            # Nothing was consulted. Whatever this turn is, it is not a
            # model turn, and it does not get to name hardware.
            source = "mock"
        labels = {
            "model": "",
            "cache": "CACHED RESPONSE — replayed from disk, not generated now",
            "mock": "NO MODEL IN THE LOOP — refusing to compose an answer; raw tool results only",
        }
        return Answer(
            text=text,
            chips=[r.to_dict() for r in results],
            runtime="helm tool-loop",
            tier=served["tier"] or ("cache" if source == "cache" else "none"),
            model=served["model"],
            hardware=served["hardware"],
            silicon=served["silicon"],
            principal=self.principal(),
            source=source,
            honesty_label=labels.get(source, ""),
            grounded=bool(results),
            refused=any(r.refused for r in results),
        )

    @staticmethod
    def _clean(text: str) -> str:
        text = text.split("</think>")[-1]
        text = " ".join(text.split())
        if len(text) > 700:
            text = text[:700].rsplit(" ", 1)[0] + "…"
        return text.strip()

    # -------------------------------------------------------- model calls
    def _phrase_with_model(
        self, tier: Tier, question: str, results: list[ToolResult], context: str
    ) -> tuple[str, str] | None:
        facts = "\n".join(f"- {r.name}: {r.summary}" for r in results)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + ("\n" + context if context else "")},
            {
                "role": "user",
                "content": (
                    f"Question: {question}\n\nTool results — the ONLY facts you "
                    f"may use:\n{facts}\n\nAnswer in at most three short sentences."
                ),
            },
        ]
        reply = self._chat(tier, messages, with_tools=False)
        if reply is None:
            return None
        text = self._clean((reply.get("message") or {}).get("content") or "")
        if not text:
            return None
        return text, reply.get("_source", "model")

    def _chat(
        self, tier: Tier, messages: list[dict[str, Any]], *, with_tools: bool
    ) -> dict[str, Any] | None:
        """Try the active rung, then walk down the ladder rather than fail."""
        ladder = [tier] + [
            other for other in self.ladder.reachable_tiers() if other.name != tier.name
        ]
        for rung in ladder:
            reply = self._chat_one(rung, messages, with_tools=with_tools)
            if reply is not None:
                reply["_tier"] = rung
                return reply
        return None

    def _chat_one(
        self, tier: Tier, messages: list[dict[str, Any]], *, with_tools: bool
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            "model": tier.model,
            "messages": messages,
            "max_tokens": self.settings.model_max_tokens,
            "temperature": 0.2,
        }
        if tier.thinking_off:
            # Without this a Qwen3-class model spends the entire budget on
            # hidden reasoning and answers with an empty string.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tier.system_prefix:
            # Nemotron-3-nano-omni needs /no_think in the SYSTEM message, or
            # it fails the same way: reasoning consumes the budget and the
            # content comes back empty.
            messages = [dict(m) for m in messages]
            if messages and messages[0].get("role") == "system":
                messages[0]["content"] = f"{tier.system_prefix} {messages[0]['content']}"
            else:
                messages.insert(0, {"role": "system", "content": tier.system_prefix})
            payload["messages"] = messages
        if with_tools and tier.supports_tools:
            payload["tools"] = self.registry.schemas()
            payload["tool_choice"] = "auto"

        cached = self._cache_read(tier, payload)
        if cached is not None:
            cached["_source"] = "cache"
            self._serving = ("cache", tier)
            return cached
        if self.settings.offline:
            return None

        headers = {"Content-Type": "application/json"}
        if tier.api_key:
            headers["Authorization"] = f"Bearer {tier.api_key}"
        # --max-num-seqs 2 on the GB10 pair: helm never fans out.
        with self._call_lock:
            try:
                response = self._post(
                    f"{tier.base_url.rstrip('/')}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self.settings.model_timeout,
                )
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("model tier %s failed: %s", tier.name, exc)
                # The rung is not up. Drop it and re-probe, so the next
                # principal cannot name a model that is not answering.
                self.ladder.invalidate(tier, reason=type(exc).__name__)
                return None
        try:
            message = body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            return None
        # An empty content with no tool call is a failed turn, not an answer.
        # It must never be rendered as a blank bubble.
        if not (message.get("content") or "").strip() and not message.get("tool_calls"):
            log.warning(
                "model tier %s returned an empty message; degrading", tier.name
            )
            return None
        self._serving = ("model", tier)
        # What the endpoint says it is. A rung slot's configured model is what
        # helm ASKED for; this is what answered.
        self._serving_reported = str(body.get("model") or "")
        result = {"message": message, "_source": "model"}
        self._cache_write(tier, payload, {"message": message})
        return result

    # -------------------------------------------------------------- cache
    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        blob = json.dumps(
            {k: v for k, v in payload.items() if k != "temperature"}, sort_keys=True
        )
        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    def _cache_read(self, tier: Tier, payload: dict[str, Any]) -> dict[str, Any] | None:
        path = self.cache_dir / f"{self._key(payload)}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def _cache_write(self, tier: Tier, payload: dict[str, Any], body: dict[str, Any]) -> None:
        path = self.cache_dir / f"{self._key(payload)}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(body, indent=2))
        except OSError as exc:  # pragma: no cover
            log.warning("could not cache model response: %s", exc)

    # ---------------------------------------------- scripted-demo cache
    @staticmethod
    def _scripted_key(question: str) -> str:
        normalized = " ".join(question.lower().split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:24]

    def scripted_cache_write(self, question: str, text: str) -> None:
        """Record a demo turn so it replays with the network off."""
        path = self.cache_dir / "scripted" / f"{self._scripted_key(question)}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"question": question, "text": text}, indent=2))
        except OSError as exc:  # pragma: no cover
            log.warning("could not cache scripted turn: %s", exc)

    def _scripted_cache_read(self, question: str) -> str | None:
        path = self.cache_dir / "scripted" / f"{self._scripted_key(question)}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())["text"]
        except (OSError, ValueError, KeyError):
            return None
