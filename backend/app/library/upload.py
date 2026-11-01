"""Receiving one upload form: a bounded, streaming multipart reader.

The ordinary form reader of the web layer keeps forms in memory and refuses
files; only the upload routes use this one: an attachment's (ATTACHMENT_FORM)
and, since U2-B1, a resume import's (imports.IMPORT_FORM). It reads the
request body as it arrives and stops at the first thing that is off:

- more bytes than the request announced in Content-Length or than the
  form's request limit, or fewer than announced (the upload broke off);
- a part the form does not send, a part sent twice, a text part longer
  than the form allows it;
- file bytes before this session's CSRF token was checked;
- a file name of a type the form does not take, or a declared type that does
  not match it (found before any byte of the file is written). A form whose
  file is optional (an import) reads its other parts on and refuses at the
  end, so typed text can be shown again; no byte of such a file is kept;
- more file bytes than the form's limit.

File bytes go straight into a staging file of the attachment store, never
through the system's temporary folder. A rejected upload removes its staging
file again, so it leaves nothing behind.
"""
from dataclasses import dataclass
from typing import AsyncIterator, Callable

from python_multipart.multipart import MultipartParseError, MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.library.attachments import (
    AGAIN,
    MAX_FILE_BYTES,
    TOO_LARGE,
    UPLOAD_KEY,
    FileType,
    ReceivedFile,
    UploadRejected,
    UploadTokenRejected,
    check_announced,
    check_content,
)
from app.library.files import FileStore, StagingFile

# Room for the boundaries, the part headers and the short fields.
MULTIPART_OVERHEAD = 64 * 1024
MAX_REQUEST_BYTES = MAX_FILE_BYTES + MULTIPART_OVERHEAD
MAX_FIELD_BYTES = 200
# The part an empty file input became: it may not carry bytes.
_NO_FILE = "file:none"
# A file part whose file was refused: its bytes are read and dropped.
_REFUSED_FILE = "file:refused"

NOT_AN_UPLOAD = "这次提交不是附件上传表单，没有执行。" + AGAIN
BROKEN = "上传的数据不完整或格式不对，这个文件没有上传。" + AGAIN
LENGTH_MISMATCH = "上传的数据和它声明的长度不一致，这个文件没有上传。" + AGAIN
UNEXPECTED_PART = "上传表单里有不认识的内容，这个文件没有上传。请刷新页面后" + AGAIN
NO_FILE = "没有收到文件。" + AGAIN
FORM_INCOMPLETE = "上传表单不完整，这个文件没有上传。请刷新页面后" + AGAIN


@dataclass(frozen=True)
class UploadForm:
    """What one kind of upload form sends, and its limits.

    fields: the text parts besides the file, each with the most bytes it may
    have and what to say when it has more. The CSRF token and the one-time
    upload_key are always among them. announced and content check the file's
    name and declared type, then its first bytes. With file_optional, a file
    part without a chosen file (an empty file name, as browsers send it)
    means "no file", and a file refused by announced is skipped while the
    other parts are still read.
    """

    fields: dict[str, tuple[int, str]]
    announced: Callable[[str, str], tuple[str, FileType]]
    content: Callable[[FileType, bytes, int], None]
    max_file_bytes: int
    too_large: str
    max_request_bytes: int
    file_optional: bool = False


ATTACHMENT_FORM = UploadForm(
    fields={"csrf_token": (MAX_FIELD_BYTES, UNEXPECTED_PART), "upload_key": (MAX_FIELD_BYTES, UNEXPECTED_PART)},
    announced=check_announced,
    content=check_content,
    max_file_bytes=MAX_FILE_BYTES,
    too_large=TOO_LARGE,
    max_request_bytes=MAX_REQUEST_BYTES,
)


@dataclass
class ReceivedForm:
    """An upload form as received: its text parts, and its file if one came (in staging)."""

    values: dict[str, str]
    upload_key: str
    filename: str | None
    file_type: FileType | None
    staging: StagingFile | None


