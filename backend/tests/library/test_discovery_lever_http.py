"""U3-D2: the one transport that reaches Lever (app.discovery.lever_http), tested with no network at all.

Every reply comes from FakeLever: the far end of a socketpair (two connected local sockets, with no address and
no network), which reads one request, keeps it, and answers with scripted bytes at a scripted pace. Requests go
through PairedConnection, http.client's own HTTPConnection on the near end, so what is tested is http.client's
real parsing, socket timeouts and reads under this module's limits. The TLS connection the product would make is
built and inspected only, never opened. Nothing here admits a source or turns real access on.
"""
import http.client
import json
import socket
import ssl
import threading
import time

import pytest

from app.discovery import lever_http
from app.discovery.lever import (
    BadReply,
    HostGuard,
    HostStopped,
    LeverSource,
    Reply,
    TransportError,
    TransportRefused,
    TransportTimeout,
    TransportTooLarge,
)
from app.discovery.lever_http import USER_AGENT, LeverHttpTransport, https_connection, tls_context
from app.discovery.sources import HeldBack
from app.discovery.sources import Query, SourceError, SourceTimeout
from lever_helpers import HARBOR, NORTH, detail_url, list_url, pid, posting

JSON_TYPE = "application/json; charset=utf-8"
QUERY = Query("Data", None, None, None)
# Keep the connection open and answer nothing more, until the client goes away.
HOLD = object()


class PairedConnection(http.client.HTTPConnection):
    """http.client's own connection, on one end of a socketpair: connecting reaches no network."""

    def __init__(self, sock: socket.socket, timeout: float):
        super().__init__(lever_http.API_HOST, timeout=timeout)
        self._paired = sock

    def connect(self):
        self._paired.settimeout(self.timeout)
        self.sock = self._paired


