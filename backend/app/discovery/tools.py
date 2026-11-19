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
- A source answering from its whole list (ListingSource, U3-D2-1) has it
  read once in a search, kept by that search alone and in memory until it
  ends: later searches of the source are answered from the list, after
  every check a search is made with, and say so. A list read to its end
  covers what is searched from it. One read only in part (PartialListing)
  gives what it holds and covers nothing; every summary and the outcome
  tell it, whatever other sources cover. A list that failed is not asked
  for again in the same search, and a source that answered with another
  posting's identity or not in its documented form (SourceAnomaly) is asked
  nothing more, nor are its postings analysed: both analysis entries check a
  posting's source as it stands now, as they check the posting itself.
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
- analyze_postings analyses several such postings with one step (U3-Q1):
  the whole list is checked first, and one posting it may not analyse, a
  posting named twice, more than one step takes or more than the analyses
  left, refuses the step with nothing sent. Then each goes through
  analyze_posting in turn, with its own outbound check, approval, gate and
  count: one stopped by the outbound check leaves the rest to go on, while
  a stop of the search (the material changed, the gate, a cancellation,
  the time running out) leaves the rest undone, and the result says which.
- A result stands only while its facts do: a later check without an
  answer, or chosen material that has changed, withdraws its analysis.
- finish ends the search for a reason it may give itself; the rules have
  the last word on whether the scope was covered, combination by
  combination of its values, and nothing suitable may be said only when
  every posting found has a conclusion and no source gave back postings
  that could not be verified or that were another source's. With
  combinations left unsearched, or a source's list read only in part, it is
  said as "nothing suitable in what was searched, the rest unchecked", and
  the scope is never said to be covered. A search with experiences chosen may say
  "enough" only with a result that stands now (U3-Q2): never with none,
  nor with analyses since withdrawn. An end the rules impose comes first.

Each result's summary is short, and it may go to a model (U3 proposal, 5.4
and 5.5): a posting's own words reach it only cut down, inside a marked
excerpt, with contact details masked, and not at all when they hold an
address or a confidential marker. Of a posting whose source is admitted and
confirmed as official, a short line holding an English address is masked
whole instead, in the excerpt as in the analysis (U3-D2-2); every other
address still withholds them. Of the user's own words it holds only the
scope's values, which are conditions: a scope with any word the outbound
rules stop does not start, and its refusal does not repeat it. A posting
is named by its source's id only when that is a plain id (PLAIN_ID): one
that is not is never taken or named, only counted as a posting that could
not be verified. A record a source gives back as another source's is never
taken or named either, only counted as a source anomaly: a source gives back
its own postings, and a lead names the official posting it points to apart
from its own id. Keywords and filters are words of conditions only, within
the scope, without sentences or contact details.

