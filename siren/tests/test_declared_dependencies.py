"""The environment this suite runs in must be the one the package declares.

This file exists because of a specific, reproduced failure. `siren` imports
throughline at module scope (siren/service.py:15 and siren/cli.py), throughline is
not on PyPI — but an unrelated project of that name IS, and it satisfies
`throughline>=0.2` perfectly well. A venv could therefore install cleanly,
report no error, pass a test run that never imported the app, and then fail at
boot with `ModuleNotFoundError: No module named 'throughline.datasets_api'`.

A green pipeline that cannot boot the thing it tested is a false green. These
three checks are the cheapest way to make that impossible to repeat:

  * every dependency the pyproject DECLARES is actually installed, at a
    version that satisfies the declaration;
  * every module imported at module scope actually imports;
  * the installed `throughline` is the federation substrate rather than a
    same-named stranger from an index.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"

#: Imported at module scope by the service. Absence is a boot failure.
LOAD_BEARING = (
    "throughline.datasets_api",
    "throughline.datasets",
    "throughline.config",
)

FIX = (
    "\n\nProvision the environment the way the pyproject documents:\n"
    "    bash scripts/fetch-throughline.sh /tmp/throughline && pip install /tmp/throughline\n"
    "    pip install -e '.[test]'\n"
    "The federation's `scripts/up` does this for you and verifies it afterwards."
)


def declared_dependencies() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data.get("project", {}).get("dependencies", [])


def _parts(text: str) -> list:
    return [int(p) if p.isdigit() else p
            for p in re.split(r"[._-]", text) if p != ""]


def _satisfies(found: str, operator: str, wanted: str) -> bool:
    left, right = _parts(found), _parts(wanted)
    width = max(len(left), len(right))
    left += [0] * (width - len(left))
    right += [0] * (width - len(right))
    try:
        return {
            "==": left == right or found == wanted,
            "!=": left != right,
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
        }[operator]
    except (TypeError, KeyError):
        # An unorderable pre-release segment, or an operator this deliberately
        # small parser does not model. Do not invent a verdict.
        return True


def test_every_declared_dependency_is_installed() -> None:
    """A declaration nothing checks is a wish, not a dependency."""
    problems: list[str] = []
    for requirement in declared_dependencies():
        requirement = requirement.split(";")[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", requirement)
        assert match, f"unparsable requirement {requirement!r}"
        name, specifiers = match.group(1), match.group(2)
        try:
            found = version(name)
        except PackageNotFoundError:
            problems.append(f"{name}: declared but NOT INSTALLED")
            continue
        for clause in [c.strip() for c in specifiers.split(",") if c.strip()]:
            spec = re.match(r"^(==|!=|>=|<=|~=|>|<)\s*([^\s,]+)$", clause)
            if not spec:
                continue
            operator = ">=" if spec.group(1) == "~=" else spec.group(1)
            wanted = spec.group(2).rstrip("*").rstrip(".")
            if not _satisfies(found, operator, wanted):
                problems.append(f"{name} {found} does not satisfy {clause}")
    assert not problems, "declared dependencies are not present:\n  " + \
        "\n  ".join(problems) + FIX


@pytest.mark.parametrize("module", LOAD_BEARING)
def test_load_bearing_module_imports(module: str) -> None:
    """Importable, not merely installed. This is the check that was missing."""
    assert find_spec(module) is not None, \
        f"{module} is not importable in this environment" + FIX
    __import__(module)


def test_installed_throughline_is_the_federation_substrate() -> None:
    """`throughline` on PyPI is somebody else's project, and pip will take it.

    The substrate is identified by what it provides, not by its name: the
    dataset discovery surface this service mounts. A throughline without it is
    the wrong throughline, and saying so here beats a ModuleNotFoundError at
    boot with no hint of which artifact got installed.
    """
    from throughline.datasets_api import attach_datasets  # noqa: F401

    assert find_spec("throughline.datasets") is not None, \
        "the installed `throughline` does not provide the dataset registry " \
        "loader — it is not the federation substrate" + FIX
