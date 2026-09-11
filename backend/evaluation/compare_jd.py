import sys
from pathlib import Path

from app.jd_record import (
    JobDescriptionRecordError,
    fingerprint_job_posting,
    load_record,
)
from app.parser import extract_skills
from evaluation.jd_cases import (
    CASE_SET_VERSION,
    CASES,
    EXPECTED_PARSER_VERSION,
    EXPECTED_PROMPT_VERSION,
    EXPECTED_SCHEMA_VERSION,
    FROZEN_ON,
)

DEFAULT_RECORDS_DIR = Path("records")

REQUIREMENT_FIELDS = (
    "required_skills",
    "preferred_skills",
    "hard_constraints",
    "uncertain_requirements",
)

EXPECTED_VERSIONS = (
    ("prompt_version", EXPECTED_PROMPT_VERSION),
    ("parser_version", EXPECTED_PARSER_VERSION),
    ("schema_version", EXPECTED_SCHEMA_VERSION),
)


def load_records(directory):
    loaded = []
    unreadable = []

    if not Path(directory).is_dir():
        return loaded, unreadable

    for path in sorted(Path(directory).glob("*.json")):
        try:
            loaded.append((path, load_record(path)))
        except JobDescriptionRecordError as error:
            unreadable.append((path, type(error).__name__))

    return loaded, unreadable


def mismatch_reason(record, case):
    fingerprint = fingerprint_job_posting(case["job_posting"])

    if record.metadata.job_posting_sha256 != fingerprint:
        return "different posting"

    for name, expected in EXPECTED_VERSIONS:
        actual = getattr(record.metadata, name)

        if actual != expected:
            return f"{name} is {actual}, the case set expects {expected}"

    return None


def select_records(case, loaded):
    matched = []
    rejected = []

    for path, record in loaded:
        reason = mismatch_reason(record, case)

        if reason is None:
            matched.append((path, record))
        elif reason != "different posting":
            rejected.append((path, reason))

    return matched, rejected


def _texts(requirements):
    return [requirement.text for requirement in requirements]


def _mentions(values, term):
    needle = term.lower()

    return any(needle in value.lower() for value in values)


def check_case(case, job_description):
    failures = []

    for field, expected in case.get("expect_equals", {}).items():
        actual = getattr(job_description, field)
        actual = getattr(actual, "value", actual)

        if actual != expected:
            failures.append(f"{field} is {actual!r}, expected {expected!r}")

    for field, needles in case.get("expect_contains", {}).items():
        actual = getattr(job_description, field)

        for needle in needles:
            if actual is None or needle.lower() not in actual.lower():
                failures.append(
                    f"{field} is {actual!r}, expected it to mention {needle!r}"
                )

    for field in case.get("expect_null", "").split():
        actual = getattr(job_description, field)

        if actual is not None:
            failures.append(f"{field} should stay null, got {actual!r}")

    for field in case.get("expect_empty", "").split():
        actual = getattr(job_description, field)

        if actual:
            failures.append(
                f"{field} should be empty, got {len(actual)} items"
            )

    for field in case.get("expect_non_empty", "").split():
        if not getattr(job_description, field):
            failures.append(f"{field} should not be empty")

    for field, terms in case.get("include", {}).items():
        values = _texts(getattr(job_description, field))

        for term in terms:
            if not _mentions(values, term):
                failures.append(f"{field} is missing {term!r}")

    for field, terms in case.get("exclude", {}).items():
        values = _texts(getattr(job_description, field))

        for term in terms:
            if _mentions(values, term):
                failures.append(f"{field} must not contain {term!r}")

    for term in case.get("expect_absent_everywhere", ()):
        hits = [
            field
            for field in REQUIREMENT_FIELDS
            if _mentions(_texts(getattr(job_description, field)), term)
        ]

        if hits:
            failures.append(
                f"{term!r} must not appear in any requirement list, "
                f"found in {', '.join(hits)}"
            )

    return failures


