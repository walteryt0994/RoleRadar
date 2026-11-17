"""Where postings come from, and what each source may be used for (U3-A1; U3 proposal, 3.3).

Every source is registered once (SourceEntry), with three things kept
apart: how it is reached (Access), whether the system may call it
(Permission) and what its postings can show (EvidenceLevel). None stands
for another: a search service the system may call still gives only leads,
and one way of reaching a site that is allowed says nothing of the others.

A tool calls a source only when it is registered, verified and has an
adapter here (Register.refusal), and when the search was confirmed for it
as it is registered (SourceEntry.grant): the same recipient, the same way
of reaching it and the same evidence. The register changes with the code,
under review, never by a setting. In U3-A1 only synthetic sources are
registered: the S1 check confirmed no real source that may be used, so no
tool can reach the network.

Some sources answer a search from their whole list (ListingSource, U3-D2-1),
such as a Lever board: the list is read once for a search and kept by that
search alone, and each of its searches is answered from it. A list cut short
at the source's page limit, with every record read checked, may be used as
it is (PartialListing): what it holds is taken, and nothing searched from it
counts as covered. No other failure is ever taken as a list read in part.

A source may say what it matches a search's keywords against (KeywordScope, U3-D2-E4 F1), such as a Lever
board: its titles and categories only, as whole words, with the city and the type left to the filters. A
decision is told so, and the tools refuse a search naming a city or a type in its keywords before the source
is asked, saying which words and what is left. A source that says nothing is held to nothing new.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# Access is defined with the contracts, since a confirmation names it (SourceGrant); it is used from here too.
from app.discovery.contracts import Access, CheckAnswer, EvidenceLevel, Posting, PostingDetail, PostingRef, SourceGrant


class Permission(StrEnum):
    # Checked under the admission threshold: the only kind a tool calls.
    VERIFIED = "verified"
    # The default: never called.
    UNVERIFIED = "unverified"
    # Terms or a plain statement say no: never called, its postings neither shown nor linked.
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class SourceEntry:
    source_id: str
    name: str
    access: Access
    permission: Permission
    evidence: EvidenceLevel
    # What the permission rests on, and when it was checked.
    basis: str
    checked_on: str

    @property
    def grant(self) -> SourceGrant:
        """The source as a search is confirmed for it: who receives what is asked, how, and what it can show."""
        return SourceGrant(self.source_id, self.name, self.access, self.evidence)


@dataclass(frozen=True)
class Query:
    """One search as a source receives it: words of conditions only, never the user's material."""

    keywords: str
    city: str | None
    job_type: str | None
    direction: str | None


@dataclass(frozen=True)
class KeywordScope:
    """What a source matches a search's keywords against, when it says (U3-D2-E4 F1): told to every decision, and
    held by the tools before the source is asked. A source that says nothing keeps its own ways, as before.

    A source declaring one matches keywords against the fields it names only, every keyword as a whole word: never
    its full text. The dimensions it names in filtered it matches by the search's own filters alone, so a keyword
    naming a value of one is refused before anything is sent (app.discovery.tools.search_postings): such a word can
    only leave out postings the filter takes, as "New York" did on a Lever board (validation B, 2026-11-12) and
    "Intern" may for a posting whose type says "Internship". Which words those are is bounded: a city the scope or
    the source names (places), a type the source reads a word as (type_of), a type the scope names. Nothing else
    is held against keywords, and nothing is taken out of them for a decision: a refusal says which words and what
    is left, and a decision makes the search again or not.
    """

    # What keywords are matched against, as a decision is told ("职位名、团队、部门和类型").
    fields: str
    # Matched by the search's filters alone, never by keywords.
    filtered: frozenset
    # Cities the source itself knows by name, as rules.normalized folds them, besides the scope's own.
    places: frozenset[str] = frozenset()
    # How the source reads one word as a job type, as it reads a posting's: the type it names, or None.
    type_of: Callable[[str], str | None] | None = None


def keyword_scope(adapter) -> KeywordScope | None:
    """The keyword scope an adapter declares, if it declares one (KeywordScope)."""
    declared = getattr(adapter, "keyword_scope", None)

    return declared if isinstance(declared, KeywordScope) else None


class SourceError(Exception):
    """The source failed; what was asked of it is not tried again.

    said_as_is: every message of the kind is fixed words of ours, so a page may show it as it is (failure_text,
    U3-D2-E2); a kind that leaves it False is told by what it is, its message never shown."""

    said_as_is = False


class SourceTimeout(SourceError):
    """The source did not answer in its time."""