A source may say what it matches keywords against (KeywordScope, U3-D2-E4
F1): a Lever board matches them only as whole words of a posting's title,
team, department and type, its place and type by the filters alone. A
search of such a source naming a city or a job type in its keywords is
refused before anything is sent (named_words): the words can only leave out
postings the filters take. The refusal says which words, what is left of the
keywords, and, when every such word's filter is set, the search as it may be
made again (SearchFix). Nothing is taken out of keywords for a decision,
nothing is relaxed and nothing else is matched: a decision makes the search
again or not, a refusal counted as any other. A search of such a source that
finds nothing says that this is no sign the source has none.
"""
import hashlib
import re
import time
from collections.abc import Callable, Iterable, Sequence
import functools
from dataclasses import dataclass, replace
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
# Moved unchanged to app.discovery.conditions (U3-D4c), shared with 每日查新; still named here as before.
from app.discovery.conditions import (  # noqa: F401
    KEYWORDS,
    NON_ASCII,
    PLAIN_ID,
    _barred_problem,
    _filters_problem,
    _plain,
    _plain_posting,
    _whole_list,
    _words_problem,
    named_words,
)
from app.discovery.contracts import (
    CATEGORY_LABELS,
    CATEGORY_NAMES,
    CLAIMS,
    DIMENSION_LABELS,
    DIMENSION_NAMES,
    END_LABELS,
    END_NAMES,
    EVIDENCE_LABELS,
    EVIDENCE_NAMES,
    NOT_PERSONALISED_WHY_KEY,
    Category,
    CheckStatus,
    Dimension,
    EndReason,
    EvidenceLevel,
    Lead,
    Outcome,
    ParentConfirmation,
    PartialRead,
    Posting,
    PostingDetail,
    PostingRef,
    Result,
    Screen,
    Screening,
    SearchApproval,
    SearchOutcome,
    SearchFix,
    SearchRecord,
    SearchScope,
    SourceGrant,
    Tool,
    ToolResult,
    Unlisted,
    search_fingerprint,
)
from app.discovery.sources import (
    NOT_SENT,
    HeldBack,
    KeywordScope,
    Listing,
    ListingSource,
    PartialListing,
    Query,
    Register,
    SourceAnomaly,
    SourceError,
    SourceTimeout,
    failure_text,
    keyword_scope,
    source_label,
)
from app.experience_analysis import PREFERENCE_TOPICS, analyze_selected_experiences
from app.job_analysis.scope import MAX_EXPERIENCES, MAX_OUTBOUND_CHARS, MAX_POSTING_CHARS, clean_posting
from app.library.clock import Clock
from app.library.db import Database
from app.library.projection import current_preferences, read_selected_confirmations
from app.live_gate import LiveBlocked
from app.outbound_review import ADDRESS, CONFIDENTIAL, masked_official_posting, masked_posting, review
from app.i18n import MessageMap, list_join, t, t_in

# The filter of each dimension, as an action names it.
FILTER_FIELDS = {Dimension.CITY: "city", Dimension.JOB_TYPE: "job_type", Dimension.DIRECTION: "direction"}
# How much of a posting's facts and words a summary carries, and how many postings it lists.
FACT_CHARS = 40
EXCERPT_CHARS = 120
LISTED = 12
# Postings of the same company, title and cities named in one posting's note (U3-D6-0 R1, F3): the others are counted,
# never listed one by one, so a note does not grow with every look-alike (each of them is still listed on its own).
ALIKE_NAMED = 2
# Analyses refused because what was chosen has changed since the search was confirmed.
CHANGED = (RejectionReason.SELECTION_INVALID.value, RejectionReason.SOURCES_CHANGED.value)
# What stopped an analysis before anything went out, besides the outbound rules' own kinds:
# a posting over U2's length, or all that one analysis would send over U2's total.
TOO_LONG = "too_long"
OVER_LIMIT = "outbound_limit"
# A read or check that failed, as a later step of the same search is told.
NOT_AGAIN = MessageMap({Tool.READ: "disc.tool.not_again_read", Tool.CHECK: "disc.tool.not_again_check"})
# What a lead read and found to point to a posting the user skipped is, when a tool names it again (U3-D3c). Said in
# the search's result language (HIDDEN_KEY); HIDDEN is its Chinese words, what a Chinese search writes as before.
HIDDEN_KEY = "disc.tool.hidden"
HIDDEN = t_in("zh-CN", HIDDEN_KEY)
# Why "enough" is refused to a search with experiences chosen and no result standing (U3-Q2): the same, by its key.
NOTHING_STANDS_KEY = "disc.tool.nothing_stands"
NOTHING_STANDS = t_in("zh-CN", NOTHING_STANDS_KEY)
# What a source declares its keywords matched against (KeywordScope.fields, as each decision is told it), said in
# the search's result language on its pages; a declaration not listed is shown as the source declares it.
SCOPE_FIELDS = MessageMap({"职位名、团队、部门和类型": "disc.tool.scope_fields_lever"})
# What stops a posting, as a summary names it: not in the outbound rules' own labels ("详细地址",
# "保密或内部内容"), which those rules stop as well.
STOPPING = MessageMap({ADDRESS: "disc.tool.stop_address", CONFIDENTIAL: "disc.tool.stop_confidential"})
# A source's whole list in this search's failed requests: one request whatever a search asks of it (U3-D2-1).
LISTING = "listing"
# Why a search stopped asking a source (SourceAnomaly): what it returned was not acceptable, its identity, its form or
# its rules. A request kept back before it left (HeldBack) is told as NOT_SENT instead: nothing was returned. What
# exactly happened is the pages' to say (failure_text, U3-D2-E2), not the next decision's.
ANOMALY = "disc.tool.anomaly"
# The rules' marks of a posting (rules.marks), codes said in the text's language: the same words in Chinese.
MARK_WORDS = MessageMap({"资深": "disc.tool.mark_senior", "缺要求": "disc.tool.mark_no_requirements"})
# A search the same as one this search already made from the same data (U3-D5): not made again, as its reason says.
REPEATED = "repeated"


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
    # What its source answered then (OPEN or CLOSED): a later check without an answer makes status UNKNOWN and never
    # takes this back (U3-D3a F1).
    answered_status: CheckStatus | None = None
    # When its latest check got no answer, if one did: the time it was tried, never an answer's.
    check_failed_at: datetime | None = None
    checked_deadline: date | None = None
    # Its analysis: only a complete one, and only taken while the search went on.
    analysis: AnalysisResult | None = None
    # When that analysis was taken, by the search's clock (U3-D3b): kept with a saved analysis, never a source's time.
    analysed_at: datetime | None = None
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
        # The postings the user skipped (U3-D3c), as confirmed: none is taken, made known, read, checked or analysed.
        self.skipped = frozenset(confirmation.skipped)
        # Those this search came across, each once however often and however (found, or pointed to by a lead).
        self.skipped_seen: set[PostingRef] = set()
        # Leads found and then read, that turned out to point to a posting the user skipped: kept among the postings
        # found, so what was done stays counted, yet never shown, grouped or acted on again.
        self.hidden: set[PostingRef] = set()
        self.searches: list[SearchRecord] = []
        # Each search answered in time and in full, as the combination it covers (rules.unsearched).
        self.searched: set[tuple[str | None, ...]] = set()
        # Each search answered only from a list read in part (U3-D2-1), as its source and the combination it would
        # have covered: tried there, covered by none of it.
        self.partly: set[tuple[str, tuple[str | None, ...]]] = set()
        # Sources whose lists were read only in part, by how many postings each held as read. Never taken away:
        # a complete search elsewhere does not make one of them complete.
        self.partial: dict[str, int] = {}
        # The lists read whole in this search, by source (ListingSource): this search's own, in memory only, and
        # dropped when it ends.
        self.listings: dict[str, Listing] = {}
        # Sources this search asks nothing more, and why (SourceAnomaly).
        self.disabled: dict[str, str] = {}
        # Requests a source failed: none is sent again in this search.
        self.failed: set[tuple] = set()
        # Each search answered in full or in part (U3-D5), by its source, words, combination and the version of what
        # its source answered from (data_version), with its number among the searches: the same again can bring
        # nothing new, and is not made.
        self.answered: dict[tuple, int] = {}
        # Each source's list as read in this search (U3-D5): its version, moved on whenever a list of it is read anew.
        self.list_versions: dict[str, int] = {}
        # Postings given back with ids that are not plain (U3-D5), each by a digest of its source and id kept here
        # for this search only, never the id: how many different ones came back, however often.
        self.unplain_seen: set[str] = set()
        self.reads = 0
        self.checks = 0
        self.analyses = 0
        self.refusals: list[str] = []
        # Where what came of each analysis tried is noted, as metadata only (U3-D2-E3): kinds, counts and lengths,
        # never a posting's words, its id or a prompt (note_analysis). None notes nothing.
        self.diagnose: Callable[[dict], None] | None = None
        self.attempts = 0
        # Why the gate let a request no further (a Blocked value), or the search's own approvals did (gate_note, in
        # words), when that ended the search (U3-D2-E3).
        self.gate_reason: str | None = None
        self.gate_note: str | None = None
        # Validation B (U3-D2-E3 R1): a source that can send nothing more ends the whole search, by these rules, with
        # why (source_note), so no decision or analysis is asked after it (sources_stopped). Other sets go on with
        # what they have.
        self.stop_with_sources = False
        self.source_note: str | None = None
        # The postings found grouped as one posting together (groups), kept until a posting is found or read; and
        # how many times they were grouped, once after each change rather than for every posting listed.
        self._groups: tuple[tuple[Found, ...], ...] | None = None
        self._group_of: dict[PostingRef, tuple[Found, ...]] = {}
        self.groupings = 0
        self.ended = None

    @property
    def ended(self) -> EndReason | None:
        return self._ended

    @ended.setter
    def ended(self, reason: EndReason | None) -> None:
        self._ended = reason

        if reason is not None:
            # Nothing is searched once the search has ended: the lists it read go with it.
            self.listings.clear()

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
        stop = t("disc.tool.1", p0=END_NAMES[self.ended])
        lines = [*done, t("disc.tool.2", p0=stop)] if done else [t("disc.tool.3", p0=stop)]

        return ToolResult(tool, Outcome.STOPPED, "\n".join(lines), self.ended.value)

    def refuse(self, tool: Tool, reason: str) -> ToolResult:
        self.refusals.append(reason)

        if len(self.refusals) >= self.limits.refusals:
            self.ended = EndReason.REFUSED

        return ToolResult(tool, Outcome.REFUSED, t("disc.tool.4", p0=reason), reason)

    def end(self, reason: EndReason) -> None:
        if self.ended is None:
            self.ended = reason

    def note_analysis(self, outcome: str, *, posting: str, outgoing: str, masked: Iterable[str] = (), **more) -> None:
        """Note what came of one analysis tried (U3-D2-E3), metadata only: its number in this search, what came of it
        (a code), the lengths of the posting, of what of it would go out and of the material, the kinds masked, and
        codes and counts (more). Never a posting's words, its id, nor a prompt. A note that cannot be kept changes
        nothing of the search."""
        self.attempts += 1

        if self.diagnose is None:
            return

        line = {
            "seq": self.attempts,
            "outcome": outcome,
            "posting_chars": len(posting),
            "outgoing_chars": len(outgoing),
            "material_chars": self.material_chars,
            "masked": sorted(set(masked)),
            **{name: value for name, value in more.items() if isinstance(value, (int, str, list)) and not isinstance(value, bool)},
        }

        try:
            self.diagnose(line)
        except Exception:  # a diagnostic is never a reason for the search to stop or fail
            pass

    def sources_stopped(self, result: ToolResult) -> ToolResult:
        """After a step that may have asked a source (validation B only, stop_with_sources): a confirmed source that
        can send nothing more in this search ends it at once, by these rules, never by asking a model; the result says
        why (source_note), the step what failed. What went out stays counted; nothing is retried. Whatever stopped the
        search first is kept."""
        if not self.stop_with_sources or self.stopping():
            return result

        for grant in self.confirmation.sources:
            adapter = self.register.adapter(grant.source_id)
            # The adapter's own word on it (lever_live.ControlledLeverSource.stopped), else this search's.
            stopped = getattr(adapter, "stopped", None)
            why = (stopped() if callable(stopped) else None) or self.disabled.get(grant.source_id)

            if why:
                self.source_note = t("disc.tool.5", p0=source_label(grant.name), p1=why)
                self.end(EndReason.SOURCES_UNAVAILABLE)

                break

        return result

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
            return t("disc.tool.6", p0=source_label(entry.name))

        if entry.grant != grant:
            return t("disc.tool.7", p0=source_label(grant.name))

        if source_id in self.disabled:
            return t("disc.tool.8", p0=source_label(grant.name), p1=self.disabled[source_id])

        return None

    def disable(self, source_id: str, why: str | None = None) -> None:
        """Ask a source nothing more in this search: it answered with what it must not (SourceAnomaly), or a request
        to it was kept back before it left (HeldBack, NOT_SENT)."""
        why = t(ANOMALY) if why is None else why
        self.disabled.setdefault(source_id, why)
        self.listings.pop(source_id, None)

    def partial_reads(self) -> tuple[PartialRead, ...]:
        """The sources whose lists were read only in part, each with the combinations searched there."""
        everything = rules.unsearched(self.scope, set())
        reads = []

        for source_id, read in self.partial.items():
            left = set(rules.unsearched(self.scope, {covered for source, covered in self.partly if source == source_id}))
            name = self.register.entry(source_id).name
            reads.append(PartialRead(source_id, name, read, tuple(shown for shown in everything if shown not in left)))

        return tuple(reads)

    def materials_changed(self) -> bool:
        """Whether the chosen experiences or preferences are no longer as confirmed, read again now."""
        if not self.confirmation.selection and not self.confirmation.preferences:
            return False

        with self.database.transaction() as db:
            _, problems, saved = _read_material(db, self.confirmation)

        return bool(problems) or _preferences_changed(self.confirmation, saved)

    def evidence(self, found: Found) -> EvidenceLevel:
        return self.register.entry(found.ref.source_id).evidence

    def take(self, source_id: str, postings: Iterable[Posting]) -> tuple[list[Found], int, int, int]:
        """Keep the postings a search gave that are new, each screened against the scope; and count, never
        taking them, those named as another source's (a source anomaly), those whose ids are not plain, and those
        the user skipped or pointing to one the user skipped (U3-D3c)."""
        new, foreign, unplain, skipped = [], 0, 0, 0

        for posting in postings:
            # Another source's posting is not this source's to give: whatever it is, it is only counted.
            if posting.ref.source_id != source_id:
                foreign += 1
                continue

            # Skipped by the user, or a lead to one so skipped (U3-D3c): not taken, never made known; counted by the
            # posting skipped, so finding it again, or another lead to it, counts it no more.
            target = self.skipped_target(posting)

            if target is not None:
                self.skipped_seen.add(target)
                skipped += 1

                if posting.ref in self.found:
                    # Taken by an earlier search of this one, and only now seen to point to a posting the user skipped
                    # (U3-D3c F1): withdrawn as one read and so seen is, from all it is shown in or acted on.
                    self.hide(posting.ref)

                continue

            # One found before is not new.
            if posting.ref in self.found:
                continue

            if not _plain_posting(posting):
                unplain += 1
                # Known again only by a digest kept for this search (U3-D5): how many different ones, never which.
                self.unplain_seen.add(hashlib.sha256(f"{posting.ref.source_id}\0{posting.ref.posting_id}".encode("utf-8")).hexdigest())
                continue

            found = Found(posting, len(self.found), rules.screen(self.scope, posting))
            self.found[posting.ref] = found
            self.known.add(posting.ref)
            self._groups = None

            if posting.official is not None:
                self.known.add(posting.official)

            new.append(found)

        return new, foreign, unplain, skipped

    def skipped_target(self, posting: Posting) -> PostingRef | None:
        """The posting the user skipped that this one is, or points to as a lead; None for any other."""
        if posting.ref in self.skipped:
            return posting.ref

        if posting.official is not None and posting.official in self.skipped:
            return posting.official

        return None

    def hide(self, ref: PostingRef) -> None:
        """Withdraw a posting found from all it may be shown in or acted on (U3-D3c): the postings grouped (grouped
        again from now on), the decider's candidates, the outcome, and every tool, which refuses it (HIDDEN). It stays
        among the postings found, so what was done with it stays counted."""
        self.hidden.add(ref)
        self._groups = None

    def shown(self) -> list[Found]:
        """The postings found that may be listed, grouped and acted on: all but leads hidden once read (U3-D3c)."""
        return [found for found in self.found.values() if found.ref not in self.hidden]

    def place(self, detail: PostingDetail) -> Found:
        """A posting as read: its source's facts now, its words, and when they were read."""
        posting = detail.posting
        found = self.found.get(posting.ref)

        if found is None:
            # The official posting a lead points to, read without being searched for.
            found = Found(posting, len(self.found), rules.screen(self.scope, posting))
            self.found[posting.ref] = found

        found.posting = posting
        # Its facts may have changed with the read, and with them which postings are one.
        self._groups = None
        found.screening = rules.screen(self.scope, posting)
        found.detail = detail
        found.read_at = self.clock.now()
        found.content_sha256 = hashlib.sha256(detail.text.encode("utf-8")).hexdigest()
        found.flags = rules.instruction_flags(detail.text)
        target = self.skipped_target(posting)

        if target is not None:
            # Read, and only then seen to be, or to point to, a posting the user skipped (U3-D3c): the read stays
            # counted, the posting skipped never becomes one to read, and this one is shown and acted on no more.
            self.skipped_seen.add(target)
            self.hide(posting.ref)
        elif posting.official is not None:
            self.known.add(posting.official)

        return found

    def groups(self) -> tuple[tuple[Found, ...], ...]:
        """The postings found, those that are one posting together, in the order first found (group_postings).

        Grouped once after each change and kept (U3-D2-1): a posting found
        (take) or read (place) groups them again, listing them never does.
        """
        if self._groups is None:
            self._groups = tuple(tuple(group) for group in group_postings(self.shown(), self.same))
            self._group_of = {found.ref: group for group in self._groups for found in group}
            self.groupings += 1

        return self._groups

    def same(self, first: Found, second: Found) -> bool:
        # A job number is taken as the same job only when both postings stand on employers' own sources.
        employers_own = self.evidence(first) is EvidenceLevel.OFFICIAL and self.evidence(second) is EvidenceLevel.OFFICIAL

        return rules.same_posting(first.posting, second.posting, employers_own=employers_own)

    def group_of(self, ref: PostingRef) -> tuple[Found, ...]:
        self.groups()

        return self._group_of[ref]

    def kept(self, group: Sequence[Found]) -> Found:
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

        if self.blocked_kinds(kept):
            return Unlisted.BLOCKED

        if kept.unfinished is not None:
            return Unlisted.INCOMPLETE

        if kept.analysis is None or changed or kept.status is not CheckStatus.OPEN:
            return None

        return rules.classify(kept.analysis, kept.screening) or Unlisted.INCOMPLETE

    def data_version(self, source_id: str, adapter) -> tuple | None:
        """What a search of the source would answer from now (U3-D5): this search's list of it, by its version; a
        source answering each search itself, as one answer for this search; None when its list would be read anew."""
        if isinstance(adapter, ListingSource):
            return None if source_id not in self.listings else ("list", self.list_versions.get(source_id, 0))

        return ("answers",)

    def unsendable_kinds(self, found: Found) -> tuple[str, ...]:
        """What would stop an analysis of a posting read, as analyze_posting finds it before sending anything (U3-D5):
        the outbound rules' kinds, TOO_LONG, or OVER_LIMIT; none when it may go, or when it was not read."""
        if found.detail is None:
            return ()

        posting = clean_posting(found.detail.text)
        masked, _, stopping = _outgoing(self, found, posting)

        if stopping:
            return stopping

        if len(posting) > MAX_POSTING_CHARS:
            return (TOO_LONG,)

        if self.personalised and len(masked) + self.material_chars > MAX_OUTBOUND_CHARS:
            return (OVER_LIMIT,)

        return ()

    def unsendable(self, found: Found) -> str | None:
        """Why a posting read cannot go out for an analysis (unsendable_kinds), in words: said as soon as it is read,
        so no step is spent on it; never cut to fit. None when it may go, or is not read."""
        kinds = self.unsendable_kinds(found)

        if not kinds:
            return None

        if kinds == (TOO_LONG,):
            return t("disc.tool.9", p0=len(clean_posting(found.detail.text)), p1=MAX_POSTING_CHARS)

        if kinds == (OVER_LIMIT,):
            return t("disc.tool.10", p0=MAX_OUTBOUND_CHARS)

        return t("disc.tool.11", p0=list_join(STOPPING[kind] for kind in kinds))

    def blocked_kinds(self, found: Found) -> tuple[str, ...]:
        """Why a posting is left out as blocked: what stopped its analysis, or, never tried, what would have (U3-D5:
        read, checked open, and unsendable in a search that analyses). The page says the same either way: nothing
        was sent."""
        if found.blocked:
            return found.blocked

        if self.personalised and found.analysis is None and found.unfinished is None and found.status is CheckStatus.OPEN:
            return self.unsendable_kinds(found)

        return ()

    def notes(self, found: Found) -> tuple[str, ...]:
        notes = [*found.screening.reasons, *found.screening.notes]
        # The marks are codes (资深, 缺要求), said here in the text's language.
        notes += [MARK_WORDS[mark] for mark in rules.marks(found.posting, found.detail.text if found.detail else None)]

        if found.analysis is None and not found.blocked:
            unsendable = self.unsendable(found)

            if unsendable is not None:
                notes.append(unsendable)

        if found.flags:
            notes.append(t("disc.tool.12"))

        group = self.group_of(found.ref)
        alike = [
            other
            for other in self.shown()
            if all(other is not item for item in group) and rules.alike(found.posting, other.posting)
        ]

        if alike:
            # One note, however many look alike (U3-D6-0 R1, F3): how many and what it means first, so that a line
            # cutting its marks short (FACT_CHARS) still says it; then a few of them by id, the rest counted. Each of
            # them stays a posting of its own, listed, read and checked on its own; none is taken for this one.
            named = list_join(f"[{_ref(other.ref)}]" for other in alike[:ALIKE_NAMED])
            rest = t("disc.tool.14") if len(alike) > ALIKE_NAMED else ""
            notes.append(t("disc.tool.15", p0=len(alike), p1=named, p2=rest))

        return tuple(notes)

    def why(self, found: Found, changed: bool = False) -> str:
        """Why a posting kept, and not left out, has no current analysis (changed: the chosen material has)."""
        if self.evidence(found) is not EvidenceLevel.OFFICIAL:
            return EVIDENCE_NAMES[self.evidence(found)] + (
                t("disc.tool.16") if found.posting.official else t("disc.tool.17")
            )

        if found.detail is None:
            return t("disc.tool.18") if (Tool.READ, found.ref) in self.failed else t("disc.tool.19")

        if found.status is None:
            return t("disc.tool.20")

        if found.status is CheckStatus.UNKNOWN:
            # An answer before stays the time it was last checked (checked_at), never the status now.
            unanswered = t("disc.tool.21") if found.checked_at is not None else t("disc.tool.22")

            return t("disc.tool.23", p0=unanswered) + (t("disc.tool.24") if found.analysis is not None else "")

        if changed:
            return t("disc.tool.25")

        if not self.personalised:
            return t(NOT_PERSONALISED_WHY_KEY)

        if self.analyses >= self.limits.analyses:
            return t("disc.tool.26")

        return t("disc.tool.27")

    def outcome(self) -> SearchOutcome:
        """What the search found, once it has ended: its results, its leads, and the rest counted with why."""
        if self.ended is None:
            raise RuntimeError("a search tells what it found once it has ended")

        # Read again now: material changed since any analysis withdraws them all.
        changed = self.ended is EndReason.SOURCES_CHANGED or self.materials_changed()
        results, leads, merged = [], [], []
        unlisted: dict[Unlisted, list[PostingRef]] = {reason: [] for reason in Unlisted}
        # Each reason an analysis was stopped for, as it was (U3-D2-E3): the pages say each, never one for all.
        blocked: dict[str, int] = {}

        for group in self.groups():
            kept = self.kept(group)
            merged += [found.ref for found in group if found is not kept]
            standing = self.standing(kept, changed)

            if standing is Unlisted.BLOCKED:
                key = ",".join(self.blocked_kinds(kept))
                blocked[key] = blocked.get(key, 0) + 1

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
            unverifiable_distinct=len(self.unplain_seen),
            foreign=sum(record.foreign for record in self.searches),
            unsearched=rules.unsearched(self.scope, self.searched),
            unjudged=rules.unjudged(self.saved_preferences, self.confirmation.preferences),
            searches=tuple(self.searches),
            refusals=tuple(self.refusals),
            partial=self.partial_reads(),
            blocked=tuple(blocked.items()),
            gate_note=self.gate_note if self.ended is EndReason.GATE else None,
            gate_reason=self.gate_reason if self.ended is EndReason.GATE else None,
            source_note=self.source_note if self.ended is EndReason.SOURCES_UNAVAILABLE else None,
            skipped=len(self.skipped_seen),
        )

    def _rank(self, ref: PostingRef) -> tuple[int, int]:
        # Ideal values first, then the order found: an acceptable one is never left out for it.
        found = self.found[ref]

        return -rules.ideal_hits(self.scope, found.posting), found.order

    def _result(self, group: Sequence[Found], kept: Found, category: Category) -> Result:
        first_checks = rules.first_checks(kept.analysis, kept.screening)

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
            first_checks=first_checks,
            # What 申请前先确认 says already is not said again among the notes (U3-D5).
            notes=tuple(note for note in self.notes(kept) if note not in first_checks),
            analysis=kept.analysis,
            cities=kept.posting.cities,
            job_type=kept.posting.job_type,
            places_stated=kept.posting.places_stated,
            type_stated=kept.posting.type_stated,
            status=kept.status,
            answered_status=kept.answered_status,
            check_failed_at=kept.check_failed_at,
            analysed_at=kept.analysed_at,
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
            job_type=kept.posting.job_type,
            places_stated=kept.posting.places_stated,
            type_stated=kept.posting.type_stated,
            status=kept.status,
            answered_status=kept.answered_status,
            check_failed_at=kept.check_failed_at,
        )


