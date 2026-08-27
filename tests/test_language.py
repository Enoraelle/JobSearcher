"""Tests for the dependency-free language-detection heuristic."""

from __future__ import annotations

import pytest

from jobsearcher.language import detect_language

_ENGLISH = (
    "We are looking for a backend engineer who will join our team and help us "
    "build the services that power our product. You will work with Python."
)
_FRENCH = (
    "Nous recherchons un développeur backend qui rejoindra notre équipe pour "
    "construire les services de notre produit. Vous travaillerez avec Python."
)
_GERMAN = (
    "Wir suchen einen Backend-Entwickler, der unser Team verstärkt und mit uns "
    "die Dienste baut, die unser Produkt antreiben. Du wirst mit Python arbeiten."
)
_SPANISH = (
    "Buscamos un desarrollador backend que se una a nuestro equipo para "
    "construir los servicios que impulsan nuestro producto con Python y más."
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [(_ENGLISH, "en"), (_FRENCH, "fr"), (_GERMAN, "de"), (_SPANISH, "es")],
)
def test_detects_each_supported_language(text: str, expected: str) -> None:
    assert detect_language(text) == expected


def test_short_text_is_undetermined() -> None:
    assert detect_language("Backend Engineer") is None


def test_text_without_stopwords_is_undetermined() -> None:
    # Real words, no function words from any supported language.
    assert detect_language("kubernetes docker terraform postgresql observability " * 3) is None


def test_undetermined_is_distinct_from_a_guess() -> None:
    # A Polish sentence: long enough, but none of our stopword sets apply.
    polish = "Poszukujemy programisty backend ktory dolaczy do naszego zespolu " * 2
    assert detect_language(polish) is None
