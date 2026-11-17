"""U3-D4d: postings the user adds themselves, below the pages (app.discovery.supplied, app.discovery.records).

A link is matched as text only, against the shapes of the registered sources, and read only from a source the app may
read now, by its own adapter, as D4a reads (one check: a 404 alone closes nothing); the address itself is never opened.
A link kept with a pasted text drops what precedes "@" and parameters named as credentials, marks those that may be,
and keeps the rest. What is read or pasted is held in memory for the session that made it: a save claims it, binds the
record it makes before the write commits, and releases it whatever happens; once bound, it never makes another, and
deleting the record, ending the session, dropping it or its lapsing drops its text (D4d pre-check 17.2). Whether a
pasted posting is another one is the user's to say, on the last page shown of it, and the write looks again (17.3). A
pasted posting is only decided on; its text goes and comes back with its record. Synthetic data only, no network, no
model.
"""
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.discovery import records, supplied, updates
from app.discovery.contracts import PostingRef
from app.discovery.sources import NotFound
from app.discovery.synthetic import CAREERS, LEADS, PAGED
from app.job_analysis.scope import MAX_POSTING_CHARS
from app.library import job_texts
from app.library.job_export import collect_job_records, jobs_markdown
from app.library.lifecycle import delete_job_record, purge_due, request_job_record_purge, restore_job_record
from app.library.models import (
    JobPosting,
    JobPostingRecheck,
    JobPostingText,
    JobRecommendation,
    JobSearch,
    JobUpdateRun,
    JobWatch,
    JobWatchPosting,
)
from app.library.service import new_id
from update_helpers import POSTING, Board, kept_posting, register_with

LINK = f"https://careers.synthetic.invalid/postings/{POSTING}"
TEXT = "岗位职责：\n- 整理合成数据\n- 每周写一份合成周报\n（合成原文，不是真实招聘）"
SESSION, OTHER_SESSION = "session-a", "session-b"


class Ticks:
    """The store's own clock, moved by hand."""

    def __init__(self):
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def ticks() -> Ticks:
    return Ticks()


@pytest.fixture
def store(ticks) -> supplied.Supplied:
    return supplied.Supplied(clock=ticks)


def pasted(clock, *, title="数据分析实习生", company="合成数据科技", text=TEXT, link=None) -> supplied.PastedContent:
    body = supplied.kept_text(text)

    return supplied.PastedContent(title, company, body, supplied.text_digest(body), None if link is None else supplied.stored_link(link), clock.now())


def save(database, store, owner, token, clock, *, session=SESSION, review=None, separate=False, hook=None) -> str:
    """加入我的岗位 as the page does it: claim (from the last page shown; shown now when no page is named), write with
    the record bound before the write commits, release."""
    if review is None:
        review = shown(store, database, owner, token, clock, session=session).review

    claim = store.claim(token, owner, session, review)
    committed = found = False

    try:
        with database.transaction() as db:
            def bind(record_id):
                store.bind(token, claim.ticket, record_id)

                if hook is not None:
                    hook(record_id)

            if claim.content.kind == supplied.PASTED:
                row = records.keep_pasted(db, owner, claim.content, claim.shown, separate, clock.now(), bind=bind)
            else:
                row = records.keep_supplied_link(db, owner, claim.content, frozenset({CAREERS, LEADS}), clock.now(), bind=bind, forget=lambda record_id: None)

            record_id = row.id

        committed = True

        return record_id
    except records.AlreadyKept:
        # The posting is the user's already: the result ends on that record, as the page's save does (F2).
        found = True

        raise
    finally:
        store.release(token, claim.ticket, committed or found)


def shown(store, database, owner, token, clock, session=SESSION) -> supplied.Held:
    """The page before saving, as it is shown: a snapshot of it, with a pasted posting's matches now."""
    held = store.get(token, owner, session)

    if held.content.kind == supplied.LINK:
        return store.show(token, owner, session, frozenset())

    with database.transaction() as db:
        matches = records.supplied_matches(db, owner, held.content, clock.now())

    return store.show(token, owner, session, frozenset(match.record_id for match in matches))


def count(database, model) -> int:
    with database.transaction() as db:
        return db.scalar(select(func.count()).select_from(model))


def row_of(database, record_id) -> JobPosting:
    with database.transaction() as db:
        return db.get(JobPosting, record_id)


# --- a link: recognised by its shape alone ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "ref"),
    [
        (LINK, PostingRef(CAREERS, POSTING)),
        (f"{LINK}/apply", PostingRef(CAREERS, POSTING)),
        (f"{LINK}/", PostingRef(CAREERS, POSTING)),
        (f"  https://CAREERS.Synthetic.invalid/postings/{POSTING}?utm_source=x#top  ", PostingRef(CAREERS, POSTING)),
        ("https://leads.synthetic.invalid/items/l-sh-analyst", PostingRef(LEADS, "l-sh-analyst")),
        ("https://jobs.lever.co/rr-demo-alpha/a0000001-0000-4000-8000-000000000001", PostingRef("lever-rr-demo-alpha", "a0000001-0000-4000-8000-000000000001")),
        ("https://jobs.lever.co/palantir/0abc-1/apply", PostingRef("lever-palantir", "0abc-1")),
    ],
)
def test_a_link_names_a_source_and_a_posting_by_its_shape_alone(link, ref):
    assert supplied.match(link) == ref


