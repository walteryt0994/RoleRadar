"""What a search for postings works with (U3-A1).

A search runs inside one confirmation of the user's (ParentConfirmation):
where it may look (SearchScope), which sources it may ask and how they are
reached (SourceGrant), which experiences, versions and fields an analysis
may send, which preferences it compares, who receives them under which
notice, and how much of each step it may take (Limits). With no experience
chosen it only finds, reads and checks postings: nothing is analysed for
the user. Its tools name postings by their reference in a registered source
(PostingRef), never by an address, and answer with a ToolResult whose
summary is all the next decision sees.

The scope has three layers (U3 proposal, 1.2). What the user cannot accept
is never searched and never widened. What would be ideal only puts postings
first; it filters nothing. Everything else is what the user confirmed for
this search, dimension by dimension, and a dimension their preferences
leave open is neither "anything" nor "nothing": until they say, the search
cannot start.

Nothing here depends on a real source or a search service: a source only
has to give postings in these shapes (app.discovery.sources).
"""
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from enum import StrEnum

from app.analysis_contracts import AnalysisResult, Recipient, SelectedPreference, SelectedSource


class Dimension(StrEnum):
    """What a search is narrowed by, and what the rules hold postings against."""

    CITY = "city"
    JOB_TYPE = "job_type"
    DIRECTION = "direction"


# The job preferences each dimension starts from (app.library.content.PREFERENCE_DIMENSIONS).
DIMENSION_PREFERENCES = {Dimension.CITY: "cities", Dimension.JOB_TYPE: "job_types", Dimension.DIRECTION: "directions"}
DIMENSION_LABELS = {Dimension.CITY: "城市", Dimension.JOB_TYPE: "求职类型", Dimension.DIRECTION: "岗位方向"}


class Others(StrEnum):
    """What the user said, for this search, about values their preferences do not list."""

    # Nothing yet (未填写): the search cannot start.
    UNDECIDED = "undecided"
    # 本次不限: any other value too, but never one the user cannot accept.
    INCLUDED = "included"
    # 本次只看所列的: the user's own choice, never the system's guess.
    EXCLUDED = "excluded"


@dataclass(frozen=True)
class DimensionScope:
    """One dimension of a search, as the user confirmed it."""

    # The values to search for.
    values: tuple[str, ...] = ()
    others: Others = Others.UNDECIDED
    # Those the preferences call ideal: they come first and filter nothing.
    ideal: tuple[str, ...] = ()
    # What the user cannot accept: never searched, never chosen, never widened.
    excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchScope:
    city: DimensionScope
    job_type: DimensionScope
    direction: DimensionScope
    # The job titles the user is looking for: what searches are about.
    targets: tuple[str, ...]

    def of(self, dimension: Dimension) -> DimensionScope:
        return getattr(self, dimension.value)


@dataclass(frozen=True)
class Limits:
    """The most one search may do (U3 proposal, 5.1: first defaults, reviewed with A1 and A2)."""

    searches: int = 4
    # Postings read and postings checked, each one counted.
    reads: int = 10
    checks: int = 10
    # Postings analysed, two generations each; so also the most full results a search shows.
    analyses: int = 3
    # Postings one read or one check may name.
    per_action: int = 5
    # Unanalysed postings listed; any more are only counted.
    leads: int = 10
    # Refused actions after which the search ends.
    refusals: int = 2
    # The whole search, and one call to a source.
    seconds: float = 600.0
    source_seconds: float = 20.0
    keyword_chars: int = 40


class EvidenceLevel(StrEnum):
    """What a posting can show, by where it came from (U3 proposal, 3.3)."""

    # The employer's own posting, on its site or on the hiring system it uses.
    OFFICIAL = "official"
    # Found elsewhere, such as a search result or a repost: never analysed until its official posting is read.
    LEAD = "lead"
    # Pasted by the user: analysed as theirs, never taken as checked by the system.
    USER_PROVIDED = "user_provided"


EVIDENCE_LABELS = {
    EvidenceLevel.OFFICIAL: "官方原始发布",
    EvidenceLevel.LEAD: "待核验线索",
    EvidenceLevel.USER_PROVIDED: "用户提供，系统未独立核验",
}


