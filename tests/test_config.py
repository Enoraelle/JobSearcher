"""Tests for jobsearcher.config."""

from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from jobsearcher.config import (
    EXAMPLE_CONFIG_RESOURCE,
    AppConfig,
    ConfigError,
    _closest_key,
    example_config_text,
    load_config,
)

VALID_CONFIG = """
profile:
  role: "Python/Django Backend Developer"
  skills:
    - python
    - django

sources:
  linkedin:
    enabled: true
    query: "python developer"

storage:
  backend: sqlite
  path: "./jobsearcher.db"

scoring:
  backend: keyword_match

exporters:
  markdown_digest:
    enabled: true
    output_path: "./digest.md"
  notion:
    enabled: true
    database_id: "abc123"
"""


def test_load_config_from_explicit_path(tmp_path: Path) -> None:
    config_path = tmp_path / "custom.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    config = load_config(config_path)

    assert config.profile.role == "Python/Django Backend Developer"
    assert config.sources["linkedin"].enabled is True
    assert config.storage.backend == "sqlite"
    assert config.scoring.backend == "keyword_match"


def test_exporters_and_sources_share_the_same_shape(tmp_path: Path) -> None:
    config_path = tmp_path / "custom.yaml"
    config_path.write_text(VALID_CONFIG, encoding="utf-8")

    config = load_config(config_path)

    assert config.exporters["markdown_digest"].enabled is True
    assert config.exporters["notion"].enabled is True
    assert type(config.sources["linkedin"]) is type(config.exporters["notion"])


def test_load_config_reads_config_yaml_from_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(VALID_CONFIG, encoding="utf-8")

    config = load_config()

    assert config.sources["linkedin"].enabled is True


def test_load_config_raises_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="No configuration file found"):
        load_config()


def test_load_config_does_not_fall_back_to_a_cwd_example_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray config.example.yaml in the working directory is never loaded."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(VALID_CONFIG, encoding="utf-8")

    with pytest.raises(ConfigError, match="No configuration file found"):
        load_config()


def test_bundled_example_config_ships_in_the_package() -> None:
    """The example config must be importable as package data (guards packaging).

    Without this, a future change to the wheel build could drop the file and
    break `jobsearcher init` from a `pip install` without any test noticing.
    """
    resource = files("jobsearcher") / EXAMPLE_CONFIG_RESOURCE
    assert resource.is_file()

    text = example_config_text()
    assert text.strip()
    assert text == resource.read_text(encoding="utf-8")


def test_bundled_example_config_is_a_valid_configuration() -> None:
    AppConfig.model_validate(yaml.safe_load(example_config_text()))


def test_load_config_invalid_yaml_raises_readable_error(tmp_path: Path) -> None:
    config_path = tmp_path / "broken.yaml"
    config_path.write_text("profile: [unbalanced", encoding="utf-8")

    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(config_path)


def test_load_config_validation_error_is_human_readable(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(
        """
sources: {}
storage:
  backend: sqlite
scoring:
  backend: keyword_match
exporters: {}
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "Traceback" not in message
    assert "profile" in message


def test_load_config_top_level_not_a_mapping_raises(tmp_path: Path) -> None:
    config_path = tmp_path / "list.yaml"
    config_path.write_text("- not\n- a\n- mapping\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="must contain a YAML mapping"):
        load_config(config_path)


# --------------------------------------------------------------------------
# Unknown keys in the root schemas
# --------------------------------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "typo.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_unknown_root_key_is_rejected_and_named(tmp_path: Path) -> None:
    """A misspelled top-level section must not be silently ignored."""
    config_path = _write_config(tmp_path, VALID_CONFIG.replace("sources:", "sourcess:"))

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "sourcess" in message
    assert "sources" in message


def test_unknown_profile_key_is_rejected_and_named(tmp_path: Path) -> None:
    """`skils:` scores every posting 0 and says nothing; it must be an error."""
    config_path = _write_config(
        tmp_path,
        VALID_CONFIG.replace("  skills:", "  skils:"),
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "profile.skils" in message
    assert "skills" in message


def test_unknown_search_key_is_rejected_and_named(tmp_path: Path) -> None:
    config_path = _write_config(
        tmp_path,
        VALID_CONFIG + "\nsearch:\n  title_keywords_exclud:\n    - senior\n",
    )

    with pytest.raises(ConfigError) as exc_info:
        load_config(config_path)

    message = str(exc_info.value)
    assert "search.title_keywords_exclud" in message
    assert "title_keywords_exclude" in message


def test_an_unknown_key_with_no_close_match_is_still_rejected(tmp_path: Path) -> None:
    """The suggestion is a bonus; the rejection is the point."""
    config_path = _write_config(tmp_path, VALID_CONFIG + "\nzzzzzzzz: 1\n")

    with pytest.raises(ConfigError, match="zzzzzzzz"):
        load_config(config_path)


def test_a_key_outside_the_typed_schemas_gets_no_suggestion() -> None:
    """The suggestion stops where the strict schemas stop.

    Under `sources:` and `exporters:` the keys belong to a plugin's own
    model, which this module deliberately knows nothing about — guessing
    from the wrong vocabulary would be worse than saying nothing.
    """
    assert _closest_key(("sources", "greenhouse", "companys")) is None
    assert _closest_key(("profile", "skills", "0")) is None
    assert _closest_key(("nosuchsection", "whatever")) is None
