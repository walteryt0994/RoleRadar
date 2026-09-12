"""The call contract of one approved Day 27 collection batch.

These constants describe a batch the user approved once and that has
already been consumed. They are not a standing authorisation: any further
real request needs a new approval and is run by the user.

Both the offline comparison and the collection tool read this module, so a
record is only treated as belonging to this batch when every field below
matches. The returned model is deliberately not part of the contract; a
provider may answer with a dated snapshot alias and that is recorded as
observed rather than forced to equal the requested name.
"""

import json
import math

from app.jd_service import (
    PARSER_VERSION,
    PROMPT_VERSION,
    build_prompt,
    build_schema_format,
)
from app.schemas import SCHEMA_VERSION
from evaluation.jd_cases import (
    EXPECTED_PARSER_VERSION,
    EXPECTED_PROMPT_VERSION,
    EXPECTED_SCHEMA_VERSION,
)

BATCH_ID = "day27-batch-1"

BATCH_PROVIDER = "openai"
BATCH_REQUESTED_MODEL = "gpt-5.6-luna"
BATCH_MAX_OUTPUT_TOKENS = 900
BATCH_REASONING_EFFORT = "none"
BATCH_MAX_REQUESTS = 6
BATCH_BUDGET_USD = 0.01

INPUT_USD_PER_TOKEN = 0.20 / 1_000_000
OUTPUT_USD_PER_TOKEN = 1.20 / 1_000_000
PRICE_NOTE = (
    "input $0.20 and output $1.20 per million tokens, from the official "
    "model page on 2026-09-11; an estimate from usage, not a bill"
)

CHARS_PER_TOKEN_LOWER_BOUND = 3.0
REQUEST_OVERHEAD_TOKENS = 200

RECORD_CONTRACT = (
    ("prompt_version", EXPECTED_PROMPT_VERSION),
    ("parser_version", EXPECTED_PARSER_VERSION),
    ("schema_version", EXPECTED_SCHEMA_VERSION),
    ("provider", BATCH_PROVIDER),
    ("requested_model", BATCH_REQUESTED_MODEL),
    ("requested_max_output_tokens", BATCH_MAX_OUTPUT_TOKENS),
    ("requested_reasoning_effort", BATCH_REASONING_EFFORT),
)

RUNNING_CODE_VERSIONS = (
    ("prompt_version", PROMPT_VERSION, EXPECTED_PROMPT_VERSION),
    ("parser_version", PARSER_VERSION, EXPECTED_PARSER_VERSION),
    ("schema_version", SCHEMA_VERSION, EXPECTED_SCHEMA_VERSION),
)


def record_mismatch(metadata):
    for name, expected in RECORD_CONTRACT:
        actual = getattr(metadata, name)

        if actual != expected:
            return f"{name} is {actual!r}, this batch expects {expected!r}"

    return None


def running_code_mismatch():
    for name, running, expected in RUNNING_CODE_VERSIONS:
        if running != expected:
            return (
                f"the running {name} is {running!r} but this batch was "
                f"approved against {expected!r}"
            )

    return None


def estimated_cost(input_tokens, output_tokens):
    return (
        input_tokens * INPUT_USD_PER_TOKEN
        + output_tokens * OUTPUT_USD_PER_TOKEN
    )


def request_cost_upper_bound(job_posting):
    schema = json.dumps(
        build_schema_format().schema,
        separators=(",", ":"),
    )
    characters = len(build_prompt(job_posting)) + len(schema)
    input_tokens = (
        math.ceil(characters / CHARS_PER_TOKEN_LOWER_BOUND)
        + REQUEST_OVERHEAD_TOKENS
    )

    return estimated_cost(input_tokens, BATCH_MAX_OUTPUT_TOKENS)
