"""The call contract of one Day 27 collection batch that is now closed.

The six fixed cases were collected under two approvals that have both been
consumed, and the live collection entry point has been sealed: nothing in
this package can send a request any more. These constants record what that
finished batch used, so the offline comparison can tell a record of this
batch apart from a record produced under other settings.

They are not a standing authorisation and they were never an enforced cost
guarantee. A further real request needs a new approval with its own model,
inputs, request count and cost boundary, and a live entry point built for
that batch.

The returned model is deliberately not part of the contract; a provider may
answer with a dated snapshot alias, which is recorded as observed rather
than forced to equal the requested name.
"""

from app.jd_service import PARSER_VERSION, PROMPT_VERSION
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

BATCH_APPROVED_REQUESTS = 6
BATCH_APPROVED_BUDGET_USD = 0.01

INPUT_USD_PER_TOKEN = 0.20 / 1_000_000
OUTPUT_USD_PER_TOKEN = 1.20 / 1_000_000
PRICE_NOTE = (
    "input $0.20 and output $1.20 per million tokens, from the official "
    "model page on 2026-09-11; an estimate from usage, not a bill"
)

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
                f"collected against {expected!r}"
            )

    return None


def estimated_cost(input_tokens, output_tokens):
    return (
        input_tokens * INPUT_USD_PER_TOKEN
        + output_tokens * OUTPUT_USD_PER_TOKEN
    )
