import pytest

from app.parser import extract_skills


@pytest.mark.parametrize(
    (
        "text",
        "expected_skills",
    ),
    [
        (
            "We need Python and SQL experience.",
            ["Python", "SQL"],
        ),
        (
            "We need PYTHON and sql.",
            ["Python", "SQL"],
        ),
        ("", []),
        ("We need Rust and Go.", []),
        ("C++ experience required.", ["C++"]),
    ],
)
def test_extract_skills_returns_expected_skills(
    text,
    expected_skills,
):
    result = extract_skills(text)

    assert result == expected_skills


def test_extract_skills_does_not_match_skill_inside_another_skill():
    text = "JavaScript experience required."

    result = extract_skills(text)

    assert result == ["JavaScript"]
