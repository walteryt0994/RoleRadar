def analyze_skill_gap(
    jd_skills: list[str],
    user_skills: list[str],
) -> dict:
    user_skills_lower = [skill.lower() for skill in user_skills]

    matched_skills = [
        skill
        for skill in jd_skills
        if skill.lower() in user_skills_lower
    ]

    missing_skills = [
        skill
        for skill in jd_skills
        if skill.lower() not in user_skills_lower
    ]

    if jd_skills:
        fit_score = len(matched_skills) / len(jd_skills) * 100
    else:
        fit_score = 0

    return {
        "matched_skills": matched_skills,
        "missing_skills": missing_skills,
        "fit_score": fit_score,
    }
