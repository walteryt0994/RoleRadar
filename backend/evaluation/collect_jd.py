import argparse
import sys
from dataclasses import replace
from pathlib import Path

from app.ai_provider import OpenAIProvider, load_openai_config
from app.jd_outcome import (
    ParseOutcomeSource,
    parse_job_description_with_fallback,
)
from app.jd_record import export_record
from evaluation.batch_contract import (
    BATCH_BUDGET_USD,
    BATCH_ID,
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_MAX_REQUESTS,
    BATCH_REASONING_EFFORT,
    BATCH_REQUESTED_MODEL,
    PRICE_NOTE,
    estimated_cost,
    record_mismatch,
    request_cost_upper_bound,
    running_code_mismatch,
)
from evaluation.compare_jd import load_records, mismatch_reason
from evaluation.jd_cases import CASES

RECORDS_DIR = Path("records")

CASE_IDS = tuple(case["case_id"] for case in CASES)


def parse_arguments(arguments):
    parser = argparse.ArgumentParser(
        prog="collect_jd",
        description="Collect the frozen Day 27 cases from the provider.",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="actually send requests; without it this is a dry run",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="CASE_ID",
        choices=CASE_IDS,
        help="restrict the batch to one case; may be repeated",
    )

    return parser.parse_args(arguments)


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


def spent_so_far(loaded, lines):
    spent = 0.0

    for path, record in loaded:
        metadata = record.metadata

        if record_mismatch(metadata) is not None:
            continue

        if metadata.input_tokens is None or metadata.output_tokens is None:
            lines.append(
                f"warning: {path.name} belongs to this batch but reports "
                f"no token counts, so its cost is not in the total"
            )
            continue

        spent += estimated_cost(
            metadata.input_tokens,
            metadata.output_tokens,
        )

    return spent


def preflight(arguments, lines):
    options = parse_arguments(arguments)

    running = running_code_mismatch()

    if running is not None:
        lines.append(f"refusing to run: {running}")

        return None, None

    config = load_openai_config()

    if config.model != BATCH_REQUESTED_MODEL:
        lines.append(
            f"refusing to run: OPENAI_MODEL is {config.model!r} but batch "
            f"{BATCH_ID} uses {BATCH_REQUESTED_MODEL!r}"
        )

        return None, None

    loaded, unreadable = load_records(RECORDS_DIR)

    for path, error_name in unreadable:
        lines.append(f"warning: {path.name} is unreadable ({error_name})")

    cases = pending_cases(loaded, lines)

    if options.only:
        cases = [case for case in cases if case["case_id"] in options.only]
        lines.append(f"limited to {', '.join(options.only)} by --only")

    spent = spent_so_far(loaded, lines)
    remaining = BATCH_BUDGET_USD - spent
    upper_bound = sum(
        request_cost_upper_bound(case["job_posting"]) for case in cases
    )

    lines.append(
        f"batch {BATCH_ID}: model {BATCH_REQUESTED_MODEL}, reasoning "
        f"{BATCH_REASONING_EFFORT}, max_output_tokens "
        f"{BATCH_MAX_OUTPUT_TOKENS}, max_retries 0, store false"
    )
    lines.append(
        f"cases to collect: {len(cases)} of {len(CASES)}, batch limit "
        f"{BATCH_MAX_REQUESTS} requests"
    )
    lines.append(
        f"budget ${BATCH_BUDGET_USD:.6f}, already spent ${spent:.6f}, "
        f"remaining ${remaining:.6f}"
    )
    lines.append(
        f"worst case for the pending cases ${upper_bound:.6f}, counted as "
        f"the full prompt, job posting and schema at "
        f"{BATCH_MAX_OUTPUT_TOKENS} output tokens"
    )
    lines.append(PRICE_NOTE)

    if len(cases) > BATCH_MAX_REQUESTS:
        lines.append("refusing to run: more cases than the batch limit")

        return None, None

    if cases and remaining <= 0:
        lines.append("refusing to run: the batch budget is already used up")

        return None, None

    for case in cases:
        target = RECORDS_DIR / f"{case['case_id']}.json"

        if target.exists():
            lines.append(
                f"refusing to run: {target} exists but does not match batch "
                f"{BATCH_ID}; move it aside first"
            )

            return None, None

    return config, (cases, remaining)


def collect(config, plan, lines):
    cases, remaining = plan
    provider = OpenAIProvider(replace(config, max_retries=0))

    sent = 0

    for case in cases:
        if sent >= BATCH_MAX_REQUESTS:
            lines.append("stopping: reached the batch request limit")
            break

        reserved = request_cost_upper_bound(case["job_posting"])

        if reserved > remaining:
            lines.append(
                f"stopping before {case['case_id']}: the remaining "
                f"${remaining:.6f} does not cover its worst case "
                f"${reserved:.6f}"
            )
            break

        sent += 1
        outcome = parse_job_description_with_fallback(
            provider,
            case["job_posting"],
            max_output_tokens=BATCH_MAX_OUTPUT_TOKENS,
            reasoning_effort=BATCH_REASONING_EFFORT,
        )

        if outcome.source is ParseOutcomeSource.RULE_FALLBACK:
            remaining -= reserved
            lines.append(
                f"{case['case_id']}: failed as "
                f"{outcome.failure_category.value}; this request may still "
                f"have consumed tokens, usage unknown, so its worst case "
                f"${reserved:.6f} is charged against the budget"
            )
            lines.append("stopping the remaining cases for diagnosis")
            break

        metadata = outcome.record.metadata

        if metadata.input_tokens is None or metadata.output_tokens is None:
            remaining -= reserved
            export_record(
                outcome.record,
                RECORDS_DIR / f"{case['case_id']}.json",
            )
            lines.append(
                f"{case['case_id']}: saved but usage is unknown, charging "
                f"its worst case ${reserved:.6f} and stopping"
            )
            break

        actual = estimated_cost(
            metadata.input_tokens,
            metadata.output_tokens,
        )
        remaining -= actual
        path = export_record(
            outcome.record,
            RECORDS_DIR / f"{case['case_id']}.json",
        )
        lines.append(
            f"{case['case_id']}: saved {path.name}, "
            f"{metadata.input_tokens} in, {metadata.output_tokens} out, "
            f"${actual:.6f}, {metadata.latency_seconds:.2f}s, "
            f"returned model {metadata.returned_model}"
        )

        if actual > reserved:
            lines.append(
                f"stopping: {case['case_id']} cost ${actual:.6f}, more than "
                f"its reserved ${reserved:.6f}"
            )
            break

    lines.append(f"requests sent: {sent}")
    lines.append(f"budget remaining: ${remaining:.6f}")
    lines.append(PRICE_NOTE)

    return sent


def main(argv):
    lines = []
    config, plan = preflight(argv[1:], lines)

    if config is None:
        for line in lines:
            print(line)

        return 1

    cases, _ = plan

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

    collect(config, plan, lines)

    for line in lines:
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
