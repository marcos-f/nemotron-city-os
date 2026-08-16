"""Every held effect must be reachable from /console, and the assistant
must announce itself — both asserted in a real browser.

Why a browser and not ``TestClient``: the first of these defects was
"fixed" once already. A reviewer found 218 of 219 held effects unreachable,
the queue was given per-row links, and the finding was closed. The links
were real. The template still rendered ``queue_rows[:25]``, so the console
went on emitting exactly 25 of them, and as the queue grew from 219 to 340
the gap widened instead of closing. Reading the template would not have
caught it either; only counting what the page actually offers does.

So these tests count. Not "are there links" — how many of the queue's ids
can a human actually get to, compared against the length of the queue
itself. No test here contains the number 25, or 340, or any other magic
figure that a growing queue would quietly invalidate: the expected value is
always ``len(queue)``, read from the substrate at run time.

The harness is lifted from ``tests/test_palette_guard.py`` — the same
uvicorn-in-a-thread and the same Chromium — because that file already
proved a real browser is the only witness this codebase accepts.

LIVE REGIONS, AND WHAT THE 368 FIGURE ACTUALLY WAS
--------------------------------------------------
A 192-render sweep (12 pages x 2 themes x 8 dependency states) reported
368 findings of "no aria-live anywhere". Counted as distinct defects that
number is wrong, and the difference matters, so it was measured rather
than argued: a census in this same browser found

  * 12 pages, 0 of which mutate anything on their own — every change on
    this console is driven by an interaction, never by a timer or a poll;
  * 5 distinct places ``helm/static/app.js`` writes text into: the
    NemoClerk transcript, the held-count badge, the decision result, the
    hot-reload note, and a control's own label;
  * of those, 1 was already correct (the ledger tail's tbody, untouched).

368 is therefore the same page-level absence recounted once per render —
and a theme cannot change an ARIA attribute at all, so half the multiplier
is definitionally noise. The real number was 4 silent regions, and the
transcript accounted for 11 of the 12 pages by itself because it lives in
one shared macro. All 4 now announce. ``/`` and ``/login`` still carry no
live region and correctly so: nothing on either page ever changes.

The guard below is what makes that durable. It does not look for
attributes; it watches the DOM through an interaction and asserts that
everything which CHANGED changed inside something that speaks.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fakes import FakeFederation  # noqa: E402

# A queue larger than any single page. 429 is the size the readiness matrix
# recorded on the live substrate; nothing below asserts against it, it is
# only here so the fixture reproduces the conditions the defect appears in.
HELD_EFFECTS = 429

# The console's default page size is a product decision, not a contract, so
# it is read off the view layer rather than restated here.
from helm.views import QUEUE_PAGE_SIZE  # noqa: E402

# Clicking Next this many times must exhaust any queue this fixture builds.
MAX_PAGER_HOPS = 200

# Set by ``loaded_console`` while it is live, so ``page`` can mint a session
# cookie directly against this run's signet instance. There is no product
# route that mints a fixed identity any more, so a browser test signs in the
# same way any other test does: construct the session, never reach a route.
_LIVE_APP = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def chromium():
    """A real Chromium, or a decision about whether its absence is fatal.

    Same contract as the palette guard: CI sets
    ``HELM_REQUIRE_BROWSER_GUARD=1`` so a missing browser fails rather than
    skips, because a guard that silently skips is how a regression returns.
    """
    required = os.environ.get("HELM_REQUIRE_BROWSER_GUARD") == "1"
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment shape
        if required:
            pytest.fail(f"HELM_REQUIRE_BROWSER_GUARD=1 but playwright is absent: {exc}")
        pytest.skip(f"playwright not installed: {exc}")

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as exc:  # pragma: no cover - environment shape
            if required:
                pytest.fail(
                    "HELM_REQUIRE_BROWSER_GUARD=1 but chromium would not launch "
                    f"(run `playwright install --with-deps chromium`): {exc}"
                )
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture(scope="module")
def loaded_console(tmp_path_factory):
    """The real app over HTTP, in front of a gate holding a real backlog.

    Returns ``(base_url, held_ids)``. ``held_ids`` is the truth the page is
    measured against — every test below compares what the console offers
    with this set, never with a literal.
    """
    import uvicorn

    import helm.clients as clients_module
    from helm.app import create_app
    from helm.config import Settings

    fake = FakeFederation()
    held_ids: list[str] = []
    for index in range(HELD_EFFECTS):
        # Descriptions vary so the search has something to discriminate on,
        # and every one of these is irreversible — the class the product
        # exists to hold for a named human.
        held_ids.append(
            fake.propose(
                f"eff-grid-{index:04d}",
                description=f"microgrid load-shed battery_{index % 17} (tick {index})",
                signal_id=f"sig-{index:04d}",
            )
        )

    workdir = tmp_path_factory.mktemp("console-reachability")
    port = _free_port()
    previous_token = os.environ.get("THROUGHLINE_CALLER_TOKEN")
    os.environ["THROUGHLINE_CALLER_TOKEN"] = fake.caller_token
    original_request = clients_module.httpx.request
    clients_module.httpx.request = fake.request

    app = create_app(
        Settings(
            env="test",
            host="127.0.0.1",
            port=port,
            data_dir=str(workdir / "data"),
            cache_dir=str(workdir / "cache"),
            offline=True,
            agent_model_url="",
            fleet_model_url="",
            nvidia_api_key="",
            session_secret="reachability-secret",
            sibling_timeout=2.0,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - boot failure
        pytest.fail("the console under test never came up")
    global _LIVE_APP
    _LIVE_APP = app
    try:
        yield f"http://127.0.0.1:{port}", set(held_ids)
    finally:
        _LIVE_APP = None
        server.should_exit = True
        thread.join(timeout=10)
        clients_module.httpx.request = original_request
        if previous_token is None:
            os.environ.pop("THROUGHLINE_CALLER_TOKEN", None)
        else:
            os.environ["THROUGHLINE_CALLER_TOKEN"] = previous_token


# Only the approval queue's own links count. A stray /approval-detail link
# elsewhere on the page must not be able to inflate the reachable set.
_COLLECT_QUEUE_IDS = """
() => [...document.querySelectorAll(
  '#approval-queue-table a[href^="/approval-detail/"]')]
  .map(a => a.getAttribute('href').replace('/approval-detail/', ''))
  .filter(Boolean)
