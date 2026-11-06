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
"""
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

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


class SourceError(Exception):
    """The source failed; what was asked of it is not tried again."""


class SourceTimeout(SourceError):
    """The source did not answer in its time."""


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