@pytest.mark.parametrize(
    "link",
    [
        "",
        f"http://careers.synthetic.invalid/postings/{POSTING}",
        "javascript:alert(1)",
        "data:text/html,hello",
        f"file:///etc/{POSTING}",
        f"https://user:secret@careers.synthetic.invalid/postings/{POSTING}",
        f"https://careers.synthetic.invalid:443/postings/{POSTING}",
        "https://127.0.0.1/postings/c-bj-analyst",
        "https://[::1]/postings/c-bj-analyst",
        "https://localhost/postings/c-bj-analyst",
        "https://careérs.synthetic.invalid/postings/c-bj-analyst",
        "https://careers.synthetic.invalid/postings/c%2Dbj-analyst",
        "https://careers.synthetic.invalid/postings/../c-bj-analyst",
        "https://careers.synthetic.invalid/postings//c-bj-analyst",
        f"https://careers.synthetic.invalid/postings/{POSTING}/extra",
        "https://careers.synthetic.invalid/postings/" + "c" * 2050,
        f"https://careers.synthetic.invalid/postings/{POSTING} x",
        f"https://careers.synthetic.invalid/postings/​{POSTING}",
        "https://unknown.example/postings/c-bj-analyst",
        "https://jobs.lever.co/rr-demo-alpha/not_an_id!",
        "https://jobs.lever.co/rr-demo-alpha",
    ],
)
def test_a_link_that_names_no_registered_posting_is_refused_and_never_opened(link):
    with pytest.raises(supplied.LinkProblem) as refused:
        supplied.match(link)

    # The words say why, never the link itself.
    assert POSTING not in refused.value.message and "user" not in refused.value.message



@pytest.mark.parametrize("host", ["127.0.0.1", "[::1]", "localhost", "jobs.localhost"])
def test_an_address_or_localhost_is_never_recognised_even_were_a_shape_to_name_one(monkeypatch, host):
    # The shapes name no such host; were one to, a link to it is still refused (the layer below the shapes).
    monkeypatch.setitem(supplied.synthetic.ADDRESSES, CAREERS, f"https://{host}/postings")

    with pytest.raises(supplied.LinkProblem):
        supplied.match(f"https://{host}/postings/{POSTING}")

def test_only_a_source_the_app_may_read_now_is_read():
    board = Board()
    register = register_with(board)
    keep = frozenset({CAREERS, LEADS})

    assert supplied.refusal(PostingRef(CAREERS, POSTING), keep, register) is None
    # A lead is never read; a source not kept by the set, not registered or not callable is not read now.
    assert supplied.refusal(PostingRef(LEADS, "l-sh-analyst"), keep, register) == supplied.LEAD_LINK
    assert supplied.refusal(PostingRef(CAREERS, POSTING), frozenset({LEADS}), register) == supplied.NOT_READABLE
    assert supplied.refusal(PostingRef(PAGED, "p-bj-intern-1"), keep | {PAGED}, register) == supplied.NOT_READABLE
    assert supplied.refusal(PostingRef("lever-palantir", "0abc-1"), keep | {"lever-palantir"}, register) == supplied.NOT_READABLE
    assert board.calls == []


def test_the_replay_boards_are_read_and_another_site_is_not():
    from app.discovery import lever_demo

    register = lever_demo.register()
    keep = frozenset(grant.source_id for grant in lever_demo.GRANTS)

    assert supplied.refusal(PostingRef("lever-rr-demo-alpha", "x"), keep, register) is None
    assert supplied.refusal(PostingRef("lever-rr-demo-gamma", "x"), keep, register) == supplied.NOT_READABLE
    assert supplied.readable_sources(keep, register) == [entry.name for entry in lever_demo.ENTRIES]


