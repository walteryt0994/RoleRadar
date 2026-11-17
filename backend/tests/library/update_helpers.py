"""A synthetic official source for the checks made from a kept posting's record (U3-D4a), tests only.

Its postings can change (revise), be taken down (a 404, and gone from its list), answer 404 while still listed, fail
by the faults of SyntheticSource, or run something while a call is made (during). Board has no list of its own, so a
search over it runs as over any synthetic source; ListedBoard adds the complete, unfiltered list a 404 is read with.
"""
from dataclasses import replace
from datetime import datetime

from app.discovery.contracts import CheckAnswer

from app.discovery import synthetic
from app.discovery.sources import NotFound, PartialListing, Register
from app.discovery.synthetic import CAREERS, LEADS, LEAD_POSTINGS, POSTINGS, SyntheticSource
from app.library.models import JobPosting, JobPostingCheck
from app.library.service import new_id

POSTING = "c-bj-analyst"
# A board told nothing: it answers with the posting's own facts.
AS_IS = object()
URL = f"{synthetic.ADDRESSES[CAREERS]}/{POSTING}"
# A kept result's facts as the search page would have kept them for the synthetic posting (records.posting_facts).
KEPT_FACTS = {
    "kind": "result",
    "source_name": "合成招聘官网",
    "evidence": "official",
    "title": "数据分析实习生（合成岗位，不是真实招聘）",
    "company": "合成数据科技",
    "cities": ["北京"],
    "places_stated": [],
    "job_type": "实习",
    "type_stated": None,
    "posted_on": "2026-10-20",
    "deadlines": [{"source_id": CAREERS, "posting_id": POSTING, "deadline": "2026-11-30"}],
    "url": URL,
    "apply_url": f"{URL}/apply",
    "read_at": "2026-10-29T15:00:00+00:00",
}


class Board(SyntheticSource):
    def __init__(self, source_id: str = CAREERS, postings=POSTINGS):
        super().__init__(source_id, postings)
        # Taken down: a 404, and not in its list.
        self.delisted: set[str] = set()
        # A 404, though its list still holds it.
        self.missing: set[str] = set()
        # Called with the operation's name while a call is made, before it answers.
        self.during = None
        # What its read, and its check, give as the deadline when told (F1): AS_IS keeps the posting's own.
        self.read_deadline = AS_IS
        self.check_deadline = AS_IS

    def revise(self, posting_id: str, *, text: str | None = None, **facts) -> None:
        item = self._postings[posting_id]
        self._postings[posting_id] = replace(item, posting=replace(item.posting, **facts), text=item.text if text is None else text)

    def text_of(self, posting_id: str) -> str:
        return self._postings[posting_id].text

    def read(self, ref, timeout):
        detail = self._call("read", ref, timeout, super().read)

        if self.read_deadline is AS_IS:
            return detail

        return replace(detail, posting=replace(detail.posting, deadline=self.read_deadline))

    def check(self, ref, timeout):
        answer = self._call("check", ref, timeout, super().check)

        return answer if self.check_deadline is AS_IS else CheckAnswer(answer.status, self.check_deadline)

    def _call(self, operation, ref, timeout, answer):
        if self.during is not None:
            self.during(operation)

        if ref.posting_id in self.delisted | self.missing:
            self.calls.append((operation, ref.posting_id))
            self.timeouts.append(timeout)
            raise NotFound("synthetic 404")

        return answer(ref, timeout)


class Listed:
    """A board's complete, unfiltered list as read once (app.discovery.sources.Listing, and posting_ids)."""

    def __init__(self, items, complete: bool = True):
        self._items = tuple(items)
        self.complete = complete

    @property
    def read(self) -> int:
        return len(self._items)

    def select(self, query):
        terms = query.keywords.split()

        return tuple(item.posting for item in self._items if item.listed or synthetic._matches(item.posting, query, terms))

    def posting_ids(self) -> frozenset[str]:
        return frozenset(item.posting.ref.posting_id for item in self._items)


class ListedBoard(Board):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Its list read only in part.
        self.partial = False

    def listing(self, timeout):
        self.calls.append(("listing", None))
        self.timeouts.append(timeout)

        if self.during is not None:
            self.during("listing")

        items = [item for posting_id, item in self._postings.items() if posting_id not in self.delisted]

        if self.partial:
            raise PartialListing("synthetic: read in part", Listed(items, complete=False))

        return Listed(items)


def register_with(board: Board) -> Register:
    return Register(synthetic.ENTRIES, {CAREERS: board, LEADS: SyntheticSource(LEADS, LEAD_POSTINGS)})


def kept_posting(database, owner: str, now: datetime, **overrides) -> str:
    """A posting kept from a synthetic search, as D3a keeps a result; returns its record id."""
    record_id = new_id()
    values = {
        "id": record_id,
        "owner_id": owner,
        "source_id": CAREERS,
        "posting_id": POSTING,
        "source_access": "synthetic",
        "facts": dict(KEPT_FACTS),
        "search_id": "search-1",
        "decision": None,
        "reasons": [],
        "note": None,
        "decided_at": None,
        "revision": 1,
        "kept_at": now,
        "updated_at": now,
        "deleted_at": None,
        "purge_after": None,
        "deletion_id": None,
    }
    values.update(overrides)

    with database.transaction() as db:
        db.add(JobPosting(**values))

    return record_id


def search_check(database, owner: str, record_id: str, status: str, at: datetime) -> None:
    """A check a search kept of it (D3a), with what its source answered then, or that it got no answer."""
    with database.transaction() as db:
        db.add(
            JobPostingCheck(
                id=new_id(), owner_id=owner, posting_record_id=record_id, search_id="search-1", status=status, checked_at=at, recorded_at=at
            )
        )
