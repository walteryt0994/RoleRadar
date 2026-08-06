from io import BytesIO
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.routers import MAX_RESUME_PDF_SIZE_BYTES, router


@pytest.fixture
def client():
    test_app = FastAPI()
    test_app.include_router(router)

    with TestClient(test_app) as test_client:
        yield test_client


def _create_blank_pdf_bytes() -> bytes:
    pdf_buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(pdf_buffer)

    return pdf_buffer.getvalue()


def test_resume_text_returns_cleaned_text(client):
    response = client.post(
        "/resumes/text",
        json={"text": "  Python Developer  "},
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "Python Developer",
        "character_count": 16,
        "source_type": "text",
        "filename": None,
    }


def test_resume_text_rejects_blank_text(client):
    response = client.post(
        "/resumes/text",
        json={"text": "   "},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Resume text cannot be empty.",
    }


def test_resume_text_rejects_missing_text(client):
    response = client.post(
        "/resumes/text",
        json={},
    )

    assert response.status_code == 422
    assert "detail" in response.json()


def test_resume_pdf_extracts_text(client):
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "synthetic_resume.pdf"
    )

    with fixture_path.open("rb") as pdf_file:
        response = client.post(
            "/resumes/pdf",
            files={
                "file": (
                    "synthetic_resume.pdf",
                    pdf_file,
                    "application/pdf",
                ),
            },
        )

    assert response.status_code == 200

    data = response.json()

    assert "Python Developer" in data["text"]
    assert "Python, FastAPI, SQL" in data["text"]
    assert data["character_count"] == len(data["text"])
    assert data["source_type"] == "pdf"
    assert data["filename"] == "synthetic_resume.pdf"


@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("resume.txt", "application/pdf"),
        ("resume.pdf", "text/plain"),
    ],
)
def test_resume_pdf_rejects_unsupported_file(
    client,
    filename,
    content_type,
):
    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                filename,
                b"%PDF-test",
                content_type,
            ),
        },
    )

    assert response.status_code == 415
    assert response.json() == {
        "detail": "Only PDF files are supported",
    }


def test_resume_pdf_rejects_empty_file(client):
    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                "resume.pdf",
                b"",
                "application/pdf",
            ),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "PDF file cannot be empty",
    }


def test_resume_pdf_rejects_invalid_header(client):
    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                "resume.pdf",
                b"not a pdf",
                "application/pdf",
            ),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Uploaded file is not a valid PDF",
    }


def test_resume_pdf_rejects_damaged_pdf(client):
    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                "resume.pdf",
                b"%PDF-test",
                "application/pdf",
            ),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Unable to read PDF file.",
    }


def test_resume_pdf_rejects_pdf_without_text(client):
    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                "resume.pdf",
                _create_blank_pdf_bytes(),
                "application/pdf",
            ),
        },
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "PDF does not contain extractable text.",
    }


def test_resume_pdf_rejects_oversized_file(client):
    oversized_pdf = (
        b"%PDF-"
        + b"0" * MAX_RESUME_PDF_SIZE_BYTES
    )

    response = client.post(
        "/resumes/pdf",
        files={
            "file": (
                "resume.pdf",
                oversized_pdf,
                "application/pdf",
            ),
        },
    )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "PDF file must be 5 MB or smaller",
    }
