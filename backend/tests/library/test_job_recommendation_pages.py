"""U3-D4b-2: current recommendations held for tracking (临时留存跟踪), through the pages.

The option is offered unticked, for one search at a time, and only where a search can have full results on sources
that may be kept and tracked; the review says what is held, for how long and that it is not 我的岗位. A ticked search
that ends as it should says what it held; the records page shows 我的岗位, 当前推荐 and 已关闭的推荐 apart. Adding,
deciding or saying a fact is not right adds one to 我的岗位 and says so; 不再跟踪 removes it at once, never through the
bin. From the moment its time comes, old links and old forms change nothing, a check sends nothing and a text a
申请前核对 read is shown no more. Another user gets nothing. The searches and checks run on synthetic postings with the
app's simulated decisions and analyses; nothing leaves the process and no model is called.
"""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.discovery import presentation, records_web
from app.discovery.contracts import RECOMMENDED
from app.discovery.records_web import LAPSED_TITLE, NOTICES
from app.library.lifecycle import HELD_REASON
from app.library.models import JobPosting, JobRecommendation, JobUpdateRun
from job_helpers import InlineExecutor, text_of  # noqa: F401
from test_discovery_pages import BASE, confirm, review, search_of, stored
from test_job_apply_check import board, current, post  # noqa: F401 (fixtures)
from test_job_record_pages import inline, library  # noqa: F401 (fixtures)
from test_job_recommendations import held


def ticked_search(browser, library, **changes) -> str:
    """A search on the synthetic postings with 临时留存并跟踪这次的推荐 ticked, run to its end; its page's address."""
    url = search_of(review(browser, sources=[library], use_preferences=True, track_recommendations="yes", **changes))
    assert confirm(browser, url).status_code == 303

    return url


def markers(app, owner) -> list[JobRecommendation]:
    with app.state.library.database.transaction() as db:
        return list(db.scalars(select(JobRecommendation).where(JobRecommendation.owner_id == owner)))


def owner_of(app, browser, library) -> str:
    """The user's id, from a search kept for review only: nothing is written."""
    return stored(app, browser, search_of(review(browser, sources=[library]))).owner_id


def free_posting(app, owner) -> str:
    """A synthetic posting the owner has no record of in use."""
    with app.state.library.database.transaction() as db:
        taken = set(db.scalars(select(JobPosting.posting_id).where(JobPosting.owner_id == owner, JobPosting.deleted_at.is_(None))))

    return next(posting for posting in ("c-choice", "c-two-cities", "c-sh-analyst", "c-bj-analyst-repost", "c-bj-senior", "c-bj-analyst") if posting not in taken)


def runs(app) -> int:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(func.count()).select_from(JobUpdateRun))


def test_the_option_is_unticked_and_held_only_where_offered(app, browser_a, library, inline):
    page = browser_a.get(BASE).text
    assert 'name="track_recommendations" value="yes">' in page and "默认不勾" in page

    # Ticked with no experience chosen: no full result can come, so it is not offered, and the review says so.
    bare = search_of(review(browser_a, track_recommendations="yes"))
    assert stored(app, browser_a, bare).confirmation.track_recommendations is False
    assert presentation.NOT_OFFERED in text_of(browser_a.get(bare).text)

    unticked = search_of(review(browser_a, sources=[library], use_preferences=True))
    ticked = search_of(review(browser_a, sources=[library], use_preferences=True, track_recommendations="yes"))
    reviewed = text_of(browser_a.get(ticked).text)

    assert stored(app, browser_a, ticked).confirmation.track_recommendations is True
    assert stored(app, browser_a, ticked).fingerprint != stored(app, browser_a, unticked).fingerprint
    assert "你勾选了“临时留存并跟踪这次的推荐”" in reviewed and "到期自动删除" in reviewed and "不是“我的岗位”" in reviewed
    assert "临时留存跟踪" not in text_of(browser_a.get(unticked).text)
    assert stored(app, browser_a, ticked).confirmation.notice_version == "discovery-preview-7"


def test_a_ticked_search_holds_its_recommendations_and_shows_them_apart(app, browser_a, library, inline):
    url = ticked_search(browser_a, library)
    task = stored(app, browser_a, url)
    recommended = [item for item in task.finished.outcome.results if item.category in RECOMMENDED]
    held_now = markers(app, task.owner_id)

    assert recommended and len(held_now) == len(recommended) <= 3 and task.retention.kept == len(recommended)
    found = text_of(browser_a.get(url).text)
    # R10 (U3-D6-3 F3): what the search's end held, said as such, then how many are held now (all of them, here).
    assert f"查找结束时，这次的推荐临时留存跟踪了 {len(recommended)} 条（当时都还没加入我的岗位）" in found
    assert f"现在这 {len(recommended)} 条仍在临时留存跟踪中" in found
    assert "临时留存跟踪中（到" in found and "还没加入我的岗位" in found
    assert "已加入我的岗位" not in found

    listing = text_of(browser_a.get("/job-records").text)
    assert "当前推荐（临时留存跟踪）" in listing and "还没有加入我的岗位的岗位" in listing
    assert f"（其中当前推荐 {len(recommended)} 个）" in listing

    record = text_of(browser_a.get(f"/job-records/postings/{held_now[0].posting_record_id}").text)
    assert "当前推荐（临时留存跟踪）" in record and "自动删除" in record and "还没加入我的岗位" in record
    assert "加入我的岗位" in record and "不再跟踪（马上删除）" in record and "删除这条记录…" not in record
    # The data is there: never said not to be kept.
    assert "没有保存" not in record


