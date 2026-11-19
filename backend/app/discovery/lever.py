"""The Lever Postings API as a source of postings, offline only (U3-D2, ahead of its batch; 2026-11-11).

One board, a company's Lever job site, is one source. Its postings are
listed, read and checked through the API as Lever documents it
(https://github.com/lever/postings-api, read 2026-11-11):

- GET /v0/postings/SITE?mode=json&skip=X&limit=Y lists the published
  postings of a site, and GET /v0/postings/SITE/POSTING-ID gives one. Only
  published postings are visible, so a check can say no more than "still
  published when checked": never that the employer is still hiring, nor
  that an application will go through.
- The API has no full-text search. A search here reads the board's own
  list and keeps, in this process, the postings whose title, team,
  department or commitment hold every keyword as a whole word. It sends no
  q parameter, and none of the category filters either: those need the
  employer's own exact values. A posting's place is matched by the city
  filter and its type by the type filter, never by keywords: the board says
  so (KEYWORD_SCOPE, U3-D2-E4 F1), every decision is told, and a search
  naming a city or a type in its keywords is refused before the list is
  read. "Software Engineer Intern New York" with New York and internships
  as filters found nothing in validation B (2026-11-12): no title holds
  "New York", and "Intern" is no whole word of "Internship".
- Only the documented fields are read. The API documents no date, so a
  posting's dates are never stated (未提供). A field left out stays
  unknown, and so does one this module cannot read plainly: a place that
  is not plainly a city, a commitment other than an internship or campus
  hire. No direction is ever made up from a team's name.

Kept apart, as in app.discovery.sources:

- Admission. A board is registered unverified (entry), so no tool calls it
  (Register.refusal), until the code says otherwise under review. Tests
  build verified entries of their own over synthetic replies; nothing
  here turns a test passing into permission.
- The network. This module has no way to it. The transport a board has
  unless another is passed in (NoNetwork) sends nothing. The one that does
  (app.discovery.lever_http) is imported by nothing in the product: only
  a separately approved one-off validation may pass it in. A transport
  sends one GET as it is asked: no retry, no redirect followed, no cookie
  and no credential.
- What a call may cost. Every request goes through the one HostGuard of
  its host: it spaces requests, counts them against a hard budget, never
  starts one the call has no time left for, and stops for good once the
  host answers 401, 402, 403, 407 or 429. For a validation it also holds a
  batch's network window and stops after failures in a row.

How answers are taken (D precheck 10.2):

- search: every page a JSON list of documented postings no longer than
  asked for; every posting's links naming this board and its own id; and no
  id given twice with different contents. A list read to its end, a page
  shorter than asked for within MAX_PAGES, is the board's whole list. One
  whose last allowed page still came back full, every record of it checked,
  is the list read in part (ListingIncomplete, U3-D2-1): what it holds may
  be taken, never as the whole list, and nothing searched from it counts as
  covered. Anything else fails the whole search (SourceError): nothing of it
  is taken, the tools do not try it again, and its combination of the scope
  is not counted as searched. Another board's or posting's identity, or a
  reply not in the documented form, is an anomaly (SourceAnomaly): the
  search stops asking this board.
- The list is read once per search (listing): the search keeps it, and its
  later searches of the board are answered from it in this process.
- read: the posting's own reply, its id and links checked the same way.
- check: OPEN only for a valid reply naming this very posting. A 404, a
  refusal, a timeout or an odd reply is no answer (SourceError), so the
  status needs confirming. Nothing here says CLOSED: that takes a complete
  unfiltered listing without the posting and a 404 for it in one check,
  and the real form of Lever's 404 is not confirmed yet (D4).
"""
import json
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Protocol

from app.discovery.contracts import (
    CheckAnswer,
    CheckStatus,
    Dimension,
    EvidenceLevel,
    Posting,
    PostingDetail,
    PostingRef,
)
from app.discovery.rules import NOT_GIVEN, normalized
from app.discovery.sources import (
    Access,
    HeldBack,
    KeywordScope,
    PartialListing,
    Permission,
    Query,
    Register,
    SourceAnomaly,
    SourceEntry,
    SourceError,
    SourceTimeout,
)
from app.i18n import t

