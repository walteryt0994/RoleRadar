"""The tools of a search for postings, and the run they act on (U3-A1).

A search starts only from a confirmation that holds (start): its approval
names exactly it, its scope leaves nothing undecided and holds nothing the
outbound rules stop, each source it names
is registered as it was confirmed, the provider it is given is the
recipient it names, and the chosen experiences and preferences are still
as confirmed and within what one analysis may send. Each tool then does
one step of it: search_postings, get_posting, check_posting,
analyze_posting and finish.

- Before every call to a source or a model, and again before what the call
  gave back is taken, the run is asked whether it may go on: not
  cancelled, not out of time, not ended. A call that went out still counts;
  what comes back after the search stopped, an answer or a failure, is not
  taken, and the step is stopped rather than failed. Each step is
  checked against the scope, the confirmed sources and the limits. A step
  that fails is not done, its reason is its result, and enough refusals
  end the search.
- A source is asked once for each search, read or check, within its time
  and never beyond the time the search has left. A request that failed is
  not sent again in the same search: a search with other conditions is
  another request, and a posting whose last check was answered may be
  checked again. Postings are checked at their official source, and a
  check refreshes the time a posting was last checked only when that
  source answered.
- A posting is analysed only as an official posting that was read, that is
  in scope or pending, checked open by an answered check, and not the same
  posting as one analysed already. The analysis is the U2 analysis entry
  (app.experience_analysis) with exactly what the parent search was
  confirmed for and only this posting's text, together within U2's limits
  of what one analysis sends. Its approval is derived here by comparing the
  two, and it goes through the provider the parent passes in: the one the
  parent's gate admitted once for the whole search (U3 proposal, 2.2). The
  tools never start a task or enter a gate of their own. The chosen
  material changing, a cancellation, the time running out or a gate
  stopping a request ends the search, so no analysis starts after it. With
  no experience chosen nothing is analysed at all.
- A result stands only while its facts do: a later check without an
  answer, or chosen material that has changed, withdraws its analysis.
- finish ends the search for a reason it may give itself; the rules have
  the last word on whether the scope was covered, combination by
  combination of its values, and nothing suitable may be said only when
  every posting found has a conclusion and no source gave back postings
  that could not be verified or that were another source's. With
  combinations left unsearched it is said as "nothing suitable in what was
  searched, the rest unchecked".

Each result's summary is short, and it may go to a model (U3 proposal, 5.4
and 5.5): a posting's own words reach it only cut down, inside a marked
excerpt, with contact details masked, and not at all when they hold an
address or a confidential marker. Of the user's own words it holds only the
scope's values, which are conditions: a scope with any word the outbound
rules stop does not start, and its refusal does not repeat it. A posting
is named by its source's id only when that is a plain id (PLAIN_ID): one
that is not is never taken or named, only counted as a posting that could
not be verified. A record a source gives back as another source's is never
taken or named either, only counted as a source anomaly: a source gives back
its own postings, and a lead names the official posting it points to apart
from its own id. Keywords and filters are words of conditions only, within
the scope, without sentences or contact details.
"""
import hashlib
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.ai_provider import AIProvider
from app.analysis_contracts import (
    JOB_MATCH,
    SINGLE_REQUEST,
    AnalysisRequest,
    AnalysisResult,
    AnalysisStatus,
    RejectionReason,
    TaskApproval,
    task_fingerprint,
)
from app.discovery import rules
from app.discovery.contracts import (
    CATEGORY_LABELS,
    CLAIMS,
    DIMENSION_LABELS,
    END_LABELS,
    EVIDENCE_LABELS,
    NOT_PERSONALISED_WHY,
    Category,
    CheckStatus,
    Dimension,
    EndReason,
    EvidenceLevel,
    Lead,
    Outcome,
    ParentConfirmation,
    Posting,
    PostingDetail,
    PostingRef,
    Result,
    Screen,
    Screening,
    SearchApproval,
    SearchOutcome,
    SearchRecord,
    SearchScope,
    SourceGrant,
    Tool,
    ToolResult,
    Unlisted,
    search_fingerprint,
)
from app.discovery.sources import Query, Register, SourceError, SourceTimeout
from app.experience_analysis import PREFERENCE_TOPICS, analyze_selected_experiences
from app.job_analysis.scope import MAX_EXPERIENCES, MAX_OUTBOUND_CHARS, MAX_POSTING_CHARS, clean_posting
from app.library.clock import Clock
from app.library.db import Database
from app.library.projection import current_preferences, read_selected_confirmations
from app.live_gate import LiveBlocked
from app.outbound_review import ADDRESS, CONFIDENTIAL, masked_posting, review

# Keywords are words of conditions: letters, digits and a few signs (C++, C#, .NET), never a sentence.
KEYWORDS = re.compile(r"[\w\s+#./-]+")
# A source's or a posting's id as summaries show it and later steps name it: letters, digits and . _ : -,
# and nothing the outbound rules stop (a phone number, an identity number). A posting named otherwise,
# or pointing to an official posting named otherwise, is not taken: its id would be a way around them.
PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# How much of a posting's facts and words a summary carries, and how many postings it lists.
FACT_CHARS = 40
EXCERPT_CHARS = 120
LISTED = 12
# Analyses refused because what was chosen has changed since the search was confirmed.
CHANGED = (RejectionReason.SELECTION_INVALID.value, RejectionReason.SOURCES_CHANGED.value)
# What stopped an analysis before anything went out, besides the outbound rules' own kinds:
# a posting over U2's length, or all that one analysis would send over U2's total.
TOO_LONG = "too_long"
OVER_LIMIT = "outbound_limit"
# A read or check that failed, as a later step of the same search is told.
NOT_AGAIN = {Tool.READ: "上次没有读到", Tool.CHECK: "上次核对没有得到回答"}
# What stops a posting, as a summary names it: not in the outbound rules' own labels ("详细地址",
# "保密或内部内容"), which those rules stop as well.
STOPPING = {ADDRESS: "疑似地址", CONFIDENTIAL: "标明不公开的内容"}


