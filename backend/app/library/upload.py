"""Receiving one attachment upload: a bounded, streaming multipart reader.

The ordinary form reader of the web layer keeps forms in memory and refuses
files; only the upload route uses this one. It reads the request body as it
arrives and stops at the first thing that is off:

- more bytes than the request announced in Content-Length or than
  MAX_REQUEST_BYTES, or fewer than announced (the upload broke off);
- a part other than the three the upload form sends (csrf_token,
  upload_key, file), a part sent twice, a field longer than MAX_FIELD_BYTES;
- file bytes before this session's CSRF token was checked;
- a file name that is not PDF, PNG or JPEG, or a declared type that does not
  match it (found before any byte of the file is written);
- more than MAX_FILE_BYTES of file.

File bytes go straight into a staging file of the attachment store, never
through the system's temporary folder. A rejected upload removes its staging
file again, so it leaves nothing behind.
"""
from typing import AsyncIterator, Callable

from python_multipart.multipart import MultipartParseError, MultipartParser, parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.library.attachments import (
    AGAIN,
    MAX_FILE_BYTES,
    TOO_LARGE,
    UPLOAD_KEY,
    ReceivedFile,
    UploadRejected,
    UploadTokenRejected,
    check_announced,
    check_content,
)
from app.library.files import FileStore

# Room for the boundaries, the part headers and the two short fields.
MULTIPART_OVERHEAD = 64 * 1024
MAX_REQUEST_BYTES = MAX_FILE_BYTES + MULTIPART_OVERHEAD
MAX_FIELD_BYTES = 200
PARTS = ("csrf_token", "upload_key", "file")

NOT_AN_UPLOAD = "这次提交不是附件上传表单，没有执行。" + AGAIN
BROKEN = "上传的数据不完整或格式不对，这个文件没有上传。" + AGAIN
LENGTH_MISMATCH = "上传的数据和它声明的长度不一致，这个文件没有上传。" + AGAIN
UNEXPECTED_PART = "上传表单里有不认识的内容，这个文件没有上传。请刷新页面后" + AGAIN
NO_FILE = "没有收到文件。" + AGAIN
FORM_INCOMPLETE = "上传表单不完整，这个文件没有上传。请刷新页面后" + AGAIN


class _Parts:
    """Callbacks of the multipart parser for the parts of the upload form."""

    def __init__(self, token_ok: Callable[[str], bool]):
        self.token_ok = token_ok
        self.seen: set[str] = set()
        self.token_checked = False
        self.ended = False
        self.upload_key: str | None = None
        self.filename: str | None = None
        self.file_type = None
        self.file_bytes = 0
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

        if disposition != b"form-data" or name not in PARTS or name in self.seen:
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
            raise UploadRejected(NO_FILE, 400)

        raw_name = options[b"filename"].decode("utf-8", errors="replace")
        declared = self._headers.get(b"content-type", b"").decode("latin-1")
        self.filename, self.file_type = check_announced(raw_name, declared)

    def _part_data(self, data, start, end):
        if self._part == "file":
            self.file_bytes += end - start

            if self.file_bytes > MAX_FILE_BYTES:
                raise UploadRejected(TOO_LARGE, 413, drain=False)

            self.pending.append(bytes(data[start:end]))
        else:
            self._text.extend(data[start:end])

            if len(self._text) > MAX_FIELD_BYTES:
                raise UploadRejected(UNEXPECTED_PART, 400)

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
    """Read one upload into a staging file and check it; raise UploadRejected if it cannot be kept.

    On success the caller owns the staging file (store_upload removes it).
    """
    kind, options = parse_options_header(content_type)
    boundary = options.get(b"boundary")

    if kind != b"multipart/form-data" or not boundary:
        raise UploadRejected(NOT_AN_UPLOAD, 400)

    parts = _Parts(token_ok)
    parser = MultipartParser(boundary, parts.callbacks())
    staging = None
    received = 0

    try:
        try:
            async for chunk in chunks:
                received += len(chunk)

                if received > content_length or received > MAX_REQUEST_BYTES:
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

        if parts.file_type is None:
            raise UploadRejected(NO_FILE, 400)

        if staging is None:
            # A file part without a single byte: nothing was written.
            check_content(parts.file_type, b"", 0)

        await run_in_threadpool(staging.finish)
        check_content(parts.file_type, staging.head, staging.size)
    except BaseException:
        if staging is not None:
            await run_in_threadpool(staging.discard)

        raise

    return ReceivedFile(
        upload_key=parts.upload_key,
        filename=parts.filename,
        file_type=parts.file_type,
        staging=staging,
    )
