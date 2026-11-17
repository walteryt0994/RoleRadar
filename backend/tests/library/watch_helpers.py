"""每日查新 (U3-D4c), tests only: a kept search summary as D3a keeps one, and synthetic sources whose postings change
between runs, fail by the city a search asks for, or run something while a search is made (on_search).

Nothing here is real: the postings are synthetic.SyntheticSource's, the companies made up and the addresses .invalid.
"""
from datetime import datetime

from app.discovery import synthetic
from app.discovery.sources import Register
from app.discovery.synthetic import CAREERS, LEAD_POSTINGS, LEADS, POSTINGS, SQL_ONLY
from app.library.models import JobSearch
from app.library.service import new_id
from update_helpers import Board, ListedBoard

# The sources a summary kept from the synthetic set records (records.search_summary).
SOURCES = [
    {"source_id": grant.source_id, "name": grant.name, "access": grant.access.value, "evidence": grant.evidence.value}
    for grant in synthetic.GRANTS
]
KEEP = frozenset(grant.source_id for grant in synthetic.GRANTS)
# 北京 or 上海, never 深圳; internships; any direction.
SCOPE = {
    "targets": ["数据分析实习生"],
    "city": {"values": ["北京", "上海"], "others": "excluded", "ideal": ["北京"], "excluded": ["深圳"]},
    "job_type": {"values": ["实习"], "others": "excluded", "ideal": [], "excluded": []},
    "direction": {"values": [], "others": "included", "ideal": [], "excluded": []},
}
BEIJING = {"source_id": CAREERS, "keywords": "数据分析", "filters": {"city": "北京", "job_type": "实习", "direction": None}}
SHANGHAI = {"source_id": CAREERS, "keywords": "数据分析", "filters": {"city": "上海", "job_type": "实习", "direction": None}}
LEADS_SH = {"source_id": LEADS, "keywords": "数据", "filters": {"city": "上海", "job_type": "实习", "direction": None}}
SEARCHES = (BEIJING, SHANGHAI, LEADS_SH)


def kept_summary(database, owner: str, now: datetime, searches=SEARCHES, *, sources=None, scope=None) -> str:
    """A search's summary kept as D3a keeps one (records.search_summary), with these searches; returns its record id."""
    record_id = new_id()
    summary = {
        "version": 3,
        "mode": "synthetic",
        "stage": "done",
        "cancelled_by": None,
        "sources": sources if sources is not None else SOURCES,
        "scope": scope if scope is not None else SCOPE,
        "experiences": [],
        "preference_fields": [],
        "recipient": {"provider": "simulation", "model": "simulated"},
        "notice_version": "discovery-preview-7",
        "limits": {},
        "end": "enough",
        "personalised": False,
        "counts": {
            "results": 0, "results_by_category": {}, "leads": 0, "unlisted": {}, "merged": 0, "unverifiable": 0, "foreign": 0,
            "skipped": 0, "blocked": {}, "refusals": 0,
        },
        "searches": [
            {"found": 0, "outcome": "done", "unverifiable": 0, "foreign": 0, "listed": None, "partial": False, "reused": False, "failure": None, **search}
            for search in searches
        ],
        "partial": [],
        "unsearched": [],
        "unjudged": [],
        "steps": {"decisions": 0, "by_tool": {}},
        "requests": {"handled": 0},
    }

    with database.transaction() as db:
        db.add(
            JobSearch(
                id=record_id, owner_id=owner, search_id=f"search-{record_id[:8]}", summary=summary, revision=1, kept_at=now,
                updated_at=now, deleted_at=None, purge_after=None, deletion_id=None,
            )
        )

    return record_id


class Watched(Board):
    """A synthetic source whose postings can be added and taken away, whose searches fail by the city asked for
    (failing), and which runs on_search with the query while a search is made, before it answers."""

    def __init__(self, source_id: str = CAREERS, postings=POSTINGS):
        super().__init__(source_id, postings)
        self.failing: dict = {}
        self.on_search = None

    def search(self, query, timeout):
        if self.on_search is not None:
            self.on_search(query)

        error = self.failing.get(query.city)

        if error is not None:
            self.calls.append(("search", query))
            self.timeouts.append(timeout)
            raise error

        return super().search(query, timeout)

    def add(self, posting_id: str, title: str = "数据分析实习生", company: str = "合成新公司", cities=("北京",), **facts):
        self._postings[posting_id] = synthetic._posting(self.source_id, posting_id, title, company, cities, SQL_ONLY, **facts)

    def take_down(self, posting_id: str) -> None:
        self._postings.pop(posting_id)

    def searches(self) -> list:
        return [query for operation, query in self.calls if operation == "search"]


class WatchedList(ListedBoard, Watched):
    """A board answering from its whole list (a ListingSource), read only in part when partial is set."""


def register_of(careers=None, leads=None) -> Register:
    return Register(
        synthetic.ENTRIES,
        {CAREERS: careers if careers is not None else Watched(), LEADS: leads if leads is not None else Watched(LEADS, LEAD_POSTINGS)},
    )
