"""Matching a confirmed profile with a parsed job description (Day 28).

match_profile_to_job is the strict entry point of the legacy profile. It
takes a saved, confirmed ProfileResponse and a parsed JobDescriptionRecord
(or an llm outcome, which carries one) and refuses anything less: an
unconfirmed or bare profile, a structured job description without its
record, or a rule fallback. Each required and preferred skill is then
matched against the profile's skill evidence: evidence makes it matched, a
skill that is only listed makes it uncertain, and nothing makes it missing,
which means no evidence in this profile, not that the person lacks it.

coverage_score is the scoring that this entry point and the experience
analysis share (SCORING_VERSION). It is skill coverage, not a hiring
probability.
"""
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

from app.jd_outcome import JobDescriptionOutcome, ParseOutcomeSource
from app.jd_record import JobDescriptionRecord
from app.schemas import (
    SCHEMA_VERSION,
    CoverageScore,
    JobRequirement,
    MatchConclusion,
    MatchInputs,
    MatchResult,
    ProfileResponse,
    RequirementMatch,
    StudentProfile,
)

MATCH_VERSION = "1"

SCORING_VERSION = "1"

REQUIRED_WEIGHT = Decimal("0.80")
PREFERRED_WEIGHT = Decimal("0.20")
SCORE_STEP = Decimal("0.01")


class MatchInputError(Exception):
    pass


def _confirmed_profile(profile: object) -> ProfileResponse:
    if not isinstance(profile, ProfileResponse):
        raise MatchInputError(
            "Matching needs a saved profile, not bare profile data."
        )

    if not profile.is_confirmed:
        raise MatchInputError("Matching needs a confirmed profile.")

    return profile


def _parsed_record(job_description: object) -> JobDescriptionRecord:
    if isinstance(job_description, JobDescriptionOutcome):
        if job_description.source is not ParseOutcomeSource.LLM:
            raise MatchInputError(
                "A rule fallback is not a parsed job description and "
                "cannot be matched."
            )

        job_description = job_description.record

    if not isinstance(job_description, JobDescriptionRecord):
        raise MatchInputError(
            "Matching needs a parsed job description record with its "
            "metadata."
        )

    if job_description.metadata.schema_version != SCHEMA_VERSION:
        raise MatchInputError(
            "The job description record uses an unsupported schema "
            "version."
        )

    return job_description


def _validated_inputs(
    profile: ProfileResponse,
    record: JobDescriptionRecord,
) -> MatchInputs:
    metadata = record.metadata

    return MatchInputs(
        profile_id=profile.id,
        profile_updated_at=profile.updated_at,
        job_posting_sha256=metadata.job_posting_sha256,
        prompt_version=metadata.prompt_version,
        parser_version=metadata.parser_version,
        schema_version=metadata.schema_version,
    )


def match_profile_to_job(
    profile: ProfileResponse,
    record: JobDescriptionRecord,
) -> MatchResult:
    profile = _confirmed_profile(profile)
    record = _parsed_record(record)
    job_description = record.job_description
    required = _matches(job_description.required_skills, profile.profile_data)
    preferred = _matches(job_description.preferred_skills, profile.profile_data)

    return MatchResult(
        match_version=MATCH_VERSION,
        scoring_version=SCORING_VERSION,
        inputs=_validated_inputs(profile, record),
        required=required,
        preferred=preferred,
        score=coverage_score(
            [match.conclusion for match in required],
            [match.conclusion for match in preferred],
        ),
    )


def requirement_key(text: str) -> str:
    """How two skill names are compared: case and spacing aside."""
    return " ".join(text.casefold().split())


def distinct_requirements(
    requirements: Sequence[JobRequirement],
) -> list[JobRequirement]:
    """Each requirement once: a posting that names one twice counts it once."""
    seen = set()
    distinct = []

    for requirement in requirements:
        key = requirement_key(requirement.text)

        if key not in seen:
            seen.add(key)
            distinct.append(requirement)

    return distinct


def _matches(
    requirements: Sequence[JobRequirement],
    profile_data: StudentProfile,
) -> tuple[RequirementMatch, ...]:
    return tuple(
        _match(requirement, profile_data)
        for requirement in distinct_requirements(requirements)
    )


def _match(
    requirement: JobRequirement,
    profile_data: StudentProfile,
) -> RequirementMatch:
    wanted = requirement_key(requirement.text)

    # The first evidence of a skill matches; more of it adds nothing.
    for index, evidence in enumerate(profile_data.skill_evidence):
        if requirement_key(evidence.skill) == wanted:
            return RequirementMatch(
                requirement=requirement,
                conclusion=MatchConclusion.MATCHED,
                evidence_index=index,
            )

    if any(requirement_key(skill) == wanted for skill in profile_data.skills):
        # Listed as a skill, but nothing in the profile shows it.
        conclusion = MatchConclusion.UNCERTAIN
    else:
        conclusion = MatchConclusion.MISSING

    return RequirementMatch(
        requirement=requirement,
        conclusion=conclusion,
        evidence_index=None,
    )


def coverage_score(
    required: Sequence[MatchConclusion],
    preferred: Sequence[MatchConclusion],
) -> CoverageScore:
    """The share of matched requirements per group, and both weighed 80 / 20.

    A group without requirements has no score, and the overall score then
    is the other group's alone; with neither, there is no score at all.
    """
    required_score = _group_score(required)
    preferred_score = _group_score(preferred)
    weighted = [
        (score, weight)
        for score, weight in (
            (required_score, REQUIRED_WEIGHT),
            (preferred_score, PREFERRED_WEIGHT),
        )
        if score is not None
    ]
    overall_score = None

    if weighted:
        overall_score = _rounded(
            sum(score * weight for score, weight in weighted)
            / sum(weight for _, weight in weighted)
        )

    return CoverageScore(
        required_total=len(required),
        required_matched=_count(required, MatchConclusion.MATCHED),
        required_uncertain=_count(required, MatchConclusion.UNCERTAIN),
        preferred_total=len(preferred),
        preferred_matched=_count(preferred, MatchConclusion.MATCHED),
        preferred_uncertain=_count(preferred, MatchConclusion.UNCERTAIN),
        required_score=required_score,
        preferred_score=preferred_score,
        overall_score=overall_score,
    )


def _count(conclusions: Sequence[MatchConclusion], wanted: MatchConclusion) -> int:
    return sum(1 for conclusion in conclusions if conclusion == wanted)


def _group_score(conclusions: Sequence[MatchConclusion]) -> Decimal | None:
    if not conclusions:
        return None

    matched = _count(conclusions, MatchConclusion.MATCHED)

    return _rounded(Decimal(matched) / Decimal(len(conclusions)) * Decimal(100))


def _rounded(value: Decimal) -> Decimal:
    return value.quantize(SCORE_STEP, rounding=ROUND_HALF_UP)
