#!/usr/bin/env python3
"""Assert a JUnit report actually ran tests.

A suite that collected nothing exits 0 and looks green. CI must be able to tell
"all tests passed" apart from "no tests ran", and an integration criterion must
be discharged by a test that RAN, not one that skipped.

    assert_test_count.py report.xml --min 1 [--no-skips]
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("--min", type=int, default=1)
    ap.add_argument("--no-skips", action="store_true")
    ap.add_argument("--label", default="tests")
    args = ap.parse_args(argv)

    root = ET.parse(args.report).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)

    total = sum(int(s.get("tests", 0)) for s in suites)
    skipped = sum(int(s.get("skipped", 0)) for s in suites)
    failures = sum(int(s.get("failures", 0)) for s in suites)
    errors = sum(int(s.get("errors", 0)) for s in suites)

    print(
        f"{args.label}: {total} run, {skipped} skipped, "
        f"{failures} failed, {errors} errored"
    )

    ok = True
    if total < args.min:
        print(f"FAIL: expected at least {args.min} tests, got {total}", file=sys.stderr)
        ok = False
    if args.no_skips and skipped:
        print(
            f"FAIL: {skipped} skipped — this criterion must run, not skip",
            file=sys.stderr,
        )
        ok = False
    if failures or errors:
        print("FAIL: suite reported failures/errors", file=sys.stderr)
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
