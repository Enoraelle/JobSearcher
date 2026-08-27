"""Tests for the shared, scorer-agnostic eligibility helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jobsearcher.config import ProfileConfig
from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.scoring.base import evaluate_location_match, evaluate_work_mode_match


def _posting(**overrides: Any) -> JobPosting:
    fields: dict[str, Any] = {
        "url": "https://example.com/jobs/1",
        "title": "Backend Engineer",
        "company": "Acme",
        "source": "example",
        "description_raw": "Build things.",
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return JobPosting(**fields)


def _profile(**overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {"role": "Backend Developer"}
    fields.update(overrides)
    return ProfileConfig(**fields)


class TestLocationMatch:
    def test_none_when_profile_lists_no_locations(self) -> None:
        assert evaluate_location_match(_posting(location="Paris"), _profile()) is None

    def test_none_when_posting_has_no_location_information(self) -> None:
        profile = _profile(locations=["France"])
        assert evaluate_location_match(_posting(), profile) is None

    def test_true_on_case_and_accent_insensitive_substring(self) -> None:
        profile = _profile(locations=["ile-de-france"])
        posting = _posting(location="Paris, Île-de-France")
        assert evaluate_location_match(posting, profile) is True

    def test_true_when_configured_location_contains_posting_location(self) -> None:
        profile = _profile(locations=["Remote - EU"])
        posting = _posting(location=None, eligible_locations=["EU"])
        assert evaluate_location_match(posting, profile) is True

    def test_false_when_nothing_lines_up(self) -> None:
        profile = _profile(locations=["France", "Remote - EU"])
        posting = _posting(location="Bangalore, India")
        assert evaluate_location_match(posting, profile) is False

    def test_eligible_locations_are_considered_not_just_location(self) -> None:
        profile = _profile(locations=["Portugal"])
        posting = _posting(location="Remote", eligible_locations=["Portugal", "Spain"])
        assert evaluate_location_match(posting, profile) is True


class TestWorkModeMatch:
    def test_none_when_posting_work_mode_unknown(self) -> None:
        profile = _profile(work_mode=WorkMode.REMOTE)
        assert evaluate_work_mode_match(_posting(work_mode=WorkMode.UNKNOWN), profile) is None

    def test_none_when_profile_has_no_preference(self) -> None:
        posting = _posting(work_mode=WorkMode.REMOTE)
        assert evaluate_work_mode_match(posting, _profile(work_mode=WorkMode.UNKNOWN)) is None

    @pytest.mark.parametrize(
        ("posting_mode", "profile_mode", "expected"),
        [
            (WorkMode.REMOTE, WorkMode.REMOTE, True),
            (WorkMode.HYBRID, WorkMode.HYBRID, True),
            (WorkMode.ONSITE, WorkMode.REMOTE, False),
            (WorkMode.HYBRID, WorkMode.REMOTE, False),
        ],
    )
    def test_equality_when_both_sides_known(
        self, posting_mode: WorkMode, profile_mode: WorkMode, expected: bool
    ) -> None:
        posting = _posting(work_mode=posting_mode)
        profile = _profile(work_mode=profile_mode)
        assert evaluate_work_mode_match(posting, profile) is expected