def group_postings(found: Iterable[Found], same: Callable[[Found, Found], bool]) -> list[list[Found]]:
    """Postings in groups that are one posting together, in the order first found: the grouping a search has always
    made (U3-A1), kept in one function so that what SearchRun.groups keeps can be held to a fresh grouping."""
    groups: list[list[Found]] = []

    for item in found:
        joined = [group for group in groups if any(same(item, other) for other in group)]
        rest = [group for group in groups if all(group is not other for other in joined)]
        groups = rest + [sorted([item, *(member for group in joined for member in group)], key=lambda member: member.order)]

    return sorted(groups, key=lambda group: group[0].order)


class _Stop:
    """What an analysis of the search checks before and after each of its calls, as its cancel:
    the search cancelled or out of time, which ends it."""

    def __init__(self, run: SearchRun):
        self._run = run

    def is_set(self) -> bool:
        return self._run.stopping()


def scope_check(scope: SearchScope) -> tuple[str, ...]:
    """What keeps a search of this scope from starting, as start() finds it; nothing when the scope may start.

    Read-only (U3-A3): a review page asks it before the user confirms, so it shows the very problems start() would
    refuse with. It reads no database, builds no service, starts nothing; start() still checks it all again.
    """
    return (*rules.scope_problems(scope), *_scope_words_problems(scope))


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
    diagnose: Callable[[dict], None] | None = None,
    stop_with_sources: bool = False,
) -> SearchRun:
    """A search of exactly this confirmation, or StartRefused with every problem found. Nothing is sent.

    provider is what every analysis of the search goes through: for a real
    service, the provider the parent's gate admitted once for the whole
    search. cancel: anything with is_set(), such as a threading.Event.
    diagnose, if given, is told what came of each analysis tried, as
    metadata only (SearchRun.note_analysis). stop_with_sources ends the
    search once a source can send nothing more (validation B,
    SearchRun.sources_stopped).
    """
    problems = list(scope_check(confirmation.scope))

    if approval is None or approval.fingerprint != search_fingerprint(confirmation):
        problems.append(t("disc.tool.28"))

    problems += _sources_problems(confirmation.sources, register)
    selection = confirmation.selection
    once = len({source.item_id for source in selection}) == len(selection)

    if not once:
        problems.append(t("disc.tool.29"))

    if len(selection) > MAX_EXPERIENCES:
        problems.append(t("disc.tool.30", p0=MAX_EXPERIENCES, p1=len(selection)))

    fields = [preference.field for preference in confirmation.preferences]

    if len(set(fields)) != len(fields) or any(field not in PREFERENCE_TOPICS for field in fields):
        problems.append(t("disc.tool.31"))

    if any(field in rules.SCOPE_FIELDS for field in fields):
        problems.append(t("disc.tool.32"))

    if fields and not selection:
        problems.append(t("disc.tool.33"))

    identity = provider.identity()
    recipient = confirmation.recipient

    if (
        identity is None
        or (identity.provider, identity.requested_model) != (recipient.provider, recipient.model)
        or (recipient.endpoint is not None and identity.endpoint != recipient.endpoint)
    ):
        problems.append(t("disc.tool.34"))

    with database.transaction() as db:
        entries, changed, saved = _read_material(db, confirmation)

    material = 0

    # An experience chosen twice is a problem already; what was read for it answers nothing.
    if once and changed:
        problems.append(t("disc.tool.35"))
    elif once:
        texts = _sent_texts(entries, confirmation)
        material = sum(len(text) for text in texts)

        if any(review(text) for text in texts):
            problems.append(t("disc.tool.36"))

        # Not even the shortest posting would fit in one analysis with them.
        if material >= MAX_OUTBOUND_CHARS:
            problems.append(
                t("disc.tool.37", p0=material, p1=MAX_OUTBOUND_CHARS)
            )

    if _preferences_changed(confirmation, saved):
        problems.append(t("disc.tool.38"))

    if problems:
        raise StartRefused(tuple(problems))

    run = SearchRun(database, confirmation, provider, register, clock, cancel, monotonic, saved, material)
    run.diagnose = diagnose
    run.stop_with_sources = stop_with_sources

    return run


