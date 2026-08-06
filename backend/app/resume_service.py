from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.schemas import ResumeTextResponse


def process_resume_text(
    text: str,
    source_type: str,
    filename: str | None = None,
) -> ResumeTextResponse:
    cleaned_text = text.strip()

    if not cleaned_text:
        raise ValueError("Resume text cannot be empty.")

    return ResumeTextResponse(
        text=cleaned_text,
        character_count=len(cleaned_text),
        source_type=source_type,
        filename=filename,
    )


def process_resume_pdf(
    pdf_bytes: bytes,
    filename: str,
) -> ResumeTextResponse:
    try:
        reader = PdfReader(BytesIO(pdf_bytes))

        if reader.is_encrypted:
            raise ValueError(
                "Encrypted PDF files are not supported."
            )

        page_texts = []

        for page in reader.pages:
            page_text = page.extract_text()

            if page_text:
                page_texts.append(page_text)

    except PdfReadError as error:
        raise ValueError(
            "Unable to read PDF file."
        ) from error

    extracted_text = "\n\n".join(page_texts)

    if not extracted_text.strip():
        raise ValueError(
            "PDF does not contain extractable text."
        )

    return process_resume_text(
        extracted_text,
        source_type="pdf",
        filename=filename,
    )
