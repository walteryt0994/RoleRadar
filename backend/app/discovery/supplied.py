"""用户补充岗位 (U3-D4d): a posting the user found elsewhere, added by a link to a source the app may read now, or by its
text, pasted.

A link is looked at in three ways, each with its own rules (D4d pre-check, section 3):
- checked_link: whether it may be looked at at all: at most MAX_LINK_CHARS, no blank or control character, a scheme
  and a host;
- match: which registered source and posting id it names, by its address shape alone (synthetic.ADDRESSES, Lever's
  hosted board pages): https only, no user, port, IP, localhost or non-ASCII host, no %-encoding, empty or dot segment.
  Each shape names the parts that name a posting, its path only for now: a parameter or a fragment never counts, and
  is never dropped from what is kept because of it. The address is never opened, resolved or sent anywhere: a source
  it names is read by its own adapter, by source and posting id, and only if the app may read it now (refusal): in the
  set's register, verified and callable, kept by the set (SourceSet.keep), reached as synthetic data
  (updates.TRACKABLE_ACCESS) and an official posting's. Read is one D4a check (updates.check_posting): MAX_CALLS calls
  at most, CALL_SECONDS each, never again; a 404 alone concludes nothing;
- stored_link: what is kept of a link the user gives with a text they paste, once they have seen it: http or https
  only, without what precedes "@", parameters plainly named as credentials, nor a path's session part; parameters that
  may or may not be credentials are marked for the user to decide, the rest kept as typed.

Nothing here writes the database, calls a model or reads an experience or the preferences. What the user reads or
pastes is held in memory (Supplied), for the session that made it, RESULT_TTL_SECONDS at most, looking never extending
it, until the user adds it to 我的岗位 (app.discovery.records.keep_supplied), drops it, logs out or it lapses. Its life
there (D4d pre-check, 17.2): a save claims it first; the record it makes is bound to it before the write commits, so
there is never a record committed that the result does not know of; the claim is released whatever happens, the
result saved if the write committed and as it was if not. Deleting that record, ending the session, dropping it or
its lapsing drops its text, and a result once bound to a record never makes another. Which matches the user saw before
saving is kept here too (17.3).

D4d F1: a save is taken only for a page the server showed. Each time the page before saving is shown, a snapshot is
made here (a random id, and for a pasted posting the matches the page lists); the form names that id, and a save
naming no snapshot, or not the last one, is refused. F2: a result whose save found the posting is the user's already
is bound to that record and ends there, as one that made it does: no record it was bound to, deleted, restored or
deleted for good, ever lets it make another. A save under way may finish past the result's thirty minutes, but from
then on nothing new reads its text: whether a result may be read is its own time alone, apart from a save's claim.
"""
import hashlib
import ipaddress
import re
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import SplitResult, unquote_plus, urlsplit, urlunsplit

from app.discovery import lever, synthetic, updates
from app.discovery.contracts import Access, EvidenceLevel, PostingRef
from app.discovery.sources import Register
from app.library.job_choices import RECHECK_REASONS

MAX_LINK_CHARS = 2048
MAX_NAME_CHARS = 100
# As U2 takes a posting's text (app.job_analysis.scope.MAX_POSTING_CHARS).
MAX_TEXT_CHARS = 8000
RESULT_TTL_SECONDS = 30 * 60
# How many a user may hold at once.
MAX_RESULTS = 5
LINK, PASTED = "link", "pasted"

# Parameters kept out of a link kept, by name, whatever case: named plainly as credentials.
CREDENTIAL_PARTS = ("token", "secret", "password", "passwd", "credential", "signature")
CREDENTIAL_NAMES = frozenset({"pwd", "apikey", "api_key", "api-key", "x-api-key", "sessionid", "session_id", "jsessionid", "phpsessid"})
# Names that may be a credential or what names the posting: kept, and marked for the user to decide.
AMBIGUOUS_NAMES = frozenset({"code", "key", "auth", "ticket", "otp", "sig", "session", "sid"})
# A posting id in a synthetic address, as the synthetic sources name theirs.
SYNTHETIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")

