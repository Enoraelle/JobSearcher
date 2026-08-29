"""The bundled example config, checked against the schemas that will read it.

``AppConfig`` accepts every ``sources:`` and ``exporters:`` block as it
stands, because :class:`~jobsearcher.config.PluginConfig` is deliberately
permissive: it is shared by every plugin and does not know which one it is.
The strict, typed model is the one each source and exporter re-validates
into once it knows it is being built — and nothing checks the shipped
example against *those*. A mistyped key would ship green here and fail for
every user who enabled that block, on the file they are handed as their
first contact with the project.

These tests close that gap by validating each example block through the same
path the real code takes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

import jobsearcher.sources  # noqa: F401  (registers the built-in sources)
from jobsearcher.config import AppConfig, PluginConfig, example_config_text
from jobsearcher.exporters import EXPORTER_FORMATS, get_exporter
from jobsearcher.exporters.notion import NotionExporterConfig
from jobsearcher.scoring import get_scorer
from jobsearcher.sources.base import SourceConfigError, get_source_class


def _example_config() -> AppConfig:
    return AppConfig.model_validate(yaml.safe_load(example_config_text()))


EXAMPLE = _example_config()


def _validate_source_block(name: str, block: PluginConfig) -> None:
    """Build the named source from ``block``, as the pipeline would.

    Construction is where a source re-validates its block into its own
    ``extra="forbid"`` model, so this is the check a typo has to survive.
    No request is made: the source is closed straight away.
    """
    source_cls = get_source_class(name)
    assert source_cls is not None, f"config.example.yaml enables an unregistered source {name!r}"
    with source_cls(name, block):
        pass


def _validate_exporter_block(fmt: str, block: PluginConfig, tmp_path: Path) -> None:
    """Validate an exporter block through the exporter's own option model."""
    assert fmt in EXPORTER_FORMATS, f"config.example.yaml documents an unknown format {fmt!r}"
    if fmt == "notion":
        # Running it would talk to Notion; its option model is the check.
        NotionExporterConfig.model_validate(block.model_dump())
        return
    options = PluginConfig(
        **{**block.model_dump(), "output_path": str(tmp_path / f"example.{fmt}")}
    )
    get_exporter(fmt).export([], options)


@pytest.mark.parametrize("name", sorted(EXAMPLE.sources))
def test_every_example_source_block_validates_against_its_own_schema(name: str) -> None:
    _validate_source_block(name, EXAMPLE.sources[name])


@pytest.mark.parametrize("fmt", sorted(EXAMPLE.exporters))
def test_every_example_exporter_block_validates_against_its_own_schema(
    fmt: str, tmp_path: Path
) -> None:
    _validate_exporter_block(fmt, EXAMPLE.exporters[fmt], tmp_path)


def test_the_example_scoring_block_validates_against_its_backend() -> None:
    get_scorer(EXAMPLE.scoring)


def test_the_source_check_would_catch_a_mistyped_key() -> None:
    """The guard above is only worth having if a typo actually trips it."""
    typo: dict[str, Any] = {**EXAMPLE.sources["greenhouse"].model_dump(), "companys": ["acme"]}

    with pytest.raises(SourceConfigError, match="companys"):
        _validate_source_block("greenhouse", PluginConfig(**typo))


def test_the_exporter_check_would_catch_a_mistyped_key(tmp_path: Path) -> None:
    typo: dict[str, Any] = {**EXAMPLE.exporters["markdown"].model_dump(), "outpout_path": "x.md"}

    with pytest.raises(Exception, match="outpout_path"):
        _validate_exporter_block("markdown", PluginConfig(**typo), tmp_path)