def _asks_sources(tool):
    """A tool that may ask a source: once it returns, a source that can send nothing more may end the search
    (SearchRun.sources_stopped, validation B only)."""

    @functools.wraps(tool)
    def asking(run: SearchRun, *args, **kwargs) -> ToolResult:
        return run.sources_stopped(tool(run, *args, **kwargs))

    return asking


@_asks_sources
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
        or _words_problem(t("disc.tool.39"), words, run.limits.keyword_chars)
        or _filters_problem(run.scope, filters, run.limits.keyword_chars)
        or _barred_problem(run.scope, words)
        or (t("disc.tool.40") if request in run.failed else None)
        or (
            t("disc.tool.41", p0=run.register.entry(source_id).name)
            if (Tool.SEARCH, source_id, LISTING) in run.failed
            else None
        )
    )

    if reason is None:
        name = source_label(run.register.entry(source_id).name)
        adapter = run.register.adapter(source_id)
        # The same search from the same data as one this search made (U3-D5): it can bring nothing new, so it is not
        # made, counted as no search and no refusal (searches used up or not), and the next decision is told so
        # plainly. Other words, another combination, another source, or the same source's data read anew is another
        # search.
        version = run.data_version(source_id, adapter)
        earlier = None if version is None else run.answered.get((source_id, words.casefold(), covered, version))

        if earlier is not None:
            return ToolResult(
                Tool.SEARCH,
                Outcome.DONE,
                t("disc.tool.42", p0=name, p1=earlier),
                REPEATED,
            )

        if len(run.searches) >= run.limits.searches:
            reason = t("disc.tool.43", p0=run.limits.searches)

    if reason is not None:
        return run.refuse(Tool.SEARCH, reason)

    query = Query(words, *(value for _, value in filters))
    declared = keyword_scope(adapter)
    # Keywords naming what the source matches by a filter (U3-D2-E4 F1): refused before anything is sent, saying
    # which words and what is left; nothing taken out of them here.
    misused = None if declared is None else _keyword_problem(run, source_id, name, declared, words, filters)

    if misused is not None:
        refused, fix = misused

        return replace(run.refuse(Tool.SEARCH, refused), fix=fix)

    # A source answering from its whole list: the list this search read of it, or one read now (U3-D2-1). Every
    # check above came first, so nothing kept lets a search past them.
    listing = run.listings.get(source_id) if isinstance(adapter, ListingSource) else None
    reused, postings, error = listing is not None, (), None

    try:
        if not isinstance(adapter, ListingSource):
            postings = adapter.search(query, run.source_seconds())
        elif listing is None:
            listing = _whole_list(adapter, run.source_seconds())
    except SourceError as failure:
        listing, error = None, failure

    if listing is not None:
        postings = listing.select(query)

    if run.stopping():
        # Back after the search stopped, answered or failed: it counts, it is a stop rather than the
        # source failing, and nothing it gave is taken or kept.
        run.searches.append(SearchRecord(source_id, words, filters, len(postings), Outcome.STOPPED))
        back = t("disc.tool.44") if error is None else _failed(error)

        return run.halted(Tool.SEARCH, [t("disc.tool.45", p0=name, p1=back)])

    if error is not None:
        run.failed.add(request)
        anomaly = isinstance(error, SourceAnomaly)

        if isinstance(adapter, ListingSource):
            # Whatever a search asks of it, the source is asked for the same list: not again in this search.
            run.failed.add((Tool.SEARCH, source_id, LISTING))

        if anomaly:
            run.disable(source_id, t(NOT_SENT) if isinstance(error, HeldBack) else t(ANOMALY))

        failure = failure_text(error)
        run.searches.append(SearchRecord(source_id, words, filters, 0, Outcome.FAILED, failure=failure))

        if isinstance(error, HeldBack):
            # Nothing was sent, so nothing came back: never told as a late or wrong answer (U3-D2-E2).
            summary = t("disc.tool.46", p0=name, p1=t(NOT_SENT))
        else:
            summary = t("disc.tool.47", p0=name, p1=_failed(error)) + (t("disc.tool.48", p0=t(ANOMALY)) if anomaly else "")

        return ToolResult(Tool.SEARCH, Outcome.FAILED, summary, type(error).__name__, failure=failure)

    partial = listing is not None and not listing.complete

    if listing is not None and not reused:
        run.listings[source_id] = listing
        # A list read anew is new data (U3-D5): searches made from an earlier one do not stand for searches of it.
        run.list_versions[source_id] = run.list_versions.get(source_id, 0) + 1

    if partial:
        # What it holds is taken; the combination is tried there and covered by none of it, whatever comes after.
        run.partial[source_id] = listing.read
        run.partly.add((source_id, covered))
    else:
        run.searched.add(covered)

    hidden_before = set(run.hidden)
    new, foreign, unplain, skipped = run.take(source_id, postings)
    # Leads listed before and withdrawn by this search (U3-D3c F1): named by their own ids, never by what they point to.
    withdrawn = sorted(run.hidden - hidden_before, key=lambda posting: (posting.source_id, posting.posting_id))
    # What was never taken is kept only as numbers: another source's records, a source anomaly, and postings
    # whose ids are not plain. Either way something came back that cannot be relied on.
    run.searches.append(
        SearchRecord(
            source_id,
            words,
            filters,
            len(postings),
            Outcome.DONE,
            unverifiable=unplain,
            foreign=foreign,
            listed=None if listing is None else listing.read,
            partial=partial,
            reused=reused,
        )
    )
    run.answered[(source_id, words.casefold(), covered, run.data_version(source_id, adapter))] = len(run.searches)
    lines = [_line(run, found) for found in new]
    head = (
        (
            t("disc.tool.49", p0=name, p1=len(postings), p2=len(new))
            if reused
            else t("disc.tool.50", p0=name, p1=len(postings), p2=len(new))
        )
        + (t("disc.tool.51", p0=foreign) if foreign else "")
        + (t("disc.tool.52", p0=unplain) if unplain else "")
        # A count only (U3-D3c): never which postings, nor why the user skipped them.
        + (t("disc.tool.53", p0=skipped) if skipped else "")
        + "".join(t("disc.tool.54", p0=_ref(posting)) for posting in withdrawn)
        + (t("disc.tool.55", p0=listing.read) if partial else "")
        + (
            # A source matching keywords only as whole words of a few fields (U3-D2-E4 F1): none found is no sign
            # it has none.
            t("disc.tool.56", p0=name, p1=_scope_fields(declared))
            if declared is not None and not postings
            else ""
        )
    )

    return ToolResult(Tool.SEARCH, Outcome.DONE, "\n".join([head, *_listed(lines)]))