"""


@pytest.fixture
def page(chromium, loaded_console):
    base, _ = loaded_console
    context = chromium.new_context(viewport={"width": 1600, "height": 1200})
    # Mint a session directly against this run's signet, the same way
    # ``tests/conftest.py::sign_in`` does for a TestClient. There is no
    # product route that mints a fixed identity any more, so a browser test
    # cannot navigate to one.
    from helm.signet import SESSION_COOKIE

    token = _LIVE_APP.state.signet.issue(
        "dana@nvidia-demo.example", "test-harness", "dana"
    )
    context.add_cookies(
        [{"name": SESSION_COOKIE, "value": token, "url": base}]
    )
    page = context.new_page()
    page.goto(base, wait_until="networkidle")
    try:
        yield page
    finally:
        context.close()


def _queue_ids(page) -> set[str]:
    return set(page.evaluate(_COLLECT_QUEUE_IDS))


# ------------------------------------------------- defect 1: reachability


def test_every_held_effect_is_reachable_by_walking_the_queue(page, loaded_console):
    """Walk the pager from the front; the ids you can reach ARE the queue.

    The assertion is set equality against the gate's own pending list, so
    it cannot be satisfied by a bigger page size, and it does not need
    revisiting when the queue changes length.
    """
    base, held = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")

    reachable = _queue_ids(page)
    hops = 0
    while hops < MAX_PAGER_HOPS:
        nxt = page.query_selector('#queue-pager a[rel="next"]:not(.is-disabled)')
        if nxt is None:
            break
        nxt.click()
        page.wait_for_load_state("networkidle")
        hops += 1
        before = len(reachable)
        reachable |= _queue_ids(page)
        assert len(reachable) > before, (
            f"page {hops + 1} of the queue added no new held effect — the pager "
            "is not advancing, so the rest of the queue is still unreachable"
        )

    missing = held - reachable
    assert not missing, (
        f"{len(missing)} of {len(held)} held effects are unreachable from "
        f"/console after walking the whole pager ({hops} hops). Each of these "
        "is an irreversible effect waiting on a human who has no link to it: "
        f"{sorted(missing)[:5]}…"
    )
    assert reachable == held, (
        f"the queue offers {len(reachable - held)} links to effects that are "
        "not held — the page is claiming more than the gate does"
    )


def test_the_whole_queue_is_one_interaction_from_the_default_view(page, loaded_console):
    """Reachable is not enough if it takes fourteen clicks to get there.

    Keeping the 25-row default is fine; leaving it as the only bounded
    option is not. One control puts every held effect on one page, so the
    interaction count to reach ANY of them is bounded and small no matter
    how far the queue grows.
    """
    base, held = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")
    assert len(_queue_ids(page)) == QUEUE_PAGE_SIZE, (
        "the demo's default first page is no longer the documented page size"
    )

    page.click('#queue-pager .queue-sizes a[aria-label="Show all rows per page"]')
    page.wait_for_load_state("networkidle")

    reachable = _queue_ids(page)
    assert reachable == held, (
        f"one click on 'all' reached {len(reachable)} of {len(held)} held "
        f"effects; {len(held - reachable)} are still unreachable"
    )


def test_the_console_states_how_many_are_held_versus_shown(page, loaded_console):
    """Showing 25 of 429 without saying so is itself an unlabelled claim."""
    base, held = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")

    shown = len(_queue_ids(page))
    line = page.inner_text("#queue-count")
    assert str(len(held)) in line, (
        f"the console shows {shown} rows of {len(held)} held effects and never "
        f"states the total. It says only: {line!r}"
    )
    assert str(shown) in line, (
        f"the console does not say how many rows it is showing: {line!r}"
    )


def test_search_reaches_an_effect_the_default_page_never_shows(page, loaded_console):
    """A named held effect is one search away, not one page-walk away."""
    base, held = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")
    first_page = _queue_ids(page)

    buried = sorted(held - first_page)
    assert buried, "the fixture is too small to bury anything — it proves nothing"
    target = buried[len(buried) // 2]

    page.fill("#queue-search", target)
    page.click('.queue-filter button[type="submit"]')
    page.wait_for_load_state("networkidle")

    found = _queue_ids(page)
    assert found == {target}, (
        f"searching for {target} — which the default page does not show — "
        f"returned {sorted(found)[:5]} instead of exactly that effect"
    )
    assert str(len(held)) in page.inner_text("#queue-count"), (
        "a filtered queue must still say how many effects are held in total, "
        "or the filter reads as a release"
    )


def test_a_filter_that_matches_nothing_does_not_read_as_an_empty_gate(
    page, loaded_console
):
    """Hiding rows is not releasing effects, and the page must say which."""
    base, held = loaded_console
    page.goto(f"{base}/console?q=no-such-effect-anywhere", wait_until="networkidle")

    assert not _queue_ids(page), "a filter matching nothing still listed rows"
    line = page.inner_text("#queue-count")
    assert str(len(held)) in line, (
        f"an empty filter result must still state the {len(held)} held effects; "
        f"it says only {line!r}"
    )


# ------------------------------------ defect 2: the assistant announces


def test_the_transcript_is_a_live_region(page, loaded_console):
    """The container app.js writes into must be the one that announces."""
    base, _ = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")
    described = page.evaluate(
        """() => {
          const t = document.querySelector('.copilot-rail .transcript');
          if (!t) { return null; }
          return {role: t.getAttribute('role'), live: t.getAttribute('aria-live')};
        }"""
    )
    assert described, "the NemoClerk transcript is not on the page at all"
    assert described["live"] == "polite" or described["role"] == "status", (
        "the transcript announces nothing: app.js replaces its contents on "
        "every turn and it carries neither aria-live nor role=status, so a "
        f"screen-reader user hears silence after Send. Got {described}"
    )
    # Polite, deliberately: the reply follows the user's own Send, so it is
    # expected rather than an interruption. Assertive here would cut across
    # whatever the user is still reading.
    assert described["live"] != "assertive", (
        "the transcript interrupts: an expected reply to the user's own Send "
        "is polite, not assertive"
    )


def test_a_new_turn_lands_inside_the_live_region(page, loaded_console):
    """Pressing Send must add the answer to the region that announces.

    A live region that the new content is appended NEXT to rather than
    INTO announces exactly as little as no live region at all, which is
    why this drives the real composer rather than reading the template.
    """
    base, _ = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")

    before = page.evaluate(
        "() => document.querySelectorAll('.copilot-rail .transcript .turn').length"
    )
    page.fill(".copilot-rail .composer input", "what is held at the gate?")
    page.click(".copilot-rail .composer .send")
    page.wait_for_function(
        "(n) => document.querySelectorAll("
        "'.copilot-rail .transcript .turn:not(.pending)').length > n",
        arg=before + 1,
        timeout=15000,
    )

    announced = page.evaluate(
        """() => {
          const region = document.querySelector(
            '.copilot-rail [aria-live], .copilot-rail [role=status]');
          if (!region) { return null; }
          const turns = [...region.querySelectorAll('.turn:not(.pending)')];
          const last = turns[turns.length - 1];
          return {
            isTranscript: region.classList.contains('transcript'),
            text: last ? (last.innerText || '').trim() : '',
          };
        }"""
    )
    assert announced, "no live region in the rail for the answer to land in"
    assert announced["isTranscript"], (
        "the live region is not the transcript, so the turn app.js writes is "
        "still written outside anything that announces"
    )
    assert announced["text"], "the announced turn is empty"


# ------------------------------------- the guard: mutation implies speech

# Installed BEFORE the interaction, so it sees the same mutations a screen
# reader's accessibility tree does. characterData and childList only:
# a class flip or a style change is not something to announce, and treating
# it as one is how a live-region audit produces hundreds of findings that
# are not defects.
_WATCH = """
() => {
  window.__mutations = [];
  const live = '[aria-live], [role=status], [role=alert], [role=log]';
  const describe = (el) => el.tagName.toLowerCase() +
    (el.id ? '#' + el.id : '') +
    (el.classList.length ? '.' + [...el.classList].join('.') : '');
  const record = (node) => {
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el || !document.documentElement.contains(el)) { return; }
    // Nothing hidden from the accessibility tree can be announced, and
    // nothing hidden from it needs to be.
    if (el.closest('[aria-hidden=true]')) { return; }
    const text = (el.innerText || el.textContent || '').trim();
    if (!text) { return; }
    // A control relabelling ITSELF while it holds focus — "Approve as
    // dana@…" becoming "Approving as dana@…" — is already announced, as a
    // name change on the focused element. Giving it a live region on top
    // would make a screen reader say it twice. This is the one documented
    // exemption, and it is written as a rule about focus rather than as a
    // carve-out for a particular button.
    const focused = document.activeElement;
    const selfRelabel = focused && focused !== document.body && focused.contains(el);
    const region = el.closest(live);
    window.__mutations.push({
      target: describe(el),
      text: text.slice(0, 60),
      announced: !!region || selfRelabel,
      region: region ? describe(region) : (selfRelabel ? ':focus' : ''),
      politeness: region
        ? (region.getAttribute('aria-live') ||
           (region.getAttribute('role') === 'alert' ? 'assertive' : 'polite'))
        : '',
    });
  };
  new MutationObserver((records) => {
    for (const r of records) {
      if (r.type === 'characterData') { record(r.target); }
      for (const added of r.addedNodes) { record(added); }
    }
  }).observe(document.body, {
    subtree: true, childList: true, characterData: true,
  });
}
"""

_MUTATIONS = "() => window.__mutations || []"


def _unannounced(page) -> list[str]:
    return [
        f"{m['target']} changed to {m['text']!r} with nothing to announce it"
        for m in page.evaluate(_MUTATIONS)
        if not m["announced"]
    ]


def test_a_new_turn_is_the_only_thing_that_changes_and_it_announces(
    page, loaded_console
):
    """The guard, on the surface where the defect was found.

    A sweep of this console reported hundreds of "no aria-live anywhere"
    findings, one per rendered page per theme per dependency state. The
    number of DISTINCT regions that actually rewrite themselves after load
    is very much smaller — it is bounded by the handful of places
    ``helm/static/app.js`` writes into — and only those can possibly need
    announcing. A static region with no live region is not a defect; it is
    a page.

    So this asserts the thing that is actually true of a usable console:
    everything that changes after load, changes inside something that
    speaks. That is the invariant a 369th instance would have to break.
    """
    base, _ = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")
    page.evaluate(_WATCH)

    page.fill(".copilot-rail .composer input", "what is held at the gate?")
    page.click(".copilot-rail .composer .send")
    page.wait_for_function(
        "() => (window.__mutations || []).some(m => m.text.includes('NemoClerk:'))",
        timeout=15000,
    )
    page.wait_for_timeout(500)

    recorded = page.evaluate(_MUTATIONS)
    assert recorded, "nothing changed on the page — the guard proved nothing"
    silent = _unannounced(page)
    assert not silent, (
        f"{len(silent)} of {len(recorded)} post-load changes on /console are "
        "silent to a screen reader:\n  " + "\n  ".join(silent)
    )


def test_a_refused_decision_is_announced_and_interrupts(page, loaded_console):
    """A decision that did NOT happen must reach the operator, out loud.

    Everywhere else on this console polite is right, because the change
    follows the user's own action and is expected. This is the exception
    and the reason the politeness level is a decision rather than a
    default: "REFUSED", or "the decision was NOT recorded — nothing was
    executed", is the one sentence a user must not be allowed to miss
    while their attention has already moved on.
    """
    base, held = loaded_console
    target = sorted(held)[0]
    page.goto(f"{base}/approval-detail/{target}", wait_until="networkidle")
    page.evaluate(_WATCH)

    decide = page.query_selector("[data-decide]")
    if decide is None:
        pytest.skip("this identity may not decide, so there is no control to press")

    # A decision with no signet subject on the substrate is refused, which
    # is the branch worth hearing. Whatever the substrate answers, the
    # answer has to land in a region that speaks.
    decide.click()
    page.wait_for_function(
        "() => (document.getElementById('decision-result').innerText || '').trim()",
        timeout=15000,
    )
    page.wait_for_timeout(300)

    silent = _unannounced(page)
    assert not silent, (
        "the outcome of releasing an irreversible effect is silent:\n  "
        + "\n  ".join(silent)
    )

    outcome = page.evaluate(
        """() => {
          const box = document.getElementById('decision-result');
          return {live: box.getAttribute('aria-live'),
                  role: box.getAttribute('role'),
                  refused: box.classList.contains('refuse'),
                  text: (box.innerText || '').trim().slice(0, 80)};
        }"""
    )
    assert outcome["live"] in ("polite", "assertive"), (
        f"the decision result announces nothing: {outcome}"
    )
    if outcome["refused"]:
        assert outcome["live"] == "assertive", (
            "a refusal was announced politely and can be missed entirely: "
            f"{outcome}"
        )


def test_every_region_app_js_writes_into_is_a_live_region(page, loaded_console):
    """The pattern, stated once, so the next one to be added is caught.

    ``helm/static/app.js`` writes text into exactly these places. Each is
    named here with the politeness it was given and why, so adding a sixth
    writer without a live region is a failing test rather than a finding
    in someone's next sweep.
    """
    base, _ = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")

    expected = {
        # the assistant's answers — expected, follows the user's own Send
        ".copilot-rail .transcript": "polite",
        # the held count, rewritten when a turn changes what is held
        "#approval-queue .hold-badge": "polite",
        # the ledger tail, correct before this change and left alone
        "#ledger-tail tbody": "polite",
    }
    wrong = []
    for selector, politeness in expected.items():
        found = page.evaluate(
            """(sel) => {
              const el = document.querySelector(sel);
              if (!el) { return null; }
              return el.getAttribute('aria-live') ||
                (el.getAttribute('role') === 'alert' ? 'assertive' :
                 el.getAttribute('role') === 'status' ? 'polite' : '');
            }""",
            selector,
        )
        if found is None:
            wrong.append(f"{selector} is not on the page at all")
        elif found != politeness:
            wrong.append(f"{selector} announces {found or 'nothing'}, expected {politeness}")
    assert not wrong, "live-region pattern broken:\n  " + "\n  ".join(wrong)


# ------------------------ related: the ledger row affordance from a keyboard


def test_the_ledger_row_walk_is_reachable_from_the_keyboard(page, loaded_console):
    """The whole-row walk was mouse-only; now Enter on the row does it too.

    The per-row anchors are asserted to be intact in the same test, because
    the cheap way to "fix" this is to make the row the only affordance.
    """
    base, _ = loaded_console
    page.goto(f"{base}/console", wait_until="networkidle")

    rows = page.query_selector_all("#ledger-tail tr[data-effect-id]")
    assert rows, "the ledger tail has no walkable rows — this proves nothing"

    anchors = page.evaluate(
        """() => [...document.querySelectorAll('#ledger-tail tr[data-effect-id]')]
             .filter(r => r.querySelector('a.verify-link[href^="/walk/"]')).length"""
    )
    assert anchors == len(rows), (
        f"only {anchors} of {len(rows)} ledger rows still carry their own walk "
        "anchor — the row affordance replaced them instead of joining them"
    )

    row = rows[0]
    effect = row.get_attribute("data-effect-id")
    assert row.get_attribute("tabindex") is not None, (
        "the ledger row advertises a pointer cursor but no key can focus it"
    )
    row.focus()
    focused = page.evaluate(
        "() => document.activeElement.getAttribute('data-effect-id')"
    )
    assert focused == effect, "focus did not land on the row itself"

    page.keyboard.press("Enter")
    page.wait_for_url(f"**/walk/{effect}", timeout=10000)
    assert page.url.endswith(f"/walk/{effect}"), (
        f"Enter on a focused ledger row went to {page.url} instead of the walk"
    )
