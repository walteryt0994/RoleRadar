"""Synthetic replies in the shape the Lever Postings API documents (read 2026-11-11), for the U3-D2 offline adapter.

Nothing here is real. The boards are made up (their sites begin with "rr-demo-"), every title says it is
synthetic, and nothing goes anywhere: ReplayTransport answers only from what a test gave it and fails any other
address, as an unreachable one would. Registers here are a test's own: verified over synthetic replies, they
admit no source of the product.

The demo library is a synthetic candidate of its own, apart from the Owner's own preferences: someone looking
for data internships in San Francisco and New York, the first demo's North American sample. The product is not
limited to it.
"""
import json
from types import SimpleNamespace

from app.analysis_contracts import SelectedPreference
from app.discovery import lever
from app.discovery.contracts import DimensionScope, Others, SearchScope
from app.discovery.lever import API_HOST, HOSTED, PAGE_SIZE, LeverBoard, Reply, TransportError
from app.discovery.sources import Permission, Register
from analysis_helpers import confirmed, saved_preferences

LABEL = "synthetic, not a real job"
NORTH = LeverBoard("rr-demo-northbay", "Northbay Analytics（合成）")
HARBOR = LeverBoard("rr-demo-harbor", "Harbor Data（合成）")
TEST_BASIS = "合成回放，只在测试中使用，不联网；不是准入"
JSON_TYPE = "application/json; charset=utf-8"
REQUIREMENTS = "<li>Use Python to clean and join data</li><li>Write SQL queries for weekly reports</li>"
NICE_TO_HAVE = "<li>Tableau dashboards</li>"
DESCRIPTION = "Northbay builds dashboards for city services. Interns work with the analytics team."
SALES = SelectedPreference("work_content_no", "销售")
# The demo candidate's search: San Francisco and New York only; any type and direction (本次不限).
DEMO_SCOPE = SearchScope(
    city=DimensionScope(("San Francisco", "New York"), Others.EXCLUDED, ("San Francisco",)),
    job_type=DimensionScope(("实习",), Others.INCLUDED, ("实习",)),
    direction=DimensionScope(("数据分析",), Others.INCLUDED, ("数据分析",)),
    targets=("Data Analyst Intern",),
)


def pid(number: int, board: LeverBoard = NORTH) -> str:
    """A made-up posting id in the form Lever's docs show (a UUID)."""
    return f"{'a' if board == NORTH else 'b'}{number:07d}-0000-4000-8000-{number:012d}"


def posting(
    number: int,
    title: str = "Data Analyst Intern",
    *,
    board: LeverBoard = NORTH,
    posting_id: str | None = None,
    location: str | None = "San Francisco, CA",
    all_locations: list | None = None,
    commitment: str | None = "Intern",
    team: str | None = "Data",
    department: str | None = "Analytics",
    workplace: str | None = "hybrid",
    requirements: str = REQUIREMENTS,
    drop: tuple[str, ...] = (),
    **fields,
) -> dict:
    """One posting with every field Lever documents for JSON mode, made up; fields replaces any, drop removes some."""
    posting_id = posting_id or pid(number, board)
    item = {
        "id": posting_id,
        "text": f"{title} ({LABEL})",
        "categories": {
            "location": location,
            "commitment": commitment,
            "team": team,
            "department": department,
            "allLocations": all_locations if all_locations is not None else ([location] if location else []),
        },
        "country": "US",
        "opening": f"<p>{DESCRIPTION}</p>",
        "openingPlain": DESCRIPTION,
        "description": f"<div><p>{DESCRIPTION}</p></div>",
        "descriptionPlain": DESCRIPTION,
        "descriptionBody": "",
        "descriptionBodyPlain": "",
        "lists": [{"text": "Requirements", "content": requirements}, {"text": "Nice to have", "content": NICE_TO_HAVE}],
        "additional": "<p>This posting is synthetic test data.</p>",
        "additionalPlain": "This posting is synthetic test data.",
        "hostedUrl": f"{HOSTED}/{board.site}/{posting_id}",
        "applyUrl": f"{HOSTED}/{board.site}/{posting_id}/apply",
        "workplaceType": workplace,
        "salaryRange": {"currency": "USD", "interval": "per-hour-wage", "min": 30, "max": 38},
        "salaryDescription": "",
        "salaryDescriptionPlain": "",
    }
    item.update(fields)

    for name in drop:
        item.pop(name, None)

    return item


