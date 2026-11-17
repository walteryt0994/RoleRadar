"""A Lever-shaped synthetic replay, for trying the 找岗位 pages offline (U3-D2-3). Nothing here is real.

Two made-up job boards answer Lever's documented addresses from data built in this module, inside this process: the
first lists 300 postings, so its list is still full at the adapter's last page and is read only in part; the second
is short and read to its end. Every company, title, place and address is invented; no posting comes from a real
board or from any earlier real run. The adapter that reads them is the product's own (app.discovery.lever), so the
pages show what it makes of Lever's shapes: a list read in part, places and types not given or not read, a title
and a type in conflict, an office address masked before an analysis, and a posting stopped for a confidential
marker.

Kept apart from any real source:

- The transport (_Replay) answers from the boards below and nothing else. It holds no socket and imports no network
  client; an address it does not know is a 404. app.discovery.lever_http is never imported here.
- The boards are this module's own, under sites beginning with "rr-demo-". Their register is built here for the
  replay alone: the product's Lever register (app.discovery.lever.register) stays unverified and closed.
- Their entries say so: reached as synthetic data (Access.SYNTHETIC), verified only as a replay is (BASIS), named
  as an offline demo. The links they give point at Lever's hosted pages of boards that do not exist: the pages show
  them as text only, and nothing opens them.
"""
import json
import re

from app.discovery import lever
from app.discovery.contracts import Access, EvidenceLevel
from app.discovery.lever import API_HOST, HOSTED, PAGE_SIZE, LeverBoard, Reply
from app.discovery.sources import Permission, Register, SourceEntry

KEY = "lever-replay"
LABEL = "Lever 格式的合成回放（离线演示）"
BASIS = "离线演示用的合成回放：在本机进程里回答，不联网，不是准入，也不是真实岗位"
CHECKED_ON = "2026-11-11"
SYNTHETIC = "This posting is synthetic demo data, not a real job."
JSON_TYPE = "application/json; charset=utf-8"
# The first board is still full at the adapter's last page: read only in part. The second is read to its end.
LONG = LeverBoard("rr-demo-alpha", "合成演示公司 A")
SHORT = LeverBoard("rr-demo-beta", "合成演示公司 B")
LONG_COUNT = lever.MAX_PAGES * PAGE_SIZE
ADDRESS = re.compile(
    rf"https://{re.escape(API_HOST)}/v0/postings/(?P<site>[A-Za-z0-9._-]+)"
    r"(?:/(?P<id>[A-Za-z0-9-]+)|\?mode=json&skip=(?P<skip>\d{1,6})&limit=(?P<limit>\d{1,4}))"
)


def posting_id(board: LeverBoard, number: int) -> str:
    """A made-up id in the form Lever's docs show (a UUID)."""
    return f"{'a' if board == LONG else 'b'}{number:07d}-0000-4000-8000-{number:012d}"


def _posting(
    board: LeverBoard,
    number: int,
    title: str,
    *,
    location: str | None = "San Francisco, CA",
    commitment: str | None = "Intern",
    team: str = "Data",
    department: str = "Analytics",
    workplace: str = "hybrid",
    additional: str = "",
) -> dict:
    identity = posting_id(board, number)
    hosted = f"{HOSTED}/{board.site}/{identity}"
    words = "Synthetic demo team. Interns clean data with Python and write SQL for weekly reports."

    return {
        "id": identity,
        "text": f"{title} (synthetic demo)",
        "categories": {
            "location": location,
            "commitment": commitment,
            "team": team,
            "department": department,
            "allLocations": [location] if location else [],
        },
        "country": "US",
        "descriptionPlain": words,
        "description": f"<p>{words}</p>",
        "lists": [
            {"text": "Requirements", "content": "<li>Use Python to clean and join data</li><li>Write SQL for weekly reports</li>"},
            {"text": "Nice to have", "content": "<li>Dashboards</li>"},
        ],
        "additionalPlain": "\n".join(piece for piece in (additional, SYNTHETIC) if piece),
        "hostedUrl": hosted,
        "applyUrl": f"{hosted}/apply",
        "workplaceType": workplace,
    }


def _long_board() -> list[dict]:
    """300 postings: a few data internships the pages can show, and roles no search of the demo matches."""
    shown = [
        # A one-line office address, masked whole before an analysis (U3-D2-2).
        _posting(LONG, 1, "Data Analyst Intern", additional="Office (synthetic):\n500 Example Street\nThe team meets there twice a week."),
        _posting(LONG, 2, "Data Analyst Intern", location="New York, NY"),
        # A title stating an internship and a type field that is not one: the type stays unknown.
        _posting(LONG, 3, "Data Analyst Internship", location="San Francisco, CA", commitment="Full-time"),
        # A place given and not read as a city, and one not given at all.
        _posting(LONG, 4, "Data Analyst Intern", location="London, United Kingdom"),
        _posting(LONG, 5, "Data Analyst Intern", location=None),
        # A confidential marker on the address line: no masking lets it go out (U3-D2-2 R1).
        _posting(LONG, 6, "Data Analyst Intern", additional="Internal only: 200 Example Avenue\nRestricted hiring plan follows."),
    ]
    rest = [
        _posting(LONG, number, "Software Engineer", location="London, United Kingdom", commitment="Full-time",
                 team="Engineering", department="Platform")
        for number in range(len(shown) + 1, LONG_COUNT + 1)
    ]

    return shown + rest


def _short_board() -> list[dict]:
    return [
        _posting(SHORT, 1, "Data Analyst Intern", location="New York, NY"),
        _posting(SHORT, 2, "Product Designer", location="San Francisco, CA", commitment="Full-time", team="Design",
                 department="Product"),
        _posting(SHORT, 3, "Data Analyst", location="New York, NY", commitment="Full-time"),
    ]


class _Replay:
    """Lever's documented addresses, answered from the boards above in this process: never a network request."""

    def __init__(self):
        self.boards = {LONG.site: _long_board(), SHORT.site: _short_board()}
        self.requests: list[str] = []

    def get(self, url: str, timeout: float) -> Reply:
        self.requests.append(url)
        found = ADDRESS.fullmatch(url)
        items = self.boards.get(found["site"]) if found else None

        if items is None:
            return Reply(404, JSON_TYPE, b'{"ok":false,"error":"Document not found"}')

        if found["id"] is not None:
            item = next((item for item in items if item["id"] == found["id"]), None)

            return _json(item) if item is not None else Reply(404, JSON_TYPE, b'{"ok":false,"error":"Document not found"}')

        skip, limit = int(found["skip"]), int(found["limit"])

        return _json(items[skip : skip + limit])


def _json(data) -> Reply:
    return Reply(200, JSON_TYPE, json.dumps(data, ensure_ascii=False).encode("utf-8"))


def entry(board: LeverBoard) -> SourceEntry:
    return SourceEntry(
        board.source_id, f"{board.company}（Lever 格式合成回放）", Access.SYNTHETIC, Permission.VERIFIED, EvidenceLevel.OFFICIAL, BASIS,
        CHECKED_ON,
    )


ENTRIES = tuple(entry(board) for board in (LONG, SHORT))
GRANTS = tuple(item.grant for item in ENTRIES)


def register() -> Register:
    """The replay's boards, new for each search: their own transport and guard. Nothing waits between requests, as
    nothing goes to a host; the guard still counts them against its budget."""
    replay = _Replay()
    guard = lever.HostGuard(interval=0.0)

    return Register(
        ENTRIES, {board.source_id: lever.LeverSource(board, transport=replay, guard=guard) for board in (LONG, SHORT)}
    )
