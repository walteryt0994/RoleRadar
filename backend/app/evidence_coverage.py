"""岗位要求依据覆盖 (evidence_coverage_v1): a descriptive count shown above an analysis (owner redesign, plan 7).

It is not the weighted skill coverage of app.match_service (CoverageScore: required 80%, preferred 20%), which stays
as it is, with its own name, meaning and versions; this module neither reads nor changes it. It says, of the skill
requirements an analysis counted, how many the chosen material supports:

- the denominator is the REQUIRED and PREFERRED requirements of the analysis, one per requirement_id, as the analysis
  checked and finalized them (an AND / OR / conditional requirement is one requirement; nothing is split again);
- the numerator is those whose conclusion is SUPPORTED; NEEDS_CONFIRMATION and NO_EVIDENCE stay in the denominator
  only, counted apart; several citations of one requirement count once;
- HARD_CONSTRAINT, UNCERTAIN, the preferences and anything else are left out (a hard constraint in conflict is shown
  apart, whatever the percentage says);
- an analysis that is not complete (failed, degraded, cancelled, stale, refused), a requirement_id given twice, a
  conclusion it does not know, or no countable requirement at all gives no percentage, only the reason;
- the percentage is rounded half up to at most one decimal, without a trailing ".0"; a share below 100% never reads
  "100%" (it reads "＞99.9%"), and one above 0 never reads "0%" (it reads "＜0.1%"). The two counts always go with it.

It is computed from the findings alone, on the server, with no model request, and only from a result that says it is
complete (an AnalysisResult with status COMPLETE). A kept analysis carries the count it was saved with
(display_metrics, coverage_record): its version and the four counts, checked again against its kept findings when it
is shown. A kept analysis without them, or with another version, shows no percentage (NOT_RECORDED): nothing is
counted again for it, and never from the experiences as they are now.
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from app.analysis_contracts import AnalysisStatus, EvidenceConclusion, RequirementGroup

VERSION = "evidence_coverage_v1"
COUNTED_GROUPS = (RequirementGroup.REQUIRED, RequirementGroup.PREFERRED)
CONCLUSIONS = {conclusion.value for conclusion in EvidenceConclusion}


class NoCoverage(StrEnum):
    """Why an analysis shows no percentage."""

    NOT_COMPLETE = "not_complete"
    NO_SKILLS = "no_skills"
    INCONSISTENT = "inconsistent"
    # A kept analysis saved without this count (or with another version of it): not made up afterwards.
    NOT_RECORDED = "not_recorded"


@dataclass(frozen=True)
class EvidenceCoverage:
    supported: int
    total: int
    needs_confirmation: int
    no_evidence: int
    version: str = VERSION

    @property
    def percent(self) -> str:
        """The share as a page writes it, without the percent sign: "50", "33.3", "＞99.9", "＜0.1"."""
        exact = Decimal(self.supported) * 100 / Decimal(self.total)

        # Judged on the exact share, before rounding: 1 of 2,000 (0.05%) is below 0.1%, never "0.1%".
        if 0 < exact < Decimal("0.1"):
            return "＜0.1"

        rounded = exact.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

        if exact < 100 and rounded >= 100:
            return "＞99.9"

        text = f"{rounded:f}"

        return text[:-2] if text.endswith(".0") else text


def _value(item, name: str):
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def counted_findings(analysis) -> list:
    """The findings the share is made of: the REQUIRED and PREFERRED requirements, in their order."""
    return [finding for finding in analysis.findings if finding.group in COUNTED_GROUPS]


def _counted(findings) -> EvidenceCoverage | NoCoverage:
    """The count of these findings, or why there is none (a requirement given twice, an unknown conclusion, none)."""
    seen: set[str] = set()
    counts = {conclusion: 0 for conclusion in CONCLUSIONS}

    for finding in findings:
        group, conclusion, requirement = _value(finding, "group"), _value(finding, "conclusion"), _value(finding, "requirement_id")

        if not requirement or requirement in seen:
            # A requirement given twice (or without its id) is an analysis the count cannot trust: no percentage,
            # rather than quietly counting it once.
            return NoCoverage.INCONSISTENT

        seen.add(requirement)

        if str(group) not in {group.value for group in COUNTED_GROUPS}:
            continue

        if str(conclusion) not in CONCLUSIONS:
            return NoCoverage.INCONSISTENT

        counts[str(conclusion)] += 1

    total = sum(counts.values())

    if total == 0:
        return NoCoverage.NO_SKILLS

    return EvidenceCoverage(
        supported=counts[EvidenceConclusion.SUPPORTED.value],
        total=total,
        needs_confirmation=counts[EvidenceConclusion.NEEDS_CONFIRMATION.value],
        no_evidence=counts[EvidenceConclusion.NO_EVIDENCE.value],
    )


def coverage_record(analysis) -> dict:
    """What a kept analysis keeps of this count (display_metrics["evidence_coverage"]): the version and the four counts
    of a complete result, or the reason it has none. Only a result with status COMPLETE is counted."""
    shown = evidence_coverage(analysis)

    if isinstance(shown, NoCoverage):
        return {"version": VERSION, "none": shown.value}

    return {
        "version": VERSION,
        "supported": shown.supported,
        "total": shown.total,
        "needs_confirmation": shown.needs_confirmation,
        "no_evidence": shown.no_evidence,
    }


def _kept(analysis) -> EvidenceCoverage | NoCoverage:
    """The count a kept analysis was saved with, if it was saved with this version of it and its kept findings still
    give the same counts; otherwise why none is shown."""
    metrics = _value(analysis, "display_metrics")
    record = metrics.get("evidence_coverage") if isinstance(metrics, dict) else None

    if not isinstance(record, dict) or record.get("version") != VERSION:
        return NoCoverage.NOT_RECORDED

    findings = _value(analysis, "findings")

    if findings is None:
        return NoCoverage.INCONSISTENT

    counted = _counted(findings)

    if "none" in record:
        # Saved with no percentage: shown with the same reason, if the kept findings still say so.
        reason = record["none"]

        if reason not in {item.value for item in NoCoverage} or reason == NoCoverage.NOT_RECORDED.value:
            return NoCoverage.INCONSISTENT

        if isinstance(counted, EvidenceCoverage):
            return NoCoverage.INCONSISTENT

        return NoCoverage(reason)

    names = ("supported", "total", "needs_confirmation", "no_evidence")

    if not all(type(record.get(name)) is int for name in names):
        return NoCoverage.INCONSISTENT

    kept = EvidenceCoverage(**{name: record[name] for name in names})

    if not isinstance(counted, EvidenceCoverage) or counted != kept:
        # The kept counts and the kept findings no longer agree: neither is shown as the count.
        return NoCoverage.INCONSISTENT

    return kept


def evidence_coverage(analysis) -> EvidenceCoverage | NoCoverage:
    """The count of an analysis, or why there is none.

    A result (anything with a status, an AnalysisResult) is counted from its findings only when its status is COMPLETE.
    A kept analysis (no status) shows only the count it was saved with (display_metrics), checked against its kept
    findings; one saved without it shows none (NOT_RECORDED)."""
    status = _value(analysis, "status")

    if status is None:
        return _kept(analysis)

    if str(status) != AnalysisStatus.COMPLETE.value:
        return NoCoverage.NOT_COMPLETE

    findings = _value(analysis, "findings")

    if findings is None:
        return NoCoverage.INCONSISTENT

    return _counted(findings)
