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


def test_create_application(client):
    payload = {
        "company": "Test Company",
        "job_title": "Backend Engineer",
        "status": "Interested",
        "fit_score": 50.0,
        "matched_skills": ["Python", "SQL"],
        "missing_skills": ["AWS", "Docker"],
    }

    response = client.post(
        "/applications",
        json=payload,
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == 1
    assert data["company"] == "Test Company"
    assert data["job_title"] == "Backend Engineer"
    assert data["status"] == "Interested"
    assert data["fit_score"] == 50.0
    assert data["matched_skills"] == ["Python", "SQL"]
    assert data["missing_skills"] == ["AWS", "Docker"]
    assert data["created_at"] is not None


def test_list_applications(client):
    payload = {
        "company": "List Test Company",
        "job_title": "Data Engineer",
        "status": "Applied",
        "fit_score": 75.0,
        "matched_skills": ["Python"],
        "missing_skills": ["AWS"],
    }

    create_response = client.post("/applications", json=payload)
    assert create_response.status_code == 200

    response = client.get("/applications")
    assert response.status_code == 200

    data = response.json()

    assert len(data) == 1
    assert data[0]["company"] == "List Test Company"
    assert data[0]["job_title"] == "Data Engineer"
    assert data[0]["status"] == "Applied"


def test_update_application_status(client):
    payload = {
        "company": "Patch Test Company",
        "job_title": "Python Developer",
        "status": "Interested",
        "fit_score": 80.0,
        "matched_skills": ["Python"],
        "missing_skills": [],
    }

    create_response = client.post("/applications", json=payload)
    application_id = create_response.json()["id"]

    response = client.patch(
        f"/applications/{application_id}",
        json={"status": "Applied"},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == application_id
    assert data["status"] == "Applied"
    assert data["company"] == "Patch Test Company"


def test_update_missing_application_returns_404(client):
    response = client.patch(
        "/applications/999",
        json={"status": "Applied"},
    )

    assert response.status_code == 404
    assert response.json() == {
        "detail": "Application not found",
    }


def test_create_application_rejects_invalid_payload(client):
    invalid_payload = {
        "company": "Incomplete Company",
    }

    response = client.post(
        "/applications",
        json=invalid_payload,
    )

    assert response.status_code == 422
    assert "detail" in response.json()
