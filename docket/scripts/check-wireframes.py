#!/usr/bin/env python3
"""Parse + same-repo link check over ux/web/wireframes/*.html (docket, scoped).

Cross-federation nav links (breaker.html, siren.html, blindspot.html,
composed.html) are expected to be absent in this component repo — recorded
as known, not failed. See ux/validation.md.
"""
import html.parser
import os
import sys

WF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   '.viper-context/specs/v0.1.0/ux/web/wireframes')
KNOWN_CROSS_FEDERATION = {'breaker.html', 'siren.html', 'blindspot.html', 'composed.html'}


class Checker(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors = []
        self.hrefs = []

    def error(self, message):
        self.errors.append(message)

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k == 'href' and v:
                self.hrefs.append(v)


def main():
    ok = True
    for fname in sorted(os.listdir(WF)):
        if not fname.endswith('.html'):
            continue
        text = open(os.path.join(WF, fname)).read()
        c = Checker()
        c.feed(text)
        c.close()
        broken = []
        for href in c.hrefs:
            if href.startswith('http') or href.startswith('#'):
                continue
            if not os.path.exists(os.path.join(WF, href)) and href not in KNOWN_CROSS_FEDERATION:
                broken.append(href)
        if c.errors:
            ok = False
            print(f'{fname}: PARSE ERRORS {c.errors}')
        elif broken:
            ok = False
            print(f'{fname}: BROKEN LINKS {broken}')
        else:
            print(f'{fname}: 0 parse errors, links OK ({len(c.hrefs)} href(s), '
                  f'known cross-federation links excluded)')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