class StartRefused(Exception):
    """A search that may not start; problems says why, every reason found."""

    def __init__(self, problems: tuple[str, ...]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(eq=False)
class Found:
    """One posting a search came across, and what its tools learned of it."""

    posting: Posting
    order: int
    screening: Screening
    detail: PostingDetail | None = None
    read_at: datetime | None = None
    content_sha256: str | None = None
    flags: tuple[str, ...] = ()
    status: CheckStatus | None = None
    # The last check its source answered.
    checked_at: datetime | None = None
    checked_deadline: date | None = None
    # Its analysis: only a complete one, and only taken while the search went on.
    analysis: AnalysisResult | None = None
    # What stopped its analysis before it was done: the outbound rules or limits, or the gate.
    blocked: tuple[str, ...] = ()
    # Why its analysis did not finish: it failed, was refused before sending, cancelled or out of time.
    unfinished: str | None = None

    @property
    def ref(self) -> PostingRef:
        return self.posting.ref

    @property
    def deadline(self) -> date | None:
        return self.checked_deadline or self.posting.deadline


class SearchRun:
    """One search as its tools act on it; made by start()."""

    def __init__(
        self,
        database: Database,
        confirmation: ParentConfirmation,
        provider: AIProvider,
        register: Register,
        clock: Clock,
        cancel,
        monotonic: Callable[[], float],
        saved_preferences: dict[str, str],
        material_chars: int,
    ):
        self.database = database
        self.confirmation = confirmation
        self.scope = confirmation.scope
        self.limits = confirmation.limits
        self.grants = {grant.source_id: grant for grant in confirmation.sources}
        # With no experience chosen nothing is analysed for the user.
        self.personalised = bool(confirmation.selection)
        self.provider = provider
        self.register = register
        self.clock = clock
        self.cancel = cancel
        self.monotonic = monotonic
        self.deadline = monotonic() + self.limits.seconds
        self.saved_preferences = saved_preferences
        # What every analysis sends besides the posting, counted as U2 counts it.
        self.material_chars = material_chars
        self.found: dict[PostingRef, Found] = {}
        # What a tool may name: the postings found, and the official postings leads point to.
        self.known: set[PostingRef] = set()
        self.searches: list[SearchRecord] = []
        # Each search answered in time, as the combination it covers (rules.unsearched).
        self.searched: set[tuple[str | None, ...]] = set()
        # Requests a source failed: none is sent again in this search.
        self.failed: set[tuple] = set()
        self.reads = 0
        self.checks = 0
        self.analyses = 0
        self.refusals: list[str] = []
        self.ended: EndReason | None = None

    def stopping(self) -> bool:
        """Whether the search may not go on: it ended, or it is cancelled or out of time now, which ends it."""
        if self.ended is None:
            if self.cancel is not None and self.cancel.is_set():
                self.ended = EndReason.CANCELLED
            elif self.monotonic() >= self.deadline:
                self.ended = EndReason.TIMEOUT

        return self.ended is not None

    def halted(self, tool: Tool, done: Iterable[str] = ()) -> ToolResult | None:
        """The result of a step once the search may not go on, after what the step did before (done)."""
        if not self.stopping():
            return None

        done = list(done)
        stop = f"查找已经结束（{END_LABELS[self.ended]}）"
        lines = [*done, f"{stop}，之后的没有执行；已经发出的请求照常计数。"] if done else [f"{stop}，这一步没有执行。"]

        return ToolResult(tool, Outcome.STOPPED, "\n".join(lines), self.ended.value)

    def refuse(self, tool: Tool, reason: str) -> ToolResult:
        self.refusals.append(reason)

        if len(self.refusals) >= self.limits.refusals:
            self.ended = EndReason.REFUSED

        return ToolResult(tool, Outcome.REFUSED, f"没有执行：{reason}", reason)

    def end(self, reason: EndReason) -> None:
        if self.ended is None:
            self.ended = reason

    def source_seconds(self) -> float:
        """The time one call to a source is given: never more than the search has left."""
        return max(0.0, min(self.limits.source_seconds, self.deadline - self.monotonic()))

    def source_refusal(self, source_id: str) -> str | None:
        """Why this search may not ask a source: not one to call, not confirmed for it, or not as confirmed."""
        refusal = self.register.refusal(source_id)

        if refusal is not None:
            return refusal

        grant = self.grants.get(source_id)
        entry = self.register.entry(source_id)

        if grant is None:
            return f"{entry.name}不在这次查找确认的来源里"

        if entry.grant != grant:
            return f"{grant.name}的接收方、读取方式或证据等级和确认时不一致"

        return None

    def materials_changed(self) -> bool:
        """Whether the chosen experiences or preferences are no longer as confirmed, read again now."""
        if not self.confirmation.selection and not self.confirmation.preferences:
            return False

        with self.database.transaction() as db:
            _, problems, saved = _read_material(db, self.confirmation)

        return bool(problems) or _preferences_changed(self.confirmation, saved)

    def evidence(self, found: Found) -> EvidenceLevel:
        return self.register.entry(found.ref.source_id).evidence

    def take(self, source_id: str, postings: Iterable[Posting]) -> tuple[list[Found], int, int]:
        """Keep the postings a search gave that are new, each screened against the scope; and count, never
        taking them, those named as another source's (a source anomaly) and those whose ids are not plain."""
        new, foreign, unplain = [], 0, 0

        for posting in postings:
            # Another source's posting is not this source's to give: whatever it is, it is only counted.
            if posting.ref.source_id != source_id:
                foreign += 1
                continue

            # One found before is not new.
            if posting.ref in self.found:
                continue

            if not _plain_posting(posting):
                unplain += 1
                continue

            found = Found(posting, len(self.found), rules.screen(self.scope, posting))
            self.found[posting.ref] = found
            self.known.add(posting.ref)

            if posting.official is not None:
                self.known.add(posting.official)

            new.append(found)

        return new, foreign, unplain

    def place(self, detail: PostingDetail) -> Found:
        """A posting as read: its source's facts now, its words, and when they were read."""
        posting = detail.posting
        found = self.found.get(posting.ref)

        if found is None:
            # The official posting a lead points to, read without being searched for.
            found = Found(posting, len(self.found), rules.screen(self.scope, posting))
            self.found[posting.ref] = found

        found.posting = posting
        found.screening = rules.screen(self.scope, posting)
        found.detail = detail
        found.read_at = self.clock.now()
        found.content_sha256 = hashlib.sha256(detail.text.encode("utf-8")).hexdigest()
        found.flags = rules.instruction_flags(detail.text)

        if posting.official is not None:
            self.known.add(posting.official)

        return found

    def groups(self) -> list[list[Found]]:
        """The postings found, those that are one posting together, in the order first found."""
        groups: list[list[Found]] = []

        for found in self.found.values():
            joined = [group for group in groups if any(self.same(found, other) for other in group)]
            rest = [group for group in groups if all(group is not other for other in joined)]
            groups = rest + [sorted([found, *(item for group in joined for item in group)], key=lambda item: item.order)]

        return sorted(groups, key=lambda group: group[0].order)

    def same(self, first: Found, second: Found) -> bool:
        # A job number is taken as the same job only when both postings stand on employers' own sources.
        employers_own = self.evidence(first) is EvidenceLevel.OFFICIAL and self.evidence(second) is EvidenceLevel.OFFICIAL

        return rules.same_posting(first.posting, second.posting, employers_own=employers_own)

    def group_of(self, ref: PostingRef) -> list[Found]:
        return next(group for group in self.groups() if any(found.ref == ref for found in group))

    def kept(self, group: list[Found]) -> Found:
        """The one a group is analysed and shown as: its first official posting, else its first."""
        return next((found for found in group if self.evidence(found) is EvidenceLevel.OFFICIAL), group[0])

    def standing(self, kept: Found, changed: bool = False) -> Unlisted | Category | None:
        """Where a posting kept ends up: left out (with why), a result (its category), or a lead (None).

        Its analysis stands only while its facts do: its last check answered
        that it is open, and the chosen material has not changed since the
        search was confirmed (changed). Otherwise the analysis is withdrawn
        and the posting is a lead again.
        """
        if kept.screening.status is Screen.OUT:
            return Unlisted.OUT_OF_SCOPE

        if kept.status is CheckStatus.CLOSED:
            return Unlisted.CLOSED

        if kept.blocked:
            return Unlisted.BLOCKED

        if kept.unfinished is not None:
            return Unlisted.INCOMPLETE

        if kept.analysis is None or changed or kept.status is not CheckStatus.OPEN:
            return None

        return rules.classify(kept.analysis, kept.screening) or Unlisted.INCOMPLETE

    def notes(self, found: Found) -> tuple[str, ...]:
        notes = [*found.screening.reasons, *found.screening.notes]
        notes += rules.marks(found.posting, found.detail.text if found.detail else None)

        if found.flags:
            notes.append("原文里有像是写给系统的指令，已标记，没有照做")

        group = self.group_of(found.ref)
        notes += [
            f"和 [{_ref(other.ref)}] 公司、标题、城市相同，但没有可靠依据确认是同一个岗位，分开列出"
            for other in self.found.values()
            if all(other is not item for item in group) and rules.alike(found.posting, other.posting)
        ]

        return tuple(notes)

    def why(self, found: Found, changed: bool = False) -> str:
        """Why a posting kept, and not left out, has no current analysis (changed: the chosen material has)."""
        if self.evidence(found) is not EvidenceLevel.OFFICIAL:
            return EVIDENCE_LABELS[self.evidence(found)] + (
                "：它指向的官方岗位这次没有读取" if found.posting.official else "：没有登记的官方来源可以核对"
            )

        if found.detail is None:
            return "读取没有成功，这次查找不再重试" if (Tool.READ, found.ref) in self.failed else "这次没有读取详情"

        if found.status is None:
            return "这次没有核对状态"

        if found.status is CheckStatus.UNKNOWN:
            # An answer before stays the time it was last checked (checked_at), never the status now.
            unanswered = "最近一次核对没有得到回答" if found.checked_at is not None else "核对没有得到回答"

            return f"状态待确认：{unanswered}" + ("，之前的分析已撤下" if found.analysis is not None else "")

        if changed:
            return "所选经历或偏好已经变化，这次查找的分析都不算数"

        if not self.personalised:
            return NOT_PERSONALISED_WHY

        if self.analyses >= self.limits.analyses:
            return "分析次数已用完"

        return "这次没有分析"

    def outcome(self) -> SearchOutcome:
        """What the search found, once it has ended: its results, its leads, and the rest counted with why."""
        if self.ended is None:
            raise RuntimeError("a search tells what it found once it has ended")

        # Read again now: material changed since any analysis withdraws them all.
        changed = self.ended is EndReason.SOURCES_CHANGED or self.materials_changed()
        results, leads, merged = [], [], []
        unlisted: dict[Unlisted, list[PostingRef]] = {reason: [] for reason in Unlisted}

        for group in self.groups():
            kept = self.kept(group)
            merged += [found.ref for found in group if found is not kept]
            standing = self.standing(kept, changed)

            if isinstance(standing, Unlisted):
                unlisted[standing].append(kept.ref)
            elif standing is None:
                leads.append(self._lead(kept, changed))
            else:
                results.append(self._result(group, kept, standing))

        categories = list(Category)
        results.sort(key=lambda result: (categories.index(result.category), *self._rank(result.ref)))
        leads.sort(key=lambda lead: self._rank(lead.ref))

        return SearchOutcome(
            end=self.ended,
            personalised=self.personalised,
            results=tuple(results),
            leads=tuple(leads[: self.limits.leads]),
            more_leads=max(0, len(leads) - self.limits.leads),
            unlisted=tuple((reason, tuple(refs)) for reason, refs in unlisted.items() if refs),
            merged=tuple(merged),
            unverifiable=sum(record.unverifiable for record in self.searches),
            foreign=sum(record.foreign for record in self.searches),
            unsearched=rules.unsearched(self.scope, self.searched),
            unjudged=rules.unjudged(self.saved_preferences, self.confirmation.preferences),
            searches=tuple(self.searches),
            refusals=tuple(self.refusals),
        )

    def _rank(self, ref: PostingRef) -> tuple[int, int]:
        # Ideal values first, then the order found: an acceptable one is never left out for it.
        found = self.found[ref]

        return -rules.ideal_hits(self.scope, found.posting), found.order

    def _result(self, group: list[Found], kept: Found, category: Category) -> Result:
        return Result(
            ref=kept.ref,
            title=kept.posting.title,
            company=kept.posting.company,
            source=self.register.entry(kept.ref.source_id).name,
            evidence=self.evidence(kept),
            url=kept.detail.url,
            apply_url=kept.detail.apply_url,
            posted_on=kept.posting.posted_on,
            deadlines=tuple((found.ref, found.deadline) for found in group),
            read_at=kept.read_at,
            content_sha256=kept.content_sha256,
            checked_at=kept.checked_at,
            category=category,
            first_checks=rules.first_checks(kept.analysis, kept.screening),
            notes=self.notes(kept),
            analysis=kept.analysis,
        )

    def _lead(self, kept: Found, changed: bool) -> Lead:
        return Lead(
            ref=kept.ref,
            title=kept.posting.title,
            company=kept.posting.company,
            cities=kept.posting.cities,
            source=self.register.entry(kept.ref.source_id).name,
            evidence=self.evidence(kept),
            posted_on=kept.posting.posted_on,
            deadline=kept.deadline,
            checked_at=kept.checked_at,
            why=self.why(kept, changed),
            notes=self.notes(kept),
        )


class _Stop:
    """What an analysis of the search checks before and after each of its calls, as its cancel:
    the search cancelled or out of time, which ends it."""

    def __init__(self, run: SearchRun):
        self._run = run

    def is_set(self) -> bool:
        return self._run.stopping()


def start(
    database: Database,
    confirmation: ParentConfirmation,
    approval: SearchApproval | None,
    provider: AIProvider,
    *,
    register: Register,
    clock: Clock,
    cancel=None,
    monotonic: Callable[[], float] = time.monotonic,
) -> SearchRun:
    """A search of exactly this confirmation, or StartRefused with every problem found. Nothing is sent.

    provider is what every analysis of the search goes through: for a real
    service, the provider the parent's gate admitted once for the whole
    search. cancel: anything with is_set(), such as a threading.Event.
    """
    problems = [*rules.scope_problems(confirmation.scope), *_scope_words_problems(confirmation.scope)]

    if approval is None or approval.fingerprint != search_fingerprint(confirmation):
        problems.append("这次查找没有经过确认，或者确认的不是这次查找")

    problems += _sources_problems(confirmation.sources, register)
    selection = confirmation.selection
    once = len({source.item_id for source in selection}) == len(selection)

    if not once:
        problems.append("同一段经历只能选择一次")

    if len(selection) > MAX_EXPERIENCES:
        problems.append(f"一次分析最多发送 {MAX_EXPERIENCES} 段经历，现在选了 {len(selection)} 段")

    fields = [preference.field for preference in confirmation.preferences]

    if len(set(fields)) != len(fields) or any(field not in PREFERENCE_TOPICS for field in fields):
        problems.append("每项偏好只能选择一次，而且只能是求职偏好里的项")

    if any(field in rules.SCOPE_FIELDS for field in fields):
        problems.append("城市、求职类型和岗位方向由规则按查找范围核对，不随分析发送")

    if fields and not selection:
        problems.append("没有选择经历时不做个性化分析，偏好也不会发送：请不要选择偏好")

    identity = provider.identity()
    recipient = confirmation.recipient

    if (
        identity is None
        or (identity.provider, identity.requested_model) != (recipient.provider, recipient.model)
        or (recipient.endpoint is not None and identity.endpoint != recipient.endpoint)
    ):
        problems.append("分析服务不是确认里写明的接收方")

    with database.transaction() as db:
        entries, changed, saved = _read_material(db, confirmation)

    material = 0

    # An experience chosen twice is a problem already; what was read for it answers nothing.
    if once and changed:
        problems.append("所选经历已经修改、重新确认或删除")
    elif once:
        texts = _sent_texts(entries, confirmation)
        material = sum(len(text) for text in texts)

        if any(review(text) for text in texts):
            problems.append("所选经历或偏好里有疑似联系方式、证件号、地址或保密内容，不能随分析发送")

        # Not even the shortest posting would fit in one analysis with them.
        if material >= MAX_OUTBOUND_CHARS:
            problems.append(
                f"所选经历和偏好共 {material} 字，加上岗位原文会超过一次分析 {MAX_OUTBOUND_CHARS} 字的上限："
                "请少选几段经历，或排除一些栏目"
            )

    if _preferences_changed(confirmation, saved):
        problems.append("所选偏好已经修改")

    if problems:
        raise StartRefused(tuple(problems))

    return SearchRun(database, confirmation, provider, register, clock, cancel, monotonic, saved, material)


def search_postings(
    run: SearchRun,
    source_id: str,
    keywords: str,
    *,
    city: str | None = None,
    job_type: str | None = None,
    direction: str | None = None,
) -> ToolResult:
    """Search one confirmed source within the scope; a dimension left open (None) must be open in the scope."""
    halted = run.halted(Tool.SEARCH)

    if halted is not None:
        return halted

    words = " ".join(keywords.split())
    filters = tuple(
        (dimension, None if value is None else " ".join(value.split()))
        for dimension, value in ((Dimension.CITY, city), (Dimension.JOB_TYPE, job_type), (Dimension.DIRECTION, direction))
    )
    # The combination it covers: what it is narrowed to in each dimension, None where it is left open.
    covered = tuple(None if value is None else rules.normalized(dimension, value) for dimension, value in filters)
    # The same request again, whatever its spacing or case.
    request = (Tool.SEARCH, source_id, words.casefold(), covered)
    reason = (
        run.source_refusal(source_id)
        or _words_problem("检索词", words, run.limits.keyword_chars)
        or _filters_problem(run.scope, filters, run.limits.keyword_chars)
        or _barred_problem(run.scope, words)
        or ("这次检索和之前没有成功的一次完全相同，不会重试" if request in run.failed else None)
        or (f"检索次数已用完（{run.limits.searches} 次）" if len(run.searches) >= run.limits.searches else None)
    )

    if reason is not None:
        return run.refuse(Tool.SEARCH, reason)

    name = run.register.entry(source_id).name
    query = Query(words, *(value for _, value in filters))

    try:
        postings, error = run.register.adapter(source_id).search(query, run.source_seconds()), None
    except SourceError as failure:
        postings, error = (), failure

    if run.stopping():
        # Back after the search stopped, answered or failed: it counts, it is a stop rather than the
        # source failing, and nothing it gave is taken.
        run.searches.append(SearchRecord(source_id, words, filters, len(postings), Outcome.STOPPED))
        back = "回答" if error is None else _failed(error)

        return run.halted(Tool.SEARCH, [f"{name}{back}时查找已经结束，返回的岗位没有采用。"])

    if error is not None:
        run.failed.add(request)
        run.searches.append(SearchRecord(source_id, words, filters, 0, Outcome.FAILED))

        return ToolResult(Tool.SEARCH, Outcome.FAILED, f"{name}{_failed(error)}：这次检索没有结果，不会重试。", type(error).__name__)

    run.searched.add(covered)
    new, foreign, unplain = run.take(source_id, postings)
    # What was never taken is kept only as numbers: another source's records, a source anomaly, and postings
    # whose ids are not plain. Either way something came back that cannot be relied on.
    run.searches.append(
        SearchRecord(source_id, words, filters, len(postings), Outcome.DONE, unverifiable=unplain, foreign=foreign)
    )
    lines = [_line(run, found) for found in new]
    head = (
        f"{name}返回 {len(postings)} 个岗位，新的 {len(new)} 个。"
        + (f"另有 {foreign} 条记录不属于这个来源（来源异常），没有采用。" if foreign else "")
        + (f"另有 {unplain} 个岗位的编号不合要求（像联系方式或带别的符号），无法核验，没有采用。" if unplain else "")
    )

    return ToolResult(Tool.SEARCH, Outcome.DONE, "\n".join([head, *_listed(lines)]))


def get_posting(run: SearchRun, refs: Iterable[PostingRef]) -> ToolResult:
    """Read up to Limits.per_action postings this search found, or official postings its leads point to."""
    halted = run.halted(Tool.READ)

    if halted is not None:
        return halted

    refs = tuple(refs)
    reason = _refs_problem(run, refs, run.known, Tool.READ)
    due = [ref for ref in refs if _unread(run.found.get(ref))]

    if reason is None and run.reads + len(due) > run.limits.reads:
        reason = f"读取次数不够（还剩 {run.limits.reads - run.reads} 个）"

    if reason is not None:
        return run.refuse(Tool.READ, reason)

    lines, read = [], 0

    for ref in refs:
        found = run.found.get(ref)

        if ref not in due:
            lines.append(f"- [{_ref(ref)}] " + ("超出条件，不读取" if found.screening.status is Screen.OUT else "已经读过"))
            continue

        halted = run.halted(Tool.READ, lines)

        if halted is not None:
            return halted

        run.reads += 1

        try:
            detail, error = run.register.adapter(ref.source_id).read(ref, run.source_seconds()), None
        except SourceError as failure:
            detail, error = None, failure

        if run.stopping():
            # Back after the search stopped, read or failed: it counts, it is a stop rather than the
            # source failing, and it is not taken.
            back = "读到" if error is None else _failed(error)

            return run.halted(Tool.READ, [*lines, f"- [{_ref(ref)}] {back}时查找已经结束，没有采用"])

        if error is not None:
            run.failed.add((Tool.READ, ref))
            lines.append(f"- [{_ref(ref)}] {_failed(error)}，没有读到，不会重试")
            continue

        if detail.posting.ref != ref:
            run.failed.add((Tool.READ, ref))
            lines.append(f"- [{_ref(ref)}] 来源给的不是这个岗位，没有采用，不会重试")
            continue

        if not _plain_posting(detail.posting):
            run.failed.add((Tool.READ, ref))
            lines.append(f"- [{_ref(ref)}] 来源给的岗位编号不合要求，没有采用，不会重试")
            continue

        found = run.place(detail)
        read += 1
        lines.append(f"{_line(run, found)}\n  {_excerpt(detail.text)}")

    return ToolResult(Tool.READ, Outcome.DONE if read or not due else Outcome.FAILED, "\n".join(lines))


def check_posting(run: SearchRun, refs: Iterable[PostingRef]) -> ToolResult:
    """Ask the official sources of up to Limits.per_action postings found whether each is still open."""
    halted = run.halted(Tool.CHECK)

    if halted is not None:
        return halted

    refs = tuple(refs)
    reason = _refs_problem(run, refs, run.found, Tool.CHECK)

    if reason is None:
        lead = next((ref for ref in refs if run.evidence(run.found[ref]) is not EvidenceLevel.OFFICIAL), None)
        reason = None if lead is None else f"[{_ref(lead)}] 是待核验线索：要到它指向的官方岗位核对"

    due = [ref for ref in refs if ref in run.found and run.found[ref].screening.status is not Screen.OUT]

    if reason is None and run.checks + len(due) > run.limits.checks:
        reason = f"核对次数不够（还剩 {run.limits.checks - run.checks} 个）"

    if reason is not None:
        return run.refuse(Tool.CHECK, reason)

    lines, answered = [], 0

    for ref in refs:
        found = run.found[ref]

        if ref not in due:
            lines.append(f"- [{_ref(ref)}] 超出条件，不核对")
            continue

        halted = run.halted(Tool.CHECK, lines)

        if halted is not None:
            return halted

        run.checks += 1

        try:
            answer = run.register.adapter(ref.source_id).check(ref, run.source_seconds())
        except SourceError:
            answer = None

        if run.stopping():
            # It answered after the search stopped: it counts, and neither its answer nor a time is taken.
            return run.halted(Tool.CHECK, [*lines, f"- [{_ref(ref)}] 回答时查找已经结束，没有采用"])

        found.checked_at = rules.checked(found.checked_at, answer, run.clock.now())

        if answer is None or answer.status is CheckStatus.UNKNOWN:
            run.failed.add((Tool.CHECK, ref))
            found.status = CheckStatus.UNKNOWN
            lines.append(f"- [{_ref(ref)}] 来源没有回答：状态待确认，核对时间不更新，不会重试")
            continue

        found.status = answer.status
        found.checked_deadline = answer.deadline or found.checked_deadline
        answered += 1
        lines.append(f"- [{_ref(ref)}] {'仍在招聘' if answer.status is CheckStatus.OPEN else '已关闭'}，截止 {rules.stated(found.deadline)}")

    return ToolResult(Tool.CHECK, Outcome.DONE if answered or not due else Outcome.FAILED, "\n".join(lines))


def analyze_posting(run: SearchRun, ref: PostingRef) -> ToolResult:
    """Analyse one posting with exactly what the search was confirmed for, through the provider it was given."""
    halted = run.halted(Tool.ANALYZE)

    if halted is not None:
        return halted

    reason = _analysis_problem(run, ref)

    if reason is not None:
        return run.refuse(Tool.ANALYZE, reason)

    found = run.found[ref]
    posting = clean_posting(found.detail.text)
    masked, _, stopping = masked_posting(posting)
    # All one analysis sends, counted as U2 counts it: the posting as it goes out, the chosen fields and preferences.
    total = len(masked) + run.material_chars

    if stopping:
        problem = f"岗位原文里有{'、'.join(STOPPING[kind] for kind in stopping)}"
    elif len(posting) > MAX_POSTING_CHARS:
        stopping, problem = (TOO_LONG,), f"岗位原文超过 {MAX_POSTING_CHARS} 字"
    elif total > MAX_OUTBOUND_CHARS:
        stopping, problem = (OVER_LIMIT,), f"这次分析要发送的内容共 {total} 字，超过 {MAX_OUTBOUND_CHARS} 字的上限"
    else:
        problem = None

    if problem is not None:
        found.blocked = stopping

        return ToolResult(
            Tool.ANALYZE,
            Outcome.BLOCKED,
            f"[{_ref(ref)}] {problem}，外发检查拦下了它：什么都没有发送，也不计分析次数。",
            ",".join(stopping),
        )

    request = child_request(run.confirmation, masked)
    approval = derive_approval(run.confirmation, request)

    if approval is None:
        return run.refuse(Tool.ANALYZE, "这次分析和查找的确认不一致")

    try:
        result = analyze_selected_experiences(run.database, request, approval, run.provider, cancel=_Stop(run))
    except LiveBlocked as blocked:
        # Its first request may have gone out, so it counts as an analysis; the gate ends the search.
        run.analyses += 1
        found.blocked = (f"gate_{blocked.reason.value}",)
        run.end(EndReason.GATE)

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            "闸门没有放行这次分析的请求（之前发出的照常计数）：查找到此结束，不再发起新的分析。",
            blocked.reason.value,
        )

    if result.requests_sent:
        run.analyses += 1

    if run.stopping() or result.status is AnalysisStatus.CANCELLED:
        # Cancelled or out of time while it ran: what came back is not taken; what went out still counts.
        run.end(EndReason.CANCELLED)
        found.unfinished = run.ended.value

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            f"[{_ref(ref)}] 查找已经结束（{END_LABELS[run.ended]}）：这次分析的结果不采用，已经发出的请求照常计数，不再发起新的分析。",
            run.ended.value,
        )

    return _analysed(run, found, result)


