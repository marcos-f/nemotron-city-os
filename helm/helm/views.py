"""Build the render context for each page from the real federation."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from helm.console import Console
from helm.nemoclerk import NemoClerk
from helm.pages import PAGES, TABS, Page
from helm.signet import Identity, Signet

PORTS = {
    "throughline": 8600,
    "docket": 8601,
    "breaker": 8602,
    "siren": 8603,
    "blindspot": 8604,
    "helm": 8610,
}

# Marker TYPE is encoded by shape, marker STATE by colour. The keys are the
# REAL Seattle Fire 911 vocabulary siren serves — "Aid Response", "MVI -
# Motor Vehicle Incident", "Natural Gas Odor", "Activated CO Detector" — not
# tidy category words, so the match is on substrings of what actually
# arrives. Order matters: the more specific needle is tried first, because
# "Auto Fire Alarm" is an alarm and "Brush Fire" is a fire.
MARKER_SHAPES: tuple[tuple[str, str, str], ...] = (
    ("motor vehicle", "square", "collision"),
    ("mvi", "square", "collision"),
    ("collision", "square", "collision"),
    ("natural gas", "diamond", "hazard"),
    ("hazmat", "diamond", "hazard"),
    ("co detector", "diamond", "hazard"),
    ("carbon monoxide", "diamond", "hazard"),
    ("fire", "triangle", "fire"),
    ("burn", "triangle", "fire"),
    ("smoke", "triangle", "fire"),
    ("alarm", "triangle", "fire"),
    ("medic", "circle", "medical"),
    ("aid", "circle", "medical"),
    ("rescue", "circle", "medical"),
    ("crisis", "circle", "medical"),
)

# The approval queue's default page size. Kept at 25 because that is the
# view the scripted demo shows; what changed is that 25 is now the FIRST
# page of a queue you can walk, not the only 25 rows that exist.
QUEUE_PAGE_SIZE = 25

# Page sizes the operator can pick from. ``0`` means "every held effect on
# one page" — the bound that makes the whole queue reachable in a single
# interaction from anywhere in it, no matter how long the queue grows.
QUEUE_PAGE_SIZES: tuple[int, ...] = (25, 100, 250, 0)

# How many numbered page links to show around the current page. The first
# and last page always get a link as well, so any page is at most two hops
# away and the last page is always one.
QUEUE_PAGE_WINDOW = 2

# The fields a queue search looks at. An operator hunting a specific held
# effect knows one of: the approval id, the effect id, the description, or
# that it is irreversible.
QUEUE_SEARCH_FIELDS = ("id", "effect_id", "description", "reversibility")

MARKER_LEGEND = (
    {"shape": "triangle", "label": "fire / alarm"},
    {"shape": "square", "label": "collision"},
    {"shape": "circle", "label": "medical / aid"},
    {"shape": "diamond", "label": "hazard / other"},
)

MARKER_SLOTS = (
    (346, 92),
    (460, 156),
    (228, 166),
    (326, 236),
    (498, 96),
    (204, 240),
)

# The Seattle bounding box the reference map is drawn to. Incidents are placed
# by their REAL latitude and longitude, so a marker means where it says.
SEATTLE_BBOX = (47.49, 47.74, -122.44, -122.24)
# x0, x1, y0, y1 inside the 620x300 view. x1 keeps a numbered pin (r=9) and
# its ring fully inside the map frame — nothing may cross the right edge.
MAP_PLOT = (118.0, 592.0, 20.0, 280.0)


def _classify(kind: str) -> tuple[str, str]:
    """Shape and legend category for a raw incident_type string."""
    lowered = kind.lower()
    for needle, shape, category in MARKER_SHAPES:
        if needle in lowered:
            return shape, category
    return "diamond", "hazard"


def _incident_list(payload: Any) -> list[dict[str, Any]]:
    """siren serves a bare list; tolerate an envelope without inventing rows."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("incidents")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


#: A numbered pin and the ring around it. Two pins closer than this overlap.
PIN_PITCH = 26.0
#: Half-height of a pin glyph, used when clearing the map's own lettering.
PIN_RADIUS = 12.0

# The map's own furniture, in the same 620x300 user space the template draws
# it in: (x-start, baseline-y, character count). A pin landing on a district
# name splits it — "S. L(5)KE UNION" — so these are treated as occupied and
# pins are pushed clear of them, rather than merely haloed on top.
DISTRICT_LABELS: tuple[tuple[float, float, int], ...] = (
    (196, 36, 7), (300, 36, 7), (432, 36, 10),
    (196, 114, 11), (300, 114, 13), (432, 114, 12),
    (196, 192, 8), (300, 192, 8), (432, 192, 12),
    (196, 270, 4), (300, 270, 11), (432, 270, 7),
    (20, 162, 11),   # PUGET SOUND, rotated
    (150, 292, 3),   # I-5
    (536, 24, 8),    # the ACTIVE count, right-aligned to 600
)
#: 11px type with 1.2 letter-spacing runs about this wide per character.
LABEL_CHAR_WIDTH = 7.6
LABEL_HALF_HEIGHT = 9.0


