import sys
from dataclasses import replace
from pathlib import Path

from app.ai_provider import OpenAIProvider, load_openai_config
from app.jd_outcome import (
    ParseOutcomeSource,
    parse_job_description_with_fallback,
)
from app.jd_record import export_record
from evaluation.compare_jd import load_records, mismatch_reason
from evaluation.jd_cases import CASES

APPROVED_MODEL = "gpt-5.6-luna"
APPROVED_MAX_OUTPUT_TOKENS = 900
APPROVED_REASONING_EFFORT = "none"
APPROVED_MAX_REQUESTS = 6
APPROVED_BUDGET_USD = 0.01

INPUT_USD_PER_TOKEN = 0.20 / 1_000_000
OUTPUT_USD_PER_TOKEN = 1.20 / 1_000_000
PRICE_NOTE = (
    "prices as reported on 2026-09-08, an estimate from usage, not a bill"
)

RECORDS_DIR = Path("records")


def pending_cases(loaded, lines):
    pending = []

    for case in CASES:
        matched = [
            path
            for path, record in loaded
            if mismatch_reason(record, case) is None
        ]

        if matched:
            lines.append(
                f"skip {case['case_id']}: already collected as "
                f"{matched[0].name}"
            )
            continue

        pending.append(case)

    return pending


def preflight(argv, lines):
    config = load_openai_config()

    if config.model != APPROVED_MODEL:
        lines.append(
            f"refusing to run: OPENAI_MODEL is {config.model!r} but the "
            f"approved batch uses {APPROVED_MODEL!r}"
        )
        return None, None

    loaded, unreadable = load_records(RECORDS_DIR)

    for path, error_name in unreadable:
        lines.append(f"warning: {path.name} is unreadable ({error_name})")

    cases = pending_cases(loaded, lines)

    only = tuple(
        argv[index + 1]
        for index, value in enumerate(argv)
        if value == "--only" and index + 1 < len(argv)
    )

    if only:
        known = {case["case_id"] for case in CASES}
        unknown = [case_id for case_id in only if case_id not in known]

        if unknown:
            lines.append(f"refusing to run: unknown case id {unknown}")
            return None, None

        cases = [case for case in cases if case["case_id"] in only]
        lines.append(f"limited to {', '.join(only)} by --only")

    lines.append(
        f"model {APPROVED_MODEL}, reasoning {APPROVED_REASONING_EFFORT}, "
        f"max_output_tokens {APPROVED_MAX_OUTPUT_TOKENS}, max_retries 0, "
        f"store false"
    )
    lines.append(
        f"cases to collect: {len(cases)} of {len(CASES)}, approved limit "
        f"{APPROVED_MAX_REQUESTS} requests and {APPROVED_BUDGET_USD} USD"
    )
    lines.append(PRICE_NOTE)

    if len(cases) > APPROVED_MAX_REQUESTS:
        lines.append("refusing to run: more cases than the approved limit")
        return None, None

    for case in cases:
        target = RECORDS_DIR / f"{case['case_id']}.json"

        if target.exists():
            lines.append(
                f"refusing to run: {target} exists but does not match the "
                f"current posting and versions; move it aside first"
            )
            return None, None

    return config, cases


def collect(config, cases, lines):
    provider = OpenAIProvider(replace(config, max_retries=0))

    sent = 0
    input_tokens = 0
    output_tokens = 0
    unknown_usage = False

    for case in cases:
        if sent >= APPROVED_MAX_REQUESTS:
            lines.append("stopping: reached the approved request limit")
            break

        sent += 1
        outcome = parse_job_description_with_fallback(
            provider,
            case["job_posting"],
            max_output_tokens=APPROVED_MAX_OUTPUT_TOKENS,
            reasoning_effort=APPROVED_REASONING_EFFORT,
        )

        if outcome.source is ParseOutcomeSource.RULE_FALLBACK:
            unknown_usage = True
            lines.append(
                f"{case['case_id']}: failed as "
                f"{outcome.failure_category.value}; this request may still "
                f"have consumed tokens, usage unknown"
            )
            lines.append("stopping the remaining cases for diagnosis")
            break

        path = export_record(
            outcome.record,
            RECORDS_DIR / f"{case['case_id']}.json",
        )
        metadata = outcome.record.metadata

        if metadata.input_tokens is None or metadata.output_tokens is None:
            unknown_usage = True
            usage = "usage unknown"
        else:
            input_tokens += metadata.input_tokens
            output_tokens += metadata.output_tokens
            usage = (
                f"{metadata.input_tokens} in, "
                f"{metadata.output_tokens} out"
            )

        lines.append(
            f"{case['case_id']}: saved {path.name}, {usage}, "
            f"{metadata.latency_seconds:.2f}s, "
            f"returned model {metadata.returned_model}"
        )

    cost = (
        input_tokens * INPUT_USD_PER_TOKEN
        + output_tokens * OUTPUT_USD_PER_TOKEN
    )

    lines.append(f"requests sent: {sent}")
    lines.append(f"counted tokens: {input_tokens} in, {output_tokens} out")
    lines.append(f"estimated cost of counted tokens: ${cost:.6f}")
    lines.append(PRICE_NOTE)

    if unknown_usage:
        lines.append(
            "some usage is unknown and is not included in the estimate"
        )

    if cost > APPROVED_BUDGET_USD:
        lines.append("warning: the estimate exceeds the approved budget")

    return sent


def main(argv):
    lines = []
    config, cases = preflight(argv, lines)

    if config is None:
        for line in lines:
            print(line)

        return 1

    if "--send" not in argv[1:]:
        for case in cases:
            lines.append(
                f"would send {case['case_id']}, "
                f"{len(case['job_posting'])} characters of posting"
            )

        lines.append("dry run: no request was sent, pass --send to collect")

        for line in lines:
            print(line)

        return 0

    collect(config, cases, lines)

    for line in lines:
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