def finish(run: SearchRun, claim: EndReason) -> ToolResult:
    """End the search as enough, the scope covered, or nothing suitable found; the rules may say otherwise."""
    halted = run.halted(Tool.FINISH)

    if halted is not None:
        return halted

    if claim not in CLAIMS:
        return run.refuse(Tool.FINISH, "只能以“够了”“范围内查完了”或“没有合适结果”结束")

    left = rules.unsearched(run.scope, run.searched)
    standings = [run.standing(run.kept(group)) for group in run.groups()]
    # Postings with no conclusion yet: never analysed, analysed and withdrawn, stopped or unfinished.
    unsettled = sum(standing is None or standing in (Unlisted.BLOCKED, Unlisted.INCOMPLETE) for standing in standings)
    # Postings sources gave back that could not be verified, and records they gave back as another source's:
    # something came back, so never "nothing suitable", whether or not the scope was covered.
    unverifiable = sum(record.unverifiable for record in run.searches)
    foreign = sum(record.foreign for record in run.searches)

    if run.materials_changed():
        end = EndReason.SOURCES_CHANGED
    elif run.searches and all(record.outcome is not Outcome.DONE for record in run.searches):
        end = EndReason.SOURCES_UNAVAILABLE
    elif left and len(run.searches) >= run.limits.searches:
        # It could not have covered the scope: never "nothing suitable", never only "enough".
        end = EndReason.LIMIT
    elif left and claim is EndReason.SCOPE_DONE:
        return run.refuse(Tool.FINISH, f"范围里还有 {len(left)} 个组合没查：{_listed_left(left)}")
    elif claim is EndReason.NO_RESULTS and any(isinstance(standing, Category) for standing in standings):
        return run.refuse(Tool.FINISH, "已经有完整的分析结果，不能说没有合适结果")
    elif claim is EndReason.NO_RESULTS and (unsettled or unverifiable or foreign):
        return run.refuse(Tool.FINISH, _unconcluded(unsettled, unverifiable, foreign))
    elif left and claim is EndReason.NO_RESULTS:
        # Nothing suitable in what was searched, and nobody knows of the rest: said as it is, nothing more sent.
        end = EndReason.PARTLY_NO_RESULTS
    else:
        end = claim

    run.end(end)

    return ToolResult(Tool.FINISH, Outcome.DONE, f"查找结束：{END_LABELS[end]}。")


