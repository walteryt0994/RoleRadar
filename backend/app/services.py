from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application, StudentProfileRecord
from app.schemas import ApplicationCreate, StudentProfile


def create_application(
    db: Session,
    payload: ApplicationCreate,
) -> Application:
    application = Application(**payload.model_dump())

    db.add(application)
    db.commit()
    db.refresh(application)

    return application


def list_applications(
    db: Session,
) -> list[Application]:
    statement = select(Application)
    applications = db.scalars(statement).all()

    return applications


def update_application_status(
    db: Session,
    application_id: int,
    status: str,
) -> Application | None:
    application = db.get(
        Application,
        application_id,
    )

    if application is None:
        return None

    application.status = status

    db.commit()
    db.refresh(application)

    return application


def get_profile(db: Session) -> StudentProfileRecord | None:
    statement = select(StudentProfileRecord)

    return db.scalars(statement).first()


def save_or_replace_profile(
    db: Session,
    profile: StudentProfile,
) -> StudentProfileRecord:
    record = get_profile(db)

    if record is None:
        record = StudentProfileRecord(
            profile_data=profile.model_dump(mode="json"),
            is_confirmed=False,
        )
        db.add(record)
    else:
        record.profile_data = profile.model_dump(mode="json")
        record.is_confirmed = False

    db.commit()
    db.refresh(record)

    return record


def update_profile_confirmation(
    db: Session,
) -> StudentProfileRecord | None:
    record = get_profile(db)

    if record is None:
        return None

    record.is_confirmed = True

    db.commit()
    db.refresh(record)

    return record