def _on_a_label(x: float, y: float) -> bool:
    """Would a pin at (x, y) sit on top of the map's own lettering?"""
    for lx, ly, chars in DISTRICT_LABELS:
        width = chars * LABEL_CHAR_WIDTH
        if (
            lx - PIN_RADIUS <= x <= lx + width + PIN_RADIUS
            and ly - LABEL_HALF_HEIGHT - PIN_RADIUS <= y <= ly + LABEL_HALF_HEIGHT + PIN_RADIUS
        ):
            return True
    return False


def _place_incidents(incidents: list[dict[str, Any]], limit: int = 6) -> list[dict[str, Any]]:
    """Numbered pins, placed by real coordinates and guaranteed not to collide.

    The map carries NO inline text. Earlier revisions labelled each marker in
    place and the labels overprinted each other around Downtown and ran off
    the right edge — a label you cannot read is worse than no label. The ids
    live in the list beneath the map, keyed by the pin number, which is also
    how a dispatcher reads a real incident board.
    """
    lat0, lat1, lon0, lon1 = SEATTLE_BBOX
    x0, x1, y0, y1 = MAP_PLOT
    markers: list[dict[str, Any]] = []
    placed: list[tuple[float, float]] = []

    def collides(x: float, y: float, *, avoid_labels: bool = True) -> bool:
        if any((x - px) ** 2 + (y - py) ** 2 < PIN_PITCH**2 for px, py in placed):
            return True
        return avoid_labels and _on_a_label(x, y)

    def find_free(x: float, y: float) -> tuple[float, float]:
        """Spiral outward from the true position to the nearest free spot.

        The pin keeps its real location whenever it can; when it cannot, it
        moves the smallest distance that clears both its neighbours and the
        map's own lettering, and it never leaves the plot box. If the map is
        so crowded that no clear spot exists, separating the PINS wins over
        keeping the district names pristine — two merged pins are a worse
        defect than one obscured word.
        """
        import math

        for avoid_labels in (True, False):
            if not collides(x, y, avoid_labels=avoid_labels):
                return x, y
            for ring in range(1, 10):
                radius = (PIN_PITCH * 0.75) * ring
                steps = max(8, ring * 8)
                for step in range(steps):
                    angle = (2 * math.pi * step) / steps
                    cx = min(max(x + radius * math.cos(angle), x0), x1)
                    cy = min(max(y + radius * math.sin(angle), y0), y1)
                    if not collides(cx, cy, avoid_labels=avoid_labels):
                        return cx, cy
        return x, y

    for index, incident in enumerate(incidents):
        if len(markers) >= limit:
            break
        kind = str(incident.get("type") or incident.get("incident_type") or "incident")
        shape, category = _classify(kind)
        try:
            lat = float(incident["lat"])
            lon = float(incident["lon"])
            x = x0 + (x1 - x0) * (lon - lon0) / (lon1 - lon0)
            y = y1 - (y1 - y0) * (lat - lat0) / (lat1 - lat0)
            located = True
        except (KeyError, TypeError, ValueError):
            if index >= len(MARKER_SLOTS):
                continue
            x, y = MARKER_SLOTS[index]
            located = False
        x, y = find_free(min(max(x, x0), x1), min(max(y, y0), y1))
        placed.append((x, y))
        closed = str(incident.get("status", "active")).lower() in {"closed", "cleared"}
        markers.append(
            {
                "n": len(markers) + 1,
                "x": round(x),
                "y": round(y),
                "shape": shape,
                "category": category,
                "tone": "muted" if closed else "amber",
                "id": str(incident.get("id", "INC")),
                "kind": kind,
                "address": str(incident.get("address", "")),
                "state": "closed" if closed else "active",
                "located": located,
            }
        )
    return markers