def test_adding_deciding_or_saying_not_right_adds_it_and_says_so(app, browser_a, library, inline):
    owner = owner_of(app, browser_a, library)
    database, now = app.state.library.database, app.state.library.now()
    added, decided, said = (held(database, owner, now, posting) for posting in ("c-sh-analyst", "c-two-cities", "c-choice"))

    response = post(browser_a, f"/job-records/postings/{added}/add", expected_revision="1")
    assert response.status_code == 303 and response.headers["location"].endswith("?notice=kept_added")
    response = post(browser_a, f"/job-records/postings/{decided}/decide", decision="favorite", expected_revision="1")
    assert response.headers["location"].endswith("?notice=decided_added")
    response = post(browser_a, f"/job-records/postings/{said}/feedback", field="place", feedback_reason="wrong", expected_revision="1")
    assert response.headers["location"].endswith("?notice=feedback_added")
    assert NOTICES["decided_added"] in text_of(browser_a.get(response.headers["location"].replace(said, decided).replace("feedback_added", "decided_added")).text)

    for record_id in (added, decided, said):
        assert current(app, record_id) is not None

        with database.transaction() as db:
            assert db.get(JobRecommendation, record_id) is None

    # Adding again: it is 我的岗位 already; nothing changes.
    assert post(browser_a, f"/job-records/postings/{added}/add", expected_revision=str(current(app, added).revision)).status_code == 422


def test_untracking_removes_it_at_once_never_through_the_bin(app, browser_a, library, inline):
    url = ticked_search(browser_a, library)
    owner = stored(app, browser_a, url).owner_id
    record_id = markers(app, owner)[0].posting_record_id
    mine = held(app.state.library.database, owner, app.state.library.now(), free_posting(app, owner))
    assert post(browser_a, f"/job-records/postings/{mine}/add", expected_revision="1").status_code == 303

    # The bin is not for it: the delete page and the delete itself refuse.
    for response in (browser_a.get(f"/job-records/postings/{record_id}/delete"), post(browser_a, f"/job-records/postings/{record_id}/delete", expected_revision="2")):
        assert response.status_code == 409 and HELD_REASON in text_of(response.text)

    response = post(browser_a, f"/job-records/postings/{record_id}/untrack", expected_revision=str(current(app, record_id).revision))
    assert response.status_code == 303 and response.headers["location"].endswith("?notice=untracked")
    assert current(app, record_id) is None and "回收站" in NOTICES["untracked"]
    # An old form for it changes nothing; 我的岗位 is never untracked.
    assert post(browser_a, f"/job-records/postings/{record_id}/untrack", expected_revision="2").status_code == 404
    assert post(browser_a, f"/job-records/postings/{mine}/untrack", expected_revision=str(current(app, mine).revision)).status_code == 409


def test_from_its_time_old_links_and_forms_change_nothing_and_a_text_goes(app, browser_a, library, inline, board, clock):
    owner = owner_of(app, browser_a, library)
    record_id = held(app.state.library.database, owner, clock.now(), "c-sh-analyst", until=clock.now() + timedelta(hours=1))
    response = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision="1")
    assert response.status_code == 303
    text_page = response.headers["location"]
    texts = app.state.discovery.apply_checks
    assert browser_a.get(text_page).status_code == 200 and len(texts) == 1
    revision, before_runs, calls = current(app, record_id).revision, runs(app), len(board.calls)

    clock.advance(hours=2)

    # The text a 申请前核对 read is shown no more, and goes at once.
    gone = browser_a.get(text_page)
    assert gone.status_code == 410 and LAPSED_TITLE in text_of(gone.text) and len(texts) == 0
    assert browser_a.get(f"/job-records/postings/{record_id}").status_code == 410

    for path, fields in (
        ("decide", {"decision": "favorite"}),
        ("feedback", {"field": "place", "feedback_reason": "wrong"}),
        ("add", {}),
        ("recheck", {}),
        ("apply-check", {}),
    ):
        assert post(browser_a, f"/job-records/postings/{record_id}/{path}", expected_revision=str(revision), **fields).status_code == 410

    # Nothing written, nothing sent, and not added to 我的岗位.
    assert current(app, record_id).revision == revision and current(app, record_id).decision is None
    assert (runs(app), len(board.calls)) == (before_runs, calls)
    assert "c-sh-analyst" not in browser_a.get("/job-records").text

    response = post(browser_a, f"/job-records/postings/{record_id}/untrack", expected_revision=str(revision))
    assert response.headers["location"].endswith("?notice=untrack_lapsed") and current(app, record_id) is None


