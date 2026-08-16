"""Per-page contract: coordinates, About copy, banners, and prompt chips.

The reference wireframes in ``.viper-context/specs/v0.1.0/ux/web/wireframes``
are the UX contract. This module carries the copy those pages fixed, so the
templates render the same product rather than a re-design of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TABS = (
    {"slug": "docket", "label": "docket", "path": "/docket"},
    {"slug": "breaker", "label": "breaker", "path": "/breaker"},
    {"slug": "siren", "label": "siren", "path": "/siren"},
    {"slug": "blindspot", "label": "blindspot", "path": "/blindspot"},
    {"slug": "composed", "label": "composed", "path": "/composed"},
    {"slug": "datasets", "label": "datasets", "path": "/datasets"},
)


@dataclass
class About:
    what: str
    calls: str
    doing: str
    why: str
    wow: str

    def as_dict(self) -> dict[str, str]:
        return {
            "What": self.what,
            "Calls": self.calls,
            "Doing": self.doing,
            "Why it matters": self.why,
        }

    def priming(self, feature_area: str) -> str:
        return (
            f"You are answering inside the {feature_area} page of helm. "
            f"What: {self.what} Calls: {self.calls} Doing: {self.doing} "
            f"Why it matters: {self.why}"
        )


@dataclass
class Page:
    slug: str
    title: str
    h1: str
    coordinate: str
    feature_area: str
    footer: str
    about: About
    banner_tone: str = ""
    banner_text: str = ""
    banner_sub: str = ""
    banner_link: str = ""
    chips: tuple[str, ...] = ()
    placeholder: str = "Ask NemoClerk…"
    open_feed: str = "document"
    component: str = ""
    seed: tuple[dict[str, Any], ...] = field(default_factory=tuple)


PAGES: dict[str, Page] = {
    "helm": Page(
        slug="helm",
        title="NemoCity — helm",
        h1="helm — console overview",
        coordinate="scenario://helm/shell-tabs",
        feature_area="helm",
        footer="scenario://helm/shell-tabs · scenario://helm/ledger-tail",
        component="throughline",
        open_feed="auto",
        banner_tone="amber",
        banner_text="{pending} approval waiting",
        banner_link="/breaker",
        chips=(
            "what's waiting for approval?",
            "walk the newest chain",
            "which feeds are live?",
        ),
        placeholder="Ask NemoClerk…",
        about=About(
            what="The console — one substrate, four skins, and an agent at the helm.",
            calls=(
                "Every pane is a documented OpenAPI call; NemoClerk drives the same "
                "API over MCP tools."
            ),
            doing=(
                "Live ledger tail with per-row verification, the approval queue, "
                "signal-class status — and a resident agent that operates everything "
                "you see."
            ),
            why=(
                "Four unrelated domains — permits, power, 911, video — visibly one "
                "machine. The operator never leaves this page."
            ),
            wow=(
                "the copilot runs the whole console — and when it tries to approve "
                "its own irreversible effect, the gate refuses it BY ROLE, on the "
                "record, two inches from where the human's approval succeeds."
            ),
        ),
        seed=(
            {"role": "you", "text": "what's waiting for approval?"},
        ),
    ),
    "docket": Page(
        slug="docket",
        title="NemoCity — docket",
        h1="docket — permit intake feed",
        coordinate="scenario://docket/route-ledger",
        feature_area="docket",
        footer=(
            "scenario://docket/judgment-quote · scenario://docket/abstention · "
            "scenario://docket/route-ledger"
        ),
        component="docket",
        open_feed="document",
        banner_text="permit intake — routing is reversible and auto-executes",
        banner_link="/composed",
        chips=("why this route?", "show me an abstention", "quote the source"),
        placeholder="Ask about this permit…",
        about=About(
            what="Permit Triage — the reviewer that quotes its sources or shuts up.",
            calls=(
                "Seattle permits (Socrata 76t5-zqzr, offline snapshot) → Nemotron "
                "judgment (nemotron-3-super-120b) + dim-2048 embeddings → throughline "
                "route effect (reversible)."
            ),
            doing=(
                "Judges each permit; every card carries a VERBATIM quote string-matched "
                "to the source, or an explicit abstention. Accepted routes auto-execute "
                "and land in the ledger."
            ),
            why=(
                "Dana stops skimming 400 applications and starts deciding. This is the "
                "judges' own published Do-track example — running."
            ),
            wow=(
                "an AI that treats “I don't know” as a first-class answer. It "
                "quotes exactly, or it abstains — it never paraphrases legal text."
            ),
        ),
    ),
    "breaker": Page(
        slug="breaker",
        title="NemoCity — breaker",
        h1="breaker — microgrid telemetry intake feed",
        coordinate="scenario://breaker/gate-hold",
        feature_area="breaker",
        footer=(
            "scenario://breaker/divergence-rule · scenario://breaker/gate-hold · "
            "scenario://breaker/identified-approval"
        ),
        component="breaker",
        open_feed="telemetry",
        banner_tone="amber",
        banner_text="battery_4 dispatch waiting at gate",
        banner_sub="no timeout exists",
        banner_link="/composed",
        chips=("is battery_4 safe to leave?", "why is dispatch held?", "walk the chain"),
        placeholder="Ask about the gate…",
        about=About(
            what="Grid Watch — a breaker trips; only a human resets it.",
            calls=(
                "Microgrid telemetry (spec-03 fixture) → deterministic divergence rule "
                "(never a model) → throughline gate → signet-identified approval."
            ),
            doing=(
                "Detects battery_4's SOC/temperature divergence, renders the rule "
                "line-by-line, and files a dispatch proposal that WAITS at the gate — "
                "no timeout exists in any code path."
            ),
            why=(
                "Automation that acts alone is what Ruiz was hired to prevent; missing "
                "the 2am divergence is what he fears. Breaker does everything except "
                "the one thing it must not."
            ),
            wow=(
                "the proposal that cannot proceed — then, after approval, one click "
                "walks effect → judgment → signal with a hash verifying at every hop."
            ),
        ),
    ),
    "siren": Page(
        slug="siren",
        title="NemoCity — siren",
        h1="siren — 911 incident intake feed",
        coordinate="feature-area://siren",
        feature_area="siren",
        footer=(
            "scenario://siren/hot-reload · scenario://throughline/refusal-named"
        ),
        component="siren",
        open_feed="incident",
        banner_tone="red",
        banner_text="config refused: rules[2] dispatch_units",
        banner_sub="incident class hot-loaded",
        chips=("did the new config load?", "why was the second config refused?"),
        placeholder="Ask about the reload…",
        about=About(
            what="City Pulse — a new kind of emergency, live, without a redeploy.",
            calls=(
                "Seattle Fire 911 (Socrata kzjm-xkqj, live with snapshot fallback) → "
                "signal class registration → throughline."
            ),
            doing=(
                "Drops a new incident class into config and watches it validate, "
                "register and flow — while a second config that tries to auto-execute "
                "an irreversible effect is REFUSED at load, naming file, rule and line."
            ),
            why=(
                "The system extends at the speed of a config file, and refuses the one "
                "extension that would remove the human."
            ),
            wow=(
                "the refusal is the pitch: a config that tries to switch the gate off "
                "is rejected at load time, not silently downgraded."
            ),
        ),
    ),
    "blindspot": Page(
        slug="blindspot",
        title="NemoCity — blindspot",
        h1="blindspot — warehouse video intake feed",
        coordinate="scenario://blindspot/frame-captions",
        feature_area="blindspot",
        footer=(
            "scenario://blindspot/frame-captions · scenario://blindspot/near-miss-alert "
            "· scenario://blindspot/honesty-labels"
        ),
        component="blindspot",
        open_feed="video",
        banner_tone="amber",
        banner_text="near-miss on cam3 — work order queued",
        chips=("what almost happened?", "how fresh are these captions?"),
        placeholder="Ask about the near-miss…",
        about=About(
            what="Floor Watch — the frame you didn't see until it mattered.",
            calls=(
                "NVIDIA PhysicalAI warehouse footage (one 5GB shard) → ffmpeg frame "
                "sampling → Nemotron VLM captions (~5.5s/frame, batch) → alert → "
                "reversible work order via throughline."
            ),
            doing=(
                "Replays yesterday's footage, captions sampled frames, surfaces the "
                "forklift–human near-miss as an alert card feeding the same loop as "
                "every other signal."
            ),
            why=(
                "Near-misses are the leading indicator everyone ignores because nobody "
                "watches 400 hours of footage."
            ),
            wow=(
                "the near-miss surfaces itself — honestly labelled batch-not-streaming, "
                "because claiming realtime would be a lie and this product refuses lies."
            ),
        ),
    ),
    "composed": Page(
        slug="composed",
        title="NemoCity — composed",
        h1="composed — live gate-hold view",
        coordinate="scenario://helm/composed-state",
        feature_area="composed",
        footer="scenario://helm/composed-state",
        component="throughline",
        open_feed="auto",
        banner_tone="amber",
        banner_text="this view composed on a live gate-hold",
        chips=("why did this view appear?", "what needs me right now?"),
        placeholder="Ask why this composed…",
        about=About(
            what="Composed view — the console reacts to the signal.",
            calls=(
                "Assembled automatically from a gate-hold event: proposal + sparkline + "
                "approval + filtered ledger + nearby incidents."
            ),
            doing=(
                "Nothing here is a screen an operator navigates to; it is helm reacting "
                "to a live gate-hold by pulling every relevant pane into one view."
            ),
            why=(
                "When the irreversible moment arrives, the operator should not have to "
                "hunt across four tabs for context."
            ),
            wow=(
                "the UI composes itself in response to a live signal — the adaptive "
                "experience, designed as a first-class state."
            ),
        ),
    ),
    "approval-detail": Page(
        slug="approval-detail",
        title="NemoCity — approval record",
        h1="approval record — the chain, and who signed it",
        coordinate="scenario://signet/subject-in-record",
        feature_area="approval-detail",
        footer=(
            "scenario://signet/subject-in-record · scenario://helm/agent-refusal · "
            "scenario://breaker/ledger-walk"
        ),
        component="throughline",
        open_feed="auto",
        banner_tone="green",
        banner_text="chain verified",
        chips=("who approved this?", "why was the agent refused?"),
        placeholder="Ask about this approval…",
        about=About(
            what="Approval record — WHO, as verifiably as WHY.",
            calls=(
                "throughline approval schema (subject required) + signet OIDC identity "
                "+ hash chain."
            ),
            doing=(
                "One approval, fully accounted: effect, judgment, signal, the human's "
                "OIDC subject, rationale — and the refused variant beside it, with the "
                "agent principal named."
            ),
            why=(
                "A compliance reviewer's entire job — 'establish why an action was "
                "taken' — answered by one record."
            ),
            wow=(
                "the approval carries a verified identity, and the refusal is just as "
                "permanent as the approval. Both are ledger entries; neither can be "
                "quietly edited."
            ),
        ),
    ),
    "admin": Page(
        slug="admin",
        title="NemoCity — admin",
        h1="admin — site configuration",
        coordinate="use-case://signet/bind-identity",
        feature_area="admin",
        footer=(
            "use-case://signet/bind-identity · scenario://throughline/no-bypass · "
            "scenario://signet/provider-policy"
        ),
        component="throughline",
        open_feed="auto",
        banner_text="{signet_holders} signet holders active",
        chips=("who can approve dispatches?", "what would happen if I disabled the gate?"),
        placeholder="Ask about roles…",
        about=About(
            what="Site configuration under RBAC — roles, signal classes, gate policy, providers.",
            calls="signet's role table, siren's class registry, throughline's effect registry.",
            doing="Role edits are effects: ledgered, with the editing admin's signet identity.",
            why="Configuration is where governance lives or dies.",
            wow=(
                "even admin cannot switch the gate off — the disable control is "
                "permanently inert, by construction, and the UI says why."
            ),
        ),
    ),
}


def page(slug: str) -> Page:
    return PAGES[slug]