DOCS = "https://github.com/lever/postings-api"
DOCS_CHECKED_ON = "2026-11-11"
# The global instance. The EU one (api.eu.lever.co, jobs.eu.lever.co) is not read: its robots were never checked.
API_HOST = "api.lever.co"
HOSTED = "https://jobs.lever.co"
# A board's site and a posting's id as D1 accepted them; nothing else is ever put into a request. "lever-" and a
# site stay a plain source id (app.discovery.tools.PLAIN_ID: at most 64 characters).
SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,57}")
POSTING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
PAGE_SIZE = 100
MAX_PAGES = 3
# api.lever.co/robots.txt asks for 1 second between requests (read 2026-11-10); D1 kept 2.
MIN_INTERVAL = 2.0
REQUEST_SECONDS = 15.0
# The least time a request is started with: with less left, it is not sent.
MIN_REQUEST_SECONDS = 2.0
MAX_BYTES = 5 * 1024 * 1024
# One search with the default limits: 4 searches of up to 3 pages, 10 reads and 10 checks.
REQUEST_BUDGET = 32
# A host asking for a login, a payment or a proxy, or saying too many requests: nothing more is sent to it.
STOP_STATUSES = frozenset({401, 402, 403, 407, 429})
UNADMITTED = (
    "未核实：Lever 公开职位接口文档写明已发布岗位公开可见、可能被第三方读取（2026-11-11 核对）；"
    "持续读取、展示、保存和交给模型都没有依据。按 Owner 认可的企业名单逐家审核准入前不调用"
)

# A place is a city only when it says so plainly; anything wider, vaguer, remote or several at once stays unknown.
NOT_A_CITY = re.compile(
    r"(?<![a-z])(remote|hybrid|anywhere|global|worldwide|multiple|various|flexible|distributed|virtual|tbd|"
    r"nationwide|greater|area|region|metro)(?![a-z])|[;/|&、]|远程|多地|全国|不限|地区|或",
    re.IGNORECASE,
)
# "New York or Boston": several places in one. Case kept, so "Portland, OR" stays a city of Oregon.
SEVERAL = re.compile(r"(?<![A-Za-z])(or|and|Or|And)(?![A-Za-z])")
PLACE_NAME = re.compile(r"[A-Za-z][A-Za-z .'-]{0,40}")
REGION_CODE = re.compile(r"[A-Z]{2}")
# Cities RoleRadar's users write in Chinese, as Lever postings may write them in English.
CITY_NAMES = {
    "beijing": "北京",
    "shanghai": "上海",
    "shenzhen": "深圳",
    "guangzhou": "广州",
    "hangzhou": "杭州",
    "chengdu": "成都",
    "nanjing": "南京",
    "wuhan": "武汉",
    "suzhou": "苏州",
    "xi'an": "西安",
    "xian": "西安",
    "tianjin": "天津",
    "chongqing": "重庆",
    "xiamen": "厦门",
    "changsha": "长沙",
    "hong kong": "香港",
}
CHINESE_CITIES = frozenset(CITY_NAMES.values())
# A commitment that plainly says what kind of job it is; any other stays unknown.
INTERNSHIP = re.compile(r"(?<![a-z])(interns?|internships?)(?![a-z])|实习", re.IGNORECASE)
CAMPUS = re.compile(r"校招|校园招聘|应届")
NON_ASCII = re.compile(r"[^\x00-\x7f]")


# The errors below say what happened in fixed words of this module (said_as_is): a page may show them as they are.


class NetworkClosed(SourceError):
    """No transport that sends: nothing was sent."""

    said_as_is = True


class HostStopped(SourceError):
    """The host refused us, or the budget is spent: nothing more is sent to it."""

    said_as_is = True


class NotFound(SourceError):
    """A 404: no conclusion of any kind, a posting gone and an address wrong look the same."""

    said_as_is = True


class TimeUsedUp(SourceTimeout):
    """Too little of a call's time, or of the batch's, was left to send one more request: nothing was sent."""

    said_as_is = True


class BadReply(SourceAnomaly):
    """Not what the documented API gives: a status (a redirect among them), a type, a size, a shape or a field."""

    said_as_is = True


class IdentityMismatch(SourceAnomaly):
    """A posting's id or links name another posting or another board."""

    said_as_is = True


class TransportHeldBack(HeldBack):
    """The transport would not send the request (TransportRefused): nothing went out, and nothing more is asked of
    the source in this search. Told as held back with the transport's reason, never as an answer (U3-D2-E2)."""

    said_as_is = True