@_asks_sources
def get_posting(run: SearchRun, refs: Iterable[PostingRef]) -> ToolResult:
    """Read up to Limits.per_action postings this search found, or official postings its leads point to."""
    halted = run.halted(Tool.READ)

    if halted is not None:
        return halted

    refs = tuple(refs)
    reason = _refs_problem(run, refs, run.known, Tool.READ)
    due = [ref for ref in refs if _unread(run.found.get(ref))]

    if reason is None and run.reads + len(due) > run.limits.reads:
        reason = t("disc.tool.57", p0=run.limits.reads - run.reads)

    if reason is not None:
        return run.refuse(Tool.READ, reason)

    lines, read, failures, missed = [], 0, [], []

    for ref in refs:
        found = run.found.get(ref)

        if ref not in due:
            lines.append(f"- [{_ref(ref)}] " + (t("disc.tool.58") if found.screening.status is Screen.OUT else t("disc.tool.59")))
            continue

        if ref.source_id in run.disabled:
            # Stopped asking its source earlier in this step: nothing is sent, nothing counted.
            lines.append(t("disc.tool.60", p0=_ref(ref), p1=run.disabled[ref.source_id]))
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
            back = t("disc.tool.61") if error is None else _failed(error)

            return run.halted(Tool.READ, [*lines, t("disc.tool.62", p0=_ref(ref), p1=back)])

        if error is not None:
            run.failed.add((Tool.READ, ref))
            failures.append(failure_text(error))
            missed.append((ref, failure_text(error)))

            if isinstance(error, HeldBack):
                run.disable(ref.source_id, t(NOT_SENT))
                lines.append(t("disc.tool.63", p0=_ref(ref), p1=t(NOT_SENT)))
                continue

            if isinstance(error, SourceAnomaly):
                run.disable(ref.source_id)
                lines.append(t("disc.tool.64", p0=_ref(ref), p1=t(ANOMALY)))
                continue

            lines.append(t("disc.tool.65", p0=_ref(ref), p1=_failed(error)))
            continue

        if detail.posting.ref != ref:
            run.failed.add((Tool.READ, ref))
            missed.append((ref, t("disc.tool.66")))
            lines.append(t("disc.tool.67", p0=_ref(ref)))
            continue

        if not _plain_posting(detail.posting):
            run.failed.add((Tool.READ, ref))
            missed.append((ref, t("disc.tool.68")))
            lines.append(t("disc.tool.69", p0=_ref(ref)))
            continue

        found = run.place(detail)
        read += 1

        if found.ref in run.hidden:
            # Read and counted (U3-D3c), yet it points to a posting the user skipped: named no further, that posting
            # least of all, and neither is listed or acted on again.
            lines.append(t("disc.tool.70", p0=_ref(ref)))
            continue

        lines.append(f"{_line(run, found)}\n  {_excerpt(run, found, detail.text)}")

    return ToolResult(
        Tool.READ, Outcome.DONE if read or not due else Outcome.FAILED, "\n".join(lines), failure=_why(failures), failed=tuple(missed)
    )