def test_untracking_drops_a_text_its_apply_check_read(app, browser_a, library, inline, board):
    owner = owner_of(app, browser_a, library)
    record_id = held(app.state.library.database, owner, app.state.library.now(), "c-sh-analyst")
    text_page = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision="1").headers["location"]
    texts = app.state.discovery.apply_checks
    assert len(texts) == 1

    assert post(browser_a, f"/job-records/postings/{record_id}/untrack", expected_revision=str(current(app, record_id).revision)).status_code == 303
    assert len(texts) == 0 and browser_a.get(text_page).status_code == 404


# Codex's two cases (U3-D4b-2 F1): the record's time comes after the route has looked at it in the database and before
# the text is published or read. A hook on the route's own code moves the app's clock at that point: no sleeping.


@pytest.mark.parametrize("hook", ["check", "put"])
def test_a_text_is_not_published_once_its_time_comes_after_the_check_is_written(app, browser_a, library, inline, board, clock, monkeypatch, hook):
    owner = owner_of(app, browser_a, library)
    until = clock.now() + timedelta(seconds=1)
    record_id = held(app.state.library.database, owner, clock.now(), "c-sh-analyst", until=until)
    texts, written = app.state.discovery.apply_checks, []

    if hook == "check":
        # Codex's case: its time comes once the check is written, before the route goes on.
        check = records_web._check

        def written_then_its_time_comes(*args, **kwargs):
            finished = check(*args, **kwargs)
            written.append((finished.written, finished.until))
            clock.current = until

            return finished

        monkeypatch.setattr(records_web, "_check", written_then_its_time_comes)
    else:
        # Later still: as the text is handed to the store, after anything the route itself could look at.
        put = texts.put

        def its_time_comes_then_publish(user_id, session, record_id, finished):
            written.append((finished.written, finished.until))
            clock.current = until

            return put(user_id, session, record_id, finished)

        monkeypatch.setattr(texts, "put", its_time_comes_then_publish)

    response = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision="1")

    # Written while it was current, with its time; never published, and said to have lapsed.
    assert written == [(True, until)]
    assert response.status_code == 410 and LAPSED_TITLE in text_of(response.text)
    assert len(texts) == 0


def test_a_text_asked_for_as_its_time_comes_is_dropped_never_shown(app, browser_a, library, inline, board, clock, monkeypatch):
    owner = owner_of(app, browser_a, library)
    until = clock.now() + timedelta(minutes=10)
    record_id = held(app.state.library.database, owner, clock.now(), "c-sh-analyst", until=until)
    location = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision="1").headers["location"]
    texts = app.state.discovery.apply_checks

    # Said to go by its time, not the thirty minutes; shown up to one moment before it.
    assert "大约 10 分钟后消失" in text_of(browser_a.get(location).text)
    clock.current = until - timedelta(microseconds=1)
    assert browser_a.get(location).status_code == 200

    get = texts.get

    def its_time_comes_then_read(*args, **kwargs):
        clock.current = until

        return get(*args, **kwargs)

    monkeypatch.setattr(texts, "get", its_time_comes_then_read)
    response = browser_a.get(location)

    assert response.status_code == 410 and LAPSED_TITLE in text_of(response.text) and len(texts) == 0


def test_added_to_mine_a_new_apply_check_shows_its_text_past_the_old_time(app, browser_a, library, inline, board, clock):
    owner = owner_of(app, browser_a, library)
    until = clock.now() + timedelta(minutes=10)
    record_id = held(app.state.library.database, owner, clock.now(), "c-sh-analyst", until=until)
    before = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision="1").headers["location"]

    assert post(browser_a, f"/job-records/postings/{record_id}/add", expected_revision=str(current(app, record_id).revision)).status_code == 303
    after = post(browser_a, f"/job-records/postings/{record_id}/apply-check", expected_revision=str(current(app, record_id).revision))
    assert after.status_code == 303
    # The text read before is shown still, until the time it had.
    assert browser_a.get(before).status_code == 200

    clock.current = until
    gone = text_of(browser_a.get(before).text)

    # 我的岗位 now: the text read before goes by the time it had, said as such and never as 已到期; nothing else of the
    # record goes, and the text the new check read is shown.
    assert LAPSED_TITLE not in gone and "核对时定的到期时间" in gone and browser_a.get(before).status_code == 404
    assert browser_a.get(after.headers["location"]).status_code == 200
    assert current(app, record_id) is not None and len(app.state.discovery.apply_checks) == 1


def test_another_user_gets_nothing_of_them(app, browser_a, browser_b, library, inline):
    url = ticked_search(browser_a, library)
    record_id = markers(app, stored(app, browser_a, url).owner_id)[0].posting_record_id

    assert browser_b.get(f"/job-records/postings/{record_id}").status_code == 404
    assert 'id="held-title"' not in browser_b.get("/job-records").text

    for path in ("untrack", "add"):
        assert post(browser_b, f"/job-records/postings/{record_id}/{path}", expected_revision="2").status_code == 404

    assert current(app, record_id) is not None
