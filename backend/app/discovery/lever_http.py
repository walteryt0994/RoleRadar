"""The one transport that reaches Lever over the network (U3-D2; 2026-11-11). Off unless a caller passes it in.

Nothing in the product imports this module. A board's adapter keeps lever.NoNetwork unless a caller hands it
this transport, and every board stays unverified (app.discovery.lever.entry), so no tool calls one either way.
Only a separately approved, one-off validation may build one.

What it sends: one GET over HTTPS to api.lever.co on port 443, with the certificate and the host name checked
(ssl.create_default_context, TLS 1.2 at least), to a path of the Postings API or /robots.txt, with no query but
the documented list parameters. A redirect is never followed: a 3xx comes back as it is. Nothing is tried
again: one connection per request. No cookie, no credential and no proxy: http.client uses none of them by
itself, and nothing here adds one. The headers are Host, Accept, Accept-Encoding: identity, User-Agent and
Connection: close.

What it waits for: the whole request, from resolving the name to the last byte, within the time it is given.
The request runs in a worker thread that the caller waits for only until the deadline; then the request is
given up (TransportTimeout: it may have gone out), its socket is shut down so the thread ends, and anything it
gets afterwards is dropped. Each read is also given only the time left. While a request given up has not ended,
the next is not sent. A reply larger than max_bytes is refused as soon as it says so (Content-Length) or reads
past it, and the connection is closed: the rest is never read.

What it gives back: a refusal (401, 402, 403, 407, 429) as its status alone, its body never read, so nothing in
the body can hide it and the guard stops the host; a redirect (3xx) the same way, never followed, so a caller
always sees it as a redirect. Any other reply whole, or a failure: a body shorter than its
Content-Length, a Content-Length that is not a number, or chunks cut short fail the request; only a reply with
neither a length nor chunks ends where the connection closes.
"""
import http.client
import re
import socket
import ssl
import threading
import time
from collections.abc import Callable
from urllib.parse import urlsplit

from app.discovery.lever import (
    API_HOST,
    MAX_BYTES,
    STOP_STATUSES,
    Reply,
    TransportError,
    TransportRefused,
    TransportTimeout,
    TransportTooLarge,
)

PORT = 443
# A generic name: automated, read-only, one-off. Naming the project needs the Owner's consent.
USER_AGENT = "coverage-probe/1 (automated; read-only; one-off)"
CHUNK = 64 * 1024
# What may be asked: a board's list, one of its postings, or robots.txt; and for a list, its pages only.
BOARD_PATH = re.compile(r"/v0/postings/[A-Za-z0-9][A-Za-z0-9._-]{0,57}")
POSTING_PATH = re.compile(r"/v0/postings/[A-Za-z0-9][A-Za-z0-9._-]{0,57}/[A-Za-z0-9][A-Za-z0-9-]{0,63}")
ROBOTS = "/robots.txt"
PAGE_QUERY = re.compile(r"mode=json&skip=\d{1,4}&limit=\d{1,3}")
# A Content-Length: ASCII digits only.
DIGITS = re.compile(r"[0-9]{1,15}")


def tls_context() -> ssl.SSLContext:
    """Certificates and host names checked, as the default context does; nothing here loosens it."""
    context = ssl.create_default_context()
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)

    return context


def https_connection(timeout: float) -> http.client.HTTPSConnection:
    """A connection to api.lever.co:443 over TLS; making it sends nothing. http.client reads no proxy settings."""
    return http.client.HTTPSConnection(API_HOST, PORT, timeout=timeout, context=tls_context())


def target_of(url: str) -> str:
    """The path and query to send for url; TransportRefused, with nothing sent, for anything but these."""
    parts = urlsplit(url)

    if parts.scheme != "https":
        raise TransportRefused("只用 HTTPS")

    # Exactly the host: no other name, no port, no user or password.
    if parts.netloc != API_HOST:
        raise TransportRefused("只访问 api.lever.co")

    if parts.fragment:
        raise TransportRefused("地址不能带片段")

    if parts.path == ROBOTS or POSTING_PATH.fullmatch(parts.path):
        if parts.query:
            raise TransportRefused("这个地址不带参数")
    elif BOARD_PATH.fullmatch(parts.path):
        if not PAGE_QUERY.fullmatch(parts.query):
            raise TransportRefused("职位列表只带 mode、skip、limit 三个参数")
    else:
        raise TransportRefused("只读职位接口和 robots.txt")

    return parts.path + (f"?{parts.query}" if parts.query else "")