@_asks_sources
def check_posting(run: SearchRun, refs: Iterable[PostingRef]) -> ToolResult:
    """Ask the official sources of up to Limits.per_action postings found whether each is still open."""
    halted = run.halted(Tool.CHECK)

    if halted is not None:
        return halted

    refs = tuple(refs)
    reason = _refs_problem(run, refs, run.found, Tool.CHECK)

    if reason is None:
        lead = next((ref for ref in refs if run.evidence(run.found[ref]) is not EvidenceLevel.OFFICIAL), None)
        reason = None if lead is None else t("disc.tool.71", p0=_ref(lead))

    due = [ref for ref in refs if ref in run.found and run.found[ref].screening.status is not Screen.OUT]

    if reason is None and run.checks + len(due) > run.limits.checks:
        reason = t("disc.tool.72", p0=run.limits.checks - run.checks)

    if reason is not None:
        return run.refuse(Tool.CHECK, reason)

    lines, answered, failures, missed = [], 0, [], []

    for ref in refs:
        found = run.found[ref]

        if ref not in due:
            lines.append(t("disc.tool.73", p0=_ref(ref)))
            continue

        if ref.source_id in run.disabled:
            # Stopped asking its source earlier in this step: nothing is sent, nothing counted, nothing changes.
            lines.append(t("disc.tool.60", p0=_ref(ref), p1=run.disabled[ref.source_id]))
            continue

        halted = run.halted(Tool.CHECK, lines)

        if halted is not None:
            return halted

        run.checks += 1

        try:
            answer, error = run.register.adapter(ref.source_id).check(ref, run.source_seconds()), None
        except SourceError as failure:
            answer, error = None, failure

        if run.stopping():
            # It answered after the search stopped: it counts, and neither its answer nor a time is taken.
            return run.halted(Tool.CHECK, [*lines, t("disc.tool.74", p0=_ref(ref))])

        found.checked_at = rules.checked(found.checked_at, answer, run.clock.now())

        if answer is None or answer.status is CheckStatus.UNKNOWN:
            run.failed.add((Tool.CHECK, ref))
            found.status = CheckStatus.UNKNOWN
            found.check_failed_at = run.clock.now()
            missed.append((ref, failure_text(error) if error is not None else t("disc.tool.75")))

            if error is not None:
                failures.append(failure_text(error))

            if isinstance(error, HeldBack):
                run.disable(ref.source_id, t(NOT_SENT))
                lines.append(t("disc.tool.76", p0=_ref(ref), p1=t(NOT_SENT)))
                continue

            if isinstance(error, SourceAnomaly):
                run.disable(ref.source_id)
                lines.append(t("disc.tool.77", p0=_ref(ref), p1=t(ANOMALY)))
                continue

            lines.append(t("disc.tool.78", p0=_ref(ref)))
            continue

        found.status = answer.status
        found.answered_status = answer.status
        found.checked_deadline = answer.deadline or found.checked_deadline
        answered += 1
        lines.append(t("disc.tool.79", p0=_ref(ref), p1=t("disc.tool.162") if answer.status is CheckStatus.OPEN else t("disc.tool.163"), p2=rules.stated(found.deadline)))

    return ToolResult(
        Tool.CHECK, Outcome.DONE if answered or not due else Outcome.FAILED, "\n".join(lines), failure=_why(failures), failed=tuple(missed)
    )


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
    masked, kinds, stopping = _outgoing(run, found, posting)
    # All one analysis sends, counted as U2 counts it: the posting as it goes out, the chosen fields and preferences.
    total = len(masked) + run.material_chars
    noted = {"posting": posting, "outgoing": masked, "masked": kinds}

    if stopping:
        problem = t("disc.tool.80", p0=list_join(STOPPING[kind] for kind in stopping))
    elif len(posting) > MAX_POSTING_CHARS:
        stopping, problem = (TOO_LONG,), t("disc.tool.81", p0=MAX_POSTING_CHARS)
    elif total > MAX_OUTBOUND_CHARS:
        stopping, problem = (OVER_LIMIT,), t("disc.tool.82", p0=total, p1=MAX_OUTBOUND_CHARS)
    else:
        problem = None

    if problem is not None:
        found.blocked = stopping
        run.note_analysis("blocked", reasons=list(stopping), **noted)

        return ToolResult(
            Tool.ANALYZE,
            Outcome.BLOCKED,
            t("disc.tool.83", p0=_ref(ref), p1=problem),
            ",".join(stopping),
        )

    request = child_request(run.confirmation, masked)
    approval = derive_approval(run.confirmation, request)

    if approval is None:
        run.note_analysis("refused", **noted)

        return run.refuse(Tool.ANALYZE, t("disc.tool.84"))

    try:
        result = analyze_selected_experiences(run.database, request, approval, run.provider, cancel=_Stop(run))
    except LiveBlocked as blocked:
        # Its first request may have gone out, so it counts as an analysis; the gate ends the search.
        run.analyses += 1
        found.blocked = (f"gate_{blocked.reason.value}",)
        run.end(EndReason.GATE)

        if run.ended is EndReason.GATE and run.gate_reason is None:
            run.gate_reason = blocked.reason.value

        run.note_analysis("gate", reasons=list(found.blocked), **noted)

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            t("disc.tool.85"),
            blocked.reason.value,
        )

    if result.requests_sent:
        run.analyses += 1

    run.note_analysis(
        "analysed",
        status=result.status.value,
        problem=_code(result.problem),
        requests_sent=int(result.requests_sent or 0),
        **noted,
    )

    if run.stopping() or result.status is AnalysisStatus.CANCELLED:
        # Cancelled or out of time while it ran: what came back is not taken; what went out still counts.
        run.end(EndReason.CANCELLED)
        found.unfinished = run.ended.value

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            t("disc.tool.86", p0=_ref(ref), p1=END_NAMES[run.ended]),
            run.ended.value,
        )

    return _analysed(run, found, result)


def analyze_postings(run: SearchRun, refs: Iterable[PostingRef]) -> ToolResult:
    """Analyse several postings with one step (U3-Q1): all of them checked before anything is sent, then each in
    turn through analyze_posting, exactly as one at a time; see the module docstring for when the rest go undone."""
    halted = run.halted(Tool.ANALYZE)

    if halted is not None:
        return halted

    refs = tuple(refs)
    reason = _refs_problem(run, refs, run.found, Tool.ANALYZE)

    if reason is None:
        # Each posting as analyze_posting checks it, so a list of one is refused exactly as before.
        reason = next((problem for problem in (_analysis_problem(run, ref) for ref in refs) if problem is not None), None)

    if reason is None and len(refs) > run.limits.analyses - run.analyses:
        reason = t("disc.tool.87", p0=len(refs), p1=run.limits.analyses - run.analyses)

    if reason is not None:
        # Nothing of the list is sent, the postings that could be analysed included: one refusal for the step.
        return run.refuse(Tool.ANALYZE, reason)

    lines, outcomes = [], []

    for index, ref in enumerate(refs):
        undone = [t("disc.tool.88", p0=_ref(left)) for left in refs[index:]]

        if run.stopping():
            # Stopped between two of them (a cancellation, the time running out): the rest are not sent.
            return _batch_stopped(run, refs, outcomes, [*lines, *undone])

        # Checked again right before it is sent; the whole list passed a moment ago, so this is a safeguard only.
        problem = _analysis_problem(run, ref)

        if problem is not None:
            lines += [t("disc.tool.89", p0=_ref(ref), p1=problem), *undone[1:]]
            break

        result = analyze_posting(run, ref)
        lines.append(f"- {result.summary}")
        outcomes.append(result.outcome)

        if result.outcome is Outcome.STOPPED:
            # The search ended with it (the material changed, the gate, a cancellation): the rest are not sent.
            return _batch_stopped(run, refs, outcomes, [*lines, *undone[1:]], result.reason)

    if Outcome.DONE in outcomes:
        outcome = Outcome.DONE
    elif outcomes and set(outcomes) == {Outcome.BLOCKED} and len(outcomes) == len(refs):
        outcome = Outcome.BLOCKED
    else:
        outcome = Outcome.FAILED

    return ToolResult(Tool.ANALYZE, outcome, _batch_summary(refs, outcomes, lines))


def _batch_stopped(run: SearchRun, refs, outcomes: list[Outcome], lines: list[str], reason: str | None = None) -> ToolResult:
    stop = t("disc.tool.90", p0=END_NAMES[run.ended])

    return ToolResult(Tool.ANALYZE, Outcome.STOPPED, f"{_batch_summary(refs, outcomes, lines)}\n{stop}", reason or run.ended.value)


def _batch_summary(refs: tuple[PostingRef, ...], outcomes: list[Outcome], lines: list[str]) -> str:
    """What a step of several analyses came to: how many of each, never one word for all of them, then each."""
    counted = [
        t("disc.tool.91", p0=label, p1=outcomes.count(outcome))
        for outcome, label in (
            (Outcome.DONE, t("disc.tool.92")),
            (Outcome.BLOCKED, t("disc.tool.93")),
            (Outcome.FAILED, t("disc.tool.94")),
            (Outcome.STOPPED, t("disc.tool.95")),
        )
        if outcome in outcomes
    ]
    undone = len(refs) - len(outcomes)

    if undone:
        counted.append(t("disc.tool.96", p0=undone))

    return "\n".join([t("disc.tool.97", p0=len(refs), p1=list_join((counted), "comma")), *lines])