def _unconcluded(unsettled: int, unverifiable: int, foreign: int) -> str:
    """Why nothing suitable may not be said: postings found with no conclusion, given back unverifiable, or
    given back as another source's."""
    pieces = [f"还有 {unsettled} 个岗位没有结论（没有分析、待核验，或者分析没有完成）"] if unsettled else []

    if unverifiable:
        pieces.append(f"来源返回的岗位里{'另' if unsettled else ''}有 {unverifiable} 个编号不合要求，无法核验")

    if foreign:
        pieces.append(f"来源返回的记录里{'另' if pieces else ''}有 {foreign} 条不属于该来源（来源异常）")

    return "；".join(pieces) + "，不能说没有合适结果"


def child_request(confirmation: ParentConfirmation, posting: str) -> AnalysisRequest:
    """One analysis within a search: exactly what the search was confirmed for, and this posting."""
    return AnalysisRequest(
        owner_id=confirmation.owner_id,
        selection=confirmation.selection,
        job_posting=posting,
        recipient=confirmation.recipient,
        preferences=confirmation.preferences,
        notice_version=confirmation.notice_version,
    )


def derive_approval(confirmation: ParentConfirmation, request: AnalysisRequest) -> TaskApproval | None:
    """The approval of one analysis, derived from the search's confirmation only when they agree item by item."""
    agrees = (
        request.owner_id == confirmation.owner_id
        and (request.purpose, request.mode) == (JOB_MATCH, SINGLE_REQUEST)
        and sorted(map(_source_key, request.selection)) == sorted(map(_source_key, confirmation.selection))
        and sorted(map(_preference_key, request.preferences)) == sorted(map(_preference_key, confirmation.preferences))
        and request.recipient == confirmation.recipient
        and request.notice_version == confirmation.notice_version
        and bool(request.job_posting.strip())
    )

    return TaskApproval(task_fingerprint(request)) if agrees else None


