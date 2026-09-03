import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.database as database
from app.database import Base, get_db


@pytest.fixture
def client(tmp_path, monkeypatch):
    database_path = tmp_path / "test_roleradar.db"
    test_database_url = f"sqlite:///{database_path}"

    test_engine = create_engine(
        test_database_url,
        connect_args={"check_same_thread": False},
    )

    monkeypatch.setattr(database, "engine", test_engine)

    from app.main import app

    test_session_factory = sessionmaker(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db():
        db = test_session_factory()

        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.pop(get_db, None)
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


SAMPLE_PROFILE = {
    "education": [],
    "experience": [],
    "projects": [],
    "courses": [],
    "certifications": [],
    "skills": ["Python"],
    "preferences": None,
    "work_authorization": {
        "work_country": "US",
        "status": "authorized",
        "requires_sponsorship": False,
    },
    "skill_evidence": [
        {
            "skill": "Python",
            "evidence_text": "Completed a Data Structures course.",
            "source_type": "course",
            "source_id": None,
            "confidence": 0.8,
            "user_confirmed": None,
        }
    ],
}


def test_put_profile_creates_new_profile(client):
    response = client.put("/profile", json=SAMPLE_PROFILE)

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == 1
    assert data["is_confirmed"] is False
    assert data["profile_data"]["skills"] == ["Python"]


def test_get_profile_returns_saved_nested_profile(client):
    client.put("/profile", json=SAMPLE_PROFILE)

    response = client.get("/profile")

    assert response.status_code == 200

    data = response.json()

    assert data["profile_data"]["skill_evidence"][0]["skill"] == "Python"
    assert data["profile_data"]["skill_evidence"][0]["source_type"] == "course"
    assert data["profile_data"]["skill_evidence"][0]["confidence"] == 0.8
    assert data["profile_data"]["work_authorization"]["status"] == "authorized"


def test_put_profile_replaces_existing_without_duplicate(client):
    client.put("/profile", json=SAMPLE_PROFILE)

    replacement = dict(SAMPLE_PROFILE)
    replacement["skills"] = ["SQL"]
    replacement["skill_evidence"] = []

    response = client.put("/profile", json=replacement)

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == 1
    assert data["profile_data"]["skills"] == ["SQL"]
    assert data["profile_data"]["skill_evidence"] == []


def test_put_profile_resets_confirmation_after_change(client):
    client.put("/profile", json=SAMPLE_PROFILE)
    client.patch("/profile/confirmation")

    replacement = dict(SAMPLE_PROFILE)
    replacement["skills"] = ["SQL"]

    response = client.put("/profile", json=replacement)

    assert response.status_code == 200
    assert response.json()["is_confirmed"] is False


def test_patch_confirmation_confirms_profile(client):
    client.put("/profile", json=SAMPLE_PROFILE)

    response = client.patch("/profile/confirmation")

    assert response.status_code == 200
    assert response.json()["is_confirmed"] is True


def test_get_profile_returns_404_when_not_found(client):
    response = client.get("/profile")

    assert response.status_code == 404
    assert response.json() == {"detail": "Profile not found"}


def test_patch_confirmation_returns_404_when_not_found(client):
    response = client.patch("/profile/confirmation")

    assert response.status_code == 404
    assert response.json() == {"detail": "Profile not found"}


def test_put_profile_rejects_invalid_payload(client):
    invalid_payload = {
        "skill_evidence": [
            {
                "skill": "Python",
                "evidence_text": "Some evidence text.",
                "source_type": "not_a_real_source",
            }
        ]
    }

    response = client.put("/profile", json=invalid_payload)

    assert response.status_code == 422


def test_profile_persists_across_separate_sessions(client):
    client.put("/profile", json=SAMPLE_PROFILE)

    first_read = client.get("/profile").json()
    second_read = client.get("/profile").json()

    assert first_read["profile_data"] == second_read["profile_data"]
    assert first_read["created_at"] == second_read["created_at"]


def test_application_and_profile_are_independent(client):
    application_payload = {
        "company": "Independent Test Co",
        "job_title": "Backend Engineer",
        "status": "Interested",
        "fit_score": 50.0,
        "matched_skills": ["Python"],
        "missing_skills": [],
    }

    client.post("/applications", json=application_payload)
    client.put("/profile", json=SAMPLE_PROFILE)

    applications = client.get("/applications").json()
    profile = client.get("/profile").json()

    assert len(applications) == 1
    assert profile["profile_data"]["skills"] == ["Python"]