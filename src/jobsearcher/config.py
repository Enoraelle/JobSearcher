"""Loading and validation of JobSearcher's config.yaml.

This module has a single implementation, so it stays a flat file rather than
a package — see the "File vs. package" convention in CLAUDE.md. If it grows
past 250 lines, split it into config/schema.py and config/loader.py, keeping
the public names below re-exported unchanged from config/__init__.py.
"""

from __future__ import annotations

import difflib
import importlib.resources
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jobsearcher.models import WorkMode

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.yaml")

# The example configuration ships as data inside the package (see the
# ``[tool.hatch.build.targets.wheel.force-include]`` entry in pyproject.toml),
# so ``jobsearcher init`` works from a plain ``pip install`` just as well as
# from a checkout. This is the resource name within the ``jobsearcher``
# package, not a filesystem path.
EXAMPLE_CONFIG_RESOURCE = "config.example.yaml"


def example_config_text() -> str:
    """Return the contents of the example configuration bundled in the package.

    Returns:
        The full text of ``config.example.yaml`` as shipped inside the
        installed ``jobsearcher`` package.
    """
    resource = importlib.resources.files("jobsearcher") / EXAMPLE_CONFIG_RESOURCE
    return resource.read_text(encoding="utf-8")


class ConfigError(Exception):
    """Raised when the on-disk configuration is missing or invalid."""


class SearchConfig(BaseModel):
    """Keyword and language filters applied when collecting postings.

    ``extra="forbid"``, like every plugin's own schema: a filter list that
    is never read is invisible. ``title_keywords_exclud:`` would leave the
    exclusions off and quietly widen the run.
    """

    model_config = ConfigDict(extra="forbid")

    title_keywords_include: list[str] = Field(default_factory=list)
    title_keywords_exclude: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=lambda: ["en"])


class ProfileConfig(BaseModel):
    """The job seeker's profile, used as scoring input.

    ``extra="forbid"`` because this is where a typo costs the most: the
    scorers read ``skills`` and ``absent_skills`` and nothing else, so
    ``skils:`` produces a profile with no skills at all — every posting
    scored 0, with nothing anywhere saying why.
    """

    model_config = ConfigDict(extra="forbid")

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
    """Root JobSearcher configuration, loaded from config.yaml.

    ``extra="forbid"``, for the same reason every source, scorer and
    exporter schema forbids unknown keys: a key nothing reads is a key the
    user believes is doing something. ``sourcess:`` collects from no source
    at all, and the run that follows looks like a quiet day on the job
    market. The permissive schema in this file is
    :class:`PluginConfig`, and only because it is shared by every plugin and
    cannot know which keys belong to which one — each plugin re-validates
    its own block strictly.
    """

    model_config = ConfigDict(extra="forbid")

    search: SearchConfig = Field(default_factory=SearchConfig)
    profile: ProfileConfig
    sources: dict[str, PluginConfig] = Field(default_factory=dict)
    storage: BackendSectionConfig
    scoring: BackendSectionConfig
    exporters: dict[str, PluginConfig] = Field(default_factory=dict)


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate JobSearcher configuration.

    Resolution: the explicit ``path`` if given, otherwise ``config.yaml`` in
    the current directory. There is no implicit fallback to the bundled
    example — run ``jobsearcher init`` to materialize it as ``config.yaml``
    first.

    Args:
        path: Optional explicit path to a config file, bypassing resolution.

    Returns:
        The validated application configuration.

    Raises:
        ConfigError: If no config file can be found, the YAML is malformed,
            or the content fails schema validation.
    """
    resolved_path = path or DEFAULT_CONFIG_PATH
    if not resolved_path.exists():
        raise ConfigError(
            f"No configuration file found at '{resolved_path}'. Run `jobsearcher init` "
            f"to create '{DEFAULT_CONFIG_PATH}' from the bundled example, or pass "
            f"--config PATH."
        )
    raw = _read_yaml(resolved_path)

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(str(resolved_path), exc)) from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Could not read configuration file '{path}': {exc}") from exc
    return _parse_yaml(content, str(path))


def _parse_yaml(content: str, label: str) -> dict[str, Any]:
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Configuration file '{label}' is not valid YAML: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(
            f"Configuration file '{label}' must contain a YAML mapping at the top level."
        )
    return data


def _schema_at(location: tuple[Any, ...]) -> type[BaseModel] | None:
    """Return the schema that owns ``location``, walking down from AppConfig.

    Args:
        location: A prefix of a pydantic error location, e.g. ``("profile",)``.

    Returns:
        The model class at that path, or ``None`` if the path leaves the
        typed schemas (a dict of plugin blocks, a list, a scalar).
    """
    model: type[BaseModel] = AppConfig
    for part in location:
        field = model.model_fields.get(str(part))
        if field is None:
            return None
        annotation = field.annotation
        if not (isinstance(annotation, type) and issubclass(annotation, BaseModel)):
            return None
        model = annotation
    return model


def _closest_key(location: tuple[Any, ...]) -> str | None:
    """Return the valid key nearest the unknown one reported at ``location``.

    Args:
        location: The full error location, whose last element is the
            offending key.

    Returns:
        The closest field name of the schema that rejected the key, or
        ``None`` when nothing is close enough to be worth suggesting.
    """
    schema = _schema_at(location[:-1])
    if schema is None:
        return None
    matches = difflib.get_close_matches(str(location[-1]), list(schema.model_fields), n=1)
    return matches[0] if matches else None


def _format_validation_error(path: str, exc: ValidationError) -> str:
    """Render a pydantic failure as lines the user can act on.

    An unknown key gets its own phrasing: pydantic's "Extra inputs are not
    permitted" says that a key is wrong without saying which one is right,
    and handing back the correct spelling is the whole point of refusing a
    typo instead of ignoring it.
    """
    lines = [f"Invalid configuration in '{path}':"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        if error["type"] == "extra_forbidden" and error["loc"]:
            suggestion = _closest_key(error["loc"])
            hint = f" - did you mean '{suggestion}'?" if suggestion else ""
            lines.append(f"  - {location}: unknown key '{error['loc'][-1]}'{hint}")
        else:
            lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)
