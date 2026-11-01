"""Reading the text of a resume file for an import (U2-B1).

A PDF is read page by page and a DOCX by the paragraphs and tables of its
body, only for their text. Nothing a file links to, embeds or could run is
opened: no images, no relationships, no macros, no fields' instructions.
Complex layouts are not rebuilt; what is read is kept as it was found, with
where it was found (a page, a paragraph, a table), so that later steps can
point back to it.

The reading runs in a child process of its own (read_file). The app waits
at most READ_SECONDS and then ends the process, so a file that takes too
long stops being read, not only being waited for. The child gets the
file's bytes on its standard input and answers with JSON on its standard
output. It starts without environment variables or the user's site
packages, and before it reads anything it limits its own CPU time, may not
write files, open network connections or start programs (an audit hook
refuses them), and on systems that allow it, limits its memory. On macOS
the memory limit is not available; the size, page and inflation limits
below bound the work instead.

Outcomes are short codes (see READ, PARTIAL, FAILED and the FAILURE codes);
the web layer turns them into Chinese. Nothing here writes to the library.
"""
import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

BACKEND_DIR = Path(__file__).resolve().parents[2]

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PDF_PAGES = 50
# A PDF stream may not inflate to more than this (pypdf's own default is 75 MB).
MAX_PDF_STREAM_BYTES = 20 * 1024 * 1024
MAX_DOCX_MEMBERS = 500
# The two parts of a DOCX that are inflated, at most this much each.
MAX_DOCX_PART_BYTES = 10 * 1024 * 1024
MAX_CONTENT_TYPES_BYTES = 1024 * 1024
# How deep the body of a DOCX may nest before it counts as damaged.
MAX_DOCX_DEPTH = 100
READ_SECONDS = 20.0
CPU_SECONDS = 15
# Where the child may grow to, on systems that enforce it.
CHILD_MEMORY_BYTES = 1024 * 1024 * 1024
# The answer of the child: the text (at most MAX_TEXT_CHARS) as JSON, with room to spare.
MAX_ANSWER_BYTES = 8 * 1024 * 1024
# Readings that may run at the same time in this app, and how long a reading
# waits for its turn before it gives up.
PARALLEL_READS = 2
QUEUE_SECONDS = 10.0

PDF, DOCX = "pdf", "docx"
READ, PARTIAL, FAILED = "read", "partial", "failed"
# Why a file could not be read.
ENCRYPTED = "encrypted"
NO_TEXT = "no_text"
DAMAGED = "damaged"
TOO_MANY_PAGES = "too_many_pages"
NOT_WORD = "not_word"
MACROS = "macros"
TOO_BIG_INSIDE = "too_big_inside"
TIMEOUT = "timeout"
CRASHED = "crashed"
BUSY = "busy"
CANCELLED = "cancelled"
# Worth another try: the file was not at fault.
TRANSIENT = frozenset({TIMEOUT, CRASHED, BUSY})
# What made a reading partial.
EMPTY_PAGES = "empty_pages"
UNREADABLE_PAGES = "unreadable_pages"
TRUNCATED = "truncated"

WORD_NAMESPACES = (
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    # Strict Office Open XML names the same elements here.
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
)
MARKUP_COMPATIBILITY = "http://schemas.openxmlformats.org/markup-compatibility/2006"
CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
}
MACRO_MAIN_TYPES = {
    "application/vnd.ms-word.document.macroEnabled.main+xml",
    "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
}
# An old .doc, or a Word file with a password: both are OLE compound files.
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# Control characters other than tab and line feed are dropped from what is read.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class Reading:
    """What reading a file found.

    segments are where the text was found, in order: {"kind": "page",
    "number", "text"} for a PDF, with "unreadable": True on a page whose
    content could not be read; {"kind": "paragraph", "number", "text"} and
    {"kind": "table", "number", "rows": [[cell, ...], ...]} for a DOCX.
    notes say what made a partial reading partial; failure why nothing was read.
    """

    status: str
    segments: list[dict] = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    failure: str | None = None
    pages: int | None = None

    @property
    def chars(self) -> int:
        return sum(segment_chars(segment) for segment in self.segments)

    def as_json(self) -> dict:
        return {
            "status": self.status,
            "segments": self.segments,
            "notes": self.notes,
            "failure": self.failure,
            "pages": self.pages,
        }


