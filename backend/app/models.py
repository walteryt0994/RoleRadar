from datetime import datetime

from sqlalchemy import Boolean, JSON, DateTime, Float, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String)
    job_title: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)
    fit_score: Mapped[float] = mapped_column(Float)
    matched_skills: Mapped[list[str]] = mapped_column(JSON)
    missing_skills: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )

class StudentProfileRecord(Base):
    __tablename__ = "student_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    profile_data: Mapped[dict] = mapped_column(JSON)
    is_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
    )
