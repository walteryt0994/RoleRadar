import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from app.ai_provider import GenerationResult
from app.schemas import SCHEMA_VERSION, StructuredJobDescription

RECORD_VERSION = "1"


class JobDescriptionRecordError(Exception):
    pass


class JobDescriptionRecordExistsError(JobDescriptionRecordError):
    pass


class JobDescriptionRecordFormatError(JobDescriptionRecordError):
    pass


class JobDescriptionRecordVersionError(JobDescriptionRecordError):
    pass


def fingerprint_job_posting(job_description_text: str) -> str:
    normalised = job_description_text.strip().encode("utf-8")

    return hashlib.sha256(normalised).hexdigest()


def utc_timestamp() -> str:
    now = datetime.now(timezone.utc)

    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class ParseMetadata:
    generated_at: str
    prompt_version: str
    parser_version: str
    schema_version: str
    provider: str
    requested_model: str
    returned_model: str
    requested_max_output_tokens: int | None
    requested_reasoning_effort: str | None
    job_posting_sha256: str
    latency_seconds: float
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


METADATA_FIELDS = (
    "generated_at",
    "prompt_version",
    "parser_version",
    "schema_version",
    "provider",
    "requested_model",
    "returned_model",
    "requested_max_output_tokens",
    "requested_reasoning_effort",
    "job_posting_sha256",
    "latency_seconds",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)


@dataclass(frozen=True)
class JobDescriptionRecord:
    job_description: StructuredJobDescription
    metadata: ParseMetadata
    generation: GenerationResult | None = None


def record_to_payload(record: JobDescriptionRecord) -> dict[str, object]:
    metadata = {
        name: getattr(record.metadata, name)
        for name in METADATA_FIELDS
    }

    return {
        "record_version": RECORD_VERSION,
        "metadata": metadata,
        "job_description": record.job_description.model_dump(mode="json"),
    }


def export_record(record: JobDescriptionRecord, path) -> Path:
    target = Path(path)
    payload = record_to_payload(record)

    target.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(target, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError:
        raise JobDescriptionRecordExistsError(
            "A record file already exists at the target path."
        ) from None

    return target


def load_record(path) -> JobDescriptionRecord:
    source = Path(path)

    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise JobDescriptionRecordFormatError(
            "The record file could not be read as JSON."
        ) from None

    if not isinstance(payload, dict):
        raise JobDescriptionRecordFormatError(
            "The record file does not contain a record object."
        )

    if payload.get("record_version") != RECORD_VERSION:
        raise JobDescriptionRecordVersionError(
            "The record file uses an unsupported record version."
        )

    metadata_payload = payload.get("metadata")
    job_payload = payload.get("job_description")

    if not isinstance(metadata_payload, dict):
        raise JobDescriptionRecordFormatError(
            "The record file has no metadata object."
        )

    if not isinstance(job_payload, dict):
        raise JobDescriptionRecordFormatError(
            "The record file has no job description object."
        )

    if set(metadata_payload) != set(METADATA_FIELDS):
        raise JobDescriptionRecordFormatError(
            "The record metadata does not match the expected fields."
        )

    if metadata_payload["schema_version"] != SCHEMA_VERSION:
        raise JobDescriptionRecordVersionError(
            "The record uses an unsupported job description schema "
            "version."
        )

    try:
        job_description = StructuredJobDescription.model_validate(job_payload)
    except ValidationError:
        raise JobDescriptionRecordFormatError(
            "The stored job description does not match the current "
            "schema."
        ) from None

    return JobDescriptionRecord(
        job_description=job_description,
        metadata=ParseMetadata(**metadata_payload),
    )