def failed(reason: str) -> Reading:
    return Reading(status=FAILED, failure=reason)


def segment_chars(segment: dict) -> int:
    if segment["kind"] == "table":
        return sum(len(cell) for row in segment["rows"] for cell in row)

    return len(segment["text"])


def clean_text(text: str) -> str:
    """The text with line breaks made plain and control characters dropped; nothing else changes."""
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


class _Budget:
    """Counts the characters read; the reading is cut where MAX_TEXT_CHARS is reached."""

    def __init__(self, limit: int = MAX_TEXT_CHARS):
        self.left = limit
        self.cut = False

    def take(self, text: str) -> str:
        if len(text) > self.left:
            text = text[: self.left]
            self.cut = True

        self.left -= len(text)

        return text

    @property
    def spent(self) -> bool:
        return self.left <= 0


# --- PDF ------------------------------------------------------------------------


def read_pdf(data: bytes) -> Reading:
    """Read a PDF's text page by page.

    A page without text is kept, empty, with its number. A page whose own
    content cannot be read (an unknown filter, a broken stream) is kept the
    same way, marked unreadable, and the pages after it are still read: what
    was read before is never lost to a later page. What concerns the whole
    file is refused whole: a file that cannot be opened, is encrypted or has
    too many pages, and any page that runs into a limit (a stream inflating
    past MAX_PDF_STREAM_BYTES, memory, nesting), so no limit is given up for
    a partial result.
    """
    from pypdf import PdfReader
    from pypdf.errors import LimitReachedError

    try:
        reader = PdfReader(io.BytesIO(data), strict=False)

        if reader.is_encrypted:
            return failed(ENCRYPTED)

        count = len(reader.pages)

        if count > MAX_PDF_PAGES:
            return Reading(status=FAILED, failure=TOO_MANY_PAGES, pages=count)

        pages = list(reader.pages)
    except RecursionError:
        return failed(DAMAGED)
    except (LimitReachedError, MemoryError):
        return failed(TOO_BIG_INSIDE)
    except Exception:  # pypdf raises many kinds of errors for a file it cannot open
        return failed(DAMAGED)

    budget = _Budget()
    segments = []

    for number, page in enumerate(pages, start=1):
        if budget.spent:
            budget.cut = True
            break

        try:
            raw = page.extract_text() or ""
        except (LimitReachedError, MemoryError):
            return failed(TOO_BIG_INSIDE)
        except RecursionError:
            return failed(DAMAGED)
        except Exception:  # this page's content cannot be read; the others still can
            segments.append({"kind": "page", "number": number, "text": "", "unreadable": True})
            continue

        segments.append({"kind": "page", "number": number, "text": budget.take(clean_text(raw).strip("\n"))})

    unreadable = [segment["number"] for segment in segments if segment.get("unreadable")]
    empty = [segment["number"] for segment in segments if not segment["text"].strip() and not segment.get("unreadable")]

    if len(unreadable) + len(empty) == len(segments):
        return Reading(status=FAILED, failure=DAMAGED if unreadable else NO_TEXT, pages=count)

    notes = {}

    if unreadable:
        notes[UNREADABLE_PAGES] = unreadable

    if empty:
        notes[EMPTY_PAGES] = empty

    if budget.cut:
        notes[TRUNCATED] = True

    return Reading(status=PARTIAL if notes else READ, segments=segments, notes=notes, pages=count)


# --- DOCX -----------------------------------------------------------------------


class _NotWord(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def read_docx(data: bytes) -> Reading:
    """Read the paragraphs and tables of a DOCX body, in their order."""
    if data.startswith(OLE_SIGNATURE):
        # A Word file with a password, or an old .doc renamed.
        return failed(ENCRYPTED)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            members = package.infolist()

            if len(members) > MAX_DOCX_MEMBERS:
                return failed(TOO_BIG_INSIDE)

            names = {member.filename for member in members}

            if any(name.lower().endswith("vbaproject.bin") for name in names):
                return failed(MACROS)

            main = _main_part(package, names)
            root = _parse_part(package, main, MAX_DOCX_PART_BYTES)
    except _NotWord as error:
        return failed(error.reason)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError, ValueError):
        return failed(NOT_WORD)
    except RecursionError:
        return failed(DAMAGED)
    except MemoryError:
        return failed(TOO_BIG_INSIDE)
    except RuntimeError:
        # zipfile: a member is encrypted.
        return failed(ENCRYPTED)
    except OSError:
        return failed(DAMAGED)

    body = _child(root, "body")

    if body is None:
        return failed(NOT_WORD)

    collector = _Collector()

    try:
        collector.blocks(body, 0)
    except _TooDeep:
        return failed(DAMAGED)

    segments = collector.segments

    if not segments:
        return failed(NO_TEXT)

    notes = {TRUNCATED: True} if collector.budget.cut else {}

    return Reading(status=PARTIAL if notes else READ, segments=segments, notes=notes)