def finish(run: SearchRun, claim: EndReason) -> ToolResult:
    """End the search as enough, the scope covered, or nothing suitable found; the rules may say otherwise."""
    halted = run.halted(Tool.FINISH)

    if halted is not None:
        return halted

    if claim not in CLAIMS:
        return run.refuse(Tool.FINISH, t("disc.tool.98"))

    left = rules.unsearched(run.scope, run.searched)
    # Lists read only in part (U3-D2-1): what was searched there covers nothing, whatever other sources covered.
    partial = run.partial_reads()
    standings = [run.standing(run.kept(group)) for group in run.groups()]
    # Postings with no conclusion yet: never analysed, analysed and withdrawn, stopped or unfinished.
    unsettled = sum(standing is None or standing in (Unlisted.BLOCKED, Unlisted.INCOMPLETE) for standing in standings)
    # Postings sources gave back that could not be verified, and records they gave back as another source's:
    # something came back, so never "nothing suitable", whether or not the scope was covered.
    # Different postings, however often each came back (U3-D5).
    unverifiable = len(run.unplain_seen)
    foreign = sum(record.foreign for record in run.searches)

    if run.materials_changed():
        end = EndReason.SOURCES_CHANGED
    elif run.searches and all(record.outcome is not Outcome.DONE for record in run.searches):
        end = EndReason.SOURCES_UNAVAILABLE
    elif left and len(run.searches) >= run.limits.searches:
        # It could not have covered the scope: never "nothing suitable", never only "enough".
        end = EndReason.LIMIT
    elif claim is EndReason.SCOPE_DONE and (left or partial):
        return run.refuse(Tool.FINISH, _not_covered(left, partial))
    elif claim is EndReason.NO_RESULTS and any(isinstance(standing, Category) for standing in standings):
        return run.refuse(Tool.FINISH, t("disc.tool.99"))
    elif claim is EndReason.NO_RESULTS and (unsettled or unverifiable or foreign):
        return run.refuse(Tool.FINISH, _unconcluded(unsettled, unverifiable, foreign))
    elif (left or partial) and claim is EndReason.NO_RESULTS:
        # Nothing suitable in what was searched, and nobody knows of the rest: said as it is, nothing more sent.
        end = EndReason.PARTLY_NO_RESULTS
    elif claim is EndReason.ENOUGH and run.personalised and not any(isinstance(standing, Category) for standing in standings):
        # Experiences chosen and no result stands now (none, or analyses since withdrawn): "enough" would say there
        # is one (U3-Q2). Refused as it is, never turned into another end.
        return run.refuse(Tool.FINISH, t(NOTHING_STANDS_KEY))
    else:
        end = claim

    run.end(end)

    return ToolResult(Tool.FINISH, Outcome.DONE, t("disc.tool.100", p0=END_NAMES[end]))


def _not_covered(left: tuple[str, ...], partial: tuple[PartialRead, ...]) -> str:
    """Why the scope may not be said to be covered: combinations no search covered, and lists read only in part."""
    if not partial:
        return t("disc.tool.101", p0=len(left), p1=_listed_left(left))

    pieces = [t("disc.tool.102", p0=len(left), p1=_listed_left(left))] if left else []
    pieces += [t("disc.tool.103", p0=source_label(read.source), p1=read.read) for read in partial]

    return list_join((pieces), "clauses") + t("disc.tool.105")


def _unconcluded(unsettled: int, unverifiable: int, foreign: int) -> str:
    """Why nothing suitable may not be said: postings found with no conclusion, given back unverifiable, or
    given back as another source's."""
    pieces = [t("disc.tool.106", p0=unsettled)] if unsettled else []

    if unverifiable:
        pieces.append(t("disc.tool.107", p0=t("disc.tool.165") if unsettled else '', p1=unverifiable))

    if foreign:
        pieces.append(t("disc.tool.108", p0=t("disc.tool.165") if pieces else '', p1=foreign))

    return list_join((pieces), "clauses") + t("disc.tool.109")


def child_request(confirmation: ParentConfirmation, posting: str) -> AnalysisRequest:
    """One analysis within a search: exactly what the search was confirmed for, and this posting."""
    return AnalysisRequest(
        owner_id=confirmation.owner_id,
        selection=confirmation.selection,
        job_posting=posting,
        recipient=confirmation.recipient,
        preferences=confirmation.preferences,
        notice_version=confirmation.notice_version,
        # The posting a source adapter read: the set lines of its head are the source's own fields (U3-D6 R5).
        posting_from_source=True,
        result_language=confirmation.result_language,
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
        and request.result_language == confirmation.result_language
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
        (DIMENSION_NAMES[dimension], (*scope.of(dimension).values, *scope.of(dimension).ideal, *scope.of(dimension).excluded))
        for dimension in Dimension
    ]
    parts.append((t("disc.tool.110"), scope.targets))

    return [
        t("disc.tool.111", p0=label)
        for label, words in parts
        if any(review(text) or review(" ".join(text.split())) for text in words)
    ]


def _sources_problems(grants: tuple[SourceGrant, ...], register: Register) -> list[str]:
    """What keeps the sources a search was confirmed for from being the ones it would ask."""
    if not grants:
        return [t("disc.tool.112")]

    if len({grant.source_id for grant in grants}) != len(grants):
        return [t("disc.tool.113")]

    # One registered otherwise is not the one confirmed; one not registered is never asked.
    return [
        t("disc.tool.114", p0=source_label(grant.name))
        for grant in grants
        if register.entry(grant.source_id) is not None and register.entry(grant.source_id).grant != grant
    ]


def _analysis_problem(run: SearchRun, ref: PostingRef) -> str | None:
    if not run.personalised:
        return t("disc.tool.115")

    found = run.found.get(ref)

    if found is None:
        return t("disc.tool.116", p0=_ref(ref))

    if ref in run.hidden:
        return f"[{_ref(ref)}] {t(HIDDEN_KEY)}"

    # Its source as it stands now (D2-1 R1): registered and verified, as this search confirmed it, and not stopped
    # after an anomaly. Both analysis entries check this here, so neither analyses through a source the search may
    # no longer ask.
    refusal = run.source_refusal(ref.source_id)

    if refusal is not None:
        return refusal

    if run.evidence(found) is not EvidenceLevel.OFFICIAL:
        official = found.posting.official

        return t("disc.tool.117") + (
            t("disc.tool.118", p0=_ref(official)) if official else t("disc.tool.17")
        )

    if found.detail is None:
        return t("disc.tool.119")

    if found.screening.status is Screen.OUT:
        return t("disc.tool.120")

    group = run.group_of(ref)
    kept = run.kept(group)

    if kept is not found:
        return t("disc.tool.121", p0=_ref(kept.ref))

    if any(other.analysis is not None for other in group):
        return t("disc.tool.122")

    if found.unfinished is not None:
        return t("disc.tool.123")

    if found.blocked:
        return t("disc.tool.124")

    if found.status is None:
        return t("disc.tool.125")

    if found.status is CheckStatus.CLOSED:
        return t("disc.tool.126")

    if found.status is CheckStatus.UNKNOWN:
        return t("disc.tool.127")

    if run.analyses >= run.limits.analyses:
        return t("disc.tool.128", p0=run.limits.analyses)

    return None


def _analysed(run: SearchRun, found: Found, result: AnalysisResult) -> ToolResult:
    status, tag = result.status, f"[{_ref(found.ref)}]"

    if status is AnalysisStatus.COMPLETE:
        found.analysis = result
        found.analysed_at = run.clock.now()
        category = rules.classify(result, found.screening)
        # What the next decision sees of it: the category, and what to confirm without the user's own words.
        checks = [_shown(check, EXCERPT_CHARS) for check in rules.first_checks(result, found.screening, own_words=False)]

        return ToolResult(
            Tool.ANALYZE,
            Outcome.DONE,
            t("disc.tool.129", p0=tag, p1=CATEGORY_NAMES[category]) + (t("disc.tool.130", p0=list_join((checks), "clauses")) if checks else ""),
        )

    if status is AnalysisStatus.STALE or result.problem in CHANGED:
        run.end(EndReason.SOURCES_CHANGED)

        return ToolResult(
            Tool.ANALYZE,
            Outcome.STOPPED,
            t("disc.tool.131", p0=tag),
            result.problem or status.value,
        )

    if status is AnalysisStatus.REJECTED and result.problem == RejectionReason.OUTBOUND_BLOCKED.value:
        found.blocked = (result.problem,)

        return ToolResult(Tool.ANALYZE, Outcome.BLOCKED, t("disc.tool.132", p0=tag), result.problem)

    found.unfinished = result.problem or status.value

    if status is AnalysisStatus.REJECTED:
        run.end(EndReason.GATE)

        return ToolResult(Tool.ANALYZE, Outcome.STOPPED, t("disc.tool.133", p0=tag), result.problem)

    # Failed, or read by rules only: no complete result, and it is not tried again.
    return ToolResult(Tool.ANALYZE, Outcome.FAILED, t("disc.tool.134", p0=tag, p1=result.problem), result.problem)


