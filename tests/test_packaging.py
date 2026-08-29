"""Tests for what the distribution declares about itself.

These read ``pyproject.toml`` from the checkout rather than installed
metadata: the point is to catch a promise the packaging makes and the code
cannot keep, which only the source of truth can show.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

_PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _project_metadata() -> dict[str, Any]:
    if not _PYPROJECT.is_file():
        pytest.skip("not running from a source checkout")
    metadata: dict[str, Any] = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project: dict[str, Any] = metadata["project"]
    return project


def test_no_extra_installs_nothing() -> None:
    """An extra with no dependencies is a promise the install cannot keep.

    Documenting `pip install ".[x]"` for an empty `x` sends the user through
    a command that changes nothing, and makes the guard code that checks for
    `x`'s dependencies unreachable.
    """
    extras: dict[str, list[str]] = _project_metadata().get("optional-dependencies", {})

    assert [name for name, requirements in extras.items() if not requirements] == []