class _Parts:
    """Callbacks of the multipart parser for the parts of an upload form."""

    def __init__(self, token_ok: Callable[[str], bool], form: UploadForm = ATTACHMENT_FORM):
        self.token_ok = token_ok
        self.form = form
        self.seen: set[str] = set()
        self.values: dict[str, str] = {}
        self.token_checked = False
        self.ended = False
        self.upload_key: str | None = None
        self.filename: str | None = None
        self.file_type = None
        self.file_bytes = 0
        # Why the file of an optional-file form was refused, raised once the form is read.
        self.file_problem: UploadRejected | None = None
        # File bytes the parser handed over, waiting to be written to staging.
        self.pending: list[bytes] = []
        self._part: str | None = None
        self._headers: dict[bytes, bytes] = {}
        self._field = bytearray()
        self._value = bytearray()
        self._text = bytearray()

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self._part_begin,
            "on_header_field": lambda data, start, end: self._field.extend(data[start:end]),
            "on_header_value": lambda data, start, end: self._value.extend(data[start:end]),
            "on_header_end": self._header_end,
            "on_headers_finished": self._headers_finished,
            "on_part_data": self._part_data,
            "on_part_end": self._part_end,
            "on_end": self._end,
        }

    def _part_begin(self):
        self._part = None
        self._headers = {}
        self._text = bytearray()

    def _header_end(self):
        name = bytes(self._field).strip().lower()
        self._field.clear()

        if name in self._headers:
            raise UploadRejected(UNEXPECTED_PART, 400)

        self._headers[name] = bytes(self._value).strip()
        self._value.clear()

    def _headers_finished(self):
        disposition, options = parse_options_header(self._headers.get(b"content-disposition"))
        name = options.get(b"name", b"").decode("latin-1")

        if disposition != b"form-data" or not (name == "file" or name in self.form.fields) or name in self.seen:
            raise UploadRejected(UNEXPECTED_PART, 400)

        self.seen.add(name)
        self._part = name

        if name != "file":
            if b"filename" in options:
                raise UploadRejected(UNEXPECTED_PART, 400)

            return

        # Nothing of the file is kept before the form has proven it comes from this session.
        if not self.token_checked:
            raise UploadTokenRejected()

        if b"filename" not in options:
            if self.form.file_optional:
                self._part = _NO_FILE
                return

            raise UploadRejected(NO_FILE, 400)

        raw_name = options[b"filename"].decode("utf-8", errors="replace")

        if not raw_name and self.form.file_optional:
            # The file input was left empty: no file, and no byte may come.
            self._part = _NO_FILE
            return

        declared = self._headers.get(b"content-type", b"").decode("latin-1")

        try:
            self.filename, self.file_type = self.form.announced(raw_name, declared)
        except UploadRejected as problem:
            if not self.form.file_optional:
                raise

            self.file_problem = problem
            self._part = _REFUSED_FILE

    def _part_data(self, data, start, end):
        if self._part == "file":
            self.file_bytes += end - start

            if self.file_bytes > self.form.max_file_bytes:
                raise UploadRejected(self.form.too_large, 413, drain=False)

            self.pending.append(bytes(data[start:end]))
        elif self._part == _NO_FILE:
            if end > start:
                raise UploadRejected(BROKEN, 400)
        elif self._part == _REFUSED_FILE:
            # Dropped: the request's own limit bounds how much arrives.
            return
        else:
            self._text.extend(data[start:end])
            limit, message = self.form.fields[self._part]

            if len(self._text) > limit:
                raise UploadRejected(message, 400 if message == UNEXPECTED_PART else 413)

    def _part_end(self):
        if self._part == "csrf_token":
            if not self.token_ok(self._text.decode("utf-8", errors="replace")):
                raise UploadTokenRejected()

            self.token_checked = True
        elif self._part == "upload_key":
            value = self._text.decode("ascii", errors="replace")

            if not UPLOAD_KEY.fullmatch(value):
                raise UploadRejected(FORM_INCOMPLETE, 400)

            self.upload_key = value
        elif self._part not in (None, "file", _NO_FILE, _REFUSED_FILE):
            self.values[self._part] = self._text.decode("utf-8", errors="replace")

        self._part = None

    def _end(self):
        self.ended = True


async def receive_upload(
    chunks: AsyncIterator[bytes],
    *,
    content_type: str,
    content_length: int,
    files: FileStore,
    token_ok: Callable[[str], bool],
) -> ReceivedFile:
    """Read one attachment upload into a staging file and check it; raise UploadRejected if it cannot be kept.

    On success the caller owns the staging file (store_upload removes it).
    """
    received = await receive_form(
        chunks,
        content_type=content_type,
        content_length=content_length,
        files=files,
        token_ok=token_ok,
        form=ATTACHMENT_FORM,
    )

    return ReceivedFile(
        upload_key=received.upload_key,
        filename=received.filename,
        file_type=received.file_type,
        staging=received.staging,
    )


async def receive_form(
    chunks: AsyncIterator[bytes],
    *,
    content_type: str,
    content_length: int,
    files: FileStore,
    token_ok: Callable[[str], bool],
    form: UploadForm,
) -> ReceivedForm:
    """Read one upload form, its file (if any) into a staging file; raise UploadRejected if it cannot be kept.

    On success the caller owns the staging file, if there is one, and must
    discard it.
    """
    kind, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")

    if kind != b"multipart/form-data" or not boundary:
        raise UploadRejected(NOT_AN_UPLOAD, 400)

    parts = _Parts(token_ok, form)
    parser = MultipartParser(boundary, parts.callbacks())
    staging = None
    received = 0

    try:
        try:
            async for chunk in chunks:
                received += len(chunk)

                if received > content_length or received > form.max_request_bytes:
                    raise UploadRejected(LENGTH_MISMATCH, 400, drain=False)

                try:
                    parser.write(chunk)
                except MultipartParseError as error:
                    raise UploadRejected(BROKEN, 400) from error

                if parts.pending:
                    if staging is None:
                        staging = await run_in_threadpool(files.new_staging)

                    data = b"".join(parts.pending)
                    parts.pending.clear()
                    await run_in_threadpool(staging.write, data)
        except ClientDisconnect as error:
            raise UploadRejected(BROKEN, 400, drain=False) from error

        if received != content_length or not parts.ended:
            raise UploadRejected(BROKEN, 400, drain=False)

        if not parts.token_checked:
            raise UploadTokenRejected()

        if parts.upload_key is None:
            raise UploadRejected(FORM_INCOMPLETE, 400)

        if parts.file_problem is not None:
            raise parts.file_problem

        if parts.file_type is None and not form.file_optional:
            raise UploadRejected(NO_FILE, 400)

        if parts.file_type is not None:
            if staging is None:
                # A file part without a single byte: nothing was written.
                form.content(parts.file_type, b"", 0)

            await run_in_threadpool(staging.finish)
            form.content(parts.file_type, staging.head, staging.size)
    except BaseException as error:
        if staging is not None:
            await run_in_threadpool(staging.discard)

        if isinstance(error, UploadRejected):
            error.values = dict(parts.values)

        raise

    return ReceivedForm(
        values=parts.values,
        upload_key=parts.upload_key,
        filename=parts.filename,
        file_type=parts.file_type,
        staging=staging,
    )