def _main_part(package: zipfile.ZipFile, names: set[str]) -> str:
    """The name of the document's main part, from the package's content types."""
    if "[Content_Types].xml" not in names:
        raise _NotWord(NOT_WORD)

    types = _parse_part(package, "[Content_Types].xml", MAX_CONTENT_TYPES_BYTES)

    for override in types.iter(f"{{{CONTENT_TYPES}}}Override"):
        content_type = override.get("ContentType", "")
        part = override.get("PartName", "").lstrip("/")

        if content_type in MACRO_MAIN_TYPES:
            raise _NotWord(MACROS)

        if content_type in WORD_MAIN_TYPES and part in names:
            return part

    raise _NotWord(NOT_WORD)


def _parse_part(package: zipfile.ZipFile, name: str, limit: int) -> ElementTree.Element:
    """Inflate one part, at most limit bytes (counted while inflating), and parse it.

    A document type declaration is refused: Office files never have one, and
    it is how entity tricks would begin.
    """
    with package.open(name) as handle:
        data = handle.read(limit + 1)

    if len(data) > limit:
        raise _NotWord(TOO_BIG_INSIDE)

    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise _NotWord(DAMAGED)

    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as error:
        raise _NotWord(DAMAGED) from error


def _local(element: ElementTree.Element) -> tuple[str, str]:
    """(namespace, local name) of an element."""
    tag = element.tag

    if not isinstance(tag, str) or not tag.startswith("{"):
        return "", tag if isinstance(tag, str) else ""

    namespace, _, name = tag[1:].partition("}")

    return namespace, name


def _is(element: ElementTree.Element, name: str) -> bool:
    namespace, local = _local(element)

    return local == name and namespace in WORD_NAMESPACES


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in element:
        if _is(child, name):
            return child

    return None


class _TooDeep(Exception):
    pass


class _Collector:
    """Walks a DOCX body and collects its paragraphs and tables as segments."""

    def __init__(self):
        self.segments: list[dict] = []
        self.budget = _Budget()
        self.paragraphs = 0
        self.tables = 0

    def blocks(self, container: ElementTree.Element, depth: int) -> None:
        """Paragraphs and tables of a container (the body, a content control, a text box)."""
        if depth > MAX_DOCX_DEPTH:
            raise _TooDeep()

        for child in container:
            if self.budget.spent:
                self.budget.cut = True
                return

            namespace, local = _local(child)

            if namespace == MARKUP_COMPATIBILITY and local == "AlternateContent":
                choice = next(iter(child), None)

                if choice is not None:
                    self.blocks(choice, depth + 1)
            elif namespace not in WORD_NAMESPACES:
                continue
            elif local == "p":
                self.paragraph(child, depth + 1)
            elif local == "tbl":
                self.table(child, depth + 1)
            elif local in ("sdt", "customXml", "ins", "smartTag"):
                content = _child(child, "sdtContent") if local == "sdt" else child

                if content is not None:
                    self.blocks(content, depth + 1)

    def paragraph(self, element: ElementTree.Element, depth: int) -> None:
        boxes: list[ElementTree.Element] = []
        text = self.budget.take(clean_text("".join(_runs(element, boxes, depth))).strip())

        if text:
            self.paragraphs += 1
            self.segments.append({"kind": "paragraph", "number": self.paragraphs, "text": text})

        # Text boxes anchored in the paragraph come after it.
        for box in boxes:
            self.blocks(box, depth + 1)

    def table(self, element: ElementTree.Element, depth: int) -> None:
        rows = []

        for row in _table_rows(element):
            cells = []

            for cell in row:
                inner = _Collector()
                inner.budget = self.budget
                inner.blocks(cell, depth + 1)
                cells.append("\n".join(_segment_text(segment) for segment in inner.segments))

            if any(cells):
                rows.append(cells)

        if rows:
            self.tables += 1
            self.segments.append({"kind": "table", "number": self.tables, "rows": rows})


