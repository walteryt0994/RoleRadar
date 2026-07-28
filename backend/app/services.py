from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Application
from app.schemas import ApplicationCreate


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
