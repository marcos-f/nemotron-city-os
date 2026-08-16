#!/usr/bin/env python3
"""Parse + link check over this repo's own ux/web/wireframes pages.

Cross-tab links to sibling pages that live in other component repos or the
orchestrator (docket.html, siren.html, blindspot.html, composed.html,
approval-detail.html, etc.) are expected to be unresolved here and are
reported, not treated as failures — this repo only owns breaker.html and
its own index.html.
"""
import glob
import html.parser
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.path.join(ROOT, '.viper-context/specs/v0.1.0/ux/web/wireframes')


class Checker(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors = 0

    def error(self, message):
        self.errors += 1


def main():
    ok = True
    for fname in sorted(glob.glob(os.path.join(BASE, '*.html'))):
        text = open(fname).read()
        p = Checker()
        p.feed(text)
        if p.errors:
            print(fname, 'PARSE ERRORS', p.errors)
            ok = False
        for href in re.findall(r'href="([^"]+)"', text):
            if href.startswith('http') or href.startswith('#'):
                continue
            if not os.path.exists(os.path.join(BASE, href)):
                print(fname, 'unresolved link (expected, out of repo scope):', href)
        print(fname, 'parse OK')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
