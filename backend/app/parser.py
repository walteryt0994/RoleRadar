import re

KNOWN_SKILLS = [
    "Python",
    "SQL",
    "JavaScript",
    "Java",
    "C++",
    "AWS",
    "Docker",
    "Kubernetes",
    "Excel",
    "Tableau",
    "Power BI",
    "Machine Learning",
    "Deep Learning",
    "NLP",
    "Computer Vision",
    "Data Analysis",
    "Data Visualization",
    "Data Engineering",
    "DevOps",
    "Agile Methodologies",
    "Project Management",
]


def extract_skills(text: str) -> list[str]:
    found_skills = []

    for skill in KNOWN_SKILLS:
        pattern = rf"(?<!\w){re.escape(skill)}(?!\w)"

        if re.search(pattern, text, re.IGNORECASE):
            found_skills.append(skill)

    return found_skills