class Access(StrEnum):
    """How a source is reached (U3 proposal, 3.1): each way is registered and confirmed on its own."""

    # RoleRadar's own requests to the site.
    LOCAL_FETCH = "local_fetch"
    # A search service reading pages for us, live.
    SEARCH_SERVICE = "search_service"
    # A search service's cache or index: nothing read live for the request.
    CACHED_SEARCH = "cached_search"
    # Frozen data inside this process: nothing goes anywhere.
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class SourceGrant:
    """A source one search may ask, as the user confirmed it (U3 proposal, 5.5).

    name is who receives what the search asks of it, access how it is
    reached and evidence what its postings can show. A source registered
    otherwise now is not the one confirmed: nothing is asked of it.
    """

    source_id: str
    name: str
    access: Access
    evidence: EvidenceLevel


@dataclass(frozen=True)
class PostingRef:
    """A posting by its source and that source's own id for it: never an address."""

    source_id: str
    posting_id: str


@dataclass(frozen=True)
class Posting:
    """A posting's public facts, as its source states them: no conclusion of any kind."""

    ref: PostingRef
    title: str
    company: str
    # The cities it names; none when it names none.
    cities: tuple[str, ...]
    # Whether it says the hire may choose among them.
    city_choice: bool
    job_type: str | None
    direction: str | None
    posted_on: date | None
    deadline: date | None
    # For a lead: its official posting, when that stands in a registered source.
    official: PostingRef | None = None
    # The employer's own number for the job (职位编号), when its source states one.
    identity: str | None = None
    # Its places and job type as the source wrote them, when it wrote any (U3-D2-2): kept apart from the cities and
    # the type read from them, so a fact not given and one given but not read are told apart. What is not read stays
    # here as written, never guessed into a city or a type.
    places_stated: tuple[str, ...] = ()
    type_stated: str | None = None


@dataclass(frozen=True)
class PostingDetail:
    """A posting as read from its source. Its words are data, never instructions."""

    posting: Posting
    text: str
    url: str
    apply_url: str | None = None


class CheckStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    # The source gave no answer: the status needs confirming, and no time is refreshed.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CheckAnswer:
    """What a source answered when asked whether a posting is still open (OPEN or CLOSED)."""

    status: CheckStatus
    deadline: date | None = None


class Tool(StrEnum):
    SEARCH = "search_postings"
    READ = "get_posting"
    CHECK = "check_posting"
    ANALYZE = "analyze_posting"
    FINISH = "finish"


class Outcome(StrEnum):
    DONE = "done"
    # Not done: outside the scope or the register, over a limit or not ready (see reason). Counted.
    REFUSED = "refused"
    # Done, but the source failed or did not answer in time; never tried again.
    FAILED = "failed"
    # Nothing sent: the outbound rules stopped this posting (app.outbound_review).
    BLOCKED = "blocked"
    # The search has ended (see EndReason): nothing more is done.
    STOPPED = "stopped"


@dataclass(frozen=True)
class SearchFix:
    """A search refused for naming in its keywords what its source matches by a filter (U3-D2-E4 F1), as it may be
    made again: the same source and filters, the keywords less those words. Never wider than the search refused:
    nothing left open that it narrowed, nothing of the scope changed. Only told; it is made again by a decision or
    not at all."""

    source_id: str
    keywords: str
    city: str | None
    job_type: str | None
    direction: str | None


@dataclass(frozen=True)
class ToolResult:
    tool: Tool
    outcome: Outcome
    # All the next decision sees, so it may go to a model: short, a posting's own words only
    # inside a marked excerpt, contact details masked, nothing with an address or marked
    # confidential, and none of the user's own words. What a page shows comes from the
    # search's own records (SearchRun.outcome), never from here.
    summary: str
    reason: str | None = None
    # Why a source's call failed, for the pages only (app.discovery.sources.failure_text, U3-D2-E2): fixed words,
    # never told to the next decision.
    failure: str | None = None
    # R10 (U3-D6-3 F3): the postings of a read or a check whose call did not come to an answer taken, each with why
    # in fixed words, for the pages only: a step's failure belongs to these, never to the step's first line.
    failed: tuple[tuple[PostingRef, str], ...] = ()
    # A refused search as it may be made again (U3-D2-E4 F1), when it was refused for keywords its source matches by
    # a filter and words are left; its summary says the same in words.
    fix: SearchFix | None = None