NOT_READABLE = "这个来源现在不能读取：它不在这次运行的来源里，或者不是本机合成数据。没有发出请求；可以在下面粘贴岗位原文。"
LEAD_LINK = "这是待核验线索的地址，不读取；请补充它指向的官方岗位的链接，或者在下面粘贴原文。没有发出请求。"
READ_FAILED = "读取没有成功（{reason}），没有重试。可以在下面粘贴岗位原文。"
NOT_FOUND = (
    "来源说找不到这个编号（404）。单独一次 404 不说明岗位已关闭：编号可能不对，岗位可能已下线，来源也可能暂时异常。"
    "没有重试；可以在下面粘贴岗位原文。"
)
MISMATCH = "来源回答的岗位和链接里的编号对不上，没有采用。可以在下面粘贴岗位原文。"


class LinkProblem(Exception):
    """A link not recognised, read or kept, and why, in the words a page shows: never the link itself."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- a link: may it be looked at, which posting does it name, what is kept of it --------------------------------------


def checked_link(raw: str) -> str:
    """The link as typed, its ends trimmed, if it may be looked at at all; LinkProblem otherwise."""
    link = (raw or "").strip()

    if not link:
        raise LinkProblem("没有填写链接。")

    if len(link) > MAX_LINK_CHARS:
        raise LinkProblem(f"链接超过 {MAX_LINK_CHARS} 个字符，没有识别，也不会保存。")

    if any(character.isspace() or unicodedata.category(character) in ("Cc", "Cf") for character in link):
        raise LinkProblem("链接中间有空白或看不见的控制字符，没有识别，也不会保存。")

    parts = _split(link)

    if not parts.scheme or not parts.netloc:
        raise LinkProblem("这不像一个网址（没有协议或主机），没有识别，也不会保存。")

    return link


def match(raw: str) -> PostingRef:
    """The registered source and posting id the link names, by its address shape alone; LinkProblem if it names none.
    Which of them the app may read now is refusal's to say."""
    parts = _split(checked_link(raw))
    unknown = LinkProblem("认不出这个链接属于哪个已登记的来源。没有打开这个链接，也没有发出请求；可以在下面粘贴岗位原文。")

    if parts.scheme.lower() != "https":
        raise LinkProblem("只认 https 的岗位链接。没有打开这个链接，也没有发出请求；可以在下面粘贴岗位原文。")

    if "@" in parts.netloc or _port(parts) is not None:
        raise unknown

    host = parts.hostname or ""

    if not host.isascii() or host == "localhost" or host.endswith(".localhost") or _is_address(host):
        raise unknown

    segments = parts.path.split("/")[1:]

    if segments and segments[-1] == "":
        # One slash at the end is the same address.
        segments = segments[:-1]

    if "%" in parts.path or not segments or any(segment in ("", ".", "..") for segment in segments):
        raise unknown

    if segments[-1] == "apply":
        segments = segments[:-1]

    for source_id, base in synthetic.ADDRESSES.items():
        shape = urlsplit(base)
        prefix = [segment for segment in shape.path.split("/") if segment]

        if host == shape.hostname and segments[:-1] == prefix and len(segments) == len(prefix) + 1 and SYNTHETIC_ID.fullmatch(segments[-1]):
            return PostingRef(source_id, segments[-1])

    if host == urlsplit(lever.HOSTED).hostname and len(segments) == 2:
        site, posting_id = segments

        if lever.SITE.fullmatch(site) and lever.POSTING_ID.fullmatch(posting_id):
            return PostingRef(f"lever-{site}", posting_id)

    raise unknown


def refusal(ref: PostingRef, keep: frozenset[str], register: Register) -> str | None:
    """Why the app may not read the posting now, or None: the server's, every time."""
    entry = register.entry(ref.source_id)

    if ref.source_id not in keep or entry is None or entry.access not in updates.TRACKABLE_ACCESS or register.refusal(ref.source_id):
        return NOT_READABLE

    if entry.evidence is not EvidenceLevel.OFFICIAL:
        return LEAD_LINK

    return None


@dataclass(frozen=True)
class StoredLink:
    """What is kept of a link the user gave with a text (D4d 3.3): exactly what the page before saving shows; what was
    taken out, by name only; and the parameters kept that may be credentials, for the user to decide."""

    text: str
    removed: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()


