from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import services
from app.analyzer import analyze_skill_gap
from app.database import get_db
from app.parser import extract_skills
from app.schemas import (
    ApplicationCreate,
    ApplicationResponse,
    ApplicationStatusUpdate,
    JobAnalysisRequest,
    JobDescription,
)


router = APIRouter()


@router.get("/")
def read_root():
    return {"message": "RoleRadar backend is running"}


@router.get("/health")
def health_check():
    return {"status": "ok"}


@router.post("/parse-jd")
def parse_jd(payload: JobDescription):
    found_skills = extract_skills(payload.text)

    return {
        "message": "JD parser endpoint is ready",
        "jd_text_length": len(payload.text),
        "skills": found_skills,
    }


@router.post("/analyze-job")
def analyze_job(payload: JobAnalysisRequest):
    jd_skills = extract_skills(payload.text)
    analysis = analyze_skill_gap(
        jd_skills,
        payload.user_skills,
    )

    return {
        "skills": jd_skills,
        "matched_skills": analysis["matched_skills"],
        "missing_skills": analysis["missing_skills"],
        "fit_score": analysis["fit_score"],
    }


@router.post(
    "/applications",
    response_model=ApplicationResponse,
)
def create_application(
    payload: ApplicationCreate,
    db: Session = Depends(get_db),
):
    return services.create_application(
        db,
        payload,
    )


@router.get(
    "/applications",
    response_model=list[ApplicationResponse],
)
def list_applications(
    db: Session = Depends(get_db),
):
    return services.list_applications(db)


@router.patch(
    "/applications/{application_id}",
    response_model=ApplicationResponse,
)
def update_application_status(
    application_id: int,
    payload: ApplicationStatusUpdate,
    db: Session = Depends(get_db),
):
    application = services.update_application_status(
        db,
        application_id,
        payload.status,
    )

    if application is None:
        raise HTTPException(
            status_code=404,
            detail="Application not found",
        )

    return application
