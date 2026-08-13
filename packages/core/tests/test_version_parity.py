"""
Every package's ``__version__`` must equal the version in its own
``pyproject.toml``.

They drifted: all four packages declared ``__version__ = "0.1.0"`` while their
``pyproject.toml`` said ``0.2.0``. That is not cosmetic — ``__version__`` is
what ``opencomplai --version`` prints and what is stamped into scan artifacts
and reports as ``tool_version``, so every artifact produced named a version that
was never released. For a tool whose output is meant to be audit evidence,
"which version produced this" has to be true.

The check reads both files as text rather than importing the packages, so it
does not depend on ``PYTHONPATH`` and cannot be fooled by a stale copy of the
package installed in ``site-packages`` — which is exactly the situation this
repository is in.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_PACKAGES_DIR = Path(__file__).resolve().parents[2]

# (distribution directory, import package name)
_PACKAGES = [
    ("core", "opencomplai_core"),
    ("cli", "opencomplai_cli"),
    ("ai", "opencomplai_ai"),
    ("sdk-python", "opencomplai"),
]


def _pyproject_version(pyproject: Path) -> str:
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^version\s*=\s*"([^"]+)"', line.strip())
        if match:
            return match.group(1)
    raise AssertionError(f"no version declared in {pyproject}")


def _dunder_version(init_py: Path) -> str:
    for line in init_py.read_text(encoding="utf-8").splitlines():
        match = re.match(r'^__version__\s*=\s*"([^"]+)"', line.strip())
        if match:
            return match.group(1)
    raise AssertionError(f"no __version__ declared in {init_py}")


@pytest.mark.parametrize(("dist_dir", "module"), _PACKAGES)
def test_dunder_version_matches_pyproject(dist_dir: str, module: str) -> None:
    pyproject = _PACKAGES_DIR / dist_dir / "pyproject.toml"
    init_py = _PACKAGES_DIR / dist_dir / "src" / module / "__init__.py"

    assert pyproject.is_file(), pyproject
    assert init_py.is_file(), init_py

    declared = _pyproject_version(pyproject)
    exported = _dunder_version(init_py)

    assert exported == declared, (
        f"{module}.__version__ is {exported!r} but {dist_dir}/pyproject.toml "
        f"declares {declared!r}. Every artifact this package stamps would carry "
        f"the wrong version."
    )
