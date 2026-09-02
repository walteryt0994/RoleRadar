from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class JobDescription(BaseModel):
    text: str


class ResumeTextRequest(BaseModel):
    text: str


class ResumeTextResponse(BaseModel):
    text: str
    character_count: int
    source_type: str
    filename: str | None = None


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


class ProfileBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkAuthorizationStatus(str, Enum):
    AUTHORIZED = "authorized"
    NOT_AUTHORIZED = "not_authorized"
    UNKNOWN = "unknown"
    PREFER_NOT_TO_SAY = "prefer_not_to_say"


class WorkAuthorization(ProfileBaseModel):
    work_country: str = "US"
    status: WorkAuthorizationStatus | None = None
    requires_sponsorship: bool | None = None


class Education(ProfileBaseModel):
    school: str
    degree: str | None = None
    major: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    gpa: float | None = None


class Experience(ProfileBaseModel):
    company: str
    title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    description: str | None = None


class Project(ProfileBaseModel):
    name: str
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    link: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class Course(ProfileBaseModel):
    name: str
    institution: str | None = None
    completion_date: str | None = None


class Certification(ProfileBaseModel):
    name: str
    issuer: str | None = None
    issue_date: str | None = None
    expiration_date: str | None = None


class WorkMode(str, Enum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    NO_PREFERENCE = "no_preference"


class Preferences(ProfileBaseModel):
    desired_roles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    work_mode: WorkMode | None = None


class EvidenceSourceType(str, Enum):
    EDUCATION = "education"
    EXPERIENCE = "experience"
    PROJECT = "project"
    COURSE = "course"
    CERTIFICATION = "certification"
    RESUME = "resume"

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class SkillEvidence(ProfileBaseModel):
    skill: NonEmptyStr
    evidence_text: NonEmptyStr
    source_type: EvidenceSourceType
    source_id: str | None = None
    confidence: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
    )
    user_confirmed: bool | None = None


class StudentProfile(ProfileBaseModel):
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    courses: list[Course] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    preferences: Preferences | None = None
    work_authorization: WorkAuthorization | None = None
    skill_evidence: list[SkillEvidence] = Field(default_factory=list)