class EndReason(StrEnum):
    ENOUGH = "enough"
    SCOPE_DONE = "scope_done"
    NO_RESULTS = "no_results"
    # Nothing suitable in what was searched, while some of the scope was not: what "no results" comes to then.
    PARTLY_NO_RESULTS = "partly_no_results"
    LIMIT = "limit"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    SOURCES_UNAVAILABLE = "sources_unavailable"
    SOURCES_CHANGED = "sources_changed"
    # The gate, or the analysis entry's own checks before sending, let a request no further.
    GATE = "gate"
    REFUSED = "refused"


END_LABELS = {
    EndReason.ENOUGH: "够了",
    EndReason.SCOPE_DONE: "范围内查完了",
    EndReason.NO_RESULTS: "没有合适结果",
    EndReason.PARTLY_NO_RESULTS: "已查查询无结果，仍有范围未核验",
    EndReason.LIMIT: "到了上限",
    EndReason.TIMEOUT: "超时",
    EndReason.CANCELLED: "已取消",
    EndReason.SOURCES_UNAVAILABLE: "来源不可用",
    EndReason.SOURCES_CHANGED: "资料已变化",
    EndReason.GATE: "闸门拦下",
    EndReason.REFUSED: "被拒的动作达到上限",
}
# What a search may end with by its own choice; the others are for the rules to say.
CLAIMS = (EndReason.ENOUGH, EndReason.SCOPE_DONE, EndReason.NO_RESULTS)


class Screen(StrEnum):
    """A posting against the scope, from its public facts alone."""

    IN = "in"
    # Plainly outside: never analysed or shown, only counted (超出条件).
    OUT = "out"
    # Not plain from the posting: neither taken as inside nor left out (待确认).
    PENDING = "pending"


@dataclass(frozen=True)
class Screening:
    status: Screen
    # Why it is out, or what about it is not plain.
    reasons: tuple[str, ...] = ()
    # What else to know, such as the city it is judged by.
    notes: tuple[str, ...] = ()


class Category(StrEnum):
    """Where an analysed posting stands, by fixed rules (app.discovery.rules), never by the model."""

    STRONG = "strong"
    PARTIAL = "partial"
    CONFLICT = "conflict"


CATEGORY_LABELS = {
    Category.STRONG: "依据较充分",
    Category.PARTIAL: "有基础但有差距或待确认",
    # Shown as it is, after the others, and never called a recommendation.
    Category.CONFLICT: "有明确冲突，需谨慎考虑",
}
RECOMMENDED = (Category.STRONG, Category.PARTIAL)


class Unlisted(StrEnum):
    """Why a posting found is neither a result nor a lead."""

    CLOSED = "closed"
    OUT_OF_SCOPE = "out_of_scope"
    BLOCKED = "blocked"
    # Its analysis did not finish (failed, cancelled, out of time): nothing came of it.
    INCOMPLETE = "incomplete"


# The outcome of a search with no experience chosen, and why none of its postings was analysed.
NOT_PERSONALISED = "未进行个性化分析：没有选择经历，只按检索范围查找、读取和核对岗位"
NOT_PERSONALISED_WHY = "没有选择经历，未进行个性化分析"


@dataclass(frozen=True)
class Result:
    """One posting analysed in full, with what it rests on (U3 proposal, 1.3).

    Only as current as its facts: its last check answered that it is open,
    and the experiences and preferences it was analysed with are still as
    confirmed. Otherwise the analysis is withdrawn and the posting is a lead.
    """

    ref: PostingRef
    title: str
    company: str
    source: str
    evidence: EvidenceLevel
    url: str
    apply_url: str | None
    posted_on: date | None
    # The deadline at each place it was found, side by side: none is picked.
    deadlines: tuple[tuple[PostingRef, date | None], ...]
    read_at: datetime
    content_sha256: str
    # The last check its source answered; a check without an answer never moves it.
    checked_at: datetime | None
    category: Category
    # What to confirm before applying, at most two.
    first_checks: tuple[str, ...]
    notes: tuple[str, ...]
    analysis: AnalysisResult
    # Its places and type as read, and as the source wrote them (U3-D2-3): pages tell a fact not given from one
    # given and not read.
    cities: tuple[str, ...] = ()
    job_type: str | None = None
    places_stated: tuple[str, ...] = ()
    type_stated: str | None = None
    # Its checks as the search ended (U3-D3a F1), each said as it is and none guessed from another: its status then
    # (OPEN for a result), what its source answered at checked_at, and when its latest check got no answer, if one did.
    status: CheckStatus | None = None
    answered_status: CheckStatus | None = None
    check_failed_at: datetime | None = None
    # When its analysis was taken, by the search's clock (U3-D3b): what a saved analysis says of when it was made.
    analysed_at: datetime | None = None


