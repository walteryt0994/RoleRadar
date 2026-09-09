import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from app.ai_provider import GenerationResult
from app.schemas import SCHEMA_VERSION, StructuredJobDescription

RECORD_VERSION = "1"

TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


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

    return now.strftime(TIMESTAMP_FORMAT)


NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

Sha256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]

UsageCount = Annotated[int, Field(ge=0)]

OutputLimit = Annotated[int, Field(gt=0)]

LatencySeconds = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class ParseMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    generated_at: str
    prompt_version: NonEmptyText
    parser_version: NonEmptyText
    schema_version: NonEmptyText
    provider: NonEmptyText
    requested_model: NonEmptyText
    returned_model: NonEmptyText
    requested_max_output_tokens: OutputLimit | None
    requested_reasoning_effort: NonEmptyText | None
    job_posting_sha256: Sha256Hex
    latency_seconds: LatencySeconds
    input_tokens: UsageCount | None
    output_tokens: UsageCount | None
    total_tokens: UsageCount | None

    @field_validator("generated_at")
    @classmethod
    def _validate_utc_timestamp(cls, value: str) -> str:
        try:
            datetime.strptime(value, TIMESTAMP_FORMAT)
        except (TypeError, ValueError):
            raise ValueError(
                "generated_at must be a UTC timestamp"
            ) from None

        return value


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


def _validated_metadata(metadata_payload: object) -> ParseMetadata:
    if not isinstance(metadata_payload, dict):
        raise JobDescriptionRecordFormatError(
            "The record metadata is not an object."
        )

    try:
        return ParseMetadata.model_validate(metadata_payload)
    except ValidationError:
        raise JobDescriptionRecordFormatError(
            "The record metadata does not match the expected contract."
        ) from None


def export_record(record: JobDescriptionRecord, path) -> Path:
    target = Path(path)
    payload = record_to_payload(record)

    _validated_metadata(payload["metadata"])

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


RECORD_KEYS = ("record_version", "metadata", "job_description")


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

    if set(payload) != set(RECORD_KEYS):
        raise JobDescriptionRecordFormatError(
            "The record file does not match the expected record keys."
        )

    metadata = _validated_metadata(payload["metadata"])

    if metadata.schema_version != SCHEMA_VERSION:
        raise JobDescriptionRecordVersionError(
            "The record uses an unsupported job description schema "
            "version."
        )

    job_payload = payload["job_description"]

    if not isinstance(job_payload, dict):
        raise JobDescriptionRecordFormatError(
            "The record file has no job description object."
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
        metadata=metadata,
    )