# --- a link kept with a pasted text --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "kept", "removed", "flagged"),
    [
        (
            "https://name:pa55@jobs.example.com/careers/123?id=123&gh_jid=77&utm_source=mail",
            "https://jobs.example.com/careers/123?id=123&gh_jid=77&utm_source=mail",
            ("登录信息（@ 前面的部分）",),
            (),
        ),
        (
            "https://jobs.example.com/job;jsessionid=AB12/view?access_token=t0k&Password=p&X-Amz-Signature=s&apikey=k&code=JOB9&key=v",
            "https://jobs.example.com/job/view?code=JOB9&key=v",
            ("路径里的 jsessionid", "参数 access_token", "参数 Password", "参数 X-Amz-Signature", "参数 apikey"),
            ("code", "key"),
        ),
        ("https://jobs.example.com/#/jobs/123", "https://jobs.example.com/#/jobs/123", (), ()),
        ("https://jobs.example.com/#/jobs/123?token=t&x=1", "https://jobs.example.com/#/jobs/123?x=1", ("# 后的参数 token",), ()),
        ("https://jobs.example.com/#access_token=t&state=s", "https://jobs.example.com/#state=s", ("# 后的参数 access_token",), ()),
        ("http://jobs.example.com/a?id=1", "http://jobs.example.com/a?id=1", (), ()),
    ],
)
def test_a_kept_link_drops_credentials_marks_doubtful_names_and_keeps_the_rest(link, kept, removed, flagged):
    result = supplied.stored_link(link)

    assert (result.text, result.removed, result.flagged) == (kept, removed, flagged)
    # What was taken out is named, never shown: none of its values is in what a page says of it.
    assert not any(value in " ".join(result.removed) for value in ("pa55", "AB12", "t0k", "=p", "=s", "=k"))


@pytest.mark.parametrize("link", ["javascript:alert(1)", "data:text/html,x", "mailto:hr@example.com", "https://user@/x", "x" * 2100])
def test_what_is_not_a_web_address_is_never_kept(link):
    with pytest.raises(supplied.LinkProblem):
        supplied.stored_link(link)


def test_the_text_limit_is_u2s():
    assert supplied.MAX_TEXT_CHARS == MAX_POSTING_CHARS


# --- reading: one D4a check, a 404 alone closes nothing ------------------------------------------------------------------


def test_reading_a_link_is_one_d4a_check_through_its_sources_own_adapter(clock):
    board = Board()
    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(board), delisting=frozenset(), now=clock.now)

    assert content.reading.status == "open" and content.reading.calls == 2 and content.text
    # By source and id, never the address the user gave.
    assert board.calls == [("read", POSTING), ("check", POSTING)]


def test_a_404_alone_is_never_a_close(clock):
    board = Board()
    board.delisted.add(POSTING)
    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(board), delisting=frozenset(), now=clock.now)

    assert content.reading.facts is None and content.reading.status == "unknown"
    assert "404" in supplied.failure_text(content.reading) and "不说明岗位已关闭" in supplied.failure_text(content.reading)

    class CheckGone(Board):
        def check(self, ref, timeout):
            self.calls.append(("check", ref.posting_id))
            raise NotFound("synthetic 404")

    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(CheckGone()), delisting=frozenset(), now=clock.now)

    assert content.reading.facts is not None and (content.reading.status, content.reading.reason) == ("unknown", "not_found")


def test_a_read_that_fails_is_never_tried_again(clock):
    from app.discovery.sources import SourceTimeout

    board = Board()
    board.faults[("read", POSTING)] = SourceTimeout("synthetic")
    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(board), delisting=frozenset(), now=clock.now)

    assert content.reading.facts is None and board.calls == [("read", POSTING)]
    assert "没有重试" in supplied.failure_text(content.reading)


# --- the store: one session's, thirty minutes, one read at a time ----------------------------------------------------------


def test_a_result_is_its_sessions_alone_and_lapses_whatever_looks_at_it(store, ticks, clock, owners):
    owner, other = owners
    token = store.put(owner, SESSION, pasted(clock))

    assert store.get(token, other, SESSION) is None and store.get(token, owner, OTHER_SESSION) is None
    ticks.advance(29 * 60)
    assert store.get(token, owner, SESSION) is not None and store.prefill(token, owner, SESSION) == supplied.kept_text(TEXT)
    ticks.advance(60)
    assert store.get(token, owner, SESSION) is None and store.prefill(token, owner, SESSION) is None

    with pytest.raises(supplied.Gone):
        store.claim(token, owner, SESSION, None)


def test_a_read_that_comes_back_after_the_session_ended_is_never_held(store, clock, owners):
    owner, _ = owners
    ticket = store.reserve(owner, SESSION)
    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(Board()), delisting=frozenset(), now=clock.now)
    store.end_session(SESSION)

    assert store.publish(ticket, owner, SESSION, content) is None and len(store) == 0
    assert store.reserve(owner, SESSION) is None


def test_one_read_of_a_user_at_a_time_and_at_most_five_held(store, clock, owners):
    owner, other = owners
    ticket = store.reserve(owner, SESSION)

    assert store.reserve(owner, OTHER_SESSION) is None and store.reserve(other, OTHER_SESSION) is not None
    store.cancel(ticket)
    assert store.reserve(owner, SESSION) is not None

    tokens = [store.put(owner, SESSION, pasted(clock, title=f"岗位{number}")) for number in range(supplied.MAX_RESULTS)]

    with pytest.raises(supplied.Full):
        store.put(owner, SESSION, pasted(clock))

    assert store.discard(tokens[0], owner, SESSION) and store.get(tokens[0], owner, SESSION) is None
    assert store.put(owner, SESSION, pasted(clock)) is not None