def _source_key(source) -> tuple:
    return source.item_id, source.version_id, ("*",) if source.fields is None else tuple(sorted(source.fields))


def _preference_key(preference) -> tuple:
    return preference.field, preference.text


def _read_material(db: Session, confirmation: ParentConfirmation):
    """The chosen experiences as confirmed, what keeps any from being so, and the saved preferences: read again."""
    entries, problems = read_selected_confirmations(
        db, confirmation.owner_id, {source.item_id: source.version_id for source in confirmation.selection}
    )

    return entries, problems, current_preferences(db, confirmation.owner_id)


def _preferences_changed(confirmation: ParentConfirmation, saved: dict[str, str]) -> bool:
    return any(saved.get(preference.field) != preference.text for preference in confirmation.preferences)


def _sent_texts(entries, confirmation: ParentConfirmation) -> list[str]:
    """What every analysis of the search sends besides the posting: the chosen fields, then the preferences."""
    fields = {source.item_id: source.fields for source in confirmation.selection}
    texts = [
        value
        for entry in entries
        for name, value in entry.content.items()
        if fields[entry.item_id] is None or name in fields[entry.item_id]
    ]

    return texts + [preference.text for preference in confirmation.preferences]


def _scope_words_problems(scope: SearchScope) -> list[str]:
    """The parts of a scope holding words the outbound rules stop, named by the part, never by the words.

    A scope's words go to sources as conditions and back to the next step
    as what is left to search, so none may hold contact details, identity
    numbers, an address or a confidential marker: not a dimension's values,
    ideal or excluded ones, nor the job titles looked for. Each is checked
    as written and with its spacing evened out, as keywords are.
    """
    parts = [
        (DIMENSION_LABELS[dimension], (*scope.of(dimension).values, *scope.of(dimension).ideal, *scope.of(dimension).excluded))
        for dimension in Dimension
    ]
    parts.append(("要找的岗位名称", scope.targets))

    return [
        f"{label}里有疑似联系方式、证件号、地址或保密内容，不能作为查找条件：请改正后再确认"
        for label, words in parts
        if any(review(text) or review(" ".join(text.split())) for text in words)
    ]