def stored_link(raw: str) -> StoredLink:
    """The link as it would be kept; LinkProblem if none of it can be."""
    parts = _split(checked_link(raw))

    if parts.scheme.lower() not in ("http", "https"):
        raise LinkProblem("这不是网页地址（只保存 http 或 https 的链接），不会保存；可以改正，或者留空。")

    removed: list[str] = []
    flagged: list[str] = []
    netloc = parts.netloc

    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]
        removed.append("登录信息（@ 前面的部分）")

    if not netloc or not urlsplit(f"//{netloc}").hostname:
        raise LinkProblem("这个链接没有主机，不会保存；可以改正，或者留空。")

    path = "/".join(_kept_segment(segment, removed) for segment in parts.path.split("/"))
    query = _kept_pairs(parts.query, "参数", removed, flagged)
    fragment = _kept_fragment(parts.fragment, removed, flagged)
    text = urlunsplit((parts.scheme, netloc, path, query, fragment))

    if len(text) > MAX_LINK_CHARS:
        raise LinkProblem(f"链接超过 {MAX_LINK_CHARS} 个字符，不会保存；可以改短，或者留空。")

    return StoredLink(text, tuple(removed), tuple(flagged))


def link_key(text: str | None) -> str:
    """A kept link as compared (D4d 6.3): its scheme and host whatever case, the rest as kept; empty for none."""
    if not text:
        return ""

    parts = urlsplit(text)

    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, parts.fragment))


def _split(link: str) -> SplitResult:
    try:
        return urlsplit(link)
    except ValueError:
        raise LinkProblem("这不像一个网址，没有识别，也不会保存。") from None


def _port(parts: SplitResult):
    try:
        return parts.port
    except ValueError:
        return "bad"


def _is_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False

    return True


def _credential(name: str) -> bool:
    plain = unquote_plus(name).casefold()

    return plain in CREDENTIAL_NAMES or plain.startswith("x-amz-") or any(part in plain for part in CREDENTIAL_PARTS)


def _kept_segment(segment: str, removed: list[str]) -> str:
    """A path segment without a parameter named as a credential (";jsessionid=..." and the like)."""
    head, *params = segment.split(";")
    kept = [head]

    for param in params:
        name = param.split("=", 1)[0]

        if _credential(name):
            removed.append(f"路径里的 {_shown_name(name)}")
        else:
            kept.append(param)

    return ";".join(kept)


def _kept_pairs(value: str, where: str, removed: list[str], flagged: list[str]) -> str:
    kept = []

    for pair in value.split("&") if value else []:
        name = pair.split("=", 1)[0]

        if _credential(name):
            removed.append(f"{where} {_shown_name(name)}")
            continue

        if unquote_plus(name).casefold() in AMBIGUOUS_NAMES:
            flagged.append(_shown_name(name))

        kept.append(pair)

    return "&".join(kept)


def _kept_fragment(fragment: str, removed: list[str], flagged: list[str]) -> str:
    """What follows "#": a page's own route (as "#/jobs/123") is kept; parameters in it are looked at as a query's."""
    if "=" not in fragment:
        return fragment

    route, mark, pairs = fragment.partition("?")

    if mark:
        kept = _kept_pairs(pairs, "# 后的参数", removed, flagged)

        return route + (f"?{kept}" if kept else "")

    return _kept_pairs(fragment, "# 后的参数", removed, flagged)


def _shown_name(name: str) -> str:
    plain = unquote_plus(name)

    return plain if len(plain) <= 40 else plain[:40] + "…"


# --- a posting the user pasted: its identity -------------------------------------------------------------------------


def plain(value: str | None) -> str:
    """A company or a title as compared (D4d 6.3): the same writing whatever its width, case or spaces; nothing more."""
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def text_digest(text: str) -> str:
    """The text as compared and kept with its record: its blanks made one and its ends trimmed."""
    return hashlib.sha256(" ".join(unicodedata.normalize("NFC", text).split()).encode("utf-8")).hexdigest()


