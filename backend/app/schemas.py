from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobDescription(BaseModel):
    text: str


class JobAnalysisRequest(BaseModel):
    text: str
    user_skills: list[str]


class ApplicationCreate(BaseModel):
    company: str
    job_title: str
    status: str
    fit_score: float
    matched_skills: list[str]
    missing_skills: list[str]


class ApplicationStatusUpdate(BaseModel):
    status: str


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
