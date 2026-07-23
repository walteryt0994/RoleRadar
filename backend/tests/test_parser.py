from app.parser import extract_skills


def test_extract_skills_finds_known_skills():
    text = "We need Python and SQL experience."

    result = extract_skills(text)

    assert result == ["Python", "SQL"]
