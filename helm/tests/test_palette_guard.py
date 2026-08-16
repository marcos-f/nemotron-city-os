"""palette guard — PALETTE v2.1 hue roles, asserted on COMPUTED styles.

Why this file exists, and why it drives a real browser instead of reading
``app.css``: both of the defects it guards against were invisible to source
inspection and to a stylesheet diff. They were CASCADE defects, where a rule
stating the right colour was present in the file, correct, and last — and
still never applied, because an earlier rule with a MORE SPECIFIC SELECTOR
won. Twice a fix was reported landed on the strength of reading the
stylesheet, and twice the browser kept painting the retired hue.

So the only evidence this file accepts is ``getComputedStyle`` on a live DOM,
with the focus ring reached the way a keyboard user reaches it — by pressing
Tab, which is also the only way ``:focus-visible`` matches at all.

Two invariants, both from the frozen contract in
``.viper-context/specs/v0.1.0/ux/wireframes.md``:

1. **No focusable element's focus ring is the reserved green.** GREEN means
   verified/flowing; LIGHT BLUE means interactive, and a focus ring is the
   most interactive thing on the page. The ring must also clear the WCAG
   2.2 SC 1.4.11 non-text-contrast floor of 3:1 against the surfaces it is
   drawn on — #76b900 measured 2.41:1 on --panel and 2.07:1 on --soft in
   light theme, on the control that authorises irreversible effects.
2. **No link outside a completed/verified context computes green.** A link
   is navigation, not a verdict; painting one in the verified hue claims
   something the system has not established.

The reserved hues are read from the live document's own custom properties
rather than hardcoded, so re-tuning the palette cannot silently defeat the
guard — only reassigning green to interaction can, which is the point.
"""

from __future__ import annotations

import os
import socket
import threading
import time

import pytest

# WCAG 2.2 SC 1.4.11 non-text contrast floor for a focus indicator.
NON_TEXT_CONTRAST_FLOOR = 3.0

# Pages a keyboard user actually traverses. `/` is the front door and is
# nothing but navigation, which is where a green link is most damaging.
GUARDED_PAGES = ("/", "/console", "/docket", "/siren", "/admin", "/login")

# Tab hops before we declare the cycle unbounded. Real pages are well under.
MAX_TAB_HOPS = 120

# Set by ``live_console`` while it is live, so ``_open`` can mint a session
# cookie directly against this run's signet. There is no product route that
# mints a fixed identity any more, so a browser test signs in the same way
# any other test does: construct the session, never reach a route.
_LIVE_APP = None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _srgb(channel: float) -> float:
    channel /= 255.0
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(rgb: tuple[int, int, int]) -> float:
    red, green, blue = rgb
    return 0.2126 * _srgb(red) + 0.7152 * _srgb(green) + 0.0722 * _srgb(blue)


def contrast(first: tuple[int, int, int], second: tuple[int, int, int]) -> float:
    """WCAG relative-contrast ratio between two opaque colours."""
    high, low = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def parse_rgb(value: str) -> tuple[int, int, int]:
    """``rgb(1, 2, 3)`` / ``rgba(1, 2, 3, .5)`` -> ``(1, 2, 3)``."""
    body = value.strip().removeprefix("rgba(").removeprefix("rgb(").rstrip(")")
    parts = [float(p) for p in body.replace("/", " ").replace(",", " ").split()[:3]]
    return tuple(int(round(p)) for p in parts)  # type: ignore[return-value]