# --- saving: claimed, bound before the commit, released whatever happens (17.2) ------------------------------------------


def test_a_result_saves_once_however_often_it_is_sent(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    claim = store.claim(token, owner, SESSION, review)

    # Sent again while the first save runs: refused, nothing written.
    with pytest.raises(supplied.Busy):
        store.claim(token, owner, SESSION, review)

    with pytest.raises(supplied.Busy):
        store.discard(token, owner, SESSION)

    store.release(token, claim.ticket, False)
    record_id = save(database, store, owner, token, clock, review=review)

    with pytest.raises(supplied.Saved) as again:
        store.claim(token, owner, SESSION, review)

    assert again.value.record_id == record_id and count(database, JobPosting) == 1
    assert store.get(token, owner, SESSION).saved == record_id


class Logged:
    """The database, saying when each write commits."""

    def __init__(self, database, log: list):
        self.database, self.log = database, log

    @contextmanager
    def transaction(self):
        with self.database.transaction() as db:
            yield db

        self.log.append("committed")


def test_the_record_is_bound_before_the_write_commits(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    log = []
    record_id = save(Logged(database, log), store, owner, token, clock, review=review, hook=lambda rid: log.append(("bound", store._entries[token].bound)))

    # Bound inside the write, before its commit: no moment when the record is there and the result does not know it.
    assert log == [("bound", record_id), "committed"] and store.get(token, owner, SESSION).saved == record_id


def test_a_session_ended_before_the_bind_writes_nothing(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    claim = store.claim(token, owner, SESSION, review)
    store.end_session(SESSION)

    with pytest.raises(supplied.Gone):
        with database.transaction() as db:
            records.keep_pasted(db, owner, claim.content, claim.shown, False, clock.now(), bind=lambda rid: store.bind(token, claim.ticket, rid))

    store.release(token, claim.ticket, False)

    assert count(database, JobPosting) == count(database, JobPostingText) == 0
    assert store.prefill(token, owner, SESSION) is None


def test_a_session_ended_after_the_bind_finishes_the_save_and_its_text_is_never_read_again(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    record_id = save(database, store, owner, token, clock, review=review, hook=lambda rid: store.end_session(SESSION))

    assert row_of(database, record_id) is not None
    assert store.get(token, owner, SESSION) is None and store.prefill(token, owner, SESSION) is None

    with pytest.raises(supplied.Gone):
        store.claim(token, owner, SESSION, review)


def test_a_write_that_rolls_back_after_the_bind_releases_the_claim_and_a_retry_saves_once(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review

    def fail(record_id):
        raise RuntimeError("the write fails after the bind")

    with pytest.raises(RuntimeError):
        save(database, store, owner, token, clock, review=review, hook=fail)

    held = store.get(token, owner, SESSION)

    assert count(database, JobPosting) == count(database, JobPostingText) == 0
    assert held.saved is None and not held.saving and store._entries[token].bound is None

    record_id = save(database, store, owner, token, clock, review=review)

    assert count(database, JobPosting) == 1 and store.get(token, owner, SESSION).saved == record_id


def test_deleting_the_record_drops_the_text_and_no_old_form_makes_another_even_after_a_restore(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    record_id = save(database, store, owner, token, clock, review=review)

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())
        # As the delete route does, before the deletion commits.
        store.forget_record(record_id)

    assert store.get(token, owner, SESSION) is None and store.prefill(token, owner, SESSION) is None

    with pytest.raises(supplied.Saved) as again:
        store.claim(token, owner, SESSION, review)

    assert again.value.record_id == record_id

    with database.transaction() as db:
        restore_job_record(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())

    assert store.prefill(token, owner, SESSION) is None and count(database, JobPosting) == 1


def test_a_result_being_saved_outlives_its_time_until_the_save_ends_then_goes(store, ticks, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review

    def late(record_id):
        ticks.advance(31 * 60)
        store.sweep()

    record_id = save(database, store, owner, token, clock, review=review, hook=late)
    store.sweep()

    assert row_of(database, record_id) is not None and store.get(token, owner, SESSION) is None

    with pytest.raises(supplied.Gone):
        store.claim(token, owner, SESSION, review)

    assert count(database, JobPosting) == 1


def test_after_a_discard_an_expiry_or_a_deletion_a_new_supply_saves_as_any_other(store, ticks, database, clock, owners):
    owner, _ = owners
    first = store.put(owner, SESSION, pasted(clock))
    record_id = save(database, store, owner, first, clock, review=shown(store, database, owner, first, clock).review)

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())
        store.forget_record(record_id)

    dropped = store.put(owner, SESSION, pasted(clock))
    assert store.discard(dropped, owner, SESSION)
    lapsed = store.put(owner, SESSION, pasted(clock))
    ticks.advance(31 * 60)
    assert store.get(lapsed, owner, SESSION) is None

    again = store.put(owner, SESSION, pasted(clock))
    held = shown(store, database, owner, again, clock)

    # The one in the bin is listed, exactly the same: saved anew only as another posting, and still restorable.
    assert held.shown == {record_id}
    second = save(database, store, owner, again, clock, review=held.review, separate=True)

    with database.transaction() as db:
        restore_job_record(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())

    assert second != record_id and count(database, JobPosting) == 2



# --- D4d F1: nothing is saved from a page the server never showed --------------------------------------------------------


def test_nothing_is_saved_from_a_page_never_shown_and_only_the_last_one_shown_is_taken(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))

    # No page was shown: whatever the form names, the user saw nothing of what would be kept.
    for review in ("", "0", "1", "forged"):
        with pytest.raises(supplied.Unreviewed):
            store.claim(token, owner, SESSION, review)

    first = shown(store, database, owner, token, clock).review
    last = shown(store, database, owner, token, clock).review

    for review in ("", "forged", first):
        with pytest.raises(supplied.Stale):
            store.claim(token, owner, SESSION, review)

    assert count(database, JobPosting) == 0 and save(database, store, owner, token, clock, review=last)


def test_a_links_read_is_saved_only_from_its_page_too(store, database, clock, owners):
    owner, _ = owners
    token = linked(store, owner, clock)

    with pytest.raises(supplied.Unreviewed):
        store.claim(token, owner, SESSION, "")

    review = store.show(token, owner, SESSION, frozenset()).review

    assert save(database, store, owner, token, clock, review=review) and count(database, JobPosting) == 1


# --- D4d F2: a result that found the user's record ends there; nothing new reads it past its time --------------------------


def save_link(database, store, owner, token, clock, *, forget=lambda record_id: None) -> tuple[str, str]:
    """加入我的岗位 for a link's read, as the page does it: what came of it, and the record."""
    claim = store.claim(token, owner, SESSION, store.show(token, owner, SESSION, frozenset()).review)
    committed = found = False

    try:
        with database.transaction() as db:
            row = records.keep_supplied_link(
                db, owner, claim.content, frozenset({CAREERS, LEADS}), clock.now(), bind=lambda rid: store.bind(token, claim.ticket, rid), forget=forget
            )
            record_id = row.id

        committed = True

        return "saved", record_id
    except records.AlreadyKept as kept:
        found = True

        return ("bin" if kept.in_bin else "kept"), kept.record_id
    finally:
        store.release(token, claim.ticket, committed or found)


def delete_for_good(database, store, owner, record_id, clock) -> None:
    """Deleted as the delete page does it (its result forgotten before the deletion commits), then purged."""
    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())
        store.forget_record(record_id)

    with database.transaction() as db:
        request_job_record_purge(db, owner, JobPosting, record_id, row_of(database, record_id).revision, clock.now())

    purge_due(database, clock.now() + timedelta(seconds=1))


def test_a_link_save_that_finds_the_users_posting_ends_its_result_on_that_record(store, database, clock, owners):
    owner, _ = owners
    # Two result pages of the same posting, opened before either is saved.
    older, newer = linked(store, owner, clock), linked(store, owner, clock)

    assert save_link(database, store, owner, newer, clock)[0] == "saved"
    outcome, record_id = save_link(database, store, owner, older, clock)

    # The older read found the record the newer one made: bound to it, and over.
    assert outcome == "kept" and store.get(older, owner, SESSION).saved == record_id

    with pytest.raises(supplied.Saved) as again:
        store.claim(older, owner, SESSION, "")

    assert again.value.record_id == record_id

    delete_for_good(database, store, owner, record_id, clock)

    with pytest.raises(supplied.Saved):
        store.claim(older, owner, SESSION, "")

    assert count(database, JobPosting) == 0
    # A new read is a new result: it saves as any other.
    assert save_link(database, store, owner, linked(store, owner, clock), clock)[0] == "saved" and count(database, JobPosting) == 1


def test_a_link_save_that_finds_the_posting_in_the_bin_ends_there_and_a_restore_brings_back_no_second(store, database, clock, owners):
    owner, _ = owners
    older = linked(store, owner, clock)
    _, record_id = save_link(database, store, owner, linked(store, owner, clock), clock)

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, record_id, 1, clock.now())
        store.forget_record(record_id)

    assert save_link(database, store, owner, older, clock) == ("bin", record_id)

    with database.transaction() as db:
        restore_job_record(db, owner, JobPosting, record_id, 2, clock.now())

    with pytest.raises(supplied.Saved) as again:
        store.claim(older, owner, SESSION, "")

    assert again.value.record_id == record_id and count(database, JobPosting) == 1


def test_a_found_record_is_bound_inside_the_write_that_found_it(store, database, clock, owners):
    owner, _ = owners
    older = linked(store, owner, clock)
    _, record_id = save_link(database, store, owner, linked(store, owner, clock), clock)
    claim = store.claim(older, owner, SESSION, store.show(older, owner, SESSION, frozenset()).review)
    log = []

    def bind(rid):
        store.bind(older, claim.ticket, rid)
        log.append(("bound", rid))

    with pytest.raises(records.AlreadyKept):
        with Logged(database, log).transaction() as db:
            records.keep_supplied_link(db, owner, claim.content, frozenset({CAREERS, LEADS}), clock.now(), bind=bind, forget=lambda rid: None)

    # Bound before the write (holding the lock a deletion waits on) ended: no moment for the record to go unseen.
    assert log == [("bound", record_id)]
    store.release(older, claim.ticket, True)
    assert store.get(older, owner, SESSION).saved == record_id


def test_a_claim_past_its_time_lets_its_save_finish_but_nothing_new_reads_the_text(store, ticks, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    review = shown(store, database, owner, token, clock).review
    claim = store.claim(token, owner, SESSION, review)
    ticks.advance(30 * 60)

    # The save's own snapshot is the save's alone: no new reader, page or prefill gets the text.
    assert store.get(token, owner, SESSION) is None and store.prefill(token, owner, SESSION) is None
    assert store.show(token, owner, SESSION, frozenset()) is None and not store.discard(token, owner, SESSION)

    with database.transaction() as db:
        row = records.keep_pasted(db, owner, claim.content, claim.shown, False, clock.now(), bind=lambda rid: store.bind(token, claim.ticket, rid))
        record_id = row.id

    store.release(token, claim.ticket, True)

    assert row_of(database, record_id) is not None and store.prefill(token, owner, SESSION) is None

# --- whether a pasted posting is another one: the user's, on the last page shown (17.3) ---------------------------------


@pytest.mark.parametrize(
    ("changes", "same", "different"),
    [
        ({"company": "另一家合成公司"}, ("职位名", "链接", "原文"), ("公司",)),
        ({"title": "另一个合成岗位"}, ("公司", "链接", "原文"), ("职位名",)),
        ({"link": "https://jobs.example.com/careers/2"}, ("公司", "职位名", "原文"), ("链接",)),
        ({"text": TEXT + "\n多一句合成要求"}, ("公司", "职位名", "链接"), ("原文",)),
    ],
)
def test_one_thing_alike_is_listed_never_merged_and_saved_only_as_another(store, database, clock, owners, changes, same, different):
    owner, _ = owners
    first = save(database, store, owner, store.put(owner, SESSION, pasted(clock, link="https://jobs.example.com/careers/1")), clock)
    before = row_of(database, first)
    fields = {"link": "https://jobs.example.com/careers/1", **changes}
    token = store.put(owner, SESSION, pasted(clock, **fields))
    held = shown(store, database, owner, token, clock)

    with database.transaction() as db:
        [match] = records.supplied_matches(db, owner, held.content, clock.now())

    assert held.shown == {first} and not match.exact and (match.same, match.different) == (same, different)

    # Not said to be another posting: nothing written.
    with pytest.raises(records.NeedsReview):
        save(database, store, owner, token, clock, review=held.review, separate=False)

    second = save(database, store, owner, token, clock, review=held.review, separate=True)
    after = row_of(database, first)

    assert second != first and count(database, JobPosting) == 2
    assert (after.facts, after.revision, after.decision) == (before.facts, before.revision, before.decision)


def test_an_exact_copy_is_saved_only_when_the_user_says_it_is_another(store, database, clock, owners):
    owner, _ = owners
    first = save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)
    token = store.put(owner, SESSION, pasted(clock, text=TEXT.replace("\n", "\r\n") + "  "))
    held = shown(store, database, owner, token, clock)

    with database.transaction() as db:
        [match] = records.supplied_matches(db, owner, held.content, clock.now())

    assert match.exact and match.same == records.IDENTITY_PARTS

    with pytest.raises(records.NeedsReview):
        save(database, store, owner, token, clock, review=held.review)

    assert save(database, store, owner, token, clock, review=held.review, separate=True) != first


def test_a_page_that_is_not_the_last_shown_saves_nothing(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    old = shown(store, database, owner, token, clock).review
    new = shown(store, database, owner, token, clock).review

    with pytest.raises(supplied.Stale):
        store.claim(token, owner, SESSION, old)

    assert new != old and count(database, JobPosting) == 0


def test_a_match_the_page_did_not_show_stops_the_write_and_is_shown_next(store, database, clock, owners):
    owner, _ = owners
    token = store.put(owner, SESSION, pasted(clock))
    held = shown(store, database, owner, token, clock)
    assert held.shown == frozenset()

    # Meanwhile, another tab saves the same posting.
    other = store.put(owner, SESSION, pasted(clock))
    elsewhere = save(database, store, owner, other, clock, review=shown(store, database, owner, other, clock).review)

    with pytest.raises(records.NeedsReview):
        save(database, store, owner, token, clock, review=held.review)

    assert count(database, JobPosting) == 1 and not store.get(token, owner, SESSION).saving
    assert shown(store, database, owner, token, clock).shown == {elsewhere}



def test_a_new_match_stops_a_save_as_another_too(store, database, clock, owners):
    owner, _ = owners
    first = save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)
    token = store.put(owner, SESSION, pasted(clock))
    held = shown(store, database, owner, token, clock)
    assert held.shown == {first}

    # Meanwhile, another tab saves the same text as another posting: the page said nothing of it.
    other = store.put(owner, SESSION, pasted(clock))
    save(database, store, owner, other, clock, review=shown(store, database, owner, other, clock).review, separate=True)

    with pytest.raises(records.NeedsReview):
        save(database, store, owner, token, clock, review=held.review, separate=True)

    assert count(database, JobPosting) == 2

def test_the_matches_are_the_users_own(store, database, clock, owners):
    owner, other = owners
    save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)
    token = store.put(other, OTHER_SESSION, pasted(clock))
    held = shown(store, database, other, token, clock, session=OTHER_SESSION)

    assert held.shown == frozenset()
    assert save(database, store, other, token, clock, session=OTHER_SESSION, review=held.review)

    with pytest.raises(supplied.Gone):
        store.claim(token, owner, SESSION, held.review)


def test_a_posting_deleted_for_good_is_no_match(store, database, clock, owners):
    owner, _ = owners
    first = save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)

    with database.transaction() as db:
        delete_job_record(db, owner, JobPosting, first, row_of(database, first).revision, clock.now())

    with database.transaction() as db:
        request_job_record_purge(db, owner, JobPosting, first, row_of(database, first).revision, clock.now())

    assert shown(store, database, owner, store.put(owner, SESSION, pasted(clock)), clock).shown == frozenset()


