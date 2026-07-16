from datetime import datetime

from fastapi import Depends, FastAPI
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzer import analyze_skill_gap
from app.database import Base, engine, get_db
from app.models import Application
from app.parser import extract_skills


class JobDescription(BaseModel):
    text:str

class JobAnalysisRequest(BaseModel):
    text:str
    user_skills:list[str]

class ApplicationCreate(BaseModel):
    company: str
    job_title: str
    status: str
    fit_score: float
    matched_skills: list[str]
    missing_skills: list[str]

class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company: str
    job_title: str
    status: str
    fit_score: float
    matched_skills: list[str]
    missing_skills: list[str]
    created_at: datetime


Base.metadata.create_all(bind=engine)

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

@app.post("/applications", response_model=ApplicationResponse)
def create_application(
    payload: ApplicationCreate,
    db: Session = Depends(get_db),
):
    application = Application(**payload.model_dump())

    db.add(application)
    db.commit()
    db.refresh(application)
    return application

@app.get("/applications", response_model=list[ApplicationResponse])
def list_applications(db: Session = Depends(get_db)):
    statement = select(Application)
    applications = db.scalars(statement).all()

    return applications