class FakeLever:
    """Answers each connection made through opener: reads one request, keeps it with the time it came, then plays
    the script given for it (bytes to send, seconds to pause, or HOLD). Scripts are used in order; the last repeats."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.requests: list[tuple[float, bytes]] = []

    def opener(self, timeout: float) -> PairedConnection:
        near, far = socket.socketpair()
        script = self.scripts.pop(0) if len(self.scripts) > 1 else self.scripts[0]
        threading.Thread(target=self._serve, args=(far, script), daemon=True).start()

        return PairedConnection(near, timeout)

    def _serve(self, far: socket.socket, script) -> None:
        try:
            data = b""

            while b"\r\n\r\n" not in data:
                chunk = far.recv(4096)

                if not chunk:
                    return

                data += chunk

            self.requests.append((time.monotonic(), data))

            for step in script:
                if step is HOLD:
                    while far.recv(4096):
                        pass

                    return

                if isinstance(step, (int, float)):
                    time.sleep(step)
                else:
                    far.sendall(step)
        except OSError:
            return
        finally:
            far.close()

    def request_head(self, number: int = 0) -> tuple[str, dict[str, str]]:
        head = self.requests[number][1].partition(b"\r\n\r\n")[0].decode("latin-1")
        line, *fields = head.split("\r\n")

        return line, {name.casefold(): value for name, value in (field.split(": ", 1) for field in fields)}


def response(status: int = 200, body: bytes = b"[]", *, content_type: str = JSON_TYPE, headers=(), length: bool = True) -> bytes:
    lines = [f"HTTP/1.1 {status} X", f"Content-Type: {content_type}", *headers, "Connection: close"]

    if length:
        lines.append(f"Content-Length: {len(body)}")

    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def chunked(*pieces: bytes) -> bytes:
    head = f"HTTP/1.1 200 OK\r\nContent-Type: {JSON_TYPE}\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"

    return head.encode() + b"".join(f"{len(piece):x}\r\n".encode() + piece + b"\r\n" for piece in pieces) + b"0\r\n\r\n"


def never_opened(timeout: float):
    raise AssertionError("a connection was opened")


def elapsed(started: float) -> float:
    return time.monotonic() - started


def settle(transport: LeverHttpTransport) -> None:
    """Wait for a request given up to end: its socket was shut down, so it does at once."""
    deadline = time.monotonic() + 3.0

    while transport.busy() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert not transport.busy()


# --- what is sent -------------------------------------------------------------------------------------------


def test_a_get_sends_one_request_with_only_its_own_headers():
    body = json.dumps([posting(1)]).encode("utf-8")
    fake = FakeLever([response(200, body)])
    transport = LeverHttpTransport(opener=fake.opener)

    reply = transport.get(list_url(NORTH), 5.0)
    line, headers = fake.request_head()

    assert reply == Reply(200, JSON_TYPE, body)
    assert line == "GET /v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100 HTTP/1.1"
    # No cookie, credential or proxy header: only these.
    assert headers == {
        "host": "api.lever.co",
        "accept": "application/json",
        "accept-encoding": "identity",
        "user-agent": USER_AGENT,
        "connection": "close",
    }
    assert (transport.opened, len(fake.requests)) == (1, 1)


def test_robots_and_a_posting_are_asked_for_with_no_query():
    fake = FakeLever([response(200, b"User-agent: *\nAllow: /\nCrawl-delay: 1\n", content_type="text/plain")])
    transport = LeverHttpTransport(opener=fake.opener)

    transport.get("https://api.lever.co/robots.txt", 5.0)
    transport.get(detail_url(NORTH, pid(1)), 5.0)

    assert fake.request_head(0) == (
        "GET /robots.txt HTTP/1.1",
        {"host": "api.lever.co", "accept": "text/plain", "accept-encoding": "identity", "user-agent": USER_AGENT, "connection": "close"},
    )
    assert fake.request_head(1)[0] == f"GET /v0/postings/rr-demo-northbay/{pid(1)} HTTP/1.1"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.lever.co/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        "https://jobs.lever.co/rr-demo-northbay",
        "https://api.eu.lever.co/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        "https://api.lever.co.evil.test/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        "https://api.lever.co:8443/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        "https://user:secret@api.lever.co/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        "https://api.lever.co/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100&q=data",
        "https://api.lever.co/v0/postings/rr-demo-northbay?location=Boston",
        "https://api.lever.co/v0/postings/rr-demo-northbay",
        f"https://api.lever.co/v0/postings/rr-demo-northbay/{pid(1)}?mode=json",
        f"https://api.lever.co/v0/postings/rr-demo-northbay/{pid(1)}/apply",
        "https://api.lever.co/v0/postings/../robots.txt",
        "https://api.lever.co/robots.txt?x=1",
        "https://api.lever.co/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100#top",
        "https://api.lever.co/v1/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
    ],
)
def test_nothing_but_https_get_to_api_lever_co_is_ever_opened(url):
    transport = LeverHttpTransport(opener=never_opened)

    with pytest.raises(TransportRefused):
        transport.get(url, 5.0)

    assert transport.opened == 0


def test_the_product_connection_is_tls_to_port_443_with_certificates_checked(monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")

    connection = https_connection(5.0)
    context = tls_context()

    assert isinstance(connection, http.client.HTTPSConnection)
    assert (connection.host, connection.port, connection.timeout) == ("api.lever.co", 443, 5.0)
    # Built only: nothing connected, and no proxy whatever the environment says.
    assert connection.sock is None and connection._tunnel_host is None
    for checked in (context, connection._context):
        assert checked.verify_mode is ssl.CERT_REQUIRED and checked.check_hostname
        assert checked.minimum_version >= ssl.TLSVersion.TLSv1_2


# --- what comes back --------------------------------------------------------------------------------------


def test_a_chunked_reply_is_read_whole():
    fake = FakeLever([chunked(b'[{"id"', b': "x"}]')])

    assert LeverHttpTransport(opener=fake.opener).get(list_url(NORTH), 5.0).body == b'[{"id": "x"}]'


def test_a_redirect_comes_back_as_it_is_and_is_never_followed():
    fake = FakeLever([response(301, b"", headers=["Location: https://evil.example.invalid/steal"])])
    transport = LeverHttpTransport(opener=fake.opener)

    reply = transport.get(list_url(NORTH), 5.0)

    assert reply.status == 301
    assert (transport.opened, len(fake.requests)) == (1, 1)


def test_a_reply_saying_it_is_too_large_is_refused_before_its_body_is_read():
    # It says 5,000 bytes and then sends nothing: read, it would only time out.
    fake = FakeLever([response(200, b"", headers=["Content-Length: 5000"], length=False), HOLD])
    transport = LeverHttpTransport(opener=fake.opener, max_bytes=1_000)
    started = time.monotonic()

    with pytest.raises(TransportTooLarge):
        transport.get(list_url(NORTH), 3.0)

    assert elapsed(started) < 1.0


def test_a_reply_growing_past_the_limit_is_cut_off():
    fake = FakeLever([response(200, b"", length=False), b"x" * 600, b"y" * 600, HOLD])
    transport = LeverHttpTransport(opener=fake.opener, max_bytes=1_000)
    started = time.monotonic()

    with pytest.raises(TransportTooLarge):
        transport.get(list_url(NORTH), 3.0)

    assert elapsed(started) < 1.0


def test_a_compressed_reply_is_not_read():
    fake = FakeLever([response(200, b"\x1f\x8b", headers=["Content-Encoding: gzip"])])

    with pytest.raises(TransportError):
        LeverHttpTransport(opener=fake.opener).get(list_url(NORTH), 3.0)


@pytest.mark.parametrize("script", [[b"garbage\r\n"], []], ids=["bad_status_line", "closed_at_once"])
def test_a_broken_reply_is_an_error_and_nothing_is_tried_again(script):
    fake = FakeLever(script)
    transport = LeverHttpTransport(opener=fake.opener)

    with pytest.raises(TransportError) as raised:
        transport.get(list_url(NORTH), 3.0)

    assert not isinstance(raised.value, (TransportTimeout, TransportRefused))
    assert (transport.opened, len(fake.requests)) == (1, 1)


# Codex F2 (2026-11-11): a body ending before its length was taken as a whole, empty list.
CUT_SHORT = [response(200, b"[]", headers=["Content-Length: 100"], length=False)]
TRANSPORT_ONLY = (TransportTimeout, TransportRefused, TransportTooLarge)


def test_a_body_cut_short_of_its_length_is_a_failure_never_an_empty_list():
    with pytest.raises(TransportError) as raised:
        LeverHttpTransport(opener=FakeLever(CUT_SHORT).opener).get(list_url(NORTH), 3.0)

    assert not isinstance(raised.value, TRANSPORT_ONLY)

    # Through the adapter: the search fails, rather than finding a board with no postings.
    source = LeverSource(
        NORTH, transport=LeverHttpTransport(opener=FakeLever(CUT_SHORT).opener), guard=HostGuard(interval=0.01, min_request=0.05)
    )

    with pytest.raises(SourceError) as failed:
        source.search(QUERY, 5.0)

    assert type(failed.value) is SourceError
    assert (source.guard.log, source.guard.failures) == ([(list_url(NORTH), "error")], 1)


@pytest.mark.parametrize(
    "script",
    [
        # Chunks with no last one: the connection closes after the first.
        [f"HTTP/1.1 200 OK\r\nContent-Type: {JSON_TYPE}\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n2\r\n[]\r\n".encode()],
        # A length that is not plain digits cannot tell a whole body from a cut one. Python's int() takes "+2"
        # as 2; only the digits rule turns it down.
        [response(200, b"[]", headers=["Content-Length: 2x"], length=False)],
        [response(200, b"[]", headers=["Content-Length: +2"], length=False)],
    ],
    ids=["chunks_cut_short", "length_not_a_number", "length_with_a_sign"],
)
def test_a_reply_that_cannot_be_known_whole_is_a_failure(script):
    with pytest.raises(TransportError) as raised:
        LeverHttpTransport(opener=FakeLever(script).opener).get(list_url(NORTH), 3.0)

    assert not isinstance(raised.value, TRANSPORT_ONLY)


def test_a_reply_with_neither_a_length_nor_chunks_ends_where_the_connection_closes():
    fake = FakeLever([response(200, b'[{"id": "x"}]', length=False)])

    assert LeverHttpTransport(opener=fake.opener).get(list_url(NORTH), 3.0).body == b'[{"id": "x"}]'


# --- time -------------------------------------------------------------------------------------------------


def test_a_slow_drip_is_cut_at_the_deadline_however_fast_each_byte_comes():
    # Each byte comes well within any per-read timeout: only a deadline over the whole read ends it.
    fake = FakeLever([response(200, b"", length=False), *([0.05, b"x"] * 400)])
    transport = LeverHttpTransport(opener=fake.opener)
    started = time.monotonic()

    with pytest.raises(TransportTimeout):
        transport.get(list_url(NORTH), 0.5)

    assert 0.4 <= elapsed(started) < 1.5
    settle(transport)


def test_a_silent_server_is_given_up_at_the_deadline():
    fake = FakeLever([HOLD])
    transport = LeverHttpTransport(opener=fake.opener)
    started = time.monotonic()

    with pytest.raises(TransportTimeout):
        transport.get(list_url(NORTH), 0.4)

    assert 0.3 <= elapsed(started) < 1.5
    settle(transport)


def test_a_request_stuck_before_sending_is_given_up_and_never_sent_later():
    fake = FakeLever([response(200, b"[]")])
    released = threading.Event()
    # Released after 3 seconds whatever happens, so a run where the deadline does not hold ends too.
    threading.Timer(3.0, released.set).start()

    def stuck(timeout):
        connection = fake.opener(timeout)
        connect = connection.connect

        def slow_connect():
            # Like a name that takes long to resolve: no socket to shut down yet.
            released.wait(5.0)
            connect()

        connection.connect = slow_connect

        return connection

    transport = LeverHttpTransport(opener=stuck)
    started = time.monotonic()

    with pytest.raises(TransportTimeout):
        transport.get(list_url(NORTH), 0.4)

    assert elapsed(started) < 1.5

    # While it has not ended, nothing more is sent.
    with pytest.raises(TransportRefused):
        transport.get(list_url(NORTH), 1.0)

    released.set()
    settle(transport)

    # Connected only after it was given up: closed without sending its request.
    assert fake.requests == []
    assert transport.opened == 1


# --- with the guard and the adapter ---------------------------------------------------------------------------


def test_the_adapter_reads_through_the_real_client_and_its_limits():
    item = posting(1)
    fake = FakeLever([response(200, json.dumps([item, posting(2)]).encode("utf-8"))], [response(200, json.dumps(item).encode("utf-8"))])
    source = LeverSource(NORTH, transport=LeverHttpTransport(opener=fake.opener), guard=HostGuard(interval=0.01, min_request=0.05))

    found = source.search(QUERY, 5.0)
    detail = source.read(found[0].ref, 5.0)

    assert [item.ref.posting_id for item in found] == [pid(1), pid(2)]
    assert detail.url == f"https://jobs.lever.co/rr-demo-northbay/{pid(1)}"
    assert [fake.request_head(number)[0].split()[1] for number in range(2)] == [
        "/v0/postings/rr-demo-northbay?mode=json&skip=0&limit=100",
        f"/v0/postings/rr-demo-northbay/{pid(1)}",
    ]


def test_the_budget_and_the_interval_hold_on_the_wire():
    fake = FakeLever([response(200, b"[]")])
    transport = LeverHttpTransport(opener=fake.opener)
    source = LeverSource(NORTH, transport=transport, guard=HostGuard(budget=3, interval=0.15, request_seconds=1.0, min_request=0.05))

    for _ in range(3):
        source.search(QUERY, 5.0)

    with pytest.raises(HostStopped):
        source.search(QUERY, 5.0)

    arrivals = [at for at, _ in fake.requests]

    assert (transport.opened, len(arrivals)) == (3, 3)
    assert all(later - earlier >= 0.15 for earlier, later in zip(arrivals, arrivals[1:]))


def test_the_batch_window_cuts_a_slow_reply_and_stops_what_follows():
    fake = FakeLever([HOLD])
    transport = LeverHttpTransport(opener=fake.opener)
    host = HostGuard(window=0.6, interval=0.05, request_seconds=5.0, min_request=0.1)
    source = LeverSource(NORTH, transport=transport, guard=host)
    started = time.monotonic()

    # The call has 10 seconds and a request 5: the window's 0.6 is all it is given.
    with pytest.raises(SourceTimeout):
        source.search(QUERY, 10.0)

    assert elapsed(started) < 1.5

    with pytest.raises(HostStopped):
        source.search(QUERY, 10.0)

    assert (transport.opened, host.sent) == (1, 1)
    assert host.stopped == "这一批的网络时间窗口到了，之后不再发请求"


def test_failures_in_a_row_stop_the_rest_and_an_answer_resets_the_count():
    fake = FakeLever(
        [response(500, b"{}")],
        [response(200, b"[]")],
        [response(500, b"{}")],
        [response(301, b"", headers=["Location: https://jobs.lever.co/"])],
    )
    transport = LeverHttpTransport(opener=fake.opener)
    host = HostGuard(failures_in_a_row=2, interval=0.01, min_request=0.05)
    source = LeverSource(NORTH, transport=transport, guard=host)

    with pytest.raises(BadReply):
        source.search(QUERY, 5.0)

    source.search(QUERY, 5.0)

    for _ in range(2):
        with pytest.raises(BadReply):
            source.search(QUERY, 5.0)

    with pytest.raises(HostStopped):
        source.search(QUERY, 5.0)

    assert transport.opened == 4
    assert host.stopped == "连续 2 个请求失败，之后不再发请求"


def test_a_request_the_transport_will_not_send_is_not_counted():
    host = HostGuard(min_request=0.05)

    # Kept back, never a bad reply: nothing went out and nothing came back (U3-D2-E2).
    with pytest.raises(HeldBack, match="传输层没有发出这个请求"):
        host.get(LeverHttpTransport(opener=never_opened), "https://jobs.lever.co/rr-demo-northbay", time.monotonic() + 5.0)

    assert (host.sent, host.failures, host.log) == (0, 0, [("https://jobs.lever.co/rr-demo-northbay", "refused")])


# Codex F1 (2026-11-11): a refusal whose body failed (compressed, too large, cut short) came back as a plain error,
# so the host was not stopped.
@pytest.mark.parametrize(
    "script",
    [
        [response(429, b"\x1f\x8b\x08\x00", headers=["Content-Encoding: gzip"])],
        [response(402, b"\x1f\x8b\x08\x00", headers=["Content-Encoding: gzip"])],
        # It says it is larger than allowed, then sends nothing: reading it would only time out.
        [response(403, b"", headers=["Content-Length: 99999999"], length=False), HOLD],
        [response(401, b"short", headers=["Content-Length: 100"], length=False)],
        [response(407, b"x" * 5_000)],
    ],
    ids=["429_gzip", "402_gzip", "403_says_too_large", "401_cut_short", "407_over_the_limit"],
)
def test_a_refusal_stops_the_host_whatever_its_body_is(script):
    fake = FakeLever(script)
    transport = LeverHttpTransport(opener=fake.opener, max_bytes=1_000)
    host = HostGuard(interval=0.01, min_request=0.05)
    north, harbor = (LeverSource(board, transport=transport, guard=host) for board in (NORTH, HARBOR))
    status = script[0].split(b" ", 2)[1].decode()
    started = time.monotonic()

    with pytest.raises(HostStopped):
        north.search(QUERY, 5.0)

    # Its body was not waited for.
    assert elapsed(started) < 1.0

    # Nothing more goes to the host, for this company or another.
    with pytest.raises(HostStopped):
        harbor.search(QUERY, 5.0)

    assert (transport.opened, host.sent, len(fake.requests)) == (1, 1, 1)
    assert host.log == [(list_url(NORTH), status)]
    assert host.stopped == f"来源回应 {status}（要求登录、付费或限流），之后不再向它发请求"


# Step1-F1 (2026-11-11): a redirect whose body failed (compressed, cut short) came back as a plain error, its status
# lost, so a batch that stops on any redirect went on.
MOVED = "Location: https://other.invalid/"


@pytest.mark.parametrize(
    "script",
    [
        [response(302, b"", headers=[MOVED])],
        [response(302, b"gzip-data", headers=[MOVED, "Content-Encoding: gzip"])],
        [response(302, b"[]", headers=[MOVED, "Content-Length: 100"], length=False)],
        # It says it is larger than allowed, then sends nothing: reading it would only time out.
        [response(301, b"", headers=[MOVED, "Content-Length: 99999999"], length=False), HOLD],
        # A body that never ends.
        [response(307, b"", headers=[MOVED], length=False), *([0.05, b"x"] * 100)],
    ],
    ids=["302", "302_gzip", "302_cut_short", "301_says_too_large", "307_endless"],
)
def test_a_redirect_comes_back_as_its_status_whatever_its_body_is(script):
    fake = FakeLever(script)
    transport = LeverHttpTransport(opener=fake.opener, max_bytes=1_000)
    status = int(script[0].split(b" ", 2)[1])
    started = time.monotonic()

    reply = transport.get(list_url(NORTH), 3.0)

    # At once, its body not read, and never followed: one request on the wire.
    assert (reply.status, reply.body) == (status, b"")
    assert elapsed(started) < 1.0
    assert (transport.opened, len(fake.requests)) == (1, 1)

    # Through the guard and the adapter it is a redirect, never a plain failure; the refusals alone stop the host.
    host = HostGuard(interval=0.01, min_request=0.05)
    source = LeverSource(NORTH, transport=LeverHttpTransport(opener=FakeLever(script).opener, max_bytes=1_000), guard=host)

    with pytest.raises(BadReply) as raised:
        source.search(QUERY, 5.0)

    assert f"来源回应 {status}" in str(raised.value)
    assert (host.log, host.failures, host.stopped) == ([(list_url(NORTH), str(status))], 1, None)
