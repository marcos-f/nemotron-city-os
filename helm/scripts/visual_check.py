#!/usr/bin/env python3
"""visual+ — screenshot every live console view and assert its components.

Two jobs, in one pass:

1. **Structural check.** Every component in ELEMENT-COVERAGE.md must be
   present in the LIVE DOM, found by its data-coordinate or its id. A missing
   component fails the run — this is what stops a page quietly losing a pane.
2. **Screenshots.** Every view, in both themes, archived to
   artifacts/screenshots/ and committed. Pixel diffing is deferred; the
   images are evidence a human reads.

It also exercises the three SG9 choreography transitions and the rail
collapse round-trip, because a transition that is never driven is a
transition nobody knows is broken.

Usage:
    python scripts/visual_check.py [--base http://127.0.0.1:8610]

KNOWN LIMITATION (tracked in the mock-login removal issue): this script signs
in by navigating to ``/auth/mock``, which no longer exists — there is no
mock/dev one-click sign-in as a product capability. Unlike the pytest
Playwright fixtures (tests/test_console_reachability.py,
tests/test_palette_guard.py), this script drives an already-running,
out-of-process server over ``--base`` and has no in-process ``signet`` to
mint a cookie against. It needs either a real OIDC/GitHub credential wired
into CI, or a dedicated ops-only mechanism for handing this script a session
— neither of which is decided here. Until then this script cannot sign in.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "artifacts" / "screenshots"

# Every anchor from ELEMENT-COVERAGE.md, mapped to the page that must carry
# it and the selector that proves it is really rendered.
COMPONENTS: list[tuple[str, str, str]] = [
    # (component, page path, selector)
    ("IdentityBadge / ProfileMenu", "/console", ".user-menu .avatar-btn"),
    ("TabNav", "/console", "nav.tabs a[aria-current='page']"),
    ("FeedsRail", "/console", "aside.feeds"),
    ("EventList", "/console", "ul.event-list"),
    ("StageTimeline", "/console", ".stage-timeline .dot"),
    ("EventSummary", "/console", "#event-summary"),
    ("SuggestedActions", "/console", ".suggested-actions"),
    ("LedgerTail", "/console", "#ledger-tail"),
    ("ApprovalQueue", "/console", "#approval-queue"),
    ("SignalClassStatus", "/console", "#signal-class-status"),
    ("NemoClerkRail", "/console", "aside.copilot-rail"),
    ("SituationBanner", "/console", "#situation-banner"),
    ("Transcript", "/console", ".transcript"),
    ("PromptChips", "/console", ".prompt-chips button"),
    ("Composer", "/console", ".composer input"),
    ("AboutCard", "/console", "details.about"),
    ("ScenarioModal", "/console", "dialog.scenario-modal"),
    ("ThemeToggle", "/console", ".theme-toggle button[data-t='dark']"),
    ("JudgmentCard (quote)", "/docket", "#event-summary blockquote"),
    ("AbstentionCard", "/docket", "#abstention"),
    ("ChartToggle", "/docket", ".chart-toggle"),
    ("DocketIllustration", "/docket", "#docket-illustration svg"),
    ("ThroughputChart", "/docket", "#docket-throughput svg"),
    ("RuleEvidencePane", "/breaker", "#rule-evidence"),
    ("ProposalCard", "/breaker", "#event-summary"),
    ("Sparkline", "/breaker", "#soc-sparkline svg"),
    ("BatteryIllustration", "/breaker", "#battery-illustration svg"),
    ("MapPane", "/siren", "#incident-map svg"),
    ("MapLegendOutsidePlot", "/siren", "#incident-map .map-legend"),
    ("ReloadTimeline", "/siren", "#hot-reload"),
    ("RefusalPanel", "/siren", "#refusal-panel"),
    ("MiniTimelineRefused", "/siren", ".stage-timeline.mini .dot.refused"),
    ("FrameStrip", "/blindspot", "#frame-strip"),
    ("AlertCard", "/blindspot", "#near-miss-alert"),
    ("HonestyLabel", "/blindspot", "#batch-label"),
    ("ProgressBar", "/blindspot", ".progress-bar .fill"),
    ("OfflinePane (blindspot)", "/blindspot", "#offline-blindspot"),
    ("ComposedGrid", "/composed", "#composed-grid"),
    ("LedgerTailFiltered", "/composed", "#ledger-tail-filtered"),
    ("ApprovalRecordBlock", "/approval-detail", "#approval-block"),
    ("HashHopStrip", "/approval-detail", ".hash-strip"),
    ("AgentRefusalRecord", "/approval-detail", "#agent-refusal"),
    ("AdminRoleTable", "/admin", "#rbac table"),
    ("AdminConfigList", "/admin", "#classes table"),
    ("GatePolicyView", "/admin", "#gate button[disabled]"),
    ("AuthProviders", "/admin", "#auth table"),
    ("AdminBackBar", "/admin", ".admin-backbar"),
    ("LoginTitleScreen", "/login", ".hero-marks"),
    ("LoginAuthCard", "/login", ".oidc-btn"),
]

VIEWS = [
    ("login", "/login"),
    ("index", "/"),
    ("helm", "/console"),
    ("docket", "/docket"),
    ("breaker", "/breaker"),
    ("siren", "/siren"),
    ("blindspot", "/blindspot"),
    ("composed", "/composed"),
    ("approval-detail", "/approval-detail"),
    ("admin", "/admin"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8610")
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"base": args.base, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    failures: list[str] = []
    shots: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for theme in ("light", "dark"):
            context = browser.new_context(
                viewport={"width": 1600, "height": 1000}, color_scheme=theme
            )
            page = context.new_page()
            # Sign in through the documented endpoint, like a person would.
            page.goto(f"{args.base}/auth/mock", wait_until="networkidle")
            # A stored per-subject preference legitimately beats the OS scheme,
            # so state the theme rather than hoping the colour scheme wins.
            page.evaluate(f"() => window.setTheme('{theme}')")
            page.wait_for_timeout(200)
            for name, path in VIEWS:
                page.goto(f"{args.base}{path}", wait_until="networkidle")
                page.wait_for_timeout(350)
                suffix = "" if theme == "light" else "-dark"
                target = out / f"{name}{suffix}.png"
                page.screenshot(path=str(target), full_page=False)
                shots.append(target.name)
                print(f"  shot {target.name}")
            context.close()

        # ---------------------------------------------- structural check
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = context.new_page()
        page.goto(f"{args.base}/auth/mock", wait_until="networkidle")
        current = ""
        print("\nSTRUCTURAL CHECK — every ELEMENT-COVERAGE component in the live DOM")
        for component, path, selector in COMPONENTS:
            if path != current:
                page.goto(f"{args.base}{path}", wait_until="networkidle")
                page.wait_for_timeout(200)
                current = path
            found = page.locator(selector).count()
            status = "PASS" if found else "FAIL"
            print(f"  [{status}] {component:32} {path:18} {selector}")
            if not found:
                failures.append(f"{component} missing on {path} ({selector})")

        # ------------------------------------------------- choreography
        print("\nCHOREOGRAPHY")
        page.goto(f"{args.base}/console", wait_until="networkidle")

        # 1. dual collapse round-trip: 320 -> 40 -> 320, control stays visible
        rail = page.locator("aside.copilot-rail")
        before = rail.bounding_box()["width"]
        page.click(".rail-collapse")
        page.wait_for_timeout(250)
        collapsed = rail.bounding_box()["width"]
        control_visible = page.locator(".rail-collapse").is_visible()
        page.screenshot(path=str(out / "helm-rail-collapsed-dark.png"))
        shots.append("helm-rail-collapsed-dark.png")
        page.click(".rail-collapse")
        page.wait_for_timeout(250)
        after = rail.bounding_box()["width"]
        ok = collapsed < 60 < after and abs(after - before) < 8 and control_visible
        print(f"  [{'PASS' if ok else 'FAIL'}] rail collapse round-trip "
              f"{before:.0f} -> {collapsed:.0f} -> {after:.0f}, control visible={control_visible}")
        if not ok:
            failures.append("rail collapse round-trip")

        feeds = page.locator("aside.feeds")
        f_before = feeds.bounding_box()["width"]
        page.click(".feeds-collapse")
        page.wait_for_timeout(250)
        f_collapsed = feeds.bounding_box()["width"]
        f_control = page.locator(".feeds-collapse").is_visible()
        page.click(".feeds-collapse")
        page.wait_for_timeout(250)
        f_after = feeds.bounding_box()["width"]
        ok = f_collapsed < 60 < f_after and f_control
        print(f"  [{'PASS' if ok else 'FAIL'}] feeds collapse round-trip "
              f"{f_before:.0f} -> {f_collapsed:.0f} -> {f_after:.0f}")
        if not ok:
            failures.append("feeds collapse round-trip")

        # 2. the ProfileMenu opens, and admin is reachable from it
        page.click(".avatar-btn")
        page.wait_for_timeout(200)
        menu_open = page.locator(".user-menu .menu[data-open='true']").count() == 1
        admin_link = page.locator(".user-menu .menu a[href='/admin']").count() == 1
        page.screenshot(path=str(out / "helm-profilemenu-dark.png"))
        shots.append("helm-profilemenu-dark.png")
        print(f"  [{'PASS' if menu_open and admin_link else 'FAIL'}] "
              f"ProfileMenu opens ({menu_open}) with the admin route ({admin_link})")
        if not (menu_open and admin_link):
            failures.append("ProfileMenu")
        page.keyboard.press("Escape")

        # 3. the scenario modal — About is behind the (i), never inline
        page.click(".rail-head .icon-btn")
        page.wait_for_timeout(250)
        modal = page.locator("dialog.scenario-modal[open]").count() == 1
        page.screenshot(path=str(out / "helm-scenario-modal-dark.png"))
        shots.append("helm-scenario-modal-dark.png")
        print(f"  [{'PASS' if modal else 'FAIL'}] scenario modal opens from the (i)")
        if not modal:
            failures.append("scenario modal")
        page.keyboard.press("Escape")

        # 4. the refusal beat, driven live through the rail
        page.goto(f"{args.base}/breaker", wait_until="networkidle")
        composer = page.locator(".composer input")
        composer.fill("approve it")
        page.click(".composer .send")
        try:
            page.wait_for_selector(".transcript .tool-chip.refused", timeout=120_000)
            refused = True
        except Exception:  # noqa: BLE001
            refused = False
        page.wait_for_timeout(400)
        page.screenshot(path=str(out / "breaker-refusal-dark.png"))
        shots.append("breaker-refusal-dark.png")
        print(f"  [{'PASS' if refused else 'FAIL'}] NemoClerk refusal chip renders in the rail")
        if not refused:
            failures.append("refusal chip in the rail")

        # 4b. the map region and its key, where the labels used to collide
        page.goto(f"{args.base}/siren", wait_until="networkidle")
        page.wait_for_timeout(300)
        page.locator("#incident-map").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.locator("#incident-map").screenshot(path=str(out / "siren-incident-key.png"))
        shots.append("siren-incident-key.png")
        pin_geometry = page.evaluate(
            """() => Array.from(document.querySelectorAll('#incident-map .pin text'))
                 .map(t => [parseFloat(t.getAttribute('x')), parseFloat(t.getAttribute('y'))])"""
        )
        worst = min(
            (
                ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                for i, a in enumerate(pin_geometry)
                for b in pin_geometry[i + 1:]
            ),
            default=999.0,
        )
        rightmost = max((x for x, _ in pin_geometry), default=0.0)
        ok = worst >= 25 and rightmost <= 600
        print(f"  [{'PASS' if ok else 'FAIL'}] map pins: {len(pin_geometry)} placed, "
              f"closest pair {worst:.0f}px, rightmost x={rightmost:.0f} (frame 620)")
        if not ok:
            failures.append("map pin layout")

        # 5. reduced motion is respected
        rm = browser.new_context(
            viewport={"width": 1600, "height": 1000}, reduced_motion="reduce"
        )
        rm_page = rm.new_page()
        rm_page.goto(f"{args.base}/auth/mock", wait_until="networkidle")
        rm_page.goto(f"{args.base}/breaker", wait_until="networkidle")
        animation = rm_page.evaluate(
            "() => { const d = document.querySelector('.stage-timeline .dot.current')"
            " || document.querySelector('.stage-timeline .dot');"
            " return d ? getComputedStyle(d).animationName : 'none'; }"
        )
        ok = animation in {"none", ""}
        print(f"  [{'PASS' if ok else 'FAIL'}] reduced motion stops the pulse "
              f"(animation-name={animation!r})")
        if not ok:
            failures.append("reduced motion")
        rm_page.screenshot(path=str(out / "breaker-reduced-motion.png"))
        shots.append("breaker-reduced-motion.png")
        rm.close()

        # 6. both themes really compute different backgrounds
        for theme, expected_dark in (("light", False), ("dark", True)):
            tc = browser.new_context(viewport={"width": 1200, "height": 800})
            tp = tc.new_page()
            tp.goto(f"{args.base}/auth/mock", wait_until="networkidle")
            tp.goto(f"{args.base}/console", wait_until="networkidle")
            tp.evaluate(f"() => window.setTheme('{theme}')")
            tp.wait_for_timeout(200)
            bg = tp.evaluate("() => getComputedStyle(document.body).backgroundColor")
            report[f"bg_{theme}"] = bg
            print(f"  theme {theme}: body background {bg}")
            tc.close()
        if report.get("bg_light") == report.get("bg_dark"):
            failures.append("light and dark compute the same background")

        context.close()
        browser.close()

    # Leave the console as we found it: system default.
    try:
        import urllib.request

        urllib.request.urlopen(
            urllib.request.Request(
                f"{args.base}/prefs",
                data=b'{"theme": "system"}',
                headers={"Content-Type": "application/json"},
                method="PUT",
            ),
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - best effort tidy-up
        pass

    report["screenshots"] = sorted(set(shots))
    report["components_checked"] = len(COMPONENTS)
    report["failures"] = failures
    (out / "visual-report.json").write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 60)
    print(f"{len(COMPONENTS) - len([f for f in failures if 'missing' in f])}"
          f"/{len(COMPONENTS)} components present; {len(set(shots))} screenshots")
    if failures:
        for failure in failures:
            print("FAIL", failure)
        return 1
    print("visual+ PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