def _table_rows(table: ElementTree.Element):
    """The cells of each row of a table, also inside content controls."""
    for child in table:
        if _is(child, "tr"):
            yield [cell for cell in _cells(child)]
        elif _is(child, "sdt"):
            content = _child(child, "sdtContent")

            if content is not None:
                yield from _table_rows(content)


def _cells(row: ElementTree.Element):
    for child in row:
        if _is(child, "tc"):
            yield child
        elif _is(child, "sdt"):
            content = _child(child, "sdtContent")

            if content is not None:
                yield from _cells(content)


def _segment_text(segment: dict) -> str:
    if segment["kind"] == "table":
        return "\n".join(" | ".join(row) for row in segment["rows"])

    return segment["text"]


def _runs(element: ElementTree.Element, boxes: list, depth: int):
    """The visible text of a paragraph, in order.

    Deleted and moved-away text, field instructions and the fallback copy of
    alternate content are left out; text boxes are set aside in boxes.
    """
    if depth > MAX_DOCX_DEPTH:
        raise _TooDeep()

    for child in element:
        namespace, local = _local(child)

        if namespace == MARKUP_COMPATIBILITY:
            if local == "AlternateContent":
                choice = next(iter(child), None)

                if choice is not None:
                    yield from _runs(choice, boxes, depth + 1)

            continue

        if namespace in WORD_NAMESPACES:
            if local == "t":
                yield child.text or ""
                continue

            if local == "tab":
                yield "\t"
                continue

            if local in ("br", "cr"):
                yield "\n"
                continue

            if local == "noBreakHyphen":
                yield "-"
                continue

            if local in ("del", "moveFrom", "delText", "instrText", "delInstrText"):
                continue

            if local == "txbxContent":
                boxes.append(child)
                continue

        yield from _runs(child, boxes, depth + 1)


# --- the child process -----------------------------------------------------------

_BLOCKED_EVENTS = (
    "socket.",
    "subprocess.",
    "os.system",
    "os.exec",
    "os.posix_spawn",
    "os.spawn",
    "os.fork",
    "os.remove",
    "os.rename",
    "os.mkdir",
    "os.rmdir",
    "os.link",
    "os.symlink",
    "os.truncate",
    "shutil.",
    "ctypes.",
    "webbrowser.",
    "urllib.",
)
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _refuse(event: str, args) -> None:
    if event.startswith(_BLOCKED_EVENTS):
        raise PermissionError(f"refused in the resume reader: {event}")

    if event == "open" and len(args) > 2:
        mode, flags = args[1], args[2]
        writing = bool(flags & _WRITE_FLAGS) if isinstance(flags, int) else any(
            char in str(mode or "") for char in "wax+"
        )

        if writing:
            raise PermissionError("refused in the resume reader: writing a file")


def _confine() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

    try:
        resource.setrlimit(resource.RLIMIT_AS, (CHILD_MEMORY_BYTES, CHILD_MEMORY_BYTES))
    except (ValueError, OSError, AttributeError):
        # macOS does not offer it; the limits on size, pages and inflation remain.
        pass

    import pypdf.filters

    for name in (
        "ZLIB_MAX_OUTPUT_LENGTH",
        "LZW_MAX_OUTPUT_LENGTH",
        "RUN_LENGTH_MAX_OUTPUT_LENGTH",
        "MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH",
        "MAX_DECLARED_STREAM_LENGTH",
        "FLATE_MAX_BUFFER_SIZE",
    ):
        if hasattr(pypdf.filters, name):
            setattr(pypdf.filters, name, MAX_PDF_STREAM_BYTES)

    sys.addaudithook(_refuse)


def worker_main(argv: list[str] | None = None) -> int:
    """The child: read one file from standard input, answer with JSON on standard output."""
    argv = sys.argv[1:] if argv is None else argv
    kind = argv[0] if argv else ""
    _confine()
    data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)

    if kind not in (PDF, DOCX):
        reading = failed(CRASHED)
    elif len(data) > MAX_FILE_BYTES:
        reading = failed(TOO_BIG_INSIDE)
    else:
        reading = read_pdf(data) if kind == PDF else read_docx(data)

    sys.stdout.buffer.write(json.dumps(reading.as_json(), ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()

    return 0


_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {path!r}); "
    "from app.library.resume_reader import worker_main; sys.exit(worker_main())"
)


