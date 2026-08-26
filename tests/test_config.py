"""Tests for jobsearcher.config."""

from pathlib import Path

import pytest

from jobsearcher.config import ConfigError, load_config

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


def test_load_config_prefers_config_yaml_over_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(VALID_CONFIG, encoding="utf-8")
    (tmp_path / "config.example.yaml").write_text(
        VALID_CONFIG.replace("python developer", "should not be used"), encoding="utf-8"
    )

    config = load_config()

    assert config.sources["linkedin"].enabled is True


def test_load_config_falls_back_to_example(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.example.yaml").write_text(VALID_CONFIG, encoding="utf-8")

    with caplog.at_level("WARNING"):
        config = load_config()

    assert config.profile.role == "Python/Django Backend Developer"
    assert "config.example.yaml" in caplog.text


def test_load_config_raises_when_nothing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ConfigError, match="No configuration file found"):
        load_config()


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