class ViewBuilder:
    """One place where a page's context is assembled."""

    def __init__(
        self, console: Console, signet: Signet, clerk: NemoClerk, settings: Any
    ) -> None:
        self.console = console
        self.signet = signet
        self.clerk = clerk
        self.settings = settings

    # ---------------------------------------------------------------- base
    def base(self, slug: str, identity: Identity) -> dict[str, Any]:
        page: Page = PAGES[slug]
        services = self.console.service_statuses()
        feeds = self.console.feeds()
        prefs = self.signet.prefs_for(identity.subject) if identity.subject else {}
        if page.open_feed == "auto":
            # helm's own feed is the whole loop; open whichever class is
            # actually carrying events rather than a section that reads empty.
            with_events = [f for f in feeds if f["events"]]
            page = _reopen(
                page,
                max(with_events, key=lambda f: len(f["events"]))["name"]
                if with_events
                else "document",
            )
        session = self.clerk.sessions.get(
            identity.subject, page.feature_area, page.about.priming(page.feature_area)
        )
        transcript = [t.to_dict() for t in session.turns[-8:]]
        return {
            "page": page,
            "identity": identity,
            "services": services,
            "feeds": feeds,
            "prefs": prefs or dict(self.signet.prefs_for("")),
            "transcript": transcript,
            "clerk": self.clerk.pane_header(),
            "ports": PORTS,
            "auth_disabled_reason": self._auth_disabled_reason(),
            "substrate_last_seen": self._substrate_last_seen(),
            # every page needs to know whether the gate is READABLE before it
            # says anything about what is or is not in it
            "gate_known": self.console.substrate_online(),
            "substrate_alarms": self.console.substrate_alarms(),
            "env": self.settings.env,
            "auth_disabled": self.settings.auth_disabled,
        }

    def _substrate_last_seen(self) -> dict[str, Any]:
        """What throughline last proved, for the outage pane to cite."""
        chain, at = self.console.last_seen.get("ledger.chain")
        head, _ = self.console.last_seen.get("ledger.head")
        if not isinstance(chain, dict):
            return {}
        return {
            "value": f"a {chain.get('chain_length')}-entry chain, verification "
                     f"{'valid' if chain.get('valid') else 'FAILED'}",
            "head": head or "",
            "at": at,
        }

    def _auth_disabled_reason(self) -> str:
        """Why authentication is off on this run, if it is.

        There is no mock/dev one-click sign-in as a product capability any
        more, so the only flag left that can weaken this run is
        ``AUTH_DISABLED`` — and it is a boot refusal outside dev and test.
        """
        if self.settings.auth_disabled:
            return "AUTH_DISABLED=1 — dev/test only; staging and production refuse to boot."
        return ""

    def boot(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return {
            "page": ctx["page"].feature_area,
            "subject": ctx["identity"].subject,
            "role": ctx["identity"].role,
            "authenticated": ctx["identity"].authenticated,
            "prefs": ctx["prefs"],
        }

    # ---------------------------------------------------------------- helm
    def helm(
        self,
        identity: Identity,
        query: str = "",
        page: int = 1,
        per: str | int = QUEUE_PAGE_SIZE,
    ) -> dict[str, Any]:
        ctx = self.base("helm", identity)
        overview = self.console.overview()
        gate = self.console.gate()
        pending = gate.value if gate.known else []
        queue_rows = self._queue_rows(pending)
        ctx.update(
            {
                "overview": overview,
                "ledger": self.console.ledger(limit=12, reveal_identity=identity.is_admin),
                "gate": gate,
                "pending": pending,
                "queue_rows": queue_rows,
                "queue_page": self._queue_page(queue_rows, query, page, per),
                "stages": self.console.stage_states(None),
                "banner_text": _gate_banner(gate),
            }
        )
        if gate.known and not pending:
            ctx["page"] = _retone(ctx["page"], "green")
        elif not gate.known:
            ctx["page"] = _retone(ctx["page"], "red")
        return ctx

    @staticmethod
    def _queue_rows(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The queue as addressable ROWS, not one undifferentiated paragraph.

        219 held effects used to render as a single 11,000-character
        sentence with no links, so 218 of them could not be acted on at all
        while /approval-detail/{id} rendered every one of them correctly.
        The capability existed and was simply unreachable.
        """
        rows = []
        for item in pending:
            rows.append(
                {
                    "id": item.get("id", ""),
                    "effect_id": item.get("effect_id", ""),
                    "description": item.get("description") or item.get("effect_id", ""),
                    "reversibility": item.get("reversibility_class", ""),
                    "queued_at": item.get("queued_at", ""),
                    "age": _age_of(item.get("queued_at", "")),
                    "href": f"/approval-detail/{item.get('id', '')}",
                }
            )
        return rows

    @staticmethod
    def _queue_href(query: str, page: int, per: int) -> str:
        """A link back to the queue, carrying the whole view state.

        Anchored at ``#approval-queue`` so paging does not dump the operator
        at the top of the console and make them scroll back down to where
        they were working.
        """
        params: list[tuple[str, str]] = []
        if query:
            params.append(("q", query))
        if page > 1:
            params.append(("page", str(page)))
        if per != QUEUE_PAGE_SIZE:
            params.append(("per", "all" if per == 0 else str(per)))
        suffix = f"?{urlencode(params)}" if params else ""
        return f"/console{suffix}#approval-queue"

    @staticmethod
    def _normalise_per(per: str | int) -> int:
        """``per`` as a row count, where ``0`` means every matching row."""
        if isinstance(per, str):
            if per.strip().lower() in ("all", "0"):
                return 0
            try:
                per = int(per)
            except ValueError:
                return QUEUE_PAGE_SIZE
        if per <= 0:
            return 0
        # An unbounded caller-supplied page size is still bounded by the
        # queue itself, so there is nothing to clamp against here beyond
        # refusing a nonsense value.
        return int(per)

    @classmethod
    def _queue_page(
        cls,
        rows: list[dict[str, Any]],
        query: str = "",
        page: int = 1,
        per: str | int = QUEUE_PAGE_SIZE,
    ) -> dict[str, Any]:
        """One page of the approval queue, plus the way to reach every other.

        The queue used to render its first 25 rows and stop. With 340 held
        effects that left 315 irreversible effects with no link, no search
        and no next page — the product's whole claim is that a named human
        releases these, and most of them could not be reached to be
        released. The rows were never the problem: ``_queue_rows`` has
        always emitted an href for every one of them.

        So this returns the visible slice AND the navigation that makes the
        rest reachable: a search across id/effect/description/class, a
        numbered pager whose first and last pages are always one hop away,
        and a "show every held effect" size that puts the entire queue on
        one page. It also returns the counts the template is obliged to
        state, because "25 rows" shown without "of 340" is itself a claim
        the console has not earned.
        """
        needle = (query or "").strip()
        if needle:
            lowered = needle.lower()
            matches = [
                row
                for row in rows
                if any(
                    lowered in str(row.get(field, "") or "").lower()
                    for field in QUEUE_SEARCH_FIELDS
                )
            ]
        else:
            matches = list(rows)

        total = len(rows)
        matching = len(matches)
        per_page = cls._normalise_per(per)

        if per_page == 0:
            pages, current, visible = 1, 1, matches
        else:
            pages = max(1, -(-matching // per_page))
            try:
                current = int(page)
            except (TypeError, ValueError):
                current = 1
            current = min(max(1, current), pages)
            start = (current - 1) * per_page
            visible = matches[start : start + per_page]

        first_index = ((current - 1) * per_page + 1) if visible else 0
        last_index = first_index + len(visible) - 1 if visible else 0

        def href(target_page: int, target_per: int | None = None) -> str:
            return cls._queue_href(
                needle, target_page, per_page if target_per is None else target_per
            )

        low = max(1, current - QUEUE_PAGE_WINDOW)
        high = min(pages, current + QUEUE_PAGE_WINDOW)
        numbers = [
            {"page": n, "href": href(n), "current": n == current}
            for n in sorted({1, *range(low, high + 1), pages})
        ]

        return {
            # counts — always stated, never implied
            "total": total,
            "matching": matching,
            "shown": len(visible),
            "first_index": first_index,
            "last_index": last_index,
            "filtered": bool(needle),
            "query": needle,
            # the slice
            "rows": visible,
            # navigation
            "per": per_page,
            "per_label": "all" if per_page == 0 else str(per_page),
            "page": current,
            "pages": pages,
            "numbers": numbers if pages > 1 else [],
            "prev_href": href(current - 1) if current > 1 else "",
            "next_href": href(current + 1) if current < pages else "",
            "first_href": href(1) if current > 1 else "",
            "last_href": href(pages) if current < pages else "",
            "show_all_href": href(1, 0),
            "showing_all": per_page == 0 or matching <= per_page,
            "clear_href": cls._queue_href("", 1, per_page),
            "sizes": [
                {
                    "value": "all" if size == 0 else str(size),
                    "label": "all" if size == 0 else str(size),
                    "href": href(1, size),
                    "current": size == per_page,
                }
                for size in QUEUE_PAGE_SIZES
            ],
        }

    # -------------------------------------------------------------- docket
    def docket(self, identity: Identity) -> dict[str, Any]:
        ctx = self.base("docket", identity)
        permit: dict[str, Any] = {
            "permitnum": "—",
            "description": "docket is not serving; no permit to quote.",
            "permitclass": "—",
            "permittypedesc": "—",
        }
        total = 0
        mode = "component offline"
        if ctx["services"]["docket"]["online"]:
            reply = self.console.federation.docket.get("/permits", params={"limit": 1})
            if reply.ok and reply.data.get("permits"):
                permit = reply.data["permits"][0]
                total = reply.data.get("total", 0)
            health = ctx["services"]["docket"]["health"].get("mode", {})
            source = str(health.get("judgment_source", "unknown"))
            model = str(health.get("judgment_model", "unknown"))
            mode = f"{source} · {model}"
            ctx_mode_live = source == "hosted" and not health.get("mock")
            ctx["docket_judge"] = {
                "source": source,
                "model": model,
                "live": bool(ctx_mode_live),
                "chip": (
                    f"✓ live · {model}" if ctx_mode_live else f"{source} · {model}"
                ),
            }
        ctx.update(
            {
                "permit": permit,
                "docket_total": total,
                "docket_mode": mode,
                "docket_online": ctx["services"]["docket"]["online"],
                "stages": self.console.stage_states(None, reversibility="reversible"),
                "banner_text": ctx["page"].banner_text,
            }
        )
        return ctx

    # ------------------------------------------------------------- breaker
    def breaker(self, identity: Identity, effect_id: str = "") -> dict[str, Any]:
        ctx = self.base("breaker", identity)
        gate_hold = self._gate_hold(effect_id)
        series = self._telemetry()
        ctx.update(
            {
                "gate_hold": gate_hold,
                "stages": self.console.stage_states(gate_hold),
                "waiting": self._waiting_clock(gate_hold),
                "evidence": self._rule_evidence(),
                "banner_text": (
                    f"{gate_hold.get('description') or gate_hold.get('effect_id')} "
                    "waiting at gate"
                    if gate_hold
                    else "nothing waiting at the gate"
                ),
            }
        )
        ctx.update(series)
        if not gate_hold:
            ctx["page"] = _retone(ctx["page"], "")
        return ctx

    def _gate_hold(self, effect_id: str = "") -> dict[str, Any] | None:
        """The selected proposal, or the oldest irreversible one held.

        breaker used to show one hard-coded proposal and take no parameter,
        so every other held effect was unreachable from the console.
        """
        pending = self.console.approvals("pending")
        if effect_id:
            for approval in pending:
                if effect_id in (approval.get("effect_id"), approval.get("id")):
                    return approval
        for approval in pending:
            if approval.get("reversibility_class") == "irreversible":
                return approval
        return pending[0] if pending else None

    @staticmethod
    def _waiting_clock(approval: dict[str, Any] | None) -> str:
        if not approval or not approval.get("queued_at"):
            return "00:00"
        import datetime as _dt

        try:
            queued = _dt.datetime.fromisoformat(
                str(approval["queued_at"]).replace("Z", "+00:00")
            )
        except ValueError:
            return "00:00"
        delta = _dt.datetime.now(_dt.timezone.utc) - queued
        seconds = max(0, int(delta.total_seconds()))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def _rule_evidence(self, unit: str = "battery_4") -> dict[str, Any]:
        """The divergence evidence, taken from breaker rather than composed.

        The template used to carry literals — soc_delta(-8.2%), +2.1C/10m,
        38%, 31.2C — and the live-series reader looked for keys breaker does
        not emit, so the fallback fired on every request and those numbers
        appeared on the strongest frame in the deck. They exist in no
        dataset. Now the text comes from /evidence/{unit}, and when it
        cannot, the pane SAYS it is a placeholder instead of passing fiction
        off as measurement.
        """
        reply = self.console.federation.breaker.get(f"/evidence/{unit}")
        if reply.ok and isinstance(reply.data, dict) and reply.data.get("evidence"):
            data = reply.data
            return {
                "text": str(data["evidence"]),
                "live": True,
                "unit": str(data.get("unit_id", unit)),
                "tick": data.get("tick"),
                "source": f"breaker :8602 /evidence/{unit}, tick {data.get('tick')}",
            }
        return {
            "text": (
                "the divergence rule renders line by line here, from\n"
                "breaker's own evaluation of the telemetry window."
            ),
            "live": False,
            "unit": unit,
            "tick": None,
            "source": (reply.error or "breaker is not responding")[:160],
        }

    def _telemetry(self) -> dict[str, Any]:
        """The SOC/temperature series, read with the key names breaker uses.

        This read `series`/`points`; breaker emits `soc_pct`/`temp_c`. The
        names never matched, so a hardcoded polyline rendered 100% of the
        time and was labelled as live evidence.
        """
        blank = {
            "soc_points": "",
            "temp_points": "",
            "soc": "—",
            "temp": "—",
            "divergence_at": "",
            "chart_live": False,
            "chart_note": "",
        }
        reply = self.console.federation.breaker.get("/telemetry/series")
        if not reply.ok or not isinstance(reply.data, dict):
            blank["chart_note"] = (
                "no telemetry to plot — breaker is not responding. "
                "No series was substituted."
            )
            return blank
        data = reply.data
        soc = [v for v in (data.get("soc_pct") or []) if isinstance(v, (int, float))]
        temp = [v for v in (data.get("temp_c") or []) if isinstance(v, (int, float))]
        if len(soc) < 2:
            blank["chart_note"] = (
                "breaker returned no state-of-charge series; nothing was plotted."
            )
            return blank
        out = dict(blank)
        out["soc_points"] = _polyline(soc, 20.0, 90.0)
        out["soc"] = f"{float(soc[-1]):.1f}%"
        out["soc_axis"] = _axis_ticks(20.0, 90.0, "%")
        if len(temp) >= 2:
            out["temp_points"] = _polyline(temp, 24.0, 40.0)
            out["temp"] = f"{float(temp[-1]):.1f}°C"
            out["temp_axis"] = _axis_ticks(24.0, 40.0, "°C")
        ticks = data.get("ticks")
        out["divergence_at"] = f"tick {ticks}" if ticks else ""
        out["chart_live"] = True
        out["chart_source"] = f"breaker :8602 /telemetry/series · {len(soc)} ticks"
        return out

    # --------------------------------------------------------------- siren
    def siren(self, identity: Identity) -> dict[str, Any]:
        ctx = self.base("siren", identity)
        markers: list[dict[str, Any]] = []
        active = 0
        feed_label = "siren is not serving — no incident data"
        reload_steps = ["validated", "registered", "flowing"]
        refusal = None
        if ctx["services"]["siren"]["online"]:
            reply = self.console.federation.siren.get("/incidents", params={"limit": 8})
            incidents = _incident_list(reply.data if reply.ok else None)
            markers = _place_incidents(incidents)
            active = len([m for m in markers if m["tone"] == "amber"])
            status = self.console.federation.siren.get("/feed/status")
            if status.ok and isinstance(status.data, dict):
                feed_label = (
                    f"{status.data.get('label', 'siren feed')} · as of "
                    f"{status.data.get('as_of', '—')}"
                )
            timeline = self.console.federation.siren.get("/hot-reload/timeline")
            if timeline.ok and isinstance(timeline.data, dict):
                steps = timeline.data.get("steps") or timeline.data.get("timeline")
                if isinstance(steps, list) and steps:
                    reload_steps = [
                        str(s.get("step") if isinstance(s, dict) else s) for s in steps[:4]
                    ]
        config = self.console.federation.throughline.config()
        if config.ok and isinstance(config.data, dict):
            refusal = config.data.get("refusal")
        refused_stages = [
            {"stage": "signal", "state": "done", "sub": ""},
            {"stage": "judged", "state": "done", "sub": ""},
            {"stage": "gated", "state": "refused", "sub": "refused"},
            {"stage": "approved", "state": "pending", "sub": ""},
            {"stage": "executed", "state": "pending", "sub": ""},
            {"stage": "ledgered", "state": "pending", "sub": ""},
        ]
        ctx.update(
            {
                "incident_markers": markers,
                "active_incidents": active,
                "feed_label": feed_label,
                "marker_legend": MARKER_LEGEND,
                "reload_steps": reload_steps,
                "refusal": refusal,
                "refused_stages": refused_stages,
                "stages": self.console.stage_states(None, reversibility="reversible"),
                "banner_text": (
                    f"config refused: {refusal.get('rule')}"
                    if refusal
                    else "incident class hot-loaded — no standing refusal"
                ),
            }
        )
        if not refusal:
            ctx["page"] = _retone(ctx["page"], "green")
        return ctx

    # ----------------------------------------------------------- blindspot
    def blindspot(self, identity: Identity) -> dict[str, Any]:
        ctx = self.base("blindspot", identity)
        stages = [
            {"stage": "signal", "state": "done", "sub": "ingested"},
            {"stage": "judged", "state": "done", "sub": "captioned"},
            {"stage": "gated", "state": "current", "sub": "auto — reversible"},
            {"stage": "approved", "state": "pending", "sub": ""},
            {"stage": "executed", "state": "pending", "sub": ""},
            {"stage": "ledgered", "state": "pending", "sub": ""},
        ]
        ctx.update(
            {
                "stages": stages,
                "frames": [
                    {"ts": "14:41:02", "caption": "forklift approaching aisle 7, no pedestrians visible", "near_miss": False},
                    {"ts": "14:41:08", "caption": "forklift stationary, pallet load in view", "near_miss": False},
                    {"ts": "14:41:13", "caption": "forklift and pedestrian converge at aisle 7 junction, no clearance", "near_miss": True},
                    {"ts": "14:41:19", "caption": "pedestrian clear, forklift resumes", "near_miss": False},
                    {"ts": "14:41:24", "caption": "aisle 7 empty, no activity", "near_miss": False},
                ],
                "banner_text": (
                    "blindspot is designed, not built — this tab renders its wireframe"
                    if not ctx["services"]["blindspot"]["online"]
                    else ctx["page"].banner_text
                ),
            }
        )
        return ctx

    # ------------------------------------------------------------ composed
    def composed(self, identity: Identity) -> dict[str, Any]:
        ctx = self.base("composed", identity)
        composed = self.console.composed()
        gate_hold = composed.get("gate_hold")
        related: list[dict[str, Any]] = []
        if ctx["services"]["siren"]["online"]:
            reply = self.console.federation.siren.get("/incidents", params={"limit": 3})
            if reply.ok and isinstance(reply.data, dict):
                for incident in reply.data.get("incidents", [])[:3]:
                    related.append(
                        {
                            "id": incident.get("id", "INC"),
                            "type": incident.get("type") or incident.get("incident_type") or "—",
                            "status": incident.get("status", "active"),
                        }
                    )
        ctx.update(
            {
                "composed": composed,
                "gate_hold": gate_hold,
                "related_incidents": related,
                "stages": self.console.stage_states(gate_hold),
                "banner_text": (
                    "this view composed on a live gate-hold"
                    if composed["assembled"]
                    else "no gate-hold — nothing to compose"
                ),
            }
        )
        ctx.update(self._telemetry())
        return ctx

    # ----------------------------------------------------- approval detail
    def approval_detail(self, identity: Identity, approval_id: str = "") -> dict[str, Any]:
        ctx = self.base("approval-detail", identity)
        approvals = self.console.approvals()
        record = None
        if approval_id:
            record = next((a for a in approvals if a.get("id") == approval_id), None)
        if record is None:
            decided = [a for a in approvals if a.get("state") != "pending"]
            record = decided[-1] if decided else (approvals[-1] if approvals else None)
        walk: dict[str, Any] = {"hops": [], "hop_count": 0, "complete": False}
        if record and record.get("effect_id"):
            walk = self.console.walk(record["effect_id"])
        ctx.update(
            {
                "record": record,
                "record_provider": self._provider_for(record),
                "record_issuer": self._issuer_of(record),
                "walk": walk,
                "refusals": self._refusals(),
                "decidable": bool(record and record.get("state") == "pending"),
                "stages": self.console.stage_states(record),
                "banner_text": (
                    f"chain verified — {walk.get('hop_count', 0)} hops"
                    if walk.get("hops")
                    else "no chain to verify yet"
                ),
            }
        )
        return ctx

    @staticmethod
    def _issuer_of(record: dict[str, Any] | None) -> dict[str, Any]:
        """The OIDC issuer behind an approval, or an honest blank.

        throughline now carries `issuer` and `auth_mode` on approval records
        and they are null until the OIDC work lands. A null issuer rendered
        as a verified one would be the same defect as everything else here.
        """
        issuer = (record or {}).get("issuer")
        mode = (record or {}).get("auth_mode")
        if not issuer:
            return {
                "known": False,
                "text": "not recorded — this chain predates issuer capture",
                "mode": str(mode or "unrecorded"),
            }
        return {"known": True, "text": str(issuer), "mode": str(mode or "unrecorded")}

    def _provider_for(self, record: dict[str, Any] | None) -> str:
        if not record or not record.get("decided_by"):
            return "—"
        table = self.signet.roles.read()
        row = table.get(record["decided_by"], {})
        return str(row.get("provider", "—"))

    def _refusals(self) -> list[dict[str, Any]]:
        ledger = self.console.federation.throughline.ledger(limit=200)
        if not ledger.ok:
            return []
        out = []
        for entry in reversed(ledger.data.get("entries", [])):
            if entry.get("type") != "approval.refused":
                continue
            body = entry.get("body") or {}
            out.append(
                {
                    "effect_id": body.get("effect_id", ""),
                    "decided_by": body.get("decided_by", ""),
                    "caller_role": body.get("caller_role", ""),
                    "reason": body.get("reason", ""),
                    "seq": entry.get("seq"),
                    "hash_prefix": str(entry.get("sha256", ""))[:8],
                }
            )
            if len(out) >= 3:
                break
        return out

    # ---------------------------------------------------------------- walk
    def walk(self, identity: Identity, effect_id: str) -> dict[str, Any]:
        ctx = self.base("approval-detail", identity)
        walk = self.console.walk(effect_id)
        ctx.update(
            {
                "effect_id": effect_id,
                "walk": walk,
                "stages": self.console.stage_states(None),
                "banner_text": f"walking {effect_id} — {walk.get('hop_count', 0)} hops",
            }
        )
        return ctx

    # --------------------------------------------------------------- admin
    def admin(self, identity: Identity, role_change: dict[str, Any] | None = None) -> dict[str, Any]:
        ctx = self.base("admin", identity)
        services = ctx["services"]
        classes = []
        sources = {
            "document": ("docket", "Socrata 76t5-zqzr snapshot", "active"),
            "telemetry": ("breaker", "spec-03 fixture stream", "live"),
            "incident": ("siren", "kzjm-xkqj + snapshot", "hot-loaded"),
            "video": ("blindspot", "PhysicalAI shard", "batch"),
        }
        for name, (component, source, status) in sources.items():
            online = bool(services.get(component, {}).get("online"))
            classes.append(
                {
                    "name": name,
                    "source": source,
                    "status": status,
                    "online": online,
                    "reloadable": online and name in self.console.RELOADABLE,
                    "owner": component,
                    "reload_note": (
                        ""
                        if name in self.console.RELOADABLE
                        else f"{component} publishes no reload surface"
                    ),
                    "arrived": self.console.recently_arrived(name),
                }
            )
        gate_policy = []
        config = self.console.federation.throughline.config()
        if config.ok and isinstance(config.data, dict):
            rules = (config.data.get("config") or {}).get("effects") or (
                config.data.get("config") or {}
            ).get("rules") or []
            for rule in rules:
                if isinstance(rule, dict):
                    gate_policy.append(
                        {
                            "name": rule.get("name") or rule.get("effect_type") or "—",
                            "reversibility": rule.get("reversibility")
                            or rule.get("reversibility_class")
                            or "reversible",
                        }
                    )
        signet_holders = len(
            [r for r in self.signet.role_table() if r["role"] in {"admin", "operator"}]
        )
        ctx.update(
            {
                "roles": self.signet.role_table(),
                "role_options": ("admin", "operator", "viewer"),
                "classes": classes,
                "gate_policy": gate_policy,
                "providers": [
                    {
                        "name": "GitHub OIDC",
                        "state": (
                            "✓ active (build-min)"
                            if self.settings.github_client_id
                            else "not configured"
                        ),
                    },
                    {"name": "Google OIDC", "state": "designed"},
                    {"name": "NVIDIA OIDC", "state": "unavailable — adopted only if live-verified"},
                ],
                "role_change": role_change,
                "stages": self.console.stage_states(None),
                "banner_text": f"{signet_holders} signet holders active",
            }
        )
        return ctx

    # ------------------------------------------------------------- profile
    def profile(self, identity: Identity) -> dict[str, Any]:
        ctx = self.base("helm", identity)
        ctx["banner_text"] = "your identity and preferences"
        return ctx

    # --------------------------------------------------------------- guest
    def guest(self) -> dict[str, Any]:
        """A read-only view for a caller who has signed in to nothing.

        Every field here is read from the SAME console methods the public
        JSON API answers with — ``overview``/``sibling_reachability``
        addresses withheld exactly as they are from an anonymous
        ``/healthz`` — so this page can never show a guest anything the
        JSON surface would not also hand them. No write affordance is
        built: the approval queue is rendered as rows with no href, no
        form and no button, because a guest is not an identified human and
        nothing it does may reach the approval path.
        """
        overview = self.console.overview(reveal_addresses=False)
        gate = self.console.gate()
        pending = gate.value if gate.known else []
        ledger = self.console.ledger(limit=12)
        feeds = self.console.feeds(reveal_addresses=False)
        rows = [
            {
                "id": item.get("id", ""),
                "effect_id": item.get("effect_id", ""),
                "description": item.get("description") or item.get("effect_id", ""),
                "reversibility": item.get("reversibility_class", ""),
                "age": _age_of(item.get("queued_at", "")),
            }
            for item in pending
        ]
        return {
            "overview": overview,
            "siblings": overview["services"],
            "feeds": feeds,
            "ledger": ledger,
            "gate": gate,
            "queue_rows": rows,
            "banner_text": _gate_banner(gate),
        }

    # ---------------------------------------------------------------- help
    def help(self) -> dict[str, Any]:
        """What this product is, what a visitor can do, and each
        component's state — by NAME and STATE, never by address.

        Reuses ``sibling_reachability`` rather than re-deriving component
        state, so the help page and ``/healthz`` can never disagree about
        whether a sibling is reachable.
        """
        return {"siblings": self.console.sibling_reachability(reveal_addresses=False)}


def _gate_banner(gate: Any) -> str:
    """Never say the gate is empty when the gate cannot be seen."""
    if not gate.known:
        if gate.has_history:
            return (
                f"gate state UNKNOWN — throughline not responding; last seen "
                f"{gate.last_known} held"
                + (f" at {gate.last_seen_at}" if gate.last_seen_at else "")
            )
        return "gate state UNKNOWN — throughline not responding"
    count = len(gate.value)
    if not count:
        return "queue empty — nothing is held at the gate"
    first = gate.value[0]
    subject = first.get("description") or first.get("effect_id") or "an effect"
    plural = "approval" if count == 1 else "approvals"
    return f"{count} {plural} waiting — {subject}"


def _age_of(queued_at: str) -> str:
    if not queued_at:
        return "—"
    import datetime as _dt

    try:
        queued = _dt.datetime.fromisoformat(str(queued_at).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    seconds = max(0, int((_dt.datetime.now(_dt.timezone.utc) - queued).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _reopen(page: Page, feed_name: str) -> Page:
    """Return a copy of the page with a different feed section open."""
    import copy

    clone = copy.copy(page)
    clone.open_feed = feed_name
    return clone


def _retone(page: Page, tone: str) -> Page:
    """Return a copy of the page with a different banner tone."""
    import copy

    clone = copy.copy(page)
    clone.banner_tone = tone
    return clone


def _axis_ticks(low: float, high: float, unit: str) -> list[dict[str, Any]]:
    """Four labelled ticks with units — a chart without them is a diagram."""
    out = []
    for index in range(4):
        value = high - (high - low) * index / 3
        y = 16 + (180 * index / 3)
        out.append({"y": round(y), "label": f"{value:.0f}{unit}"})
    return out


def _polyline(values: list[Any], low: float, high: float) -> str:
    """Map a numeric series onto the reference chart's plot box."""
    numbers = []
    for value in values:
        try:
            numbers.append(float(value))
        except (TypeError, ValueError):
            continue
    if len(numbers) < 2:
        return ""
    span = max(high - low, 1e-6)
    count = len(numbers)
    points = []
    for index, number in enumerate(numbers):
        x = 48 + (480 * index / (count - 1))
        clamped = min(max(number, low), high)
        y = 196 - (180 * (clamped - low) / span)
        points.append(f"{x:.0f},{y:.0f}")
    return " ".join(points)


TAB_LIST = TABS
