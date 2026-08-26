"""Loading and validation of JobSearcher's config.yaml.

This module has a single implementation, so it stays a flat file rather than
a package — see the "File vs. package" convention in CLAUDE.md. If it grows
past 250 lines, split it into config/schema.py and config/loader.py, keeping
the public names below re-exported unchanged from config/__init__.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher.models import WorkMode

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")
EXAMPLE_CONFIG_PATH = Path("config.example.yaml")


class ConfigError(Exception):
    """Raised when the on-disk configuration is missing or invalid."""


class SearchConfig(BaseModel):
    """Keyword and language filters applied when collecting postings."""

    title_keywords_include: list[str] = Field(default_factory=list)
    title_keywords_exclude: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en"])


class ProfileConfig(BaseModel):
    """The job seeker's profile, used as scoring input."""

    role: str
    skills: list[str] = Field(default_factory=list)
    absent_skills: list[str] = Field(default_factory=list)
    experience_years: int | None = Field(default=None, ge=0)
    work_mode: WorkMode = WorkMode.UNKNOWN
    locations: list[str] = Field(default_factory=list)


class PluginConfig(BaseModel):
    """Configuration for one named, independently toggleable component.

    Used for both `sources` and `exporters`: both are collections of
    interchangeable implementations that can be enabled or disabled
    individually, so they share this exact shape rather than each inventing
    their own.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True


class BackendSectionConfig(BaseModel):
    """A single pluggable-backend section (storage, scoring)."""

    model_config = ConfigDict(extra="allow")

    backend: str


class AppConfig(BaseModel):
    """Root JobSearcher configuration, loaded from config.yaml."""

    search: SearchConfig = Field(default_factory=SearchConfig)
    profile: ProfileConfig
    sources: dict[str, PluginConfig] = Field(default_factory=dict)
    storage: BackendSectionConfig
    scoring: BackendSectionConfig
    exporters: dict[str, PluginConfig] = Field(default_factory=dict)


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate JobSearcher configuration.

    Resolution order: ``config.yaml`` if it exists, otherwise
    ``config.example.yaml`` (with a warning logged).

    Args:
        path: Optional explicit path to a config file, bypassing resolution.

    Returns:
        The validated application configuration.

    Raises:
        ConfigError: If no config file can be found, the YAML is malformed,
            or the content fails schema validation.
    """
    resolved_path = path or _resolve_config_path()
    raw = _read_yaml(resolved_path)

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(resolved_path, exc)) from exc


def _resolve_config_path() -> Path:
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    if EXAMPLE_CONFIG_PATH.exists():
        logger.warning(
            "%s not found; falling back to %s. Copy it to %s and fill in your own profile.",
            DEFAULT_CONFIG_PATH,
            EXAMPLE_CONFIG_PATH,
            DEFAULT_CONFIG_PATH,
        )
        return EXAMPLE_CONFIG_PATH
    raise ConfigError(
        f"No configuration file found. Expected '{DEFAULT_CONFIG_PATH}' or "
        f"'{EXAMPLE_CONFIG_PATH}' in the current directory."
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file '{path}': {exc}") from exc

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file '{path}' is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Configuration file '{path}' must contain a YAML mapping at the top level."
        )
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    lines = [f"Invalid configuration in '{path}':"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
