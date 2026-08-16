#!/usr/bin/env python3
"""Evaluate ultra phase gates (01/02/03) for this host project, strict mode.

Criteria mirror ultra-skills references/gate-criteria.yml (vendored globs —
the plugin cache is not available in CI). Metric thresholds are
host-parameterized in gate-config.yaml. Every criterion is failable; strict
mode treats WARN-severity failures as gate failures too (operator decision:
the goal's GATE clause counts WARN as not-met).

Writes .viper-context/plan/web/{version}/reports/gates/phase-{NN}.md
Exit 0 only if every criterion passes.
"""
import glob
import json
import re
import os
import sys
from datetime import datetime, timezone

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = yaml.safe_load(open(os.path.join(ROOT, 'scripts', 'gate-config.yaml')))
VERSION = CFG['version']
PLATFORM = CFG['platform']


def g(pattern):
    return sorted(glob.glob(os.path.join(ROOT, pattern.replace('${version}', VERSION)
                                         .replace('${platform}', PLATFORM))))


def coverage():
    """Element coverage from the wireframe coverage report: covered/required."""
    path = os.path.join(ROOT, f'.viper-context/specs/{VERSION}/ux/{PLATFORM}/wireframes/ELEMENT-COVERAGE.md')
    if not os.path.exists(path):
        return 0.0, 'ELEMENT-COVERAGE.md missing'
    text = open(path).read()
    m = re.search(r'Required elements:\s*(\d+).*?Covered:\s*(\d+)\.\s*Missing:\s*(\d+)', text, re.S)
    if not m:
        return 0.0, 'coverage summary line not found'
    total, covered = int(m.group(1)), int(m.group(2))
    return (covered / total if total else 0.0), f'{covered}/{total} elements'


CRITERIA = {
    '01': [
        ('feature-areas-captured', 'fail', lambda: (len(g(f'.viper-context/specs/{VERSION}/product/FA-*')) >= 1,
                                                    f'{len(g(f".viper-context/specs/{VERSION}/product/FA-*"))} FA files')),
        ('product-overview-exists', 'fail', lambda: (bool(g(f'.viper-context/specs/{VERSION}/product/PRODUCT.md')), 'PRODUCT.md')),
        ('product-roadmap-exists', 'fail', lambda: (bool(g('PRODUCT-ROADMAP.md')), 'root PRODUCT-ROADMAP.md')),
        ('ideas-captured', 'warn', lambda: (len(g(f'.viper-context/specs/{VERSION}/product/ideas/IDEA-*.md')) >= 1,
                                            f'{len(g(f".viper-context/specs/{VERSION}/product/ideas/IDEA-*.md"))} ideas')),
        ('feature-matrix-current', 'warn', lambda: (os.path.exists(os.path.join(ROOT, 'integration/matrix.json')),
                                                    'integration/matrix.json rolled up')),
        # manifest artifacts from the ultra-01-gate command contract
        ('plan-exists', 'fail', lambda: (bool(g(f'.viper-context/plan/{PLATFORM}/{VERSION}/implementation-plan.md')), 'implementation-plan.md')),
        ('proposals-exist', 'fail', lambda: (len(g(f'.viper-context/specs/{VERSION}/product/proposals/*.md')) >= 1, 'proposals/')),
    ],
    '02': [
        ('sitemap-exists', 'fail', lambda: (bool(g(f'.viper-context/specs/{VERSION}/ux/sitemaps/sitemap.mmd')), 'sitemap.mmd')),
        ('wireframes-index-exists', 'fail', lambda: (bool(g(f'.viper-context/specs/{VERSION}/ux/{PLATFORM}/wireframes/index.html')), 'index.html')),
        ('interaction-graph-exists', 'fail', lambda: (bool(g(f'.viper-context/specs/{VERSION}/ux/wireframes/interaction-graph.json')), 'interaction-graph.json')),
        ('wireframe-element-coverage', 'warn', lambda: (coverage()[0] >= CFG['element_coverage_threshold'],
                                                        f'coverage {coverage()[0]:.2f} vs threshold {CFG["element_coverage_threshold"]} ({coverage()[1]})')),
        ('persona-paths-walked', 'warn', lambda: (os.path.exists(os.path.join(ROOT, f'.viper-context/viper-skill-backlog/journal-personas-{VERSION}.md')),
                                                  'persona walk journal')),
    ],
    '03': [
        ('architecture-doc-exists', 'fail', lambda: (bool(g(f'.viper-context/specs/{VERSION}/architecture/ARCHITECTURE.md')), 'ARCHITECTURE.md')),
        ('final-diagrams-present', 'fail', lambda: (len(g(f'.viper-context/specs/{VERSION}/architecture/diagrams/final/*')) >= 1,
                                                    f'{len(g(f".viper-context/specs/{VERSION}/architecture/diagrams/final/*"))} final diagrams')),
        ('component-registry-exists', 'warn', lambda: (bool(g(f'.viper-context/specs/{VERSION}/architecture/components/registry.yml')), 'registry.yml')),
        ('tech-review-recorded', 'warn', lambda: (bool(g(f'.viper-context/specs/{VERSION}/architecture/reviews/tech-review.md')), 'tech-review.md')),
    ],
}


def run(phase):
    rows, ok = [], True
    for cid, sev, check in CRITERIA[phase]:
        try:
            passed, evidence = check()
        except Exception as exc:  # a crashing check is a failing check
            passed, evidence = False, f'check error: {exc}'
        ok = ok and passed
        rows.append((cid, sev, 'PASS' if passed else 'FAIL', evidence))
    verdict = 'PASS' if ok else 'FAIL'
    report = os.path.join(ROOT, f'.viper-context/plan/{PLATFORM}/{VERSION}/reports/gates/phase-{phase}.md')
    os.makedirs(os.path.dirname(report), exist_ok=True)
    with open(report, 'w') as h:
        h.write(f'# Phase {phase} gate — {verdict} (strict; WARN counts as failing)\n\n')
        h.write(f'Evaluated {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")} UTC · version {VERSION} · platform {PLATFORM}\n\n')
        h.write('| criterion | severity | result | evidence |\n|---|---|---|---|\n')
        for cid, sev, res, evi in rows:
            h.write(f'| {cid} | {sev} | {res} | {evi} |\n')
    print(f'phase-{phase}: {verdict}')
    for cid, sev, res, evi in rows:
        print(f'  {res:<4} [{sev}] {cid} — {evi}')
    return ok


if __name__ == '__main__':
    phases = sys.argv[1:] or ['01', '02', '03']
    sys.exit(0 if all(run(p) for p in phases) else 1)