class ListingIncomplete(PartialListing):
    """MAX_PAGES full pages, every record checked: the list may go on, so it was read only in part (U3-D2-1).
    Its listing holds what was read."""


@dataclass(frozen=True)
class Reply:
    status: int
    content_type: str
    body: bytes


class TransportError(Exception):
    """The request failed on its way: nothing came back."""


class TransportTimeout(TransportError):
    """No reply within the time the request was given."""


class TransportRefused(TransportError):
    """The transport would not send the request: nothing went out."""


class TransportTooLarge(TransportError):
    """The reply was larger than allowed: it was not read to its end."""


class Transport(Protocol):
    """One GET of a URL, sent once within timeout: no retry, no redirect followed, no cookie, no credential."""

    def get(self, url: str, timeout: float) -> Reply: ...


class NoNetwork:
    """The transport of a board unless another is passed in: it sends nothing, ever."""

    def get(self, url: str, timeout: float) -> Reply:
        raise NetworkClosed(t("disc.lever.1"))


NO_NETWORK = NoNetwork()


@dataclass(frozen=True)
class LeverBoard:
    """One company's Lever job site, as an approved list names it: its site and the company it is."""

    site: str
    company: str

    def __post_init__(self):
        if not SITE.fullmatch(self.site):
            raise ValueError("a board's site is a plain Lever site name")

        if not self.company.strip():
            raise ValueError("a board names its company")

    @property
    def source_id(self) -> str:
        return f"lever-{self.site}"

    @property
    def name(self) -> str:
        return f"{self.company}（Lever 公开职位接口）"


def entry(
    board: LeverBoard,
    permission: Permission = Permission.UNVERIFIED,
    *,
    basis: str = UNADMITTED,
    checked_on: str = DOCS_CHECKED_ON,
) -> SourceEntry:
    """A board as the register holds it: reached by our own requests, its postings the employer's own; unverified
    unless the code says otherwise."""
    return SourceEntry(board.source_id, board.name, Access.LOCAL_FETCH, permission, EvidenceLevel.OFFICIAL, basis, checked_on)