def kept_text(text: str) -> str:
    """The text as it is shown before saving and kept: its line ends made "\\n", its ends trimmed."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


# --- what a supply holds -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkContent:
    """A posting read from a link the user gave: the source and id it named, and one D4a check's reading of it."""

    ref: PostingRef
    source_name: str
    access: str
    reading: updates.Reading
    started_at: datetime
    finished_at: datetime
    kind: str = LINK

    @property
    def text(self) -> str | None:
        return self.reading.text


@dataclass(frozen=True)
class PastedContent:
    """A posting the user pasted: what they typed of it, its text as shown before saving, and its link as kept."""

    title: str
    company: str
    text: str
    digest: str
    link: StoredLink | None
    provided_at: datetime
    kind: str = PASTED


def read(ref: PostingRef, register: Register, *, delisting: frozenset[str], now: Callable[[], datetime]) -> LinkContent:
    """One D4a check of the posting the link names, through its source's own adapter: only after refusal said None."""
    entry = register.entry(ref.source_id)
    started = now()
    reading = updates.check_posting(
        register.adapter(ref.source_id),
        ref,
        synthetic=entry.access == Access.SYNTHETIC,
        delisting=ref.source_id in delisting,
        today=started.astimezone().date(),
        now=now,
    )

    return LinkContent(ref, entry.name, entry.access.value, reading, started, now())


def failure_text(reading: updates.Reading) -> str:
    """Why a read gave nothing to keep, in the words the supply page shows."""
    if reading.reason == "not_found":
        return NOT_FOUND

    if reading.reason == "identity_mismatch":
        return MISMATCH

    return READ_FAILED.format(reason=RECHECK_REASONS.get(reading.reason or "", reading.reason or "原因不明"))


# --- the store -----------------------------------------------------------------------------------------------------------


class Gone(Exception):
    """No such result for this session now: lapsed, dropped, another's, or the session ended. Nothing is written."""


class Busy(Exception):
    """The result is being saved now."""


class Stale(Exception):
    """The page a save came from is not the last one shown of the result: the matches it showed may be old."""


class Unreviewed(Exception):
    """No page before saving was ever shown of the result (D4d F1): the user saw neither what would be kept nor its
    matches, so nothing is saved."""


class Full(Exception):
    """The user holds MAX_RESULTS already."""


class Saved(Exception):
    """The result made a record already: it never makes another (record_id, or None if not known now)."""

    def __init__(self, record_id: str | None):
        super().__init__(record_id)
        self.record_id = record_id


@dataclass(frozen=True)
class Held:
    """A result as a page may show it, at the moment it was looked at."""

    token: str
    content: LinkContent | PastedContent
    # The last snapshot made of it, as the page shows it: what a save must name (None until a page is shown, F1).
    review: str | None
    shown: frozenset[str]
    saved: str | None
    minutes_left: int
    # Being saved now: a page shows no button to save it meanwhile.
    saving: bool = False


@dataclass(frozen=True)
class Claim:
    """A save under way: its ticket, the result, and the matches the page it came from showed (17.3)."""

    ticket: str
    content: LinkContent | PastedContent
    shown: frozenset[str]


@dataclass
class _Entry:
    user_id: str
    session: str
    content: LinkContent | PastedContent
    made: float
    # The id of the last snapshot shown (F1), and what that page listed.
    review: str | None = None
    shown: frozenset[str] = frozenset()
    saving: str | None = None
    bound: str | None = None
    saved: str | None = None


@dataclass
class _Used:
    """A result dropped after it made a record, remembered until it would have lapsed: a save of it says so."""

    user_id: str
    session: str
    record_id: str
    made: float


@dataclass
class _Reading:
    ticket: str
    session: str
    made: float
    valid: bool = True


