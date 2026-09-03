from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app import services
from app.analyzer import analyze_skill_gap
from app.database import get_db
from app.parser import extract_skills
from app.resume_service import (
    process_resume_pdf,
    process_resume_text,
)
from app.schemas import (
    ApplicationCreate,
    ApplicationResponse,
    ApplicationStatusUpdate,
    JobAnalysisRequest,
    JobDescription,
    ProfileResponse,
    ResumeTextRequest,
    ResumeTextResponse,
    StudentProfile,
)

MAX_RESUME_PDF_SIZE_BYTES = 5 * 1024 * 1024
PDF_CONTENT_TYPE = "application/pdf"

router = APIRouter()


@router.get("/")
def read_root():
    return {"message": "RoleRadar backend is running"}


@router.get("/health")
def health_check():
    return {"status": "ok"}


@router.post(
    "/resumes/text",
    response_model=ResumeTextResponse,
)
def intake_resume_text(payload: ResumeTextRequest):
    try:
        return process_resume_text(
            payload.text,
            source_type="text",
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error


@router.post(
    "/resumes/pdf",
    response_model=ResumeTextResponse,
)
async def intake_resume_pdf(file: UploadFile):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are supported",
        )

    if file.content_type != PDF_CONTENT_TYPE:
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are supported",
        )

    pdf_bytes = await file.read(MAX_RESUME_PDF_SIZE_BYTES + 1)

    if not pdf_bytes:
        raise HTTPException(
            status_code=400,
            detail="PDF file cannot be empty",
        )

    if len(pdf_bytes) > MAX_RESUME_PDF_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail="PDF file must be 5 MB or smaller",
        )

    if not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is not a valid PDF",
        )

    try:
        return process_resume_pdf(
            pdf_bytes,
            filename=file.filename,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        ) from error


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


@router.get(
    "/profile",
    response_model=ProfileResponse,
)
def get_profile(
    db: Session = Depends(get_db),
):
    profile = services.get_profile(db)

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Profile not found",
        )

    return profile


@router.put(
    "/profile",
    response_model=ProfileResponse,
)
def save_profile(
    payload: StudentProfile,
    db: Session = Depends(get_db),
):
    return services.save_or_replace_profile(db, payload)


@router.patch(
    "/profile/confirmation",
    response_model=ProfileResponse,
)
def confirm_profile(
    db: Session = Depends(get_db),
):
    profile = services.update_profile_confirmation(db)

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Profile not found",
        )

    return profile