def _keyword_problem(
    run: SearchRun, source_id: str, name: str, declared: KeywordScope, words: str, filters
) -> tuple[str, SearchFix | None] | None:
    """Why a search of a source with a keyword scope may not go as asked, and how it may be made again (U3-D2-E4 F1):
    its keywords name a city or a type, which the source matches by a filter alone. None when they name none."""
    named, left = named_words(declared, run.scope, words)

    if not named:
        return None

    values = dict(filters)
    dimensions = [dimension for dimension in Dimension if any(kind is dimension for _, kind in named)]
    told = list_join(t("disc.tool.135", p0=DIMENSION_NAMES[kind], p1=text) for text, kind in named)
    labels = list_join((DIMENSION_NAMES[dimension] for dimension in dimensions), "and")
    fields = list_join(FILTER_FIELDS[dimension] for dimension in dimensions)
    reason = (
        t("disc.tool.137", p0=told, p1=name, p2=_scope_fields(declared), p3=labels, p4=fields)
    )
    left_usable = bool(left) and _words_problem(t("disc.tool.39"), left, run.limits.keyword_chars) is None
    open_ = [dimension for dimension in dimensions if values[dimension] is None]

    if not left_usable:
        return reason + t("disc.tool.138"), None

    if open_:
        # The filter is open: the search as it may be made again would be wider than the one refused. Only told.
        narrowed = list_join(t("disc.tool.139", p0=DIMENSION_NAMES[dimension], p1=FILTER_FIELDS[dimension]) for dimension in open_)
        return reason + t("disc.tool.140", p0=left, p1=narrowed), None

    fix = SearchFix(source_id, left, values[Dimension.CITY], values[Dimension.JOB_TYPE], values[Dimension.DIRECTION])

    return reason + t("disc.tool.141", p0=left), fix


def _refs_problem(run: SearchRun, refs: tuple[PostingRef, ...], allowed, tool: Tool) -> str | None:
    if not refs:
        return t("disc.tool.142")

    if len(set(refs)) != len(refs):
        return t("disc.tool.143")

    if len(refs) > run.limits.per_action:
        return t("disc.tool.144", p0=run.limits.per_action)

    for ref in refs:
        if ref in run.hidden:
            return f"[{_ref(ref)}] {t(HIDDEN_KEY)}"

        if ref not in allowed:
            return t("disc.tool.145", p0=_ref(ref))

        refusal = run.source_refusal(ref.source_id)

        if refusal is not None:
            return refusal

        if (tool, ref) in run.failed:
            return t("disc.tool.146", p0=_ref(ref), p1=NOT_AGAIN[tool])

    return None


def _unread(found: Found | None) -> bool:
    return found is None or (found.screening.status is not Screen.OUT and found.detail is None)


def _line(run: SearchRun, found: Found) -> str:
    posting = found.posting
    facts = [
        _shown(posting.title, FACT_CHARS),
        _shown(posting.company, FACT_CHARS),
        _shown(list_join(posting.cities), FACT_CHARS) if posting.cities else _not_known(t("disc.tool.147"), posting.places_stated),
        _type_fact(posting),
        t("disc.tool.148", p0=rules.stated(found.deadline)),
    ]
    marks = list(run.notes(found))

    if run.evidence(found) is not EvidenceLevel.OFFICIAL:
        marks.insert(0, EVIDENCE_NAMES[run.evidence(found)])

    kept = run.kept(run.group_of(found.ref))

    if kept is not found:
        marks.append(t("disc.tool.149", p0=_ref(kept.ref)))

    shown = list_join((_shown(mark, FACT_CHARS) for mark in marks), "clauses")

    return f"- [{_ref(found.ref)}] " + " · ".join(facts) + (t("disc.tool.150", p0=shown) if marks else "")


def _excerpt(run: SearchRun, found: Found, text: str) -> str:
    """A posting's words as the next decision may see them: marked as data, masked and cut, or none at all. Masked
    as an analysis would send them (_outgoing), so nothing masked there shows here."""
    masked, _, stopping = _outgoing(run, found, text)

    if stopping:
        return t("disc.tool.151", p0=list_join(STOPPING[kind] for kind in stopping))

    return t("disc.tool.152", p0=_cut(masked, EXCERPT_CHARS))


def _outgoing(run: SearchRun, found: Found, text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """A posting's words as they may go out (U3-D2-2): (text, kinds masked, kinds that stop it).

    English address lines are masked (masked_official_posting) only for a
    posting whose source this search may still ask as it was confirmed, and
    whose register entry says its postings are the employer's own: never
    for what a posting says of itself. Any other is masked as before.
    """
    official = run.source_refusal(found.ref.source_id) is None and run.evidence(found) is EvidenceLevel.OFFICIAL

    return masked_official_posting(text) if official else masked_posting(text)


def _type_fact(posting: Posting) -> str:
    """Its type as a summary tells it (U3-D2-E3): as read, or why not, with what was written masked and cut."""
    why = rules.type_reading(posting.job_type, posting.type_stated, posting.title)

    if why is None:
        return _shown(rules.type_word(posting.job_type), FACT_CHARS)

    if why == rules.NOT_GIVEN:
        return t("disc.tool.153", p0=rules.rule_word(rules.NOT_GIVEN))

    return t("disc.tool.154", p0=rules.rule_word(why), p1=_shown(posting.type_stated, FACT_CHARS))


def _not_known(label: str, stated: tuple[str, ...]) -> str:
    """A fact not known, as a summary tells it (U3-D2-2): not given by the source, or given and not read, with what
    was written masked and cut, or withheld, like any posting's words."""
    if rules.unknown(stated) == rules.NOT_GIVEN:
        return t("disc.tool.label_not_given", label=label, why=rules.rule_word(rules.NOT_GIVEN))

    return t("disc.tool.155", p0=label, p1=rules.rule_word(rules.NOT_READ), p2=_shown(list_join(stated), FACT_CHARS))


def _scope_fields(declared: KeywordScope) -> str:
    """What a source matches keywords against, in the text's language when the declaration is one listed."""
    return SCOPE_FIELDS[declared.fields] if declared.fields in SCOPE_FIELDS else declared.fields


def _shown(text: str, most: int) -> str:
    """Words from a posting in a summary: masked and cut short, or withheld with an address or a confidential marker."""
    masked, _, stopping = masked_posting(text)

    if stopping:
        return t("disc.tool.156", p0=list_join(STOPPING[kind] for kind in stopping))

    return _cut(masked, most)


def _listed(lines: list[str]) -> list[str]:
    return lines[:LISTED] + ([t("disc.tool.157", p0=len(lines) - LISTED)] if len(lines) > LISTED else [])


def _listed_left(left: tuple[str, ...]) -> str:
    # Each combination in the search's result language; the combinations themselves stay as rules.unsearched keeps them.
    shown = [rules.combination_words(combination) for combination in left[:LISTED]]

    return list_join(shown, "clauses") + (t("disc.tool.158", p0=len(left) - LISTED) if len(left) > LISTED else "")


def _cut(text: str, most: int) -> str:
    flat = " ".join(text.split())

    return flat if len(flat) <= most else flat[: most - 1] + "…"


def _ref(ref: PostingRef) -> str:
    # A reference that is not plain is never repeated, whoever gave it: a source, or the next step.
    return f"{ref.source_id}/{ref.posting_id}" if _plain(ref) else t("disc.tool.159")


def _code(value) -> str:
    """A reason as a diagnostic keeps it: a plain code (letters, digits, _), else nothing."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value) else ""


def _why(failures: list[str]) -> str | None:
    """Why the calls of one step failed, as the pages say it: each reason once, in the order it came."""
    return list_join((dict.fromkeys(failures)), "clauses") or None


def _failed(error: SourceError) -> str:
    return t("disc.tool.160") if isinstance(error, SourceTimeout) else t("disc.tool.161")


# For the search agent (U3-A2), which shows the next decision the postings found the way these summaries do.


def posting_line(run: SearchRun, found: Found) -> str:
    """A posting as the summaries list it: its public facts masked and cut, and its marks; none of the user's words."""
    return _line(run, found)


def shown_ref(ref: PostingRef) -> str:
    """A reference as the summaries name it: never one that is not plain."""
    return _ref(ref)