def worker_command(kind: str) -> list[str]:
    """How the child is started: isolated mode, no bytecode, UTF-8, this file's worker_main."""
    return [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        _BOOTSTRAP.format(path=str(BACKEND_DIR)),
        kind,
    ]


# --- running a reading -------------------------------------------------------------


class Readings:
    """The readings running in this app: at most PARALLEL_READS at once, each stoppable by its key."""

    def __init__(self, parallel: int = PARALLEL_READS, *, seconds: float = READ_SECONDS, command=worker_command):
        self._slots = threading.BoundedSemaphore(parallel)
        self._lock = threading.Lock()
        self._running: dict[str, subprocess.Popen] = {}
        self._stopped: set[str] = set()
        self.seconds = seconds
        self.command = command

    def read(self, key: str, kind: str, data: bytes, *, queue_seconds: float = QUEUE_SECONDS) -> Reading:
        """Read one file in a child process; key names the reading for stop()."""
        if not self._slots.acquire(timeout=queue_seconds):
            return failed(BUSY)

        try:
            return self._run(key, kind, data)
        finally:
            self._slots.release()

    def stop(self, key: str) -> None:
        """End the reading with this key, if one runs; it then answers CANCELLED.

        Only the signal is sent here: the reading's own thread collects the
        ended process. A reading that has not started yet is not affected;
        its caller finds the import ended when it stores the result.
        """
        with self._lock:
            process = self._running.get(key)

            if process is None:
                return

            self._stopped.add(key)

        try:
            process.kill()
        except ProcessLookupError:
            pass

    def running(self, key: str) -> bool:
        with self._lock:
            return key in self._running

    def _run(self, key: str, kind: str, data: bytes) -> Reading:
        process = subprocess.Popen(
            self.command(kind),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={},
            close_fds=True,
        )

        with self._lock:
            self._running[key] = process

        try:
            try:
                answer, _ = process.communicate(data, timeout=self.seconds)
            except subprocess.TimeoutExpired:
                _kill(process)
                return failed(TIMEOUT)
        finally:
            with self._lock:
                self._running.pop(key, None)
                stopped = key in self._stopped
                self._stopped.discard(key)

        if stopped:
            return failed(CANCELLED)

        if process.returncode != 0 or len(answer) > MAX_ANSWER_BYTES:
            return failed(CRASHED)

        return _parse_answer(answer)


def _kill(process: subprocess.Popen) -> None:
    """End a child and wait for it, so nothing of it keeps running."""
    try:
        process.kill()
    except ProcessLookupError:
        pass

    try:
        process.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def _parse_answer(answer: bytes) -> Reading:
    """The child's JSON as a Reading; anything unexpected counts as a crash."""
    try:
        value = json.loads(answer.decode("utf-8"))
        status = value["status"]
        segments = value["segments"]
        notes = value["notes"]
        failure = value["failure"]
        pages = value["pages"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return failed(CRASHED)

    if status not in (READ, PARTIAL, FAILED) or not isinstance(segments, list) or not isinstance(notes, dict):
        return failed(CRASHED)

    if not all(_valid_segment(segment) for segment in segments):
        return failed(CRASHED)

    reading = Reading(
        status=status,
        segments=segments,
        notes=notes,
        failure=failure if isinstance(failure, str) else None,
        pages=pages if isinstance(pages, int) else None,
    )

    if reading.chars > MAX_TEXT_CHARS or (status == FAILED) != (reading.failure is not None):
        return failed(CRASHED)

    return reading


def _valid_segment(segment) -> bool:
    if not isinstance(segment, dict) or not isinstance(segment.get("number"), int):
        return False

    if segment.get("kind") == "page":
        return isinstance(segment.get("text"), str) and isinstance(segment.get("unreadable", False), bool)

    if segment.get("kind") == "paragraph":
        return isinstance(segment.get("text"), str)

    if segment.get("kind") == "table":
        rows = segment.get("rows")

        return isinstance(rows, list) and all(
            isinstance(row, list) and all(isinstance(cell, str) for cell in row) for row in rows
        )

    return False


if __name__ == "__main__":
    sys.exit(worker_main())
