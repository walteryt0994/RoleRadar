"""What an evidence analysis is asked to do, and what it gives back (U2-A1).

These types are separate from the legacy profile matching in schemas.py.
Sources here are experiences of the library, named by their own ids and
the confirmed versions the user chose, never squeezed into the legacy
integer profile id.

A task names its purpose, the chosen sources, the job posting, who receives
the material and how it is processed. task_fingerprint covers all of them,
so a confirmation given for one task (TaskApproval) does not cover another:
adding a source, changing the purpose, the job posting or the recipient
makes a new task that needs its own confirmation. In U2-A1 confirmations
are only built by tests; they are not a user's real permission.

U2-A2 adds what the job analysis page lets the user decide: which fields of
each chosen version go out, which saved job preferences are compared, and
the version of what the page said about the task. They are covered by the
fingerprint too. The page's server keeps the confirmation (see
app.job_analysis); a browser never supplies one.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.jd_record import fingerprint_job_posting
from app.library.projection import SelectionProblem
from app.schemas import CoverageScore

# Comparing one job posting with chosen experiences.
JOB_MATCH = "job_match"
# One task, run once on request; nothing is kept or run again later.
SINGLE_REQUEST = "single_request"
# The provider name of the simulated service used in U2-A1.
SIMULATION_PROVIDER = "simulation"


@dataclass(frozen=True)
class SelectedSource:
    """One chosen experience and the confirmed version that was chosen.

    fields names the fields of that version that may go out; None lets every
    field go (U2-A1). A field outside it is never sent, and never cited.
    """

    item_id: str
    version_id: str
    fields: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SelectedPreference:
    """A field of the job preferences chosen for this task, with the words it held then."""

    field: str
    text: str


@dataclass(frozen=True)
class Recipient:
    """Who receives the material: the service, the model the task names and,
    for a real service, the address its requests go to."""

    provider: str
    model: str
    endpoint: str | None = None


@dataclass(frozen=True)
class AnalysisRequest:
    """One task, from a trusted caller: owner_id comes from its session.

    selection names each experience once; a task that repeats one, in the
    same version or another, is refused before anything goes out.
    """

    owner_id: str
    selection: tuple[SelectedSource, ...]
    job_posting: str
    recipient: Recipient
    purpose: str = JOB_MATCH
    mode: str = SINGLE_REQUEST
    # Preferences to compare with the posting; none means they are not used.
    preferences: tuple[SelectedPreference, ...] = ()
    # The version of what the page told the user before they confirmed.
    notice_version: str = ""


def task_fingerprint(request: AnalysisRequest) -> str:
    """What a confirmation of this task covers, as one value.

    The order in which sources were chosen does not matter; which sources
    and versions, the job posting, the purpose, the mode and the recipient
    all do. A task that runs names each experience once, so these sorted
    pairs are exactly the versions it reads. So do the fields of each that
    may go out, the preferences with their words, and the notice version.
    """
    scope = {
        "owner_id": request.owner_id,
        "purpose": request.purpose,
        "mode": request.mode,
        "recipient": [request.recipient.provider, request.recipient.model, request.recipient.endpoint],
        "sources": sorted(
            [source.item_id, source.version_id, "*" if source.fields is None else json.dumps(sorted(source.fields))]
            for source in request.selection
        ),
        "job_posting_sha256": fingerprint_job_posting(request.job_posting),
        "preferences": sorted([preference.field, preference.text] for preference in request.preferences),
        "notice_version": request.notice_version,
    }
    encoded = json.dumps(scope, sort_keys=True, ensure_ascii=False).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TaskApproval:
    """A confirmation of exactly one task, by its fingerprint."""

    task_fingerprint: str


class AnalysisStatus(StrEnum):
    # Every requirement assessed from the chosen sources, citations checked.
    COMPLETE = "complete"
    # The job posting could only be read by rules: no full analysis was made.
    DEGRADED = "degraded"
    # A call or its answer failed; nothing of it is presented as a result.
    FAILED = "failed"
    # Refused before or between calls: see problem and selection_problems.
    REJECTED = "rejected"
    # Stopped by the user; requests_sent says what had already gone out.
    CANCELLED = "cancelled"
    # Made, but a source changed afterwards: not a current result.
    STALE = "stale"


class RejectionReason(StrEnum):
    EMPTY_SELECTION = "empty_selection"
    EMPTY_JOB_POSTING = "empty_job_posting"
    UNSUPPORTED_TASK = "unsupported_task"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_MISMATCH = "approval_mismatch"
    # The provider is configured for another service or model than the task names.
    RECIPIENT_MISMATCH = "recipient_mismatch"
    # The provider cannot tell where its requests go (no ProviderIdentity).
    RECIPIENT_UNVERIFIED = "recipient_unverified"
    # The same experience chosen more than once, in one version or several.
    REPEATED_SELECTION = "repeated_selection"
    # Something about to go out looks like contact details, an identity
    # number, an address or confidential content (app.outbound_review).
    OUTBOUND_BLOCKED = "outbound_blocked"
    SELECTION_INVALID = "selection_invalid"
    SOURCES_CHANGED = "sources_changed"


class AnalysisFailure(StrEnum):
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    RATE_LIMIT = "rate_limit"
    PROVIDER_RESPONSE = "provider_response"
    REFUSAL = "refusal"
    EMPTY_RESPONSE = "empty_response"
    INVALID_JSON = "invalid_json"
    SCHEMA = "schema"
    # A citation that is not in the chosen sources, word for word.
    CITATION = "citation"
    # Not exactly one assessment for each requirement.
    COVERAGE = "coverage"
    # The answer came from another service or model than the task names.
    RECIPIENT_MISMATCH = "recipient_mismatch"
    # The parsed job description does not belong to this posting.
    JOB_RECORD = "job_record"


class RequirementGroup(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    HARD_CONSTRAINT = "hard_constraint"
    # Requirements the posting itself leaves vague: assessed, never scored.
    UNCERTAIN = "uncertain"


class CitationBasis(StrEnum):
    """What a cited passage says, as the model reads it (a suggestion)."""

    PRACTICE = "practice"
    CLAIM = "claim"
    PLAN = "plan"
    OTHER_PERSON = "other_person"
    NEGATED = "negated"
    FACT = "fact"
    CONFLICT = "conflict"


class EvidenceConclusion(StrEnum):
    """A skill requirement against the chosen material."""

    SUPPORTED = "supported"
    NEEDS_CONFIRMATION = "needs_confirmation"
    # Nothing in the chosen material shows it: not "the user lacks it".
    NO_EVIDENCE = "no_evidence"


class ConditionConclusion(StrEnum):
    """A hard constraint of the posting against the chosen material."""

    MET = "met"
    CONFLICT = "conflict"
    NEEDS_CONFIRMATION = "needs_confirmation"
    # The chosen material does not say: never guessed.
    UNKNOWN = "unknown"


class PreferenceRelation(StrEnum):
    """How the posting stands to one preference, as the model reads it (a suggestion)."""

    CONSISTENT = "consistent"
    CONFLICTS = "conflicts"
    UNCLEAR = "unclear"
    NOT_STATED = "not_stated"


# The model names both aspects of a comparison (a suggestion); a relation stands only where
# they are the same aspect (U2-A2 R5). The docstring below is the description the model reads
# in the answer's JSON Schema (R7), so it defines each value for the model.
class PostingAspect(StrEnum):
    """What a job preference is about, and what the posting's words compared with it speak of:
    location, where the work is; work_mode, remote, on site or hybrid; travel; schedule, hours,
    pace and overtime; start_date, when the job starts or by when the hire must be available, as
    the posting states it; employment_type, full-time, internship or part-time, and how long;
    direction, the industry, field or team; work_content, what the daily work is; compensation;
    qualification, what the posting asks of the candidate, such as an education, a graduation
    date or year, skills, experience or a work authorisation: never what a preference is about,
    and never a start date; other, anything else.
    """

    LOCATION = "location"
    WORK_MODE = "work_mode"
    TRAVEL = "travel"
    SCHEDULE = "schedule"
    START_DATE = "start_date"
    EMPLOYMENT_TYPE = "employment_type"
    DIRECTION = "direction"
    WORK_CONTENT = "work_content"
    COMPENSATION = "compensation"
    QUALIFICATION = "qualification"
    OTHER = "other"


class PreferenceConclusion(StrEnum):
    """A chosen preference against the posting. Never a skill, never scored."""

    CONSISTENT = "consistent"
    # Goes against something the user would like or could accept: a wish, not a filter.
    DIFFERENT = "different"
    # Goes against what the user cannot accept or must have: shown apart.
    CONFLICT = "conflict"
    NEEDS_CONFIRMATION = "needs_confirmation"
    # The posting does not say; nothing is inferred from the company or the title.
    NOT_STATED = "not_stated"


@dataclass(frozen=True)
class SourceBasis:
    item_id: str
    version_id: str
    category: str
    version_number: int
    confirmed_at: datetime
    source_revision: int
    # The fields of this version that went out (all of its filled fields in U2-A1).
    fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class JobBasis:
    job_posting_sha256: str
    prompt_version: str
    parser_version: str
    schema_version: str
    provider: str
    requested_model: str
    returned_model: str


@dataclass(frozen=True)
class AnalysisBasis:
    analysis_prompt_version: str
    rules_version: str
    scoring_version: str
    provider: str
    requested_model: str
    returned_model: str
    # Set when preferences were compared.
    preference_rules_version: str | None = None


@dataclass(frozen=True)
class EvidenceCitation:
    item_id: str
    version_id: str
    category: str
    field: str
    quote: str
    basis: CitationBasis
    # What the local rules read the quote with, word for word from the field:
    # at each place the quote stands, its clause and the clauses before it in
    # its sentence, back to a turn such as "但" or "but".
    context: tuple[str, ...]
    # Words in the context that the basis does not account for (local rules).
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RequirementFinding:
    requirement_id: str
    group: RequirementGroup
    text: str
    job_posting_quote: str
    # An EvidenceConclusion, or a ConditionConclusion for hard constraints.
    conclusion: str
    citations: tuple[EvidenceCitation, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PreferenceFinding:
    preference_id: str
    # The preferences field, such as cities_ideal or must_haves.
    field: str
    # The user's own words in that field.
    text: str
    # A PreferenceConclusion.
    conclusion: str
    # The posting's words it rests on, each found word for word in the posting sent.
    job_quotes: tuple[str, ...]
    reasons: tuple[str, ...]
    # Whether the field says what the user cannot accept or must have.
    firm: bool


@dataclass(frozen=True)
class AnalysisResult:
    """A temporary result: never saved, confirmed or sent anywhere."""

    status: AnalysisStatus
    task_fingerprint: str
    # "simulation" for the simulated service, "live" otherwise.
    mode: str
    # Requests handed to the recipient, counted also when one failed: it may
    # have gone out, and none can be called back.
    requests_sent: int
    sources: tuple[SourceBasis, ...] = ()
    job: JobBasis | None = None
    analysis: AnalysisBasis | None = None
    findings: tuple[RequirementFinding, ...] = ()
    coverage: CoverageScore | None = None
    # Degraded: what the rules alone found in the posting.
    rule_skills: tuple[str, ...] = ()
    # The rejection reason or failure category, or the posting's failure when degraded.
    problem: str | None = None
    selection_problems: tuple[SelectionProblem, ...] = ()
    # "not_analyzed" unless the task chose preferences; then "compared".
    preferences: str = "not_analyzed"
    preference_findings: tuple[PreferenceFinding, ...] = ()

    @property
    def is_current(self) -> bool:
        return self.status is AnalysisStatus.COMPLETE

    @property
    def condition_conflicts(self) -> tuple[RequirementFinding, ...]:
        """Hard constraints the chosen material contradicts, shown apart from any score."""
        return tuple(
            finding
            for finding in self.findings
            if finding.group is RequirementGroup.HARD_CONSTRAINT
            and finding.conclusion == ConditionConclusion.CONFLICT
        )

    @property
    def preference_conflicts(self) -> tuple[PreferenceFinding, ...]:
        """What the user cannot accept or must have, which the posting goes against."""
        return tuple(
            finding for finding in self.preference_findings if finding.conclusion == PreferenceConclusion.CONFLICT
        )
