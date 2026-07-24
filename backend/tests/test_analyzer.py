import pytest

from app.analyzer import analyze_skill_gap


@pytest.mark.parametrize(
    (
        "jd_skills",
        "user_skills",
        "expected_matched",
        "expected_missing",
        "expected_score",
    ),
    [
        (
            ["Python", "SQL"],
            ["python", "SQL"],
            ["Python", "SQL"],
            [],
            100.0,
        ),
        (
            ["Python", "SQL", "Docker"],
            ["Python"],
            ["Python"],
            ["SQL", "Docker"],
            100 / 3,
        ),
        (
            ["AWS", "Docker"],
            ["Python"],
            [],
            ["AWS", "Docker"],
            0.0,
        ),
        (
            [],
            ["Python"],
            [],
            [],
            0.0,
        ),
    ],
)
def test_analyze_skill_gap_returns_expected_result(
    jd_skills,
    user_skills,
    expected_matched,
    expected_missing,
    expected_score,
):
    result = analyze_skill_gap(jd_skills, user_skills)

    assert result["matched_skills"] == expected_matched
    assert result["missing_skills"] == expected_missing
    assert result["fit_score"] == pytest.approx(expected_score)