class LeverHttpTransport:
    """The transport of a one-off validation (app.discovery.lever.Transport). opener makes the connection of each
    request, given its time: https_connection unless a test passes a stand-in. opened counts the connections made."""

    def __init__(
        self,
        *,
        opener: Callable[[float], http.client.HTTPConnection] = https_connection,
        max_bytes: int = MAX_BYTES,
        user_agent: str = USER_AGENT,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._opener = opener
        self.max_bytes = max_bytes
        self.user_agent = user_agent
        self._monotonic = monotonic
        self.opened = 0
        self._running: _Attempt | None = None
        self._lock = threading.Lock()

    def busy(self) -> bool:
        """Whether a request given up earlier has still not ended."""
        running = self._running

        return running is not None and not running.done.is_set()

    def get(self, url: str, timeout: float) -> Reply:
        target = target_of(url)

        with self._lock:
            if self.busy():
                raise TransportRefused("上一个请求还没有结束，这个没有发出")

            if timeout <= 0:
                raise TransportRefused("没有剩余时间，没有发出")

            accept = "text/plain" if target == ROBOTS else "application/json"
            headers = {"Accept": accept, "Accept-Encoding": "identity", "User-Agent": self.user_agent, "Connection": "close"}
            attempt = _Attempt(self, target, headers, self._monotonic() + timeout)
            self._running = attempt
            self.opened += 1
            threading.Thread(target=attempt.run, name="lever-get", daemon=True).start()

        if not attempt.done.wait(timeout):
            attempt.abandon()

            raise TransportTimeout("超过时限，放弃等待；请求可能已经发出")

        return attempt.outcome()


class _Attempt:
    """One request in its worker thread. Its outcome is taken only if it ends in time."""

    def __init__(self, transport: LeverHttpTransport, target: str, headers: dict, deadline: float):
        self.transport = transport
        self.target = target
        self.headers = headers
        self.deadline = deadline
        self.done = threading.Event()
        self.abandoned = False
        # The socket itself, kept from when it connects: http.client hands it to the response and forgets it on
        # a reply that closes the connection, while the response goes on reading from it.
        self.sock: socket.socket | None = None
        self.reply: Reply | None = None
        self.error: TransportError | None = None
        self._lock = threading.Lock()

    def left(self) -> float:
        left = self.deadline - self.transport._monotonic()

        if left <= 0:
            raise TransportTimeout("超过时限")

        return left

    def run(self) -> None:
        connection = None

        try:
            connection = self.transport._opener(self.left())
            # Resolving, connecting and the TLS handshake: within the socket's timeout, and the caller's deadline.
            connection.connect()

            with self._lock:
                # Given up while connecting: closed without sending anything.
                if self.abandoned:
                    return

                self.sock = connection.sock

            self._timed()
            connection.request("GET", self.target, headers=self.headers)
            self._timed()
            response = connection.getresponse()

            # A refusal (login, payment, proxy, too many requests) stops the host whatever its body is: its status
            # comes back at once, and its body, compressed, too large or cut short, is never read. So does a
            # redirect (3xx, Step1-F1): never followed, and nothing in its body, failing or endless, may hide it.
            if response.status in STOP_STATUSES or 300 <= response.status < 400:
                self.reply = Reply(response.status, response.getheader("Content-Type") or "", b"")
                return

            encoding = (response.getheader("Content-Encoding") or "identity").strip().casefold()

            if encoding != "identity":
                raise TransportError("返回经过压缩，没有读取")

            length = response.getheader("Content-Length")
            # The length it says it has. Sent in chunks, there is none: http.client checks every chunk itself and
            # fails a reply cut short (IncompleteRead).
            declared = None

            if length is not None and not response.chunked:
                if not DIGITS.fullmatch(length.strip()):
                    raise TransportError("返回声明的长度格式不对，没有读取")

                declared = int(length.strip())

                if declared > self.transport.max_bytes:
                    raise TransportTooLarge("返回声明的大小超过上限，正文没有读取")

            body = bytearray()

            while True:
                self._timed()
                chunk = response.read1(min(CHUNK, self.transport.max_bytes + 1 - len(body)))

                if not chunk:
                    break

                body += chunk

                if len(body) > self.transport.max_bytes:
                    raise TransportTooLarge("返回超过大小上限，没有读完")

            # http.client's read1 ends quietly when the connection closes early. A body shorter than it said is cut
            # short: no answer at all, never a shorter one (an empty list read as complete).
            if declared is not None and len(body) != declared:
                raise TransportError(f"返回提前结束：声明 {declared} 字节，只收到 {len(body)} 字节")

            self.reply = Reply(response.status, response.getheader("Content-Type") or "", bytes(body))
        except TransportError as error:
            self.error = error
        except TimeoutError:
            self.error = TransportTimeout("超过时限")
        except (OSError, http.client.HTTPException) as error:
            self.error = TransportError(f"请求没有成功：{type(error).__name__}")
        except Exception as error:  # noqa: BLE001 - whatever goes wrong, the caller hears of it and the thread ends
            self.error = TransportError(f"请求没有成功：{type(error).__name__}")
        finally:
            if connection is not None:
                connection.close()

            self.done.set()

    def _timed(self) -> None:
        """Past the deadline, stop (TransportTimeout); otherwise give the socket's next wait only the time left."""
        left = self.left()

        # Once the reply is read whole, http.client closes the socket for good (its descriptor is then -1).
        if self.sock is not None and self.sock.fileno() != -1:
            self.sock.settimeout(left)

    def abandon(self) -> None:
        """Give the request up: shut its socket down so its thread ends; whatever it gets is dropped."""
        with self._lock:
            self.abandoned = True
            sock = self.sock

        if sock is not None:
            try:
                # The plain socket's own shutdown, so a TLS socket's state is not touched from this thread.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass

    def outcome(self) -> Reply:
        if self.reply is None:
            raise self.error or TransportError("请求没有结果")

        return self.reply
