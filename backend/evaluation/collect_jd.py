"""Offline preview of the Day 27 collection batch, whose live entry is sealed.

The six fixed cases were collected under two approvals that have both been
consumed. This module can no longer send anything: it does not import a
provider, it never loads an API key, and it has no collection loop. It only
reports which cases already have a record of this batch, and the subtotal of
the usage those records report.

A future batch needs a new approval and a live entry point built for it,
with its own model, inputs, request count and cost boundary. Reusing these
constants, deleting records, or restarting a process is not an approval.
"""

import argparse
import sys
from pathlib import Path

from evaluation.batch_contract import (
    BATCH_APPROVED_BUDGET_USD,
    BATCH_APPROVED_REQUESTS,
    BATCH_ID,
    BATCH_MAX_OUTPUT_TOKENS,
    BATCH_REASONING_EFFORT,
    BATCH_REQUESTED_MODEL,
    PRICE_NOTE,
    estimated_cost,
    record_mismatch,
    running_code_mismatch,
)
from evaluation.compare_jd import load_records, mismatch_reason
from evaluation.jd_cases import CASES

RECORDS_DIR = Path("records")

CASE_IDS = tuple(case["case_id"] for case in CASES)

SEALED_NOTICE = (
    f"batch {BATCH_ID} is closed: its live collection entry point has been "
    f"removed and this tool cannot send a request"
)


def parse_arguments(arguments):
    parser = argparse.ArgumentParser(
        prog="collect_jd",
        description=(
            "Offline preview of the closed Day 27 collection batch. "
            "This tool cannot send requests."
        ),
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="refused: the live entry point for this batch is sealed",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="CASE_ID",
        choices=CASE_IDS,
        help="restrict the preview to one case; may be repeated",
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
                f"collected {case['case_id']}: {matched[0].name}"
            )
            continue

        pending.append(case)

    return pending


def known_usage(loaded, lines):
    spent = 0.0
    unknown = 0

    for path, record in loaded:
        metadata = record.metadata

        if record_mismatch(metadata) is not None:
            continue

        if metadata.input_tokens is None or metadata.output_tokens is None:
            unknown += 1
            lines.append(
                f"warning: {path.name} belongs to this batch but reports "
                f"no token counts"
            )
            continue

        spent += estimated_cost(
            metadata.input_tokens,
            metadata.output_tokens,
        )

    return spent, unknown


def build_preview(arguments):
    lines = []
    options = parse_arguments(arguments)

    if options.send:
        lines.append(f"refusing to run: {SEALED_NOTICE}")

        return lines, 1

    loaded, unreadable = load_records(RECORDS_DIR)

    for path, error_name in unreadable:
        lines.append(f"warning: {path.name} is unreadable ({error_name})")

    cases = pending_cases(loaded, lines)

    if options.only:
        cases = [case for case in cases if case["case_id"] in options.only]
        lines.append(f"limited to {', '.join(options.only)} by --only")

    spent, unknown = known_usage(loaded, lines)
    running = running_code_mismatch()

    lines.append(SEALED_NOTICE)
    lines.append(
        f"batch settings on record: model {BATCH_REQUESTED_MODEL}, "
        f"reasoning {BATCH_REASONING_EFFORT}, max_output_tokens "
        f"{BATCH_MAX_OUTPUT_TOKENS}, max_retries 0, store false"
    )
    lines.append(
        f"approved at the time: {BATCH_APPROVED_REQUESTS} requests and "
        f"${BATCH_APPROVED_BUDGET_USD:.6f}, both consumed"
    )
    lines.append(
        f"subtotal of the usage these records report: ${spent:.6f}"
    )
    lines.append(
        "the two failed requests of this batch saved no record, so their "
        "cost is unknown and no remaining balance is derived here"
    )
    lines.append(PRICE_NOTE)

    if running is not None:
        lines.append(f"note: {running}")

    for case in cases:
        lines.append(
            f"no record of this batch for {case['case_id']}, "
            f"{len(case['job_posting'])} characters of posting"
        )

    if not cases:
        lines.append("every case has a record of this batch")

    return lines, 0


def main(argv):
    lines, status = build_preview(argv[1:])

    for line in lines:
        print(line)

    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