def _sources_problems(grants: tuple[SourceGrant, ...], register: Register) -> list[str]:
    """What keeps the sources a search was confirmed for from being the ones it would ask."""
    if not grants:
        return ["这次查找没有确认可以调用的岗位来源"]

    if len({grant.source_id for grant in grants}) != len(grants):
        return ["同一个岗位来源只能确认一次"]

    # One registered otherwise is not the one confirmed; one not registered is never asked.
    return [
        f"{grant.name}：登记的接收方、读取方式或证据等级和确认时不一致"
        for grant in grants
        if register.entry(grant.source_id) is not None and register.entry(grant.source_id).grant != grant
    ]


def _analysis_problem(run: SearchRun, ref: PostingRef) -> str | None:
    if not run.personalised:
        return "没有选择经历：这次查找不做个性化分析"

    found = run.found.get(ref)

    if found is None:
        return f"[{_ref(ref)}] 不是这次找到的岗位"

    if run.evidence(found) is not EvidenceLevel.OFFICIAL:
        official = found.posting.official

        return "待核验线索不能直接分析" + (
            f"：先读取它指向的官方岗位 [{_ref(official)}]" if official else "：没有登记的官方来源可以核对"
        )

    if found.detail is None:
        return "还没有读取岗位详情"

    if found.screening.status is Screen.OUT:
        return "超出条件的岗位不分析"

    group = run.group_of(ref)
    kept = run.kept(group)

    if kept is not found:
        return f"和 [{_ref(kept.ref)}] 是同一个岗位，只分析那一个"

    if any(other.analysis is not None for other in group):
        return "这个岗位已经分析过"

    if found.unfinished is not None:
        return "这个岗位的分析没有完成，不会重试"

    if found.blocked:
        return "这个岗位的分析已经被拦下过"

    if found.status is None:
        return "还没有核对岗位状态"

    if found.status is CheckStatus.CLOSED:
        return "岗位已关闭，不分析"

    if found.status is CheckStatus.UNKNOWN:
        return "岗位状态待确认（核对没有得到回答），先不分析"

    if run.analyses >= run.limits.analyses:
        return f"分析次数已用完（{run.limits.analyses} 个岗位）"

    return None