# --- a link's posting: the source's; never written to one the user has ---------------------------------------------------


def linked(store, owner, clock, board=None) -> str:
    content = supplied.read(PostingRef(CAREERS, POSTING), register_with(board or Board()), delisting=frozenset(), now=clock.now)
    ticket = store.reserve(owner, SESSION)

    return store.publish(ticket, owner, SESSION, content)


def test_a_posting_read_from_a_link_keeps_its_facts_and_its_read_as_its_first_check_and_never_its_text(store, database, clock, owners):
    owner, _ = owners
    record_id = save(database, store, owner, linked(store, owner, clock), clock)

    with database.transaction() as db:
        row = db.get(JobPosting, record_id)
        [check] = db.scalars(select(JobPostingRecheck))
        run = db.get(JobUpdateRun, check.run_id)

    assert (row.source_id, row.posting_id, row.source_access, row.search_id) == (CAREERS, POSTING, "synthetic", records.LINK_SEARCH)
    assert row.facts["via"]["kind"] == "link" and row.facts["evidence"] == "official" and "text" not in row.facts
    assert (run.trigger, run.calls, run.outcome) == ("link", 2, "done") and check.status == "open" and check.content_sha256
    assert count(database, JobPostingText) == 0


@pytest.mark.parametrize("state", ["mine", "held", "bin"])
def test_a_link_read_is_never_written_to_a_posting_the_user_has(store, database, clock, owners, state):
    owner, _ = owners
    record_id = kept_posting(database, owner, clock.now(), deleted_at=clock.now() if state == "bin" else None)

    if state == "held":
        with database.transaction() as db:
            db.add(JobRecommendation(posting_record_id=record_id, owner_id=owner, search_id="search-1", recommended_at=clock.now(), until=clock.now() + timedelta(days=7)))

    before = row_of(database, record_id)
    token = linked(store, owner, clock)

    with pytest.raises(records.AlreadyKept) as kept:
        save(database, store, owner, token, clock)

    after = row_of(database, record_id)

    assert (after.revision, after.facts) == (before.revision, before.facts) and count(database, JobPostingRecheck) == 0
    # Nothing written, and the result ends on the record the user has, in use or in the bin (F2).
    assert kept.value.in_bin == (state == "bin") and store.get(token, owner, SESSION).saved == record_id


