from fastapi import FastAPI
from pydantic import BaseModel

class JobDescription(BaseModel):
    text:str

app = FastAPI(
    title="RoleRadar API",
    version="0.1.0",
    description="Backend API for RoleRadar job intelligence and skill-gap analysis.",
)

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

@app.get("/")
def read_root():
    return {"message": "RoleRadar backend is running"}   

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/parse-jd")
def parse_jd(payload: JobDescription):
    known_skills = KNOWN_SKILLS
    found_skills = []

    for skill in known_skills:
        if skill.lower() in payload.text.lower():
            found_skills.append(skill)


    return{"message": "JD parser endpoint is ready" ,
           "jd_text_length": len(payload.text),
           "skills":found_skills,
           }