def _analysed(run: SearchRun, found: Found, result: AnalysisResult) -> ToolResult:
    status, tag = result.status, f"[{_ref(found.ref)}]"

    if status is AnalysisStatus.COMPLETE:
        found.analysis = result
        category = rules.classify(result, found.screening)
        # What the next decision sees of it: the category, and what to confirm without the user's own words.
        checks = [_shown(check, EXCERPT_CHARS) for check in rules.first_checks(result, found.screening, own_words=False)]

        return ToolResult(
            Tool.ANALYZE,
            Outcome.DONE,
            f"{tag} 已分析：{CATEGORY_LABELS[category]}。" + (f"申请前先确认：{'；'.join(checks)}" if checks else ""),
        )

    if status is AnalysisStatus.STALE or result.problem in CHANGED:
        run.end(EndReason.SOURCES_CHANGED)

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            f"{tag} 所选经历或偏好已经变化：这次分析不算数，查找到此结束，不再发起新的分析。",
            result.problem or status.value,
        )

    if status is AnalysisStatus.REJECTED and result.problem == RejectionReason.OUTBOUND_BLOCKED.value:
        found.blocked = (result.problem,)

        return ToolResult(Tool.ANALYZE, Outcome.BLOCKED, f"{tag} 外发检查拦下了这次分析：什么都没有发送。", result.problem)

    found.unfinished = result.problem or status.value

    if status is AnalysisStatus.REJECTED:
        run.end(EndReason.GATE)

        return ToolResult(Tool.ANALYZE, Outcome.STOPPED, f"{tag} 发送前的检查没有放行：查找到此结束。", result.problem)

    # Failed, or read by rules only: no complete result, and it is not tried again.
    return ToolResult(Tool.ANALYZE, Outcome.FAILED, f"{tag} 分析没有完成（{result.problem}），不会重试。", result.problem)