@dataclass(frozen=True)
class Lead:
    """A posting found with no current analysis: its public facts and why, and no conclusion at all.

    Never analysed, or analysed once and withdrawn since. checked_at stays
    the last check its source answered, and why says when a later one did not.
    """

    ref: PostingRef
    title: str
    company: str
    cities: tuple[str, ...]
    source: str
    evidence: EvidenceLevel
    posted_on: date | None
    deadline: date | None
    checked_at: datetime | None
    why: str
    notes: tuple[str, ...] = ()
    # Its type as read, and its places and type as the source wrote them (U3-D2-3), as for a result.
    job_type: str | None = None
    places_stated: tuple[str, ...] = ()
    type_stated: str | None = None
    # Its checks as the search ended (U3-D3a F1), as for a result: its status then (OPEN, CLOSED, UNKNOWN, or None if
    # never checked), what its source answered at checked_at (a lead's last answer may have been CLOSED before a check
    # without an answer), and when its latest check got no answer. Never read from why.
    status: CheckStatus | None = None
    answered_status: CheckStatus | None = None
    check_failed_at: datetime | None = None


@dataclass(frozen=True)
class SearchRecord:
    """One search as it was made: where, with which words and filters, and how many postings came back.

    unverifiable: how many of those were not taken because their ids are not
    plain (app.discovery.tools.PLAIN_ID). They are counted and never named:
    no id of theirs is kept. A search that got such postings back did not
    find nothing; it found what it could not verify.

    foreign: how many of those named another source as theirs. A source gives
    back only its own postings, so these are a source anomaly: never taken,
    only counted, no id of theirs kept. A search that got them back did not
    find nothing either.

    For a source answering from its whole list (U3-D2-1): listed, how many
    postings that list held as read; partial, that it was read only in part,
    so what was found is taken and the combination is not covered; reused,
    that it was answered from the list read earlier in the same search, with
    nothing asked of the source again.
    """

    source_id: str
    keywords: str
    filters: tuple[tuple[Dimension, str | None], ...]
    found: int
    outcome: Outcome
    unverifiable: int = 0
    foreign: int = 0
    listed: int | None = None
    partial: bool = False
    reused: bool = False
    # Why a failed search failed, as the pages say it (app.discovery.sources.failure_text, U3-D2-E2).
    failure: str | None = None


@dataclass(frozen=True)
class PartialRead:
    """A source whose list a search read only in part (U3-D2-1): how many of its postings were read, and the
    combinations of the scope searched there, none of which that source covered."""

    source_id: str
    source: str
    read: int
    combinations: tuple[str, ...]


@dataclass(frozen=True)
class SearchOutcome:
    end: EndReason
    # False when no experience was chosen: nothing was analysed for the user (NOT_PERSONALISED).
    personalised: bool
    results: tuple[Result, ...]
    leads: tuple[Lead, ...]
    # Leads past Limits.leads: counted, not listed.
    more_leads: int
    unlisted: tuple[tuple[Unlisted, tuple[PostingRef, ...]], ...]
    # Postings found again, elsewhere or posted twice, folded into the one kept.
    merged: tuple[PostingRef, ...]
    # Postings sources gave back that could not be verified, their ids not being plain: counted as given back
    # (one given back by two searches counts twice), never named and never judged. While any came back, a
    # search may not say it found nothing suitable.
    unverifiable: int
    # Records sources gave back as another source's, a source anomaly: counted the same way, never named, never
    # taken. While any came back, a search may not say it found nothing suitable either.
    foreign: int
    # The combinations of the scope's values no single search covered: only with none left may a search
    # say it covered the scope or found nothing suitable.
    unsearched: tuple[str, ...]
    # Preference topics no analysis could judge, none of them being chosen.
    unjudged: tuple[str, ...]
    searches: tuple[SearchRecord, ...]
    refusals: tuple[str, ...]
    # Sources whose lists were read only in part (U3-D2-1): never taken away by a complete search elsewhere. While
    # any is here the scope was not covered, and nothing suitable is said only of what was searched.
    partial: tuple[PartialRead, ...] = ()
    # Why the analyses left out as blocked were stopped (U3-D2-E3): each reason as its codes, comma-joined (the
    # outbound rules' kinds, too_long, outbound_limit, outbound_blocked, gate_<reason>), with how many postings it
    # stopped. Pages say each as it is, never one reason for all.
    blocked: tuple[tuple[str, int], ...] = ()
    # Why the search's own approvals let a request no further, in words, when that ended the search; and why the gate
    # did, as its reason's value (U3-D2-E3).
    gate_note: str | None = None
    gate_reason: str | None = None
    # Why the search ended because its source could send nothing more (validation B, U3-D2-E3 R1): the source and
    # what stopped it, in words.
    source_note: str | None = None
    # How many postings the user skipped this search came across (U3-D3c), each once: found itself, or pointed to by
    # a lead, when searching or once a lead was read. Neither those nor the leads pointing to them are listed.
    skipped: int = 0
    # How many different postings of those not verifiable came back (U3-D5), however many times each did: the number
    # a page says as postings, unverifiable being how many times they came back. Known by a digest kept in memory
    # for the search only, never by the id.
    unverifiable_distinct: int = 0