def list_url(board: LeverBoard, page: int = 0) -> str:
    return f"https://{API_HOST}/v0/postings/{board.site}?mode=json&skip={page * PAGE_SIZE}&limit={PAGE_SIZE}"


def detail_url(board: LeverBoard, posting_id: str) -> str:
    return f"https://{API_HOST}/v0/postings/{board.site}/{posting_id}"


def ok(data) -> Reply:
    return Reply(200, JSON_TYPE, json.dumps(data, ensure_ascii=False).encode("utf-8"))


class Ticks:
    """A made-up clock for a host guard: sleeping moves it on, nothing waits."""

    def __init__(self, start: float = 100.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class ReplayTransport:
    """Answers each address from what a test gave it, in order, the last answer repeating; an address it was not
    given fails, as an unreachable one would. An answer may be a Reply, an exception to raise, or a function of
    nothing giving one. calls keeps (url, timeout) of every request it was handed, and times the guard's clock then."""

    def __init__(self, ticks: Ticks | None = None):
        self.answers: dict[str, list] = {}
        self.calls: list[tuple[str, float]] = []
        self.times: list[float] = []
        self.ticks = ticks

    def add(self, url: str, *answers) -> "ReplayTransport":
        self.answers.setdefault(url, []).extend(answers)

        return self

    def get(self, url: str, timeout: float) -> Reply:
        self.calls.append((url, timeout))
        self.times.append(self.ticks.now if self.ticks else 0.0)
        queue = self.answers.get(url)

        if not queue:
            raise TransportError("no replay for this address")

        answer = queue.pop(0) if len(queue) > 1 else queue[0]

        while callable(answer):
            answer = answer()

        if isinstance(answer, BaseException):
            raise answer

        return answer

    def urls(self) -> list[str]:
        return [url for url, _ in self.calls]


def serve_listing(transport: ReplayTransport, board: LeverBoard, postings: list[dict]) -> None:
    """A board's list, page by page as Lever pages it: a full last page is followed by an empty one."""
    pages = [postings[start : start + PAGE_SIZE] for start in range(0, len(postings), PAGE_SIZE)] or [[]]

    if len(pages[-1]) == PAGE_SIZE:
        pages.append([])

    for number, page in enumerate(pages):
        transport.add(list_url(board, number), ok(page))


def serve_details(transport: ReplayTransport, board: LeverBoard, postings: list[dict]) -> None:
    for item in postings:
        transport.add(detail_url(board, item["id"]), ok(item))


def guard(ticks: Ticks, **options) -> lever.HostGuard:
    return lever.HostGuard(monotonic=ticks.monotonic, sleep=ticks.sleep, **options)


def verified_entry(board: LeverBoard):
    """A board verified as only a test verifies it: over synthetic replies. Not an admission."""
    return lever.entry(board, Permission.VERIFIED, basis=TEST_BASIS, checked_on="2026-11-11")


def verified_register(transport: ReplayTransport, host: lever.HostGuard, boards=(NORTH, HARBOR)) -> Register:
    return Register(
        tuple(verified_entry(board) for board in boards),
        {board.source_id: lever.LeverSource(board, transport=transport, guard=host) for board in boards},
    )


def grants(boards=(NORTH, HARBOR)):
    return tuple(verified_entry(board).grant for board in boards)


def make_demo_library(database, owners, clock) -> SimpleNamespace:
    """The demo candidate: two confirmed synthetic experiences and saved preferences, all their own."""
    owner, other = owners
    project = confirmed(
        database, owner, clock, "project",
        title="Campus transit dashboard (synthetic)",
        actions="Cleaned ridership data with Python and wrote SQL queries for weekly summaries.",
        outcome="The dashboard was used in a course showcase.",
    )
    education = confirmed(
        database, owner, clock, "education", school="Synthetic State University", major="Data Science", graduation="2027-05 (expected)",
    )
    preferences = saved_preferences(
        database, owner, clock, cities_ideal="San Francisco", cities_ok="New York", job_types_ideal="实习",
        directions_ideal="数据分析", work_content_no="销售",
    )

    return SimpleNamespace(owner=owner, other=other, project=project, education=education, preferences=preferences)