def as_hex(rgb: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb


# --------------------------------------------------------------- in-page JS

# The reserved (verified/flowing) hues and the surfaces a ring is drawn on,
# resolved by the browser so a var() chain cannot hide a colour from us.
_RESOLVE_TOKENS = """
() => {
  const root = getComputedStyle(document.documentElement);
  const probe = document.createElement('div');
  document.body.appendChild(probe);
  const resolve = (token) => {
    probe.style.color = root.getPropertyValue(token).trim();
    return getComputedStyle(probe).color;
  };
  const out = {};
  for (const token of ['--accent', '--accent-text', '--panel', '--soft', '--bg', '--info']) {
    out[token] = resolve(token);
  }
  probe.remove();
  return out;
}
"""

# Not every control indicates focus with an outline, and that is legitimate:
# the composer draws an inset ring on its wrapper via :focus-within instead.
# So this reports the outline (which is what the hue rule is ABOUT) and,
# separately, whether some focus-driven indicator exists at all — an
# ancestor shadow only counts when that ancestor actually matches
# :focus-within, so a decorative static shadow cannot pass for an indicator.
_DESCRIBE_ACTIVE = """
() => {
  const element = document.activeElement;
  if (!element || element === document.body || element === document.documentElement) {
    return null;
  }
  const style = getComputedStyle(element);
  const id = element.id ? '#' + element.id : '';
  const cls = element.classList.length ? '.' + [...element.classList].join('.') : '';
  const hasOutline = style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
  let otherIndicator = style.boxShadow !== 'none';
  if (!otherIndicator) {
    let parent = element.parentElement;
    for (let depth = 0; parent && depth < 4; parent = parent.parentElement, depth++) {
      if (parent.matches(':focus-within') && getComputedStyle(parent).boxShadow !== 'none') {
        otherIndicator = true;
        break;
      }
    }
  }
  return {
    label: element.tagName.toLowerCase() + id + cls,
    text: (element.innerText || element.value || '').trim().slice(0, 40),
    hasOutline: hasOutline,
    otherIndicator: otherIndicator,
    outlineColor: style.outlineColor,
    outlineWidth: style.outlineWidth,
    boxShadow: style.boxShadow,
    focusVisible: element.matches(':focus-visible'),
  };
}
"""

# A link is exempt only if it is genuinely inside something the system has
# marked completed or verified. Everything else is navigation.
_VERIFIED_CONTEXT = (
    ".verify-badge, .badge.verify-badge, .stage.done, .seg.done, "
    "[data-verified], [data-state='verified'], [data-state='completed']"
)

_COLLECT_LINKS = """
(verifiedContext) => [...document.querySelectorAll('a')]
  .filter(a => a.offsetParent !== null || a.getClientRects().length)
  .filter(a => !a.closest(verifiedContext))
  .map(a => ({
    label: 'a' + (a.id ? '#' + a.id : '') +
           (a.classList.length ? '.' + [...a.classList].join('.') : ''),
    text: (a.innerText || '').trim().slice(0, 40),
    href: a.getAttribute('href') || '',
    color: getComputedStyle(a).color,
  }))
"""


# ------------------------------------------------------------------ harness


@pytest.fixture(scope="module")
def chromium():
    """A real Chromium, or a decision about whether its absence is fatal.

    CI sets ``HELM_REQUIRE_BROWSER_GUARD=1`` so a missing browser fails the
    pipeline instead of skipping — a guard that silently skips is how a
    regression returns for a third time. Locally it skips, so ``pytest
    tests`` still runs for someone who has not run ``playwright install``.
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
def live_console(tmp_path_factory):
    """The real app over HTTP, because a focus ring needs a real browser.

    ``TestClient`` cannot express Tab, so this boots uvicorn on an ephemeral
    port in a thread of this same process.
    """
    import uvicorn

    from helm.app import create_app
    from helm.config import Settings

    workdir = tmp_path_factory.mktemp("palette-guard")
    port = _free_port()
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
            session_secret="palette-guard-secret",
            sibling_timeout=0.25,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover - boot failure
        pytest.fail("the guard's own console never came up")
    global _LIVE_APP
    _LIVE_APP = app
    try:
        yield base
    finally:
        _LIVE_APP = None
        server.should_exit = True
        thread.join(timeout=10)


def _open(browser, base: str, theme: str):
    """A signed-in page pinned to ``theme``.

    The theme is stated rather than merely requested: a stored per-subject
    preference legitimately beats the OS colour scheme, so asking only for
    ``color_scheme`` would leave which palette we measured up to chance.

    There is no product route that mints a fixed identity any more, so the
    session is minted directly against this run's signet — the same
    mechanism ``tests/conftest.py::sign_in`` uses for a TestClient — and
    handed to the browser as a cookie.
    """
    from helm.signet import SESSION_COOKIE

    context = browser.new_context(
        viewport={"width": 1600, "height": 1000}, color_scheme=theme
    )
    token = _LIVE_APP.state.signet.issue(
        "dana@nvidia-demo.example", "test-harness", "dana"
    )
    context.add_cookies([{"name": SESSION_COOKIE, "value": token, "url": base}])
    page = context.new_page()
    page.goto(base, wait_until="networkidle")
    page.evaluate(f"() => window.setTheme('{theme}')")
    page.wait_for_timeout(150)
    return context, page


def _visit(page, base: str, path: str, theme: str):
    page.goto(f"{base}{path}", wait_until="networkidle")
    page.evaluate(f"() => window.setTheme('{theme}')")
    page.wait_for_timeout(200)


def _palette(page) -> dict[str, tuple[int, int, int]]:
    """The live document's own palette tokens, resolved to concrete RGB."""
    return {name: parse_rgb(value) for name, value in page.evaluate(_RESOLVE_TOKENS).items()}


# -------------------------------------------------------------------- tests


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_focus_ring_is_the_reserved_green(chromium, live_console, theme):
    """Tab through every focusable element; no ring may be green, ever.

    This is the accessibility floor on the control that authorises
    irreversible effects, so it asserts two things at once: the ring is not
    the reserved hue, and it is actually legible against both surfaces it
    can be drawn on.
    """
    context, page = _open(chromium, live_console, theme)
    violations: list[str] = []
    unlit: list[str] = []
    checked = 0
    try:
        for path in GUARDED_PAGES:
            _visit(page, live_console, path, theme)
            tokens = _palette(page)
            reserved = {tokens["--accent"], tokens["--accent-text"]}
            surfaces = [("--panel", tokens["--panel"]), ("--soft", tokens["--soft"])]

            seen: set[str] = set()
            for _ in range(MAX_TAB_HOPS):
                page.keyboard.press("Tab")
                described = page.evaluate(_DESCRIBE_ACTIVE)
                if described is None:
                    continue
                stop_key = f"{described['label']}|{described['text']}"
                if stop_key in seen:
                    break  # the tab ring has wrapped
                seen.add(stop_key)
                if not described["focusVisible"]:
                    continue
                checked += 1
                where = f"{theme} {path} {described['label']} {described['text']!r}"

                if not described["hasOutline"]:
                    # An inset :focus-within ring is a real indicator; nothing
                    # at all is not. Only the latter is a defect.
                    if not described["otherIndicator"]:
                        unlit.append(f"{where}: no focus indicator of any kind")
                    continue

                ring = parse_rgb(described["outlineColor"])
                if ring in reserved:
                    violations.append(
                        f"{where}: focus ring is the RESERVED green {as_hex(ring)} "
                        "— green means verified/flowing, never interactive"
                    )
                    continue
                for surface_name, surface in surfaces:
                    ratio = contrast(ring, surface)
                    if ratio < NON_TEXT_CONTRAST_FLOOR:
                        violations.append(
                            f"{where}: focus ring {as_hex(ring)} is {ratio:.2f}:1 on "
                            f"{surface_name} {as_hex(surface)}, under the "
                            f"{NON_TEXT_CONTRAST_FLOOR}:1 WCAG 1.4.11 floor"
                        )
    finally:
        context.close()

    assert checked, "the guard never reached a focusable element — it proved nothing"
    assert not unlit, "focusable elements with no focus indicator:\n  " + "\n  ".join(unlit)
    assert not violations, (
        f"{len(violations)} focus-ring violations across {checked} focus stops:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_no_link_outside_a_verified_context_is_green(chromium, live_console, theme):
    """Links are interactive, so they are blue — on the front door especially."""
    context, page = _open(chromium, live_console, theme)
    violations: list[str] = []
    checked = 0
    try:
        for path in GUARDED_PAGES:
            _visit(page, live_console, path, theme)
            tokens = _palette(page)
            reserved = {tokens["--accent"], tokens["--accent-text"]}
            for link in page.evaluate(_COLLECT_LINKS, _VERIFIED_CONTEXT):
                checked += 1
                colour = parse_rgb(link["color"])
                if colour in reserved:
                    violations.append(
                        f"{theme} {path} {link['label']} -> {link['href']} "
                        f"{link['text']!r} computes the RESERVED green {as_hex(colour)}"
                    )
    finally:
        context.close()

    assert checked, "the guard found no links — it proved nothing"
    assert not violations, (
        f"{len(violations)} green links outside any verified context, of {checked} "
        "links inspected:\n  " + "\n  ".join(violations)
    )


_INDEX_CARD_LINKS = """
() => [...document.querySelectorAll('.page-list a')].map(a => ({
  text: (a.innerText || '').trim().slice(0, 40), color: getComputedStyle(a).color }))
"""

_RESOLVE_ONE = """
(token) => {
  const probe = document.createElement('div');
  document.body.appendChild(probe);
  probe.style.color = getComputedStyle(document.documentElement)
    .getPropertyValue(token).trim();
  const value = getComputedStyle(probe).color;
  probe.remove();
  return value;
}
"""


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_front_door_cards_are_the_interactive_hue(chromium, live_console, theme):
    """`/` is pure navigation, and it is where this regression kept landing.

    Stated positively and named separately from the sweep above, so a
    failure report says which page broke and what it should have been
    without anyone having to read a list. ``.page-list a`` specifically:
    the deliberately muted footer links are a different, intended thing.
    """
    context, page = _open(chromium, live_console, theme)
    try:
        _visit(page, live_console, "/", theme)
        expected = parse_rgb(page.evaluate(_RESOLVE_ONE, "--info-text"))
        cards = page.evaluate(_INDEX_CARD_LINKS)
        assert len(cards) >= 9, f"the index lost cards: only {len(cards)} of 9"
        wrong = [
            f"{card['text']!r} computes {as_hex(parse_rgb(card['color']))}"
            for card in cards
            if parse_rgb(card["color"]) != expected
        ]
        assert not wrong, (
            f"index cards not on the interactive hue {as_hex(expected)} "
            f"({theme}):\n  " + "\n  ".join(wrong)
        )
    finally:
        context.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_focus_ring_keeps_its_green_halo(chromium, live_console, theme):
    """The identity green was ADDED to, not swapped out.

    Moving the ring to the interactive hue was allowed to lose nothing, so
    the green luminance halo outside the ring is part of the contract now.
    Without this assertion the "fix" for the contrast defect could quietly
    become a deletion, and the next reviewer would have no way to tell.
    """
    context, page = _open(chromium, live_console, theme)
    try:
        _visit(page, live_console, "/console", theme)
        accent = parse_rgb(page.evaluate(_RESOLVE_ONE, "--accent"))
        page.keyboard.press("Tab")
        described = page.evaluate(_DESCRIBE_ACTIVE)
        assert described and described["focusVisible"], "Tab reached nothing focusable"
        shadow = described["boxShadow"]
        assert shadow != "none", (
            f"{described['label']} has a focus ring but no halo at all: the green "
            "identity affordance was removed rather than kept alongside the ring"
        )
        assert as_hex(parse_rgb(shadow)) == as_hex(accent), (
            f"the focus halo is {shadow!r}, not the identity green {as_hex(accent)}"
        )
    finally:
        context.close()