class Supplied:
    """What users read from links and pasted, in memory only, each for the session that made it (module docstring)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, ttl: float = RESULT_TTL_SECONDS, limit: int = MAX_RESULTS):
        self.clock = clock
        self.ttl = ttl
        self.limit = limit
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}
        self._used: dict[str, _Used] = {}
        # One read of a user's at a time, from before its first call until it is published or given back.
        self._reading: dict[str, _Reading] = {}
        self._ended: dict[str, float] = {}

    # --- reading a link, and pasting -------------------------------------------------------------------------------

    def reserve(self, user_id: str, session: str) -> str | None:
        """A ticket for one read about to begin, or None: the session ended, or a read of the user's runs."""
        with self._lock:
            self._sweep()

            if session in self._ended or user_id in self._reading:
                return None

            ticket = secrets.token_urlsafe(12)
            self._reading[user_id] = _Reading(ticket, session, self.clock())

            return ticket

    def reading(self, user_id: str) -> bool:
        with self._lock:
            self._sweep()

            return user_id in self._reading

    def publish(self, ticket: str, user_id: str, session: str, content: LinkContent) -> str | None:
        """Hold a read, once done; None if its reservation is gone or spoilt since (the session ended): never shown."""
        with self._lock:
            self._sweep()
            reserved = self._reading.get(user_id)

            if reserved is None or reserved.ticket != ticket or not reserved.valid or reserved.session != session or session in self._ended:
                return None

            del self._reading[user_id]

            return self._hold(user_id, session, content)

    def cancel(self, ticket: str) -> None:
        """Give back a reservation not published; a no-op once published."""
        with self._lock:
            for user_id in [user_id for user_id, reserved in self._reading.items() if reserved.ticket == ticket]:
                del self._reading[user_id]

    def put(self, user_id: str, session: str, content: PastedContent) -> str | None:
        """Hold a pasted posting; None if the session ended."""
        with self._lock:
            self._sweep()

            if session in self._ended:
                return None

            return self._hold(user_id, session, content)

    def _hold(self, user_id: str, session: str, content) -> str:
        if sum(entry.user_id == user_id for entry in self._entries.values()) >= self.limit:
            raise Full()

        token = secrets.token_urlsafe(18)
        self._entries[token] = _Entry(user_id, session, content, self.clock())

        return token

    # --- looking ---------------------------------------------------------------------------------------------------

    def get(self, token: str, user_id: str, session: str) -> Held | None:
        """The result, for the session that made it, while it lasts; None if not."""
        with self._lock:
            self._sweep()
            entry = self._mine(token, user_id, session)

            return None if entry is None else self._held(token, entry)

    def show(self, token: str, user_id: str, session: str, matches: frozenset[str]) -> Held | None:
        """The result as the page before saving shows it now, with the matches it lists: a snapshot made here, under a
        random id of its own that the page's form names (17.3, F1). Only the last one shown may be saved from."""
        with self._lock:
            self._sweep()
            entry = self._mine(token, user_id, session)

            if entry is None:
                return None

            if entry.saving is None and entry.saved is None:
                entry.review = secrets.token_urlsafe(12)
                entry.shown = frozenset(matches)

            return self._held(token, entry)

    def prefill(self, token: str, user_id: str, session: str) -> str | None:
        """Its text, for the 岗位分析 page to fill in (nothing more is ever taken from here): None if it is gone."""
        held = self.get(token, user_id, session)

        return None if held is None else held.content.text

    def minutes_left(self, held: Held) -> int:
        return held.minutes_left

    # --- saving (17.2) ---------------------------------------------------------------------------------------------

    def claim(self, token: str, user_id: str, session: str, review: str) -> Claim:
        """The result to save now, as the page it came from showed it: that page's snapshot must be the last one made
        (F1). Gone, Saved, Busy, Unreviewed or Stale otherwise."""
        with self._lock:
            self._sweep()

            if session in self._ended:
                raise Gone()

            entry = self._mine(token, user_id, session)

            if entry is None:
                used = self._used.get(token)

                if used is not None and used.user_id == user_id and used.session == session:
                    raise Saved(used.record_id)

                raise Gone()

            if entry.saved is not None:
                raise Saved(entry.saved)

            if entry.saving is not None:
                raise Busy()

            if entry.review is None:
                raise Unreviewed()

            if review != entry.review:
                raise Stale()

            entry.saving = secrets.token_urlsafe(12)

            return Claim(entry.saving, entry.content, entry.shown)

    def bind(self, token: str, ticket: str, record_id: str) -> None:
        """Bind the record the save made to the result, before the write commits: Gone if the result went, the claim
        is not this one, or the session ended since (the write then rolls back, writing nothing)."""
        with self._lock:
            entry = self._entries.get(token)

            if entry is None or entry.saving != ticket or entry.session in self._ended:
                raise Gone()

            entry.bound = record_id

    def release(self, token: str, ticket: str, done: bool) -> None:
        """End the claim, whatever happened. done: the save ended with the record bound, the one it made and committed
        or the user's own it found (F2): the result is that record's for good. Otherwise as it was, to be saved again."""
        with self._lock:
            entry = self._entries.get(token)

            if entry is None or entry.saving != ticket:
                return

            entry.saving = None

            if done and entry.bound is not None:
                entry.saved = entry.bound
            else:
                entry.bound = None

    def forget_record(self, record_id: str) -> None:
        """The record is being deleted: drop the result that made it, remembering only that it did (called before the
        deletion commits, as ApplyChecks.forget_record is)."""
        with self._lock:
            for token, entry in list(self._entries.items()):
                if record_id in (entry.bound, entry.saved):
                    self._drop(token, entry)

    def discard(self, token: str, user_id: str, session: str) -> bool:
        """放弃: drop it now; False if there was none to drop. Busy while it is being saved."""
        with self._lock:
            self._sweep()
            entry = self._mine(token, user_id, session)

            if entry is None:
                return False

            if entry.saving is not None:
                raise Busy()

            self._drop(token, entry)

            return True

    def end_session(self, session: str) -> None:
        """Drop what the session holds and any read of it; from now on nothing is held, shown or saved for it."""
        with self._lock:
            self._ended[session] = self.clock()

            for token, entry in list(self._entries.items()):
                if entry.session == session:
                    self._drop(token, entry)

            for user_id, reserved in list(self._reading.items()):
                if reserved.session == session:
                    reserved.valid = False

    def ended(self, session: str) -> bool:
        with self._lock:
            self._sweep()

            return session in self._ended

    def sweep(self) -> None:
        with self._lock:
            self._sweep()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # --- inside the lock -------------------------------------------------------------------------------------------

    def _mine(self, token: str, user_id: str, session: str) -> _Entry | None:
        """The result, to be read, shown, saved or dropped by its session, while its own time lasts (F2): one kept for
        a save under way past its time is that save's alone."""
        entry = self._entries.get(token)

        if entry is None or entry.user_id != user_id or entry.session != session or session in self._ended:
            return None

        if self.clock() - entry.made >= self.ttl:
            return None

        return entry

    def _held(self, token: str, entry: _Entry) -> Held:
        seconds = self.ttl - (self.clock() - entry.made)

        return Held(token, entry.content, entry.review, entry.shown, entry.saved, max(1, int(seconds // 60)), entry.saving is not None)

    def _drop(self, token: str, entry: _Entry) -> None:
        del self._entries[token]
        record_id = entry.saved or entry.bound

        if record_id is not None:
            self._used[token] = _Used(entry.user_id, entry.session, record_id, entry.made)

    def _sweep(self) -> None:
        cutoff = self.clock() - self.ttl

        # One being saved stays until its save ends: what the save claimed while it lasted is what it saves.
        for token, entry in list(self._entries.items()):
            if entry.made <= cutoff and entry.saving is None:
                self._drop(token, entry)

        for token in [token for token, used in self._used.items() if used.made <= cutoff]:
            del self._used[token]

        for user_id in [user_id for user_id, reserved in self._reading.items() if reserved.made <= cutoff]:
            del self._reading[user_id]

        for session in [session for session, ended in self._ended.items() if ended <= cutoff]:
            del self._ended[session]


def readable_sources(keep: Iterable[str], register: Register) -> list[str]:
    """The names of the sources whose links may be read now, for the supply page."""
    names = []

    for source_id in sorted(keep):
        entry = register.entry(source_id)

        if (
            entry is not None
            and entry.access in updates.TRACKABLE_ACCESS
            and entry.evidence is EvidenceLevel.OFFICIAL
            and register.refusal(source_id) is None
        ):
            names.append(entry.name)

    return names