def test_saving_a_link_read_takes_the_same_posting_out_of_the_new_ones_of_a_watch(store, database, clock, owners):
    owner, _ = owners

    with database.transaction() as db:
        summary = JobSearch(id=new_id(), owner_id=owner, search_id="search-w", summary={"version": 3}, revision=1, kept_at=clock.now(), updated_at=clock.now(), deleted_at=None, purge_after=None, deletion_id=None)
        db.add(summary)
        db.flush()
        watch = JobWatch(
            id=new_id(), owner_id=owner, search_record_id=summary.id, notice_version="watch-1", baselined=[], baseline_at=clock.now(), run_token=None,
            last_trigger=None, last_started_at=None, last_finished_at=None, last_outcome=None, last_reason=None, last_calls=0, last_counts=None,
            attempted_at=None, completed_at=None, created_at=clock.now(), updated_at=clock.now(),
        )
        db.add(watch)
        db.flush()
        db.add(JobWatchPosting(id=new_id(), owner_id=owner, watch_id=watch.id, source_id=CAREERS, posting_id=POSTING, facts={}, screening={}, partial_read=None, position=1, found_at=clock.now(), until=clock.now() + timedelta(days=14)))

    save(database, store, owner, linked(store, owner, clock), clock)

    assert count(database, JobWatchPosting) == 0