def graded_check_count(case):
    return (
        len(case.get("expect_equals", {}))
        + sum(len(v) for v in case.get("expect_contains", {}).values())
        + len(case.get("expect_null", "").split())
        + len(case.get("expect_empty", "").split())
        + len(case.get("expect_non_empty", "").split())
        + sum(len(v) for v in case.get("include", {}).values())
        + sum(len(v) for v in case.get("exclude", {}).values())
        + len(case.get("expect_absent_everywhere", ()))
    )


def _usage(value):
    return "unknown" if value is None else str(value)


def _call_summary(metadata):
    return (
        f"model {metadata.requested_model} -> {metadata.returned_model}, "
        f"max_output_tokens "
        f"{_usage(metadata.requested_max_output_tokens)}, "
        f"reasoning {_usage(metadata.requested_reasoning_effort)}, "
        f"tokens in {_usage(metadata.input_tokens)} "
        f"out {_usage(metadata.output_tokens)}, "
        f"latency {metadata.latency_seconds:.2f}s"
    )


def report_case(case, loaded, lines):
    lines.append(f"{case['case_id']}  {case['focus']}")

    rule_skills = tuple(extract_skills(case["job_posting"]))
    frozen = case["rule_expected_skills"]
    rule_state = "matches the frozen output" if rule_skills == frozen else (
        f"DIFFERS from the frozen output {list(frozen)}"
    )
    lines.append(f"  rule  : {list(rule_skills)}  ({rule_state})")

    matched, rejected = select_records(case, loaded)

    for path, reason in rejected:
        lines.append(f"  note  : skipped {path.name}, {reason}")

    if not matched:
        lines.append("  ai    : NOT MEASURED, no saved record for this "
                     "posting at the expected versions")
        measured = False
        passed = failed = 0
    elif len(matched) > 1:
        names = ", ".join(path.name for path, _ in matched)
        lines.append(f"  ai    : AMBIGUOUS, {len(matched)} records match "
                     f"({names}); resolve before reporting")
        measured = False
        passed = failed = 0
    else:
        path, record = matched[0]
        failures = check_case(case, record.job_description)
        total = graded_check_count(case)
        failed = len(failures)
        passed = total - failed
        measured = True
        lines.append(f"  ai    : {path.name}, {total} graded checks, "
                     f"{passed} passed, {failed} failed")

        for failure in failures:
            lines.append(f"          - {failure}")

        lines.append(f"  call  : {_call_summary(record.metadata)}")

    for field, reason in sorted(case["not_graded"].items()):
        lines.append(f"  skip  : {field}, {reason}")

    for check in case["manual_checks"]:
        lines.append(f"  manual: {check}")

    lines.append("")

    return measured, passed, failed


def build_report(records_dir):
    loaded, unreadable = load_records(records_dir)

    lines = [
        "RoleRadar Day 27 offline rule-vs-LLM comparison",
        f"case set {CASE_SET_VERSION} frozen on {FROZEN_ON}",
        f"expected versions: prompt {EXPECTED_PROMPT_VERSION}, "
        f"parser {EXPECTED_PARSER_VERSION}, "
        f"schema {EXPECTED_SCHEMA_VERSION}",
        f"records directory: {records_dir} "
        f"({len(loaded)} readable, {len(unreadable)} unreadable)",
        "this script never contacts a provider",
        "",
    ]

    for path, error_name in unreadable:
        lines.append(f"unreadable: {path.name} rejected as {error_name}")

    if unreadable:
        lines.append("")

    measured = 0
    passed = 0
    failed = 0
    missing = []

    for case in CASES:
        case_measured, case_passed, case_failed = report_case(
            case, loaded, lines
        )

        if case_measured:
            measured += 1
            passed += case_passed
            failed += case_failed
        else:
            missing.append(case["case_id"])

    lines.append("summary")
    lines.append(f"  cases measured: {measured} of {len(CASES)}")
    lines.append(f"  graded checks on measured cases: "
                 f"{passed} passed, {failed} failed")

    if missing:
        lines.append(f"  not measured: {', '.join(missing)}")

    lines.append("  counts cover these fixed cases only and are not an "
                 "accuracy rate")
    lines.append("  manual semantic checks above are not automated")

    return lines


def main(argv):
    records_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_RECORDS_DIR

    for line in build_report(records_dir):
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
