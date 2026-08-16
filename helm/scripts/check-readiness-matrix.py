#!/usr/bin/env python3
"""Gate readiness/matrix.json on schema validity and evidence-pointer resolvability.

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------

It checks two things, and only two:

1. **Schema validity** against ``readiness/readiness-matrix.schema.json`` (vendored
   verbatim from the readiness-matrix contract), plus the cross-document rules
   JSON Schema cannot express: unique row ids, parent resolution, parent-kind
   nesting, dimension-key resolution, and the presence of a platform root.
2. **Evidence-pointer resolvability** — a pointer that looks like a repository
   path must resolve to a file that exists; a prose pointer (a stated reason
   rather than an artifact) is admissible only under ``unknown`` and
   ``not-applicable``, where the reason IS the evidence.

It does **not** gate on cell content. It will not fail because a cell is
``partial``, ``unknown``, ``hidden`` or ``missing``.

That restraint is the whole design. This repository legitimately holds cells in
every one of those states: the approval queue lists 25 of 429 held effects, the
MCP host has no navigation, the NemoClerk transcript has no live region, and a
real OIDC consent login has never been performed. Those are true statements
about helm, and the matrix exists to carry them. A job that went red because the
matrix was honest would teach the next contributor that the cheapest way to a
green pipeline is to overstate a cell — or to delete this job at the first
inconvenient moment. So the gate is on the document's integrity, never on its
verdict.

Usage:

    python3 scripts/check-readiness-matrix.py                       # default paths
    python3 scripts/check-readiness-matrix.py --matrix path.json --root .
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MATRIX = os.path.join(REPO_ROOT, "readiness", "matrix.json")
DEFAULT_SCHEMA = os.path.join(REPO_ROOT, "readiness", "readiness-matrix.schema.json")

PROSE_OK_STATES = ("unknown", "not-applicable")
PATH_SUFFIXES = (".json", ".yaml", ".yml", ".md", ".py", ".go", ".sh", ".toml")

# RM-101: the containment tree. persona is absent by construction (RM-103).
PARENT_KINDS = {
    "feature-area": {"platform"},
    "use-case": {"feature-area"},
    "story": {"feature-area"},
    "scenario": {"use-case", "story"},
}


def schema_errors(doc, schema):
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    out = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "<root>"
        out.append(f"schema [{err.validator}] at {path}: {err.message}")
    return out


def semantic_errors(doc):
    """Cross-document rules JSON Schema cannot express."""
    errors = []
    rows = doc.get("rows", [])
    by_id = {}

    for row in rows:
        rid = row.get("id")
        if rid in by_id:
            errors.append(f"duplicate-id: row id '{rid}' is not unique")
        by_id[rid] = row

    for row in rows:
        kind, parent = row.get("kind"), row.get("parent")
        if kind == "platform":
            continue
        if parent not in by_id:
            errors.append(
                f"parent-resolution: {row.get('id')} names parent '{parent}', which does not exist")
            continue
        pkind = by_id[parent].get("kind")
        if pkind not in PARENT_KINDS.get(kind, set()):
            errors.append(
                f"parent-kind: {row.get('id')} is a {kind} and may not nest under {pkind}")

    groups = doc.get("dimension_groups", {})
    for row in rows:
        for key in (row.get("cells") or {}):
            group, _, dim = key.partition(".")
            if group not in groups:
                errors.append(
                    f"dimension-resolution: {row.get('id')} cell '{key}' names unregistered group '{group}'")
            elif dim not in groups[group].get("dimensions", {}):
                errors.append(
                    f"dimension-resolution: {row.get('id')} cell '{key}' names unregistered dimension '{dim}'")

    if not any(r.get("kind") == "platform" for r in rows):
        errors.append("platform-root: document has no platform root")

    return errors


def looks_like_path(pointer, root):
    head = re.split(r"[\s:]", pointer.strip(), maxsplit=1)[0]
    if not head:
        return False, head
    if head.endswith(PATH_SUFFIXES):
        return True, head
    if "/" in head and " " not in head:
        return True, head
    if os.path.exists(os.path.join(root, head)):
        return True, head
    return False, head


def evidence_errors(doc, root):
    errors = []
    checked = resolved = prose = 0

    for row in doc.get("rows", []):
        for key, cell in (row.get("cells") or {}).items():
            ev = cell.get("evidence") or {}
            pointer = ev.get("pointer", "")
            state = cell.get("state")
            where = f"{row.get('id')}::{key}"
            checked += 1

            is_path, head = looks_like_path(pointer, root)
            if is_path:
                if os.path.exists(os.path.join(root, head)):
                    resolved += 1
                else:
                    errors.append(
                        f"{where}: pointer '{head}' does not resolve to an existing artifact")
            elif state in PROSE_OK_STATES:
                prose += 1
            else:
                errors.append(
                    f"{where}: state '{state}' is backed by prose, not an artifact "
                    f"(only {'/'.join(PROSE_OK_STATES)} may cite a stated reason)")

    return errors, checked, resolved, prose


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--matrix", default=DEFAULT_MATRIX)
    ap.add_argument("--schema", default=DEFAULT_SCHEMA)
    ap.add_argument("--root", default=REPO_ROOT,
                    help="subject repository root that pointers resolve against")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    print(f"matrix: {args.matrix}")
    print(f"schema: {args.schema}")
    print(f"subject root: {root}")

    with open(args.matrix) as fh:
        doc = json.load(fh)
    with open(args.schema) as fh:
        schema = json.load(fh)

    rev = doc.get("subject_revision") or {}
    if rev:
        print(f"subject revision: {rev.get('sha')}")
        print(f"  default-branch tip: {rev.get('default_branch_sha')}   "
              f"(matched: {rev.get('matched')})")

    failures = schema_errors(doc, schema) + semantic_errors(doc)
    ev_failures, checked, resolved, prose = evidence_errors(doc, root)
    failures += ev_failures

    print(f"rows: {len(doc.get('rows', []))}")
    print(f"evidence pointers checked: {checked}")
    print(f"  resolved to existing artifacts: {resolved}")
    print(f"  admissible stated reasons:      {prose}")

    if checked == 0:
        print("FAIL: no evidence pointers checked — the gate proved nothing", file=sys.stderr)
        return 1

    if failures:
        print("FAIL:", file=sys.stderr)
        for f in failures:
            print("  -", f, file=sys.stderr)
        return 1

    # Reported, never gated. A distribution is information, not a verdict.
    dist = {}
    for row in doc.get("rows", []):
        for cell in (row.get("cells") or {}).values():
            dist[cell.get("state")] = dist.get(cell.get("state"), 0) + 1
    print("state distribution (REPORTED, NOT GATED): "
          + ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda kv: -kv[1])))
    print("PASS: schema valid, tree resolves, every evidence pointer resolves "
          "or is an admissible reason")
    return 0


if __name__ == "__main__":
    sys.exit(main())