# --- a pasted posting is only decided on ---------------------------------------------------------------------------------


def test_a_pasted_posting_is_only_decided_on(store, database, clock, owners):
    owner, _ = owners
    record_id = save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)

    with database.transaction() as db:
        records.decide_on_kept(db, owner, record_id, 1, "skip", [], "", clock.now())

    with database.transaction() as db:
        with pytest.raises(records.KeepRefused):
            records.feedback_on_kept(db, owner, record_id, 2, "title", "wrong", "", clock.now())

        with pytest.raises(updates.CheckRefused) as refused:
            updates.begin(db, owner, record_id, 2, updates.Trigger.RECHECK, clock.now())

        assert refused.value.reason == updates.PASTED
        assert updates.tracked(db, owner, frozenset({CAREERS, LEADS, records.SUPPLIED_SOURCE}), clock.now()) == []
        # Skipped, it leaves every search as it was: it has no source a search could list.
        assert records.skipped_refs(db, owner, [CAREERS, LEADS, records.SUPPLIED_SOURCE]) == ()

    assert row_of(database, record_id).decision == "skip" and count(database, JobUpdateRun) == 0


# --- its text: with its record, to the bin, back and away ------------------------------------------------------------------


def test_the_text_goes_with_its_record_to_the_bin_back_and_away_for_good(store, database, clock, owners):
    owner, _ = owners
    record_id = save(database, store, owner, store.put(owner, SESSION, pasted(clock, text="合成原文 ```代码``` 和 ````四个````")), clock)

    with database.transaction() as db:
        assert job_texts.text_in_use(db, owner, record_id).startswith("合成原文")
        delete_job_record(db, owner, JobPosting, record_id, 1, clock.now())

    with database.transaction() as db:
        exported = collect_job_records(db, owner, clock.now())
        assert job_texts.text_in_use(db, owner, record_id) is None

    [entry] = exported.postings
    assert entry["kept_as"] == "recycle_bin" and entry["user_text"]["chars"] == len("合成原文 ```代码``` 和 ````四个````")
    assert entry["kept_from_search"] is None and entry["supplied"]["kind"] == "pasted"
    # Its text is never read as Markdown: its fence is longer than any run of backticks in it.
    assert "`````\n合成原文" in jobs_markdown(exported, clock.now())

    with database.transaction() as db:
        restore_job_record(db, owner, JobPosting, record_id, 2, clock.now())

    with database.transaction() as db:
        assert job_texts.text_in_use(db, owner, record_id) is not None
        delete_job_record(db, owner, JobPosting, record_id, 3, clock.now())

    with database.transaction() as db:
        request_job_record_purge(db, owner, JobPosting, record_id, 4, clock.now())

    purge_due(database, clock.now() + timedelta(seconds=1))

    assert count(database, JobPosting) == count(database, JobPostingText) == 0


def test_a_link_reads_text_is_kept_nowhere(store, database, clock, owners):
    owner, _ = owners
    record_id = save(database, store, owner, linked(store, owner, clock), clock)

    with database.transaction() as db:
        exported = collect_job_records(db, owner, clock.now())

    [entry] = exported.postings
    assert entry["user_text"] is None and entry["supplied"]["kind"] == "link" and count(database, JobPostingText) == 0
    assert entry["kept_from_search"] is None and record_id == entry["id"]


def test_a_pasted_posting_never_takes_a_sources_place(store, database, clock, owners):
    owner, _ = owners
    record_id = save(database, store, owner, store.put(owner, SESSION, pasted(clock)), clock)
    row = row_of(database, record_id)

    assert (row.source_id, row.source_access, row.search_id) == (records.SUPPLIED_SOURCE, job_texts.USER_PROVIDED, records.PASTED_SEARCH)
    # Its id is its own, never the text's: the same text may be another posting.
    assert row.posting_id != supplied.text_digest(supplied.kept_text(TEXT))
    assert row.facts["evidence"] == "user_provided" and row.facts["via"]["text_sha256"] == supplied.text_digest(supplied.kept_text(TEXT))
