"""The 911 feed: live Socrata poll, cached snapshot fallback, honest labels.

Cut order #2 says live mode degrades first. That is a design constraint, not
a disclaimer: every demo beat downstream of this module reads a snapshot the
same way it reads a live poll, so losing the network costs the demo an
as-of label and nothing else.

Two properties this module is responsible for, both of them corrections from
review:

**Offline means offline, at runtime.** ``OFFLINE_MODE=1`` is a boot-time
switch and cannot reach a process that is already running, so a scripted demo
that arms its own socket guard could not stop siren polling Socrata behind
it. There is now a runtime override as well (``set_offline``, exposed over
HTTP as ``POST /feed/mode``), and ``fetch_live`` refuses outright while it is
on — the refusal is upstream of anything that could open a socket, so
"offline" is a property of the code path, not of a timeout.

**A Seattle wall clock is not UTC.** Socrata stamps ``datetime`` as floating
LOCAL time with no zone. Relabelling it ``Z`` made every incident read seven
hours old in summer and produced the absurdity a reviewer caught live: an
as-of of 06:49:13Z with a newest incident of 23:42:00Z on a feed sorted
newest-first. It is converted through ``America/Los_Angeles`` now, and both
representations plus the zone travel with the row so the conversion is
auditable rather than trusted. When the source has no usable timestamp the
row says so — ``reported_at: null`` with ``reported_at_missing: true`` —
because inventing ``now()`` is exactly the thing this product argues against.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from .models import FeedStatus, Incident

#: Seattle Real-Time Fire 911 Calls. Open data, no app token required.
SOCRATA_DATASET = "kzjm-xkqj"
DEFAULT_FEED_URL = f"https://data.seattle.gov/resource/{SOCRATA_DATASET}.json"
DEFAULT_LIMIT = 50

#: Shipped with the package so a clean install can serve the whole demo path
#: before it has ever reached the network.
SEED_SNAPSHOT = Path(__file__).with_name("seed_snapshot.json")

LIVE_LABEL = "live · Seattle Fire 911"
SNAPSHOT_LABEL = "snapshot (cached) — live feed not reached"
OFFLINE_LABEL = "snapshot (cached) — OFFLINE_MODE"

#: The zone the Seattle Fire dataset's floating ``datetime`` is written in.
#: Named, not an offset: Seattle is UTC-7 in August and UTC-8 in January, and
#: a hard-coded offset would be right for half the year.
SOURCE_TZ_NAME = "America/Los_Angeles"
SOURCE_TZ = ZoneInfo(SOURCE_TZ_NAME)

#: Snapshots written before the timezone correction carry Seattle-local wall
#: clocks mislabelled ``Z``. Reads migrate them; writes stamp this version so
#: a migrated snapshot is not migrated twice.
SNAPSHOT_SCHEMA = 2

TRUTHY = ("1", "true", "yes", "on")

#: Runtime offline override. ``None`` means "no override — consult the
#: environment". Set by ``set_offline``.
_offline_override: Optional[bool] = None
_offline_lock = threading.Lock()


class OfflineRefusal(RuntimeError):
    """A poll was attempted while siren is offline. Nothing was sent.

    This is raised *before* a URL is built, so it is not a request that
    failed — it is a request that was never made.
    """


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_offline() -> bool:
    return os.environ.get("OFFLINE_MODE", "").strip() in TRUTHY


def offline_mode() -> bool:
    """Is siren offline right now?

    The runtime override wins when it is set, otherwise ``OFFLINE_MODE=1``.
    Either way no poll is attempted, so no socket is opened — the offline
    tests assert exactly that.
    """
    override = _offline_override
    if override is not None:
        return override
    return _env_offline()


def offline_source() -> str:
    """Where the current offline verdict comes from. Reported, never guessed."""
    if _offline_override is not None:
        return "runtime-switch"
    if _env_offline():
        return "OFFLINE_MODE"
    return "default"


def set_offline(value: Optional[bool]) -> bool:
    """Flip the runtime offline switch. ``None`` clears it back to the env.

    This exists because ``OFFLINE_MODE`` is read from the environment, and a
    scripted demo cannot put an environment variable into a siren process
    that is already running. Without a runtime switch, ``demo.py --offline``
    was offline only for the demo's own process while siren went on polling
    Socrata beside it.
    """
    global _offline_override
    with _offline_lock:
        _offline_override = value
    return offline_mode()


def offline_state() -> dict[str, Any]:
    """The switch, described: what it is, who set it, and what it forbids."""
    return {
        "offline": offline_mode(),
        "source": offline_source(),
        "override": _offline_override,
        "env": _env_offline(),
        "guarantee": (
            "no outbound request is attempted while offline: fetch_live "
            "refuses before a URL is built, and the substrate is forced to "
            "the in-process mock"
        ) if offline_mode() else "live polling is permitted",
    }


def data_dir() -> Path:
    return Path(os.environ.get("SIREN_DATA_DIR", "data"))


def snapshot_path() -> Path:
    return data_dir() / "snapshot.json"


@dataclass(frozen=True)
class ReportedAt:
    """One source timestamp, converted, with the conversion shown.

    ``utc`` and ``local`` are the same instant in two frames; ``tz`` names the
    frame the source was written in. All three travel together so a reader can
    check the arithmetic instead of trusting it.
    """

    utc: Optional[str]
    local: Optional[str]
    tz: str = SOURCE_TZ_NAME
    missing: bool = False
    raw: Optional[str] = None


def _local_iso(moment: datetime) -> str:
    """Wall clock in the source zone, carrying its offset (…-07:00)."""
    return moment.astimezone(SOURCE_TZ).isoformat(timespec="seconds")


def parse_reported_at(raw: Optional[str]) -> ReportedAt:
    """Convert one Socrata ``datetime``. Never invents one.

    Socrata's value is a floating Seattle wall clock: ``2026-08-15T23:42:00``
    means 23:42 in Seattle, which in August is ``2026-08-16T06:42:00Z``. The
    previous implementation stamped a ``Z`` on it without converting, which is
    a seven-hour error dressed as a well-formed value — undetectable
    downstream, and fatal for a feed whose whole question is "how old is
    this?".

    A missing or unparseable source timestamp returns nulls with
    ``missing=True``. It used to return ``now()``, which meant a row with no
    timestamp rendered as the freshest incident on the board.
    """
    if not raw:
        return ReportedAt(utc=None, local=None, missing=True, raw=raw)
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ReportedAt(utc=None, local=None, missing=True, raw=raw)
    if parsed.tzinfo is None:
        # Floating: this is the case Socrata actually sends, and the one the
        # bug was in. Attach the SOURCE zone, do not assume UTC.
        parsed = parsed.replace(tzinfo=SOURCE_TZ)
    return ReportedAt(
        utc=iso(parsed),
        local=_local_iso(parsed),
        tz=SOURCE_TZ_NAME,
        missing=False,
        raw=text,
    )


def _parse_reported_at(raw: Optional[str]) -> Optional[str]:
    """Back-compat shim: the UTC half of :func:`parse_reported_at`.

    Kept because callers and tests reference it. It now returns ``None``
    rather than a fabricated ``now()`` when the source has no timestamp.
    """
    return parse_reported_at(raw).utc


def record_to_incident(record: dict[str, Any]) -> Optional[Incident]:
    """Map one Socrata row. Rows without coordinates cannot be mapped, and a
    map pane that invents a location is worse than one that omits a row.

    A row without a *timestamp*, by contrast, is still mappable and is kept:
    it renders with the time unknown, which is a fact, where ``now()`` was a
    fiction. Location and time fail differently because they fail
    differently — an unplaceable row has nowhere to go on a map.
    """
    lat, lon = record.get("latitude"), record.get("longitude")
    if lat in (None, "") or lon in (None, ""):
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if lat_f == 0.0 and lon_f == 0.0:
        return None
    reported = parse_reported_at(record.get("datetime"))
    return Incident(
        id=str(record.get("incident_number") or record.get("address") or "unknown"),
        incident_type=str(record.get("type") or "unknown"),
        lat=lat_f,
        lon=lon_f,
        reported_at=reported.utc,
        reported_at_local=reported.local,
        tz=reported.tz,
        reported_at_missing=reported.missing,
        address=record.get("address"),
    )


def migrate_incident_row(row: dict[str, Any], schema: int) -> dict[str, Any]:
    """Bring one cached row up to the current timestamp schema.

    A snapshot written before the fix holds Seattle wall clocks stamped
    ``Z``. Serving it unchanged would keep the seven-hour error alive in
    every offline run, which is precisely where it would be least visible.
    The row's ``reported_at`` is therefore re-read *as a local wall clock*
    and converted, which is the same operation that should have happened
    when it was ingested.

    Idempotent by schema stamp: a row that already carries the fix is left
    exactly as it is, so a migrated cache is never migrated twice.
    """
    if schema >= SNAPSHOT_SCHEMA:
        return row
    stamped = row.get("reported_at")
    if not stamped:
        return {**row, "reported_at": None, "reported_at_local": None,
                "tz": SOURCE_TZ_NAME, "reported_at_missing": True}
    # Strip the Z that was never true, then re-read as a local wall clock.
    reported = parse_reported_at(str(stamped).rstrip("Z"))
    return {
        **row,
        "reported_at": reported.utc,
        "reported_at_local": reported.local,
        "tz": reported.tz,
        "reported_at_missing": reported.missing,
    }


class Snapshot:
    """The cached fallback, on disk, with the timestamp it was taken."""

    def __init__(self, as_of: str, incidents: list[Incident], source: str,
                 schema: int = SNAPSHOT_SCHEMA, migrated: bool = False) -> None:
        self.as_of = as_of
        self.incidents = incidents
        self.source = source
        self.schema = schema
        #: True when this snapshot was read from a pre-fix cache and corrected
        #: on the way in. Surfaced so an operator can tell a re-cut snapshot
        #: from a repaired one.
        self.migrated = migrated

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of,
            "source": self.source,
            "dataset": SOCRATA_DATASET,
            "schema": SNAPSHOT_SCHEMA,
            "tz": SOURCE_TZ_NAME,
            "incidents": [i.model_dump() for i in self.incidents],
        }

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Snapshot":
        schema = int(raw.get("schema") or 1)
        rows = [migrate_incident_row(row, schema) for row in raw.get("incidents", [])]
        return Snapshot(
            schema=SNAPSHOT_SCHEMA,
            migrated=schema < SNAPSHOT_SCHEMA,
            incidents=[Incident(**row) for row in rows],
            source=raw.get("source") or "seattle.fire.911",
            as_of=raw.get("as_of") or iso(utcnow()),
        )


def read_snapshot() -> Optional[Snapshot]:
    """Runtime cache first, packaged seed second. Either is a snapshot and
    both are labelled as one."""
    for candidate in (snapshot_path(), SEED_SNAPSHOT):
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        try:
            return Snapshot.from_dict(raw)
        except Exception:  # a corrupt cache must not take the service down
            continue
    return None


def write_snapshot(snapshot: Snapshot) -> Path:
    """Atomic: write beside the target, then rename. A reader never sees a
    half-written cache, which is the whole reason the cache is trustworthy."""
    target = snapshot_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot.as_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target


def fetch_live(limit: int = DEFAULT_LIMIT, timeout: float = 6.0) -> list[Incident]:
    """One HTTP GET against Socrata. No token: the dataset is public.

    Refuses outright while siren is offline. ``refresh`` already checks the
    switch before calling here, so this guard is redundant on the happy path
    — and that is the point. It makes "offline" a property of the function
    that opens the socket rather than of one caller's ordering, so a future
    caller cannot reach Socrata by taking a different route in.
    """
    if offline_mode():
        raise OfflineRefusal(
            f"siren is offline ({offline_source()}); no request was made to "
            f"the 911 feed. The cached snapshot is served instead."
        )
    url = os.environ.get("SIREN_FEED_URL", DEFAULT_FEED_URL)
    query = f"{url}?$limit={limit}&$order=datetime%20DESC"
    request = urllib.request.Request(query, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    incidents = [record_to_incident(row) for row in payload]
    return [i for i in incidents if i is not None]


class FeedState:
    """What the service is currently serving, and how it came to serve it."""

    def __init__(self) -> None:
        self.incidents: list[Incident] = []
        self.as_of: str = iso(utcnow())
        self.serving: str = "snapshot"
        self.label: str = SNAPSHOT_LABEL
        self.fetched_at: Optional[str] = None
        self.last_error: Optional[str] = None
        #: True when the snapshot being served was repaired on read from a
        #: pre-timezone-fix cache rather than re-cut from the source.
        self.migrated: bool = False

    def newest_reported_at(self) -> Optional[str]:
        """The freshest incident's UTC timestamp, ignoring undated rows."""
        stamps = [i.reported_at for i in self.incidents if i.reported_at]
        return max(stamps) if stamps else None

    def status(self, substrate: str) -> FeedStatus:
        stale = None
        as_of_dt = None
        try:
            as_of_dt = datetime.strptime(self.as_of, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            stale = max(0, int((utcnow() - as_of_dt).total_seconds()))
        except ValueError:
            pass

        # The newest incident's age against the as-of it is served with. This
        # is the number that made the timezone bug visible: an as-of of
        # 06:49:13Z carrying a "newest" incident of 23:42:00Z, i.e. seven
        # hours in the past on a feed sorted newest-first. Publishing it means
        # a regression shows up in the payload rather than in someone's head.
        newest = self.newest_reported_at()
        newest_age = None
        if newest and as_of_dt is not None:
            try:
                newest_dt = datetime.strptime(newest, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                newest_age = int((as_of_dt - newest_dt).total_seconds())
            except ValueError:
                newest_age = None

        return FeedStatus(
            source=self.serving,
            label=self.label,
            as_of=self.as_of,
            fetched_at=self.fetched_at,
            incident_count=len(self.incidents),
            mode="offline" if offline_mode() else "live",
            substrate=substrate,
            stale_seconds=stale,
            tz=SOURCE_TZ_NAME,
            newest_reported_at=newest,
            newest_age_seconds=newest_age,
            undated_incidents=sum(1 for i in self.incidents if i.reported_at_missing),
            snapshot_migrated=self.migrated,
        )


def refresh(state: FeedState, limit: int = DEFAULT_LIMIT) -> FeedState:
    """Poll if allowed, fall back to cache if the poll does not land.

    Order matters: the offline switch is checked before anything that could
    open a socket, so 'offline' means offline rather than 'offline after one
    timeout'. ``fetch_live`` re-checks it for the same reason.
    """
    if offline_mode():
        return _serve_snapshot(state, OFFLINE_LABEL, error=None)

    try:
        incidents = fetch_live(limit=limit)
    except OfflineRefusal as exc:
        # The switch flipped between the check above and the call. Serve the
        # cache; a race must not become a socket.
        return _serve_snapshot(state, OFFLINE_LABEL, error=str(exc))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        return _serve_snapshot(state, SNAPSHOT_LABEL, error=f"{type(exc).__name__}: {exc}")

    if not incidents:
        return _serve_snapshot(state, SNAPSHOT_LABEL, error="live feed returned no rows")

    now = iso(utcnow())
    state.incidents = incidents
    state.as_of = now
    state.fetched_at = now
    state.serving = "live"
    state.label = LIVE_LABEL
    state.last_error = None
    state.migrated = False
    write_snapshot(Snapshot(as_of=now, incidents=incidents, source="seattle.fire.911"))
    return state


def _serve_snapshot(state: FeedState, label: str, error: Optional[str]) -> FeedState:
    snapshot = read_snapshot()
    if snapshot is None:
        # Nothing cached and nothing live: say so rather than serve an empty
        # list that looks like a quiet night in Seattle.
        state.incidents = []
        state.serving = "snapshot"
        state.label = "no snapshot available"
        state.last_error = error or "no snapshot on disk"
        state.migrated = False
        return state
    state.incidents = snapshot.incidents
    state.as_of = snapshot.as_of
    state.fetched_at = None
    state.serving = "snapshot"
    state.label = label
    state.last_error = error
    state.migrated = snapshot.migrated
    return state