class NotFound(SourceError):
    """The source answered that it has no such posting, as a 404 (U3-D4a): an answer, but on its own no conclusion of
    any kind, since a posting gone and an id wrong look the same. Only a check of a kept posting reads it, and only
    with a complete, unfiltered list without the posting read in the same check, from a source whose 404 is confirmed
    (app.discovery.updates), does it take it for the posting no longer published. No real source raises it yet."""


class SourceAnomaly(SourceError):
    """The source answered with what it must not (U3-D2-1): another posting or board, or not the form it documents,
    a redirect among them. Nothing more is asked of it in the same search."""


class HeldBack(SourceAnomaly):
    """A request kept back before it left (U3-D2-E2): a stop, the day, an approval already used, a record that could
    not be written, a source stopped earlier in the search. Nothing was sent. As after an anomaly, nothing more is
    asked of the source in the same search; unlike one, it is never told as an answer of the source."""


# What a page says of a request kept back, when its kind has no fixed words of its own.
NOT_SENT = "发送前停下，没有发出请求"


def failure_text(error: SourceError) -> str:
    """What the pages say of a source's failure (U3-D2-E2): its own message when its kind's messages are fixed words
    of ours (said_as_is), else what its kind comes to. Never "late" for what was not, never "answered wrongly" for
    what was not sent."""
    if error.said_as_is and str(error):
        return str(error)

    if isinstance(error, HeldBack):
        return NOT_SENT

    if isinstance(error, SourceTimeout):
        return "来源没有在时限内回答"

    if isinstance(error, SourceAnomaly):
        return "来源返回了不符合要求的内容"

    return "请求没有成功，没有收到回答"


class Listing(Protocol):
    """A source's own list of postings as read once in a search (U3-D2-1): what that search's searches of the source
    are answered from. read: how many postings it held as read; complete: whether it was read to its end."""

    @property
    def read(self) -> int: ...

    @property
    def complete(self) -> bool: ...

    def select(self, query: "Query") -> tuple[Posting, ...]: ...


class PartialListing(SourceError):
    """A list cut short at its source's page limit, every record read checked (U3-D2-1).

    The one way a list read only in part may be used: its listing holds what
    was read, which is taken, and nothing searched from it counts as covered.
    A source raises it only when its last allowed page came back full and
    every record of every page passed its checks. Raised by anything else
    than a ListingSource's listing(), it is a failure like any other.
    """

    def __init__(self, message: str, listing: Listing):
        super().__init__(message)
        self.listing = listing


@runtime_checkable
class ListingSource(Protocol):
    """A source that answers searches from its whole list (U3-D2-1), such as a Lever board.

    listing() reads the list once: a Listing read to its end, PartialListing,
    or SourceError. The search run keeps the list for itself alone, so its
    later searches of the source are answered from it, nothing asked again.
    """

    def listing(self, timeout: float) -> Listing: ...


class Adapter(Protocol):
    """How the tools reach one source. Each call is given its time and made once."""

    def search(self, query: Query, timeout: float) -> tuple[Posting, ...]: ...

    def read(self, ref: PostingRef, timeout: float) -> PostingDetail: ...

    # OPEN or CLOSED; SourceError when the source does not say.
    def check(self, ref: PostingRef, timeout: float) -> CheckAnswer: ...


class Register:
    """The sources a search knows of, and the adapters of those it may call."""

    def __init__(self, entries: tuple[SourceEntry, ...], adapters: Mapping[str, Adapter]):
        ids = [entry.source_id for entry in entries]

        if len(set(ids)) != len(ids):
            raise ValueError("a source is registered once")

        if not set(adapters) <= set(ids):
            raise ValueError("an adapter belongs to a registered source")

        self._entries = {entry.source_id: entry for entry in entries}
        self._adapters = dict(adapters)

    @property
    def entries(self) -> tuple[SourceEntry, ...]:
        return tuple(self._entries.values())

    def entry(self, source_id: str) -> SourceEntry | None:
        return self._entries.get(source_id)

    def refusal(self, source_id: str) -> str | None:
        """Why the tools may not call this source, if they may not."""
        entry = self._entries.get(source_id)

        if entry is None:
            return "来源没有登记"

        if entry.permission is Permission.PROHIBITED:
            return f"{entry.name}禁止调用"

        if entry.permission is not Permission.VERIFIED:
            return f"{entry.name}未核实，不能调用"

        if source_id not in self._adapters:
            return f"{entry.name}没有可用的读取方式"

        return None

    def adapter(self, source_id: str) -> Adapter:
        """The adapter of a source the tools may call (refusal() said None)."""
        return self._adapters[source_id]
