from fastapi import FastAPI
from pydantic import BaseModel
from app.parser import extract_skills
from app.analyzer import analyze_skill_gap


class JobDescription(BaseModel):
    text:str

class JobAnalysisRequest(BaseModel):
    text:str
    user_skills:list[str]


app = FastAPI(
    title="RoleRadar API",
    version="0.1.0",
    description="Backend API for RoleRadar job intelligence and skill-gap analysis.",
)


@app.get("/")
def read_root():
    return {"message": "RoleRadar backend is running"}   

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/parse-jd")
def parse_jd(payload: JobDescription):

    found_skills = extract_skills(payload.text)

    return{"message": "JD parser endpoint is ready" ,
           "jd_text_length": len(payload.text),
           "skills":found_skills,
           }

@app.post("/analyze-job")
def analyze_job(payload: JobAnalysisRequest):
    jd_skills = extract_skills(payload.text)
    analysis = analyze_skill_gap(
        jd_skills,
        payload.user_skills
    )

    return{
        "skills":jd_skills,
        "matched_skills":analysis["matched_skills"],
        "missing_skills":analysis["missing_skills"],
        "fit_score":analysis["fit_score"],
    }