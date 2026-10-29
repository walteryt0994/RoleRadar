"""Who is making a request: synthetic local users and server-side sessions.

A browser only ever holds a random session token. The server stores a hash
of it and looks the user up from that, so a request cannot pick its owner by
sending an owner_id. The dev login is not authentication: anyone who can
reach the local server can choose a synthetic user. That is why the app only
accepts connections from this machine and must not be opened to anyone else
until a real login exists.
"""
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.library.models import LoginSession, User

DEV_PROVIDER = "dev_synthetic"
DEV_USER_LABELS = ("合成用户 A", "合成用户 B")


@dataclass(frozen=True)
class CurrentUser:
    id: str
    label: str
    csrf_token: str
    token_hash: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def ensure_dev_users(db: Session, now: datetime) -> None:
    existing = set(
        db.scalars(
            select(User.display_label).where(User.auth_provider == DEV_PROVIDER)
        )
    )

    for label in DEV_USER_LABELS:
        if label not in existing:
            db.add(
                User(
                    id=str(uuid.uuid4()),
                    display_label=label,
                    auth_provider=DEV_PROVIDER,
                    created_at=now,
                )
            )


def dev_users(db: Session) -> list[User]:
    statement = (
        select(User)
        .where(User.auth_provider == DEV_PROVIDER)
        .order_by(User.display_label)
    )

    return list(db.scalars(statement))


def start_session(
    db: Session,
    user_id: str,
    now: datetime,
    lifetime: timedelta,
) -> str:
    """Create a session and return the raw token for the cookie."""
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=token_hash(token),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            created_at=now,
            expires_at=now + lifetime,
        )
    )

    return token


def resolve_session(db: Session, token: str, now: datetime) -> CurrentUser | None:
    row = db.execute(
        select(LoginSession, User)
        .join(User, User.id == LoginSession.user_id)
        .where(LoginSession.token_hash == token_hash(token))
    ).first()

    if row is None:
        return None

    session, user = row

    if session.expires_at <= now:
        return None

    return CurrentUser(
        id=user.id,
        label=user.display_label,
        csrf_token=session.csrf_token,
        token_hash=session.token_hash,
    )


def end_session(db: Session, session_token_hash: str) -> None:
    db.execute(delete(LoginSession).where(LoginSession.token_hash == session_token_hash))


def remove_expired_sessions(db: Session, now: datetime) -> None:
    db.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
