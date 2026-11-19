"""The interface language layer shared by every RoleRadar page (UI bilingual batch).

Two interface languages: 简体中文 (``zh-CN``, the default) and English (``en``). Both use the same routes,
templates, data and permissions; only the system's own words change. Three layers are kept apart:

- interface text (labels, buttons, notices, errors, the words for stable codes) is a message key looked up here, in
  the language of the request, and rendered when the page is rendered;
- the explanatory text a task produced and saved keeps the result language that was chosen and confirmed for that
  task (see RESULT_LANGUAGES); switching the interface never rewrites it, sends it anywhere or asks for it again;
- the user's own words and the sources' (experiences, names, posting text, links, notes, quotes) are never passed
  through this layer, never translated and never saved twice.

The language of a request is the browser's explicit choice (cookie ``rr_lang``), only ever one of LANGUAGES; anything
else means the default. LanguageMiddleware sets it for the request, and t() reads it. Work outside a request (task
runners, sweeps, the automatic check) never renders interface text: it keeps stable codes the pages turn into words.

The messages live in app.i18n.catalog, one key per message with both languages side by side; a test checks every key
the code and templates use exists, that no translation is empty and that both languages take the same parameters.
A key that is missing at run time renders as the key itself, so it is seen and never silently shown in Chinese on an
English page.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from collections.abc import Mapping
from datetime import datetime, timedelta

from app.i18n.catalog import MESSAGES

LANGUAGES = ("zh-CN", "en")
DEFAULT_LANGUAGE = "zh-CN"
LANGUAGE_COOKIE = "rr_lang"
# Each language named in its own words: never a flag, which names a country, not a language.
LANGUAGE_NAMES = {"zh-CN": "简体中文", "en": "English"}
# The result languages a new exploration, analysis or personalised search can be asked for (plan 6.3): the same two.
RESULT_LANGUAGES = LANGUAGES
# How long the browser keeps the choice: a year, on this browser only (no account setting in this batch).
LANGUAGE_COOKIE_SECONDS = 365 * 24 * 3600

logger = logging.getLogger("roleradar.i18n")

_current: contextvars.ContextVar[str] = contextvars.ContextVar("roleradar_ui_language", default=DEFAULT_LANGUAGE)
# The words that follow a count in an English message: {name|one|other} reads "one" when the parameter name is 1,
# else "other" ("{p0} more {p0|experience is|experiences are} not confirmed yet"). Chinese texts never need it; a
# parameter that is not a whole number takes the other form. It is filled before the parameters are.
PLURAL = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\|([^{}|]*)\|([^{}|]*)\}")


def chosen(raw: object) -> str | None:
    """The language a raw value names, only from the whitelist; None for anything else (no path, no guess)."""
    if isinstance(raw, str) and raw in LANGUAGES:
        return raw

    return None


def language() -> str:
    """The interface language of the request being handled (the default outside one)."""
    return _current.get()


def is_english() -> bool:
    return _current.get() == "en"


@contextlib.contextmanager
def using(lang: str):
    """Render in lang for the duration (tests, exports built for the chosen language)."""
    token = _current.set(chosen(lang) or DEFAULT_LANGUAGE)

    try:
        yield
    finally:
        _current.reset(token)


def _entry(key: str, lang: str):
    entry = MESSAGES.get(key)

    if entry is None:
        logger.warning("missing interface message %s", key)

        return None

    return entry[0] if lang == "zh-CN" else entry[1]


def t(key: str, /, **params) -> str:
    """The interface message for key in the request's language, with its parameters filled in.

    Parameters are filled with str.format; the result is plain text (templates escape it, so a parameter that holds
    the user's words is escaped too). A missing key renders as the key itself.
    """
    lang = _current.get()
    text = _entry(key, lang)

    if text is None:
        return key

    if isinstance(text, tuple):
        # A plural message asked for without its count: the plural form.
        text = text[-1]

    return _filled(text, params)


def _filled(text: str, params: Mapping) -> str:
    if params and "|" in text:
        text = PLURAL.sub(lambda match: match.group(2) if _is_one(params.get(match.group(1))) else match.group(3), text)

    return text.format(**params) if params else text


def _is_one(value: object) -> bool:
    try:
        return int(str(value)) == 1
    except ValueError:
        return False


def t_in(lang: str, key: str, /, **params) -> str:
    """A message in a given language rather than the request's: text a task writes into its result, in the result
    language confirmed for that task (plan 6.3), whatever the interface language is when the task runs."""
    with using(lang):
        return t(key, **params)


def result_language(raw: object, default: str | None = None) -> str:
    """A result language from a form or a stored record: one of RESULT_LANGUAGES, else the default given (the
    interface language of the request when None)."""
    return chosen(raw) or default or language()


def tn(key: str, count: int, /, **params) -> str:
    """A message that changes with a count: English takes (one, other); Chinese has one form. {n} is the count."""
    lang = _current.get()
    text = _entry(key, lang)

    if text is None:
        return key

    if isinstance(text, tuple):
        text = text[0] if count == 1 else text[1]

    return _filled(text, {"n": count, **params})


def has(key: str) -> bool:
    return key in MESSAGES


def word(family: str, code: object, *, default: str | None = None) -> str:
    """The words for a stable code of a family (``family.code``): codes stay codes in data, URLs and requests."""
    key = f"{family}.{code}"

    if key in MESSAGES:
        return t(key)

    if default is not None:
        return default

    logger.warning("missing interface message %s", key)

    return str(code)


def html_lang() -> str:
    """The value of <html lang>: the interface language of the page."""
    return _current.get()


class LazyText:
    """An interface message for templates, rendered in the language of the page that shows it (a template global
    such as NOT_FILLED). Never stored and never sent anywhere; Python code calls t() instead."""

    __slots__ = ("key", "params")

    def __init__(self, key: str, **params):
        self.key = key
        self.params = params

    def __str__(self) -> str:
        return t(self.key, **self.params)

    def __html__(self) -> str:
        from markupsafe import escape

        return str(escape(str(self)))

    def __eq__(self, other) -> bool:
        return str(self) == str(other)

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        return f"LazyText({self.key!r})"

    # Used like the words it stands for, read at the moment of use (in the language of the request then). Joined
    # with more words it stays lazy, so a module-level message made of two stays in the language of each page.
    def __add__(self, other) -> "LazyJoin":
        return LazyJoin((self, other))

    def __radd__(self, other) -> "LazyJoin":
        return LazyJoin((other, self))

    def __len__(self) -> int:
        return len(str(self))

    def __contains__(self, item) -> bool:
        return item in str(self)

    def __format__(self, spec: str) -> str:
        return format(str(self), spec)

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        return getattr(str(self), name)


class LazyJoin(LazyText):
    """Interface messages (and plain words) joined, read only when used."""

    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = tuple(parts)
        self.key = "+".join(getattr(part, "key", "") for part in self.parts)
        self.params = {}

    def __str__(self) -> str:
        return "".join(str(part) for part in self.parts)

    def __add__(self, other) -> "LazyJoin":
        return LazyJoin((*self.parts, other))

    def __radd__(self, other) -> "LazyJoin":
        return LazyJoin((other, *self.parts))

    def __repr__(self) -> str:
        return f"LazyJoin({self.parts!r})"


def lazy(key: str, **params) -> LazyText:
    return LazyText(key, **params)


class MessageMap(Mapping):
    """Stable codes to interface messages, looked up when read: ``NOTICES.get(code)`` is the words in the language of
    the request being handled. The codes never change with the language; merging two maps keeps both lazy."""

    def __init__(self, keys: dict[str, "str | tuple[str, dict]"]):
        self._keys = dict(keys)

    def __getitem__(self, code: str) -> str:
        # A message with fixed parameters is (key, {name: value}); the parameters never come from a request.
        entry = self._keys[code]

        if isinstance(entry, tuple):
            key, params = entry

            return t(key, **params)

        return t(entry)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def key_of(self, code: str) -> str:
        entry = self._keys[code]

        return entry[0] if isinstance(entry, tuple) else entry

    def merged(self, more: "dict[str, str] | MessageMap") -> "MessageMap":
        extra = more._keys if isinstance(more, MessageMap) else dict(more)

        return MessageMap({**self._keys, **extra})

    def keys_map(self) -> dict[str, str]:
        return dict(self._keys)


# --- dates and times -----------------------------------------------------------------------------------------------
# The instant never changes with the language, nor the time zone (this computer's): only how it is written.


def format_local(value: datetime | None) -> str:
    """An exact instant in this computer's local time, with its UTC offset."""
    if value is None:
        return "—"

    local = value.astimezone()
    offset = local.strftime("%z")
    zone = f"UTC{offset[:3]}:{offset[3:]}"

    if is_english():
        return f"{local:%Y-%m-%d %H:%M:%S} ({zone})"

    return f"{local:%Y-%m-%d %H:%M:%S}（{zone}）"


def format_short(value: datetime) -> str:
    """Local date and minute, short enough for a list."""
    return f"{value.astimezone():%Y-%m-%d %H:%M}"


def _clock(local: datetime) -> str:
    if is_english():
        hour = local.hour % 12 or 12
        half = "AM" if local.hour < 12 else "PM"

        return f"{hour}:{local:%M} {half}"

    return f"{local:%H:%M}"


def format_when(value: datetime | None, now: datetime) -> str:
    """A saved time as people say it: today and yesterday by name, then the date (the same instant either way)."""
    if value is None:
        return "—"

    local = value.astimezone()
    today = now.astimezone().date()

    if local.date() == today:
        return t("time.today_at", time=_clock(local))

    if local.date() == today - timedelta(days=1):
        return t("time.yesterday_at", time=_clock(local))

    if local.year == today.year:
        return t("time.date_at", month=local.month, day=local.day, time=_clock(local), month_name=_month(local.month))

    return t("time.full_date_at", year=local.year, month=local.month, day=local.day, time=_clock(local), month_name=_month(local.month))


def format_until(value: datetime | None, now: datetime) -> str:
    """When something kept for a while goes: a clear time, never a count of minutes left."""
    return format_when(value, now)


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month(number: int) -> str:
    return _MONTHS[number - 1]


# How the interface joins a few items: words (、 / commas), clauses (；/ semicolons), alternatives (／ / slashes).
SEPARATORS = {
    "list": ("、", ", "),
    "clauses": ("；", "; "),
    "slash": ("／", " / "),
    "clauses_also": ("；还", "; also "),
    # Counts of one step ("完成 3 个，没有完成 1 个") and two names joined ("城市和求职类型"): Codex review F3.
    "comma": ("，", ", "),
    "and": ("和", " and "),
}


def list_join(parts, kind: str = "list") -> str:
    """Join a few words the way the language lists them (、 in Chinese, commas in English); kind "clauses" joins
    whole statements, "slash" alternatives. The parts are not translated: they may be the user's or a source's."""
    parts = [str(part) for part in parts]
    chinese, english = SEPARATORS[kind]

    return (english if is_english() else chinese).join(parts)


class LanguageMiddleware:
    """Set the interface language of each request from the browser's explicit choice (a whitelisted cookie)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)

            return

        token = _current.set(_from_headers(scope.get("headers") or ()) or DEFAULT_LANGUAGE)

        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)


def _from_headers(headers) -> str | None:
    for name, value in headers:
        if name == b"cookie":
            for part in value.decode("latin-1").split(";"):
                key, _, raw = part.strip().partition("=")

                if key == LANGUAGE_COOKIE:
                    return chosen(raw.strip())

    return None