class HostGuard:
    """Every request to one host, in turn. One is sent only within the budget, once the interval since the last has
    passed, with at least min_request seconds of its call's time and, for a batch, of the batch's network window
    left; and it is given no more than that time. Nothing more is sent once the host refused us (STOP_STATUSES), the
    window has run out, or failures_in_a_row requests in a row failed. A request the transport would not send is not
    counted, but the time it took is: the window is read again after it. log keeps each request handed to the
    transport and what came of it, never a body.

    window and failures_in_a_row are for a one-off validation; left out, neither applies."""

    def __init__(
        self,
        *,
        budget: int = REQUEST_BUDGET,
        interval: float = MIN_INTERVAL,
        window: float | None = None,
        failures_in_a_row: int | None = None,
        request_seconds: float = REQUEST_SECONDS,
        min_request: float = MIN_REQUEST_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.budget = budget
        self.interval = interval
        self.failures_in_a_row = failures_in_a_row
        self.request_seconds = request_seconds
        self.min_request = min_request
        self.monotonic = monotonic
        self._sleep = sleep
        # The batch's network window runs from when the guard is made.
        self.window_ends = None if window is None else monotonic() + window
        self.sent = 0
        self.failures = 0
        self.stopped: str | None = None
        self.log: list[tuple[str, str]] = []
        self._last: float | None = None
        self._lock = threading.Lock()

    def get(self, transport: Transport, url: str, deadline: float) -> Reply:
        with self._lock:
            if self.stopped is not None:
                raise HostStopped(self.stopped)

            if self.sent >= self.budget:
                raise HostStopped(t("disc.lever.2", p0=self.budget))

            now = self.monotonic()
            wait = 0.0 if self._last is None else max(0.0, self._last + self.interval - now)
            self._window_left(now + wait)
            deadline = deadline if self.window_ends is None else min(deadline, self.window_ends)

            if deadline - now - wait < self.min_request:
                raise TimeUsedUp(t("disc.lever.3"))

            if wait:
                self._sleep(wait)
                # The wait may have run long: the time is read again before anything is sent.
                now = self.monotonic()
                self._window_left(now)

                if deadline - now < self.min_request:
                    raise TimeUsedUp(t("disc.lever.4"))

            self.sent += 1

            try:
                reply = transport.get(url, min(self.request_seconds, deadline - now))
            except TransportRefused as refused:
                # Nothing went out: not counted, and no interval starts from it. Its time went all the same (a
                # transport made ready, say): once the window cannot hold another request, the host stops for good.
                self.sent -= 1
                self.log.append((url, "refused"))
                self._window_left(self.monotonic())

                raise TransportHeldBack(t("disc.lever.5", p0=refused)) from None
            except TransportError as error:
                self._last = self.monotonic()
                kind = (
                    "timeout"
                    if isinstance(error, TransportTimeout)
                    else "too_large"
                    if isinstance(error, TransportTooLarge)
                    else "error"
                )
                self.log.append((url, kind))
                self._failed()

                if isinstance(error, TransportTimeout):
                    raise SourceTimeout(t("disc.lever.6")) from None

                if isinstance(error, TransportTooLarge):
                    raise BadReply(t("disc.lever.7")) from None

                raise SourceError(t("disc.lever.8")) from None

            self._last = self.monotonic()
            self.log.append((url, str(reply.status)))

            if reply.status in STOP_STATUSES:
                self.stopped = t("disc.lever.9", p0=reply.status)
                raise HostStopped(self.stopped)

            # A page or a posting, or a plain 404: an answer. Anything else (a redirect, a server error) failed.
            if reply.status in (200, 404):
                self.failures = 0
            else:
                self._failed()

            return reply

    def _window_left(self, at: float) -> None:
        """Stop for good once the batch's window cannot hold another request starting at this time."""
        if self.window_ends is not None and self.window_ends - at < self.min_request:
            self.stopped = t("disc.lever.10")

            raise HostStopped(self.stopped)

    def _failed(self) -> None:
        self.failures += 1

        if self.failures_in_a_row is not None and self.failures >= self.failures_in_a_row:
            self.stopped = t("disc.lever.11", p0=self.failures)


class LeverSource:
    """The adapter of one board (app.discovery.sources.Adapter). Each call is given its time and made once.

    keyword_scope says what its searches match keywords against (KEYWORD_SCOPE): the tools hold searches to it."""

    keyword_scope: KeywordScope

    def __init__(self, board: LeverBoard, *, transport: Transport | None = None, guard: HostGuard | None = None):
        self.board = board
        self.source_id = board.source_id
        self._transport = NO_NETWORK if transport is None else transport
        self.guard = HostGuard() if guard is None else guard

    def search(self, query: Query, timeout: float) -> tuple[Posting, ...]:
        """The board's postings matching the query: its whole list read (listing), then kept here by the query's
        words and the facts it states. Unknown facts never leave a posting out. A list read only in part raises
        ListingIncomplete, its listing holding what was read."""
        return self.listing(timeout).select(query)

    def listing(self, timeout: float) -> "LeverListing":
        """The board's list, page by page (app.discovery.sources.ListingSource): read to its end, or
        ListingIncomplete when its last allowed page came back full with every record checked; nothing else."""
        deadline = self.guard.monotonic() + timeout
        seen: dict[str, str] = {}

        listed: list[_Facts] = []

        for page in range(MAX_PAGES):
            url = f"https://{API_HOST}/v0/postings/{self.board.site}?mode=json&skip={page * PAGE_SIZE}&limit={PAGE_SIZE}"
            items = _json(self._get(url, deadline, t("disc.lever.12")))

            if not isinstance(items, list):
                raise BadReply(t("disc.lever.13"))

            if len(items) > PAGE_SIZE:
                raise BadReply(t("disc.lever.14"))

            for item in items:
                facts = self._facts(item)
                posting_id = facts.posting.ref.posting_id
                content = json.dumps(item, sort_keys=True, ensure_ascii=False)
                known = seen.get(posting_id)

                if known is not None:
                    # Given again as it was: one posting. Given again otherwise: the list changed while it was read.
                    if known != content:
                        raise BadReply(t("disc.lever.15"))

                    continue

                seen[posting_id] = content
                listed.append(facts)

            if len(items) < PAGE_SIZE:
                break
        else:
            # Every record of every page was checked above, or this was never reached.
            raise ListingIncomplete(
                t("disc.lever.16", p0=MAX_PAGES, p1=PAGE_SIZE, p2=len(listed)),
                LeverListing(tuple(listed), complete=False),
            )

        return LeverListing(tuple(listed), complete=True)

    def read(self, ref: PostingRef, timeout: float) -> PostingDetail:
        facts = self._facts(self._one(ref, timeout))

        return PostingDetail(facts.posting, facts.text, facts.url, facts.apply_url)

    def check(self, ref: PostingRef, timeout: float) -> CheckAnswer:
        """OPEN when the source gives this very posting as published now; no answer (SourceError) otherwise."""
        self._facts(self._one(ref, timeout))

        return CheckAnswer(CheckStatus.OPEN)

    def _one(self, ref: PostingRef, timeout: float) -> dict:
        if ref.source_id != self.source_id:
            raise IdentityMismatch(t("disc.lever.17"))

        if not POSTING_ID.fullmatch(ref.posting_id):
            raise BadReply(t("disc.lever.18"))

        deadline = self.guard.monotonic() + timeout
        url = f"https://{API_HOST}/v0/postings/{self.board.site}/{ref.posting_id}"
        item = _json(self._get(url, deadline, t("disc.lever.19")))

        if not isinstance(item, dict):
            raise BadReply(t("disc.lever.20"))

        if item.get("id") != ref.posting_id:
            raise IdentityMismatch(t("disc.lever.21"))

        return item

    def _get(self, url: str, deadline: float, missing: str) -> Reply:
        """A 200 reply to one GET of url, or SourceError; missing says what a 404 there does and does not mean."""
        if isinstance(self._transport, NoNetwork):
            raise NetworkClosed(t("disc.lever.1"))

        reply = self.guard.get(self._transport, url, deadline)

        if reply.status == 404:
            raise NotFound(missing)

        if reply.status != 200:
            raise BadReply(t("disc.lever.22", p0=reply.status))

        return reply

    def _facts(self, item) -> "_Facts":
        """A documented posting as RoleRadar's facts, after its id and links are checked against this board."""
        if not isinstance(item, dict):
            raise BadReply(t("disc.lever.23"))

        posting_id, title = item.get("id"), item.get("text")

        if not isinstance(posting_id, str) or not POSTING_ID.fullmatch(posting_id):
            raise BadReply(t("disc.lever.24"))

        if not isinstance(title, str) or not title.strip():
            raise BadReply(t("disc.lever.25"))

        url = f"{HOSTED}/{self.board.site}/{posting_id}"
        hosted = item.get("hostedUrl")

        if not isinstance(hosted, str) or hosted.rstrip("/") != url:
            raise IdentityMismatch(t("disc.lever.26"))

        categories = _object(item, "categories")
        location, commitment = _text_field(categories, "location"), _text_field(categories, "commitment")
        team, department = _text_field(categories, "team"), _text_field(categories, "department")
        places = categories.get("allLocations")

        if places is not None and not (isinstance(places, list) and all(isinstance(place, str) for place in places)):
            raise BadReply(t("disc.lever.27"))

        workplace, country = _text_field(item, "workplaceType"), _text_field(item, "country")
        apply = item.get("applyUrl")

        if apply is not None and not isinstance(apply, str):
            raise BadReply(t("disc.lever.28"))

        # The primary location is among allLocations too (Lever's docs): the list when given, else the one.
        named = [" ".join(place.split()) for place in (places or ([location] if location else [])) if place.strip()]
        posting = Posting(
            ref=PostingRef(self.source_id, posting_id),
            title=" ".join(title.split()),
            company=self.board.company,
            cities=_cities(named, workplace),
            city_choice=False,
            job_type=_job_type(commitment),
            direction=None,
            posted_on=None,
            deadline=None,
            # As written (U3-D2-2): what was not read as a city or a type stays here, and remote work with it.
            places_stated=tuple(named) + ((workplace,) if workplace is not None and workplace.casefold() == "remote" else ()),
            type_stated=commitment,
        )
        # An application link is given only as the one Lever's hosted form has for this posting.
        apply_url = f"{url}/apply" if apply is not None and apply.rstrip("/") == f"{url}/apply" else None
        text = _posting_text(item, posting, named, commitment, team, department, workplace, country)
        words = " ".join(value for value in (posting.title, team, department, commitment) if value)

        return _Facts(posting, text, url, apply_url, words)


@dataclass(frozen=True)
class _Facts:
    posting: Posting
    text: str
    url: str
    apply_url: str | None
    # What a search's keywords are matched against: the title and the categories the posting states.
    words: str


@dataclass(frozen=True)
class LeverListing:
    """A board's list as read (app.discovery.sources.Listing): each posting's facts, kept in memory by the one search
    that read it, and matched here by each query's words and the facts each posting states."""

    facts: tuple[_Facts, ...]
    complete: bool

    @property
    def read(self) -> int:
        return len(self.facts)

    def select(self, query: Query) -> tuple[Posting, ...]:
        return tuple(facts.posting for facts in self.facts if _matches(facts, query))


def register(boards: Iterable[LeverBoard], *, transport: Transport | None = None, guard: HostGuard | None = None) -> Register:
    """The boards as a search knows them now: every one unverified, so no tool calls any of them. They share one
    guard, as they share one host."""
    boards = tuple(boards)
    guard = HostGuard() if guard is None else guard

    return Register(
        tuple(entry(board) for board in boards),
        {board.source_id: LeverSource(board, transport=transport, guard=guard) for board in boards},
    )


def _json(reply: Reply):
    if len(reply.body) > MAX_BYTES:
        raise BadReply(t("disc.lever.29"))

    if "json" not in reply.content_type.casefold():
        raise BadReply(t("disc.lever.30"))

    try:
        return json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise BadReply(t("disc.lever.31")) from None


def _object(item: dict, name: str) -> dict:
    value = item.get(name)

    if value is None:
        return {}

    if not isinstance(value, dict):
        raise BadReply(t("disc.lever.32", p0=name))

    return value


def _text_field(item: dict, name: str) -> str | None:
    value = item.get(name)

    if value is None:
        return None

    if not isinstance(value, str):
        raise BadReply(t("disc.lever.33", p0=name))

    return " ".join(value.split()) or None


def _cities(places: list[str], workplace: str | None) -> tuple[str, ...]:
    """The cities a posting names, only when every place it names is plainly one, and none for remote work."""
    if workplace is not None and workplace.casefold() == "remote":
        return ()

    cities: list[str] = []

    for place in places:
        city = _city(place)

        # One place not plainly a city leaves them all unknown: what is dropped could be where the hire works.
        if city is None:
            return ()

        if city not in cities:
            cities.append(city)

    return tuple(cities)


def _city(place: str) -> str | None:
    """"San Francisco, CA" or "Toronto, Ontario, Canada" is a city; "Beijing" is 北京; "Remote - US", "SF Bay Area",
    "Ontario, Canada" or "United States" is not plainly one."""
    text = " ".join(place.split())

    if not text or NOT_A_CITY.search(text) or SEVERAL.search(text):
        return None

    parts = [part.strip() for part in re.split(r"[,，]", text)]
    first = parts[0]

    if first.casefold() in CITY_NAMES:
        return CITY_NAMES[first.casefold()]

    if first in CHINESE_CITIES or (NON_ASCII.search(first) and first.endswith("市") and len(first) > 2):
        return first

    if PLACE_NAME.fullmatch(first) and (
        (len(parts) == 2 and REGION_CODE.fullmatch(parts[1])) or (len(parts) == 3 and all(parts[1:]))
    ):
        return first

    return None


def _job_type(commitment: str | None) -> str | None:
    if commitment is None:
        return None

    internship, campus = bool(INTERNSHIP.search(commitment)), bool(CAMPUS.search(commitment))

    if internship and not campus:
        return "实习"

    if campus and not internship:
        return "校招"

    return None


# What a board's searches match keywords against (_matches, _Facts.words): the title, team, department and
# commitment, every keyword a whole word; the place and the type by the filters alone. A word it reads as a type
# (_job_type) or a city it knows by name (CITY_NAMES) names what a filter matches.
KEYWORD_SCOPE = KeywordScope(
    fields="职位名、团队、部门和类型",
    filtered=frozenset({Dimension.CITY, Dimension.JOB_TYPE}),
    places=frozenset(normalized(Dimension.CITY, name) for pair in CITY_NAMES.items() for name in pair),
    type_of=_job_type,
)
LeverSource.keyword_scope = KEYWORD_SCOPE


def _matches(facts: _Facts, query: Query) -> bool:
    """Every keyword a whole word of the title or a category, and the city and type asked for unless unknown."""
    posting = facts.posting

    if query.city is not None and posting.cities:
        if normalized(Dimension.CITY, query.city) not in {normalized(Dimension.CITY, city) for city in posting.cities}:
            return False

    if query.job_type is not None and posting.job_type is not None:
        if normalized(Dimension.JOB_TYPE, query.job_type) != normalized(Dimension.JOB_TYPE, posting.job_type):
            return False

    return all(_has_word(facts.words, term) for term in query.keywords.split())


def _has_word(words: str, term: str) -> bool:
    folded, wanted = words.casefold(), term.casefold()

    # Chinese has no spaces between words: as it is written.
    if NON_ASCII.search(wanted):
        return wanted in folded

    return re.search(rf"(?<![a-z0-9]){re.escape(wanted)}s?(?![a-z0-9])", folded) is not None


def _posting_text(item, posting, places, commitment, team, department, workplace, country) -> str:
    """The posting as it reads: its title and stated facts, then its documented plain words."""
    facts = [posting.company, f"地点：{'；'.join(places) if places else NOT_GIVEN}", f"类型：{commitment or NOT_GIVEN}"]
    lines = [posting.title, " · ".join(facts)]
    unit = [f"{label}：{value}" for label, value in (("团队", team), ("部门", department)) if value]

    if unit:
        lines.append("；".join(unit))

    if workplace and workplace.casefold() != "unspecified":
        lines.append(f"工作方式：{workplace}")

    if country:
        lines.append(f"国家或地区代码：{country}")

    salary = _salary(item)

    if salary:
        lines.append(f"薪资：{salary}")

    body = [_plain_field(item, "descriptionPlain") or _join(_plain_field(item, "openingPlain"), _plain_field(item, "descriptionBodyPlain"))]

    if not body[0]:
        body = [_html_text(_html_field(item, "description"))]

    lists = item.get("lists")

    if lists is not None and not isinstance(lists, list):
        raise BadReply(t("disc.lever.34"))

    for part in lists or ():
        if not isinstance(part, dict):
            raise BadReply(t("disc.lever.35"))

        heading, markup = part.get("text") or "", part.get("content") or ""

        if not isinstance(heading, str) or not isinstance(markup, str):
            raise BadReply(t("disc.lever.35"))

        heading, content = " ".join(heading.split()), _html_text(markup)

        if heading or content:
            body.append("\n".join(piece for piece in (f"{heading}：" if heading else "", content) if piece))

    body.append(_plain_field(item, "additionalPlain") or _html_text(_html_field(item, "additional")))

    return "\n".join(lines) + "\n\n" + "\n\n".join(piece for piece in body if piece)


def _salary(item: dict) -> str | None:
    salary = _object(item, "salaryRange")
    low, high = salary.get("min"), salary.get("max")
    currency, interval = salary.get("currency"), salary.get("interval")
    numbers = [value for value in (low, high) if isinstance(value, (int, float)) and not isinstance(value, bool)]
    stated = "–".join(f"{value:,}" for value in numbers)

    if stated:
        stated += "".join(f" {value}" for value in (currency,) if isinstance(value, str) and value.strip())
        stated += "".join(f"（{value}）" for value in (interval,) if isinstance(value, str) and value.strip())

    words = _plain_field(item, "salaryDescriptionPlain")

    return "；".join(piece for piece in (stated, words) if piece) or None


def _plain_field(item: dict, name: str) -> str:
    value = item.get(name)

    if value is None:
        return ""

    if not isinstance(value, str):
        raise BadReply(t("disc.lever.33", p0=name))

    return _lines(value)


def _html_field(item: dict, name: str) -> str:
    value = item.get(name)

    if value is None:
        return ""

    if not isinstance(value, str):
        raise BadReply(t("disc.lever.33", p0=name))

    return value


def _join(*pieces: str) -> str:
    return "\n".join(piece for piece in pieces if piece)


def _lines(text: str) -> str:
    """Each line with its spaces collapsed, empty lines and bare list marks dropped."""
    lines = (" ".join(line.split()) for line in text.splitlines())

    return "\n".join(line for line in lines if line and line != "-")


class _HtmlText(HTMLParser):
    """The words of styled HTML, a line for each block and "- " for each list item; scripts and styles dropped."""

    BLOCKS = frozenset({"p", "div", "br", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._hidden += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._hidden = max(0, self._hidden - 1)
        elif tag == "li" or tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._hidden:
            self.parts.append(data)


def _html_text(markup: str) -> str:
    if not markup:
        return ""

    parser = _HtmlText()
    parser.feed(markup)
    parser.close()

    return _lines("".join(parser.parts))