@dataclass(frozen=True)
class ParentConfirmation:
    """What the user confirmed once for a whole search (U3 proposal, 2.2 and 5.5).

    Its searches, reads and checks ask only the sources it names, each as it
    was confirmed. Every analysis in it sends exactly these experiences,
    versions, fields and preferences, to this recipient under this notice;
    only the posting is its own. With no experience chosen there is no
    analysis, and so no preference either. The scope's dimensions are held
    against postings by rules, so their preferences are not among those an
    analysis compares.
    """

    owner_id: str
    scope: SearchScope
    selection: tuple[SelectedSource, ...]
    preferences: tuple[SelectedPreference, ...]
    # Who receives the analyses.
    recipient: Recipient
    # Who receives the searches, reads and checks.
    sources: tuple[SourceGrant, ...]
    notice_version: str
    limits: Limits = Limits()
    # The postings the user skipped on these sources (U3-D3c), as the server read them for this user when the search
    # was reviewed, each once and in order: none is taken, read, checked or analysed, nor is a lead pointing to one.
    # Nothing else of the user's records is here: no reason, no note.
    skipped: tuple[PostingRef, ...] = ()
    # Whether the user ticked 临时留存并跟踪这次的推荐 (U3-D4b-2), as the server let it be offered: only then, and only
    # if the search ends as it should, are its recommendations held for tracking (app.discovery.records).
    track_recommendations: bool = False
    # The D6 batch as it stood when the search was reviewed (app.discovery.batch_live.state_of): what its approvals had
    # used and had left. Empty for every other set; when given, a confirmation holds only while the batch stands so.
    batch_state: str = ""


def search_fingerprint(confirmation: ParentConfirmation) -> str:
    """What a confirmation of this search covers, as one value: any change makes another search."""
    scope = confirmation.scope
    payload = {
        "owner_id": confirmation.owner_id,
        "scope": {
            **{
                dimension.value: [
                    list(scope.of(dimension).values),
                    scope.of(dimension).others.value,
                    list(scope.of(dimension).ideal),
                    list(scope.of(dimension).excluded),
                ]
                for dimension in Dimension
            },
            "targets": list(scope.targets),
        },
        "sources": sorted(
            [source.item_id, source.version_id, "*" if source.fields is None else json.dumps(sorted(source.fields))]
            for source in confirmation.selection
        ),
        "preferences": sorted([preference.field, preference.text] for preference in confirmation.preferences),
        "recipient": [confirmation.recipient.provider, confirmation.recipient.model, confirmation.recipient.endpoint],
        "search_sources": sorted(
            [grant.source_id, grant.name, grant.access.value, grant.evidence.value] for grant in confirmation.sources
        ),
        "notice_version": confirmation.notice_version,
        "limits": asdict(confirmation.limits),
        "skipped": sorted({(ref.source_id, ref.posting_id) for ref in confirmation.skipped}),
        "track_recommendations": confirmation.track_recommendations,
    }

    if confirmation.batch_state:
        # Only a D6 batch's: every other confirmation's fingerprint is as it was before D6.
        payload["batch_state"] = confirmation.batch_state

    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SearchApproval:
    """A confirmation of exactly one search, by its fingerprint. In U3-A1 only tests build one."""

    fingerprint: str