def _words_problem(label: str, words: str, most: int) -> str | None:
    """Why words may not go to a source as a condition of a search."""
    if not words:
        return f"{label}是空的"

    if len(words) > most:
        return f"{label}最多 {most} 个字"

    if not KEYWORDS.fullmatch(words):
        return f"{label}只能是岗位、技能、城市这类条件词，不能带标点或整句话"

    if review(words):
        return f"{label}里有像联系方式、证件号、地址或保密内容的文字"

    return None


def _filters_problem(scope: SearchScope, filters, most: int) -> str | None:
    for dimension, value in filters:
        problem = None if value is None else _words_problem(DIMENSION_LABELS[dimension], value, most)

        if problem is None:
            problem = rules.allows(scope, dimension, value)

        if problem is not None:
            return problem

    return None


def _barred_problem(scope: SearchScope, words: str) -> str | None:
    """Keywords naming what the user cannot accept: the scope keeps it out of every search, keywords too."""
    folded = words.casefold()

    for dimension in Dimension:
        for value in scope.of(dimension).excluded:
            if rules.normalized(dimension, value) in folded:
                return f"检索词里有不能接受的{DIMENSION_LABELS[dimension]}：{value}"

    return None


def _refs_problem(run: SearchRun, refs: tuple[PostingRef, ...], allowed, tool: Tool) -> str | None:
    if not refs:
        return "没有指定岗位"

    if len(set(refs)) != len(refs):
        return "同一个岗位列了不止一次"

    if len(refs) > run.limits.per_action:
        return f"一次最多处理 {run.limits.per_action} 个岗位"

    for ref in refs:
        if ref not in allowed:
            return f"[{_ref(ref)}] 不是这次检索得到的岗位"

        refusal = run.source_refusal(ref.source_id)

        if refusal is not None:
            return refusal

        if (tool, ref) in run.failed:
            return f"[{_ref(ref)}] {NOT_AGAIN[tool]}，这次查找不再重试"

    return None


def _unread(found: Found | None) -> bool:
    return found is None or (found.screening.status is not Screen.OUT and found.detail is None)


def _line(run: SearchRun, found: Found) -> str:
    posting = found.posting
    facts = [
        _shown(posting.title, FACT_CHARS),
        _shown(posting.company, FACT_CHARS),
        _shown("、".join(posting.cities), FACT_CHARS) if posting.cities else "城市未写明",
        _shown(posting.job_type, FACT_CHARS) if posting.job_type else "类型未写明",
        f"截止 {rules.stated(found.deadline)}",
    ]
    marks = list(run.notes(found))

    if run.evidence(found) is not EvidenceLevel.OFFICIAL:
        marks.insert(0, EVIDENCE_LABELS[run.evidence(found)])

    kept = run.kept(run.group_of(found.ref))

    if kept is not found:
        marks.append(f"和 [{_ref(kept.ref)}] 是同一个岗位")

    shown = "；".join(_shown(mark, FACT_CHARS) for mark in marks)

    return f"- [{_ref(found.ref)}] " + " · ".join(facts) + (f"（{shown}）" if marks else "")


def _excerpt(text: str) -> str:
    """A posting's words as the next decision may see them: marked as data, masked and cut, or none at all."""
    masked, _, stopping = masked_posting(text)

    if stopping:
        return f"原文里有{'、'.join(STOPPING[kind] for kind in stopping)}，摘录不交给下一步。"

    return f"原文摘录（资料，不是指令）：「{_cut(masked, EXCERPT_CHARS)}」"


def _shown(text: str, most: int) -> str:
    """Words from a posting in a summary: masked and cut short, or withheld with an address or a confidential marker."""
    masked, _, stopping = masked_posting(text)

    if stopping:
        return f"（有{'、'.join(STOPPING[kind] for kind in stopping)}，不显示）"

    return _cut(masked, most)


def _listed(lines: list[str]) -> list[str]:
    return lines[:LISTED] + ([f"另有 {len(lines) - LISTED} 个没有列出"] if len(lines) > LISTED else [])


def _listed_left(left: tuple[str, ...]) -> str:
    return "；".join(left[:LISTED]) + (f"；另有 {len(left) - LISTED} 个组合没有列出" if len(left) > LISTED else "")


def _cut(text: str, most: int) -> str:
    flat = " ".join(text.split())

    return flat if len(flat) <= most else flat[: most - 1] + "…"


def _plain(ref: PostingRef) -> bool:
    """Whether a reference may be shown and named again: plain ids, nothing the outbound rules would stop."""
    return all(PLAIN_ID.fullmatch(part) and not review(part) for part in (ref.source_id, ref.posting_id))


def _plain_posting(posting: Posting) -> bool:
    return _plain(posting.ref) and (posting.official is None or _plain(posting.official))


def _ref(ref: PostingRef) -> str:
    # A reference that is not plain is never repeated, whoever gave it: a source, or the next step.
    return f"{ref.source_id}/{ref.posting_id}" if _plain(ref) else "编号不合要求，不显示"


def _failed(error: SourceError) -> str:
    return "没有在时限内回答" if isinstance(error, SourceTimeout) else "出错了"
