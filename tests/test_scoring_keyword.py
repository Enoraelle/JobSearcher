"""In-depth tests for the offline keyword scorer.

The scorer is a pure function of its inputs, so these tests pin exact
scores, exact list contents, and exact summary fragments — there is no
nondeterminism to work around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from jobsearcher.config import BackendSectionConfig, ProfileConfig
from jobsearcher.models import JobPosting, WorkMode
from jobsearcher.scoring import ScorerConfigError
from jobsearcher.scoring.keyword import (
    DEFAULT_ABSENT_SKILL_PENALTY,
    KeywordScorer,
    _normalize,
)


def _posting(
    *, title: str = "Backend Engineer", description: str = "", **overrides: Any
) -> JobPosting:
    fields: dict[str, Any] = {
        "url": "https://example.com/jobs/1",
        "title": title,
        "company": "Acme",
        "source": "example",
        "description_raw": description,
        "fetched_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return JobPosting(**fields)


def _profile(**overrides: Any) -> ProfileConfig:
    fields: dict[str, Any] = {"role": "Backend Developer"}
    fields.update(overrides)
    return ProfileConfig(**fields)


def _scorer(**config: Any) -> KeywordScorer:
    if not config:
        return KeywordScorer()
    return KeywordScorer(BackendSectionConfig(backend="keyword_match", **config))


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


class TestNormalize:
    def test_lowercases(self) -> None:
        assert _normalize("PyThOn") == "python"

    def test_strips_diacritics(self) -> None:
        assert _normalize("Développeur Sénior") == "developpeur senior"

    def test_collapses_whitespace(self) -> None:
        assert _normalize("a  \t b\n c") == "a b c"

    def test_punctuation_becomes_space(self) -> None:
        assert _normalize("REST/API, done!") == "rest api done"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("C++", "c++"),
            ("C#", "c#"),
            (".NET", ".net"),
            ("Node.js", "node.js"),
        ],
    )
    def test_significant_symbols_survive_against_alphanumerics(
        self, raw: str, expected: str
    ) -> None:
        assert _normalize(raw) == expected

    def test_lonely_significant_symbols_are_dropped(self) -> None:
        assert _normalize("great ++ stuff ... #") == "great stuff"


# --------------------------------------------------------------------------
# Skill matching
# --------------------------------------------------------------------------


class TestSkillMatching:
    def test_matched_and_unmatched_partition_the_profile_in_order(self) -> None:
        profile = _profile(skills=["Python", "Django", "Rust", "Kubernetes"])
        posting = _posting(description="We use Python and Django every day.")

        result = _scorer().score(posting, profile)

        assert result.matched_skills == ["Python", "Django"]
        assert result.unmatched_profile_skills == ["Rust", "Kubernetes"]
        assert result.missing_requirements == []

    def test_matching_is_case_and_accent_insensitive(self) -> None:
        profile = _profile(skills=["café", "PYTHON"])
        posting = _posting(description="CAFE culture and pYtHoN tooling")

        result = _scorer().score(posting, profile)

        assert result.matched_skills == ["café", "PYTHON"]

    def test_word_boundaries_prevent_substring_matches(self) -> None:
        profile = _profile(skills=["java", "react"])
        posting = _posting(description="Senior JavaScript role, very reactive UI.")

        result = _scorer().score(posting, profile)

        assert result.matched_skills == []
        assert result.unmatched_profile_skills == ["java", "react"]

    def test_multi_word_skill_matches_as_a_phrase(self) -> None:
        profile = _profile(skills=["django rest framework"])
        matches = _posting(description="Experience with Django REST Framework required.")
        misses = _posting(description="Some Django and some framework, but not together.")

        assert _scorer().score(matches, profile).matched_skills == ["django rest framework"]
        assert _scorer().score(misses, profile).matched_skills == []

    def test_symbol_bearing_skills_match(self) -> None:
        profile = _profile(skills=["c++", "c#", ".net"])
        posting = _posting(description="Stack: C++, C# and .NET on the backend.")

        result = _scorer().score(posting, profile)

        assert result.matched_skills == ["c++", "c#", ".net"]

    def test_title_text_counts_as_well_as_description(self) -> None:
        profile = _profile(skills=["python"])
        posting = _posting(title="Senior Python Engineer", description="No stack listed.")

        assert _scorer().score(posting, profile).matched_skills == ["python"]

    def test_falls_back_to_raw_description_when_clean_is_absent(self) -> None:
        profile = _profile(skills=["python"])
        posting = _posting(description="Python role", description_clean=None)

        assert _scorer().score(posting, profile).matched_skills == ["python"]

    def test_clean_description_is_preferred_when_present(self) -> None:
        profile = _profile(skills=["python"])
        posting = _posting(
            description="<p>Python</p>",
            description_clean="Ruby only, no snakes here.",
        )

        assert _scorer().score(posting, profile).matched_skills == []


# --------------------------------------------------------------------------
# Synonyms
# --------------------------------------------------------------------------


class TestSynonyms:
    def test_configured_synonym_matches_symmetrically(self) -> None:
        scorer = _scorer(synonyms=[["postgresql", "postgres", "psql"]])
        posting = _posting(description="Deep Postgres experience expected.")

        assert scorer.score(posting, _profile(skills=["postgresql"])).matched_skills == [
            "postgresql"
        ]
        assert scorer.score(
            _posting(description="We run PostgreSQL 16."), _profile(skills=["psql"])
        ).matched_skills == ["psql"]

    def test_synonyms_apply_to_absent_skills_too(self) -> None:
        scorer = _scorer(synonyms=[["php", "php7", "php8"]])
        posting = _posting(description="Legacy PHP8 codebase.")

        result = scorer.score(posting, _profile(skills=["python"], absent_skills=["php"]))

        assert result.penalized_skills == ["php"]

    def test_overlapping_synonym_groups_merge(self) -> None:
        scorer = _scorer(synonyms=[["a", "b"], ["b", "c"]])
        posting = _posting(description="only c is written here")

        assert scorer.score(posting, _profile(skills=["a"])).matched_skills == ["a"]

    def test_long_synonym_chain_merges_transitively(self) -> None:
        scorer = _scorer(synonyms=[["w", "x"], ["y", "x"], ["y", "z"]])
        posting = _posting(description="the codebase is all z")

        assert scorer.score(posting, _profile(skills=["w"])).matched_skills == ["w"]

    def test_skill_that_normalizes_to_nothing_never_matches(self) -> None:
        posting = _posting(description="+++ ... ###")

        result = _scorer().score(posting, _profile(skills=["+++", "python"]))

        assert result.matched_skills == []
        assert result.unmatched_profile_skills == ["+++", "python"]


# --------------------------------------------------------------------------
# Coverage, penalty and rounding
# --------------------------------------------------------------------------


class TestScoreArithmetic:
    def test_full_coverage_no_penalty_is_100(self) -> None:
        profile = _profile(skills=["python", "django"])
        posting = _posting(description="python and django")

        assert _scorer().score(posting, profile).score == 100

    def test_partial_coverage(self) -> None:
        profile = _profile(skills=["python", "django", "docker", "aws"])
        posting = _posting(description="python, django and docker")

        assert _scorer().score(posting, profile).score == 75

    def test_half_rounds_up_unlike_builtin_round(self) -> None:
        # 1/8 = 0.125 -> 12.5 -> floor(13.0) = 13. round(12.5) would give 12.
        profile = _profile(skills=[f"skill{i}" for i in range(8)])
        posting = _posting(description="skill0 only")

        assert _scorer().score(posting, profile).score == 13

    def test_penalty_is_subtracted_and_reported(self) -> None:
        profile = _profile(skills=["python", "django"], absent_skills=["php"])
        posting = _posting(description="python, django, and a bit of php")

        result = _scorer().score(posting, profile)

        assert result.penalized_skills == ["php"]
        assert result.score == 66  # 100 - round(0.34 * 100)
        assert "penalty -34" in result.summary
        assert "php" in result.summary

    def test_penalty_never_pushes_score_below_zero_and_summary_shows_real_delta(self) -> None:
        profile = _profile(skills=["python", "a", "b", "c"], absent_skills=["php"])
        posting = _posting(description="python and php")  # coverage 0.25

        result = _scorer().score(posting, profile)

        assert result.score == 0
        assert "penalty -25" in result.summary  # only 25 points were actually there to remove

    def test_penalty_cap_limits_the_damage(self) -> None:
        profile = _profile(
            skills=["python"],
            absent_skills=["php", "symfony", "laravel", "wordpress"],
        )
        posting = _posting(description="python php symfony laravel wordpress")

        result = _scorer(absent_skill_penalty_cap=0.5).score(posting, profile)

        # coverage 1.0, penalty capped at 0.5 -> score 50, not negative.
        assert result.score == 50
        assert result.penalized_skills == ["php", "symfony", "laravel", "wordpress"]

    def test_penalty_rate_is_configurable(self) -> None:
        profile = _profile(skills=["python"], absent_skills=["php"])
        posting = _posting(description="python php")

        assert _scorer(absent_skill_penalty=0.1).score(posting, profile).score == 90

    def test_no_penalty_leaves_penalized_empty_and_summary_clean(self) -> None:
        profile = _profile(skills=["python"], absent_skills=["php"])
        posting = _posting(description="python, no other languages")

        result = _scorer().score(posting, profile)

        assert result.penalized_skills == []
        assert "penalty" not in result.summary

    def test_default_penalty_constant_zeroes_a_full_score_at_three_hits(self) -> None:
        assert DEFAULT_ABSENT_SKILL_PENALTY * 3 >= 1.0


# --------------------------------------------------------------------------
# Edge cases
# --------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_profile_skills_scores_zero_and_says_why(self) -> None:
        result = _scorer().score(_posting(description="anything"), _profile(skills=[]))

        assert result.score == 0
        assert result.matched_skills == []
        assert result.unmatched_profile_skills == []
        assert "no skills" in result.summary

    def test_empty_profile_skills_still_reports_penalty_and_eligibility(self) -> None:
        profile = _profile(
            skills=[],
            absent_skills=["php"],
            locations=["France"],
            work_mode=WorkMode.REMOTE,
        )
        posting = _posting(
            description="php shop",
            location="Paris, France",
            work_mode=WorkMode.REMOTE,
        )

        result = _scorer().score(posting, profile)

        assert result.penalized_skills == ["php"]
        assert result.location_match is True
        assert result.work_mode_match is True

    def test_eligibility_fields_are_populated_but_do_not_move_the_score(self) -> None:
        profile = _profile(skills=["python"], locations=["France"], work_mode=WorkMode.REMOTE)
        reachable = _posting(
            description="python", location="Lyon, France", work_mode=WorkMode.REMOTE
        )
        unreachable = _posting(
            description="python", location="Tokyo, Japan", work_mode=WorkMode.ONSITE
        )

        good = _scorer().score(reachable, profile)
        bad = _scorer().score(unreachable, profile)

        assert good.score == bad.score == 100
        assert (good.location_match, good.work_mode_match) == (True, True)
        assert (bad.location_match, bad.work_mode_match) == (False, False)

    def test_scoring_is_deterministic(self) -> None:
        profile = _profile(skills=["python", "django"], absent_skills=["php"])
        posting = _posting(description="python role, some php")

        first = _scorer().score(posting, profile)
        second = _scorer().score(posting, profile)

        assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------
# Configuration validation
# --------------------------------------------------------------------------


class TestConfigValidation:
    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ScorerConfigError, match=r"[Ii]nvalid"):
            KeywordScorer(BackendSectionConfig(backend="keyword_match", synonynms=[]))

    def test_negative_penalty_is_rejected(self) -> None:
        with pytest.raises(ScorerConfigError):
            _scorer(absent_skill_penalty=-0.1)

    def test_penalty_cap_above_one_is_rejected(self) -> None:
        with pytest.raises(ScorerConfigError):
            _scorer(absent_skill_penalty_cap=1.5)
