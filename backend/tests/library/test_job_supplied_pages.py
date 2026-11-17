"""U3-D4d: 补充岗位 through the pages.

A link is read once, by its source's own adapter, only when it names a source the app may read now; otherwise the page
says why and carries the link to the paste form. A pasted posting is shown exactly as it would be kept, its link made
safe; whether it is another posting than one of the user's is theirs to say on the last page shown, the server keeping
what that page showed. Nothing is kept until 加入我的岗位, once; deleting the record, logging out or the result's lapsing
leaves nothing to read or keep again. 去分析 only fills in the 岗位分析 page. Synthetic sources only, no network, no
model.
"""
import html
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest
from sqlalchemy import select

from app.discovery import records, supplied, updates
from app.discovery.web import SYNTHETIC_SOURCES
from app.library.lifecycle import purge_due
from app.library.models import JobPosting, JobPostingRecheck, JobPostingText, JobUpdateRun
from job_helpers import text_of
from library_helpers import form_for
from test_job_record_pages import exported, rows
from test_job_supplied import LINK, TEXT, Ticks
from update_helpers import POSTING, Board, register_with

SUPPLY = "/job-records/supply"
FORM_LINK = "https://name:pa55@jobs.example.com/careers/123?id=123&access_token=t0k&code=JOB9"
SHOWN_LINK = "https://jobs.example.com/careers/123?id=123&code=JOB9"


@pytest.fixture
def board(app):
    board = Board()
    app.state.discovery.sources = replace(SYNTHETIC_SOURCES, make_register=lambda task: register_with(board))

    return board


@pytest.fixture
def ticks(app) -> Ticks:
    ticks = Ticks()
    app.state.discovery.supply.clock = ticks

    return ticks


def post(browser, path, **fields):
    return browser.post(path, {"csrf_token": browser.csrf_token(), **fields})


def read_link(browser, link=LINK):
    return post(browser, f"{SUPPLY}/link", link=link)


def paste(browser, *, title="数据分析实习生", company="合成数据科技", text=TEXT, link=""):
    return post(browser, f"{SUPPLY}/text", title=title, company=company, text=text, link=link)


def token_of(response) -> str:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{SUPPLY}/")

    return location.rsplit("/", 1)[1]


def keep_form(browser, token):
    return browser.form(f"{SUPPLY}/{token}", f"{SUPPLY}/{token}/keep")


def keep(browser, token, form=None, **changes):
    form = form or keep_form(browser, token)

    return browser.post(form.action, {**form.fields, **changes})


def record_of(response) -> str:
    assert response.status_code == 303, response.text

    return re.search(r"/job-records/postings/([^?]+)", response.headers["location"]).group(1)


def page_text(browser, path) -> str:
    return html.unescape(text_of(browser.get(path).text))


def held(app) -> int:
    return len(app.state.discovery.supply)


# --- a link ---------------------------------------------------------------------------------------------------------------


def test_a_link_is_read_once_shown_and_kept_only_when_asked(app, browser_a, board):
    token = token_of(read_link(browser_a))
    shown = page_text(browser_a, f"{SUPPLY}/{token}")

    assert "由来源自己的接口读取了一次，你贴的网址本身没有打开" in shown and "核对时仍公开发布" in shown
    assert board.calls == [("read", POSTING), ("check", POSTING)] and rows(app, JobPosting) == []

    record_id = record_of(keep(browser_a, token))
    page = page_text(browser_a, f"/job-records/postings/{record_id}")
    [check] = rows(app, JobPostingRecheck)
    [run] = rows(app, JobUpdateRun)

    assert "这个岗位是你用补充链接加入的" in page and "补充链接时读取" in page and "再核对一次" in page
    assert (run.trigger, check.status) == ("link", "open") and rows(app, JobPostingText) == []
    # Nothing was analysed and no search ran: the 岗位分析 and the search stores hold nothing.
    assert app.state.job_analysis.store._tasks == {} and len(board.calls) == 2


def test_a_link_to_a_posting_the_user_has_is_never_read_again(app, browser_a, board):
    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))
    calls = list(board.calls)
    response = read_link(browser_a, f"{LINK}/apply")

    assert response.headers["location"] == f"/job-records/postings/{record_id}?notice=supplied_mine"
    assert board.calls == calls and len(rows(app, JobPosting)) == 1


@pytest.mark.parametrize(
    ("link", "said", "calls"),
    [
        (f"http://careers.synthetic.invalid/postings/{POSTING}", "只认 https", 0),
        ("https://leads.synthetic.invalid/items/l-sh-analyst", "待核验线索", 0),
        ("https://jobs.lever.co/palantir/0abc-1", "这个来源现在不能读取", 0),
        ("https://unknown.example/jobs/1", "认不出这个链接", 0),
    ],
)
def test_a_link_that_cannot_be_read_says_why_reads_nothing_and_goes_to_the_paste_form(app, browser_a, board, link, said, calls):
    response = read_link(browser_a, link)
    paste_form = form_for(response.text, f"{SUPPLY}/text")

    assert response.status_code == 422 and said in text_of(response.text)
    assert paste_form.fields["link"] == link and len(board.calls) == calls and held(app) == 0


def test_a_404_says_it_closes_nothing_and_nothing_is_held(app, browser_a, board):
    board.delisted.add(POSTING)
    response = read_link(browser_a)

    assert response.status_code == 422 and "单独一次 404 不说明岗位已关闭" in text_of(response.text)
    assert board.calls == [("read", POSTING)] and held(app) == 0


def test_a_read_that_comes_back_after_logging_out_is_never_held(app, browser_a, board):
    store = app.state.discovery.supply

    def log_out(operation):
        if operation == "check":
            # As logging out does (session_end_listeners), while the source is answering.
            app.state.discovery.end_session(next(iter(store._reading.values())).session)

    board.during = log_out
    response = read_link(browser_a)

    assert response.status_code == 409 and "这次读取的结果没有保留" in text_of(response.text)
    assert held(app) == 0 and rows(app, JobPosting) == []


# --- a pasted posting -------------------------------------------------------------------------------------------------------


def test_a_pasted_posting_is_shown_exactly_as_it_would_be_kept(app, browser_a):
    token = token_of(paste(browser_a, link=FORM_LINK))
    source = browser_a.get(f"{SUPPLY}/{token}").text
    shown = html.unescape(text_of(source))

    assert SHOWN_LINK in shown and "登录信息（@ 前面的部分）" in shown and "参数 access_token" in shown
    assert "参数 code 可能是登录凭证" in shown and "pa55" not in source and "t0k" not in source
    assert not re.search(r'<a [^>]*href="https?://jobs\.example\.com', source) and rows(app, JobPosting) == []

    record_id = record_of(keep(browser_a, token))
    [text] = rows(app, JobPostingText)
    [record] = rows(app, JobPosting)
    page_html = browser_a.get(f"/job-records/postings/{record_id}").text
    page = text_of(page_html)

    assert record.facts["via"]["link"] == SHOWN_LINK and text.text == supplied.kept_text(TEXT)
    assert "这是你提供的原文（用户提供，系统未独立核验）" in page and updates.PASTED in page
    assert "再核对一次</button>" not in page_html and "/feedback" not in page_html
    assert f'href="/job-analysis?record={record_id}"' in page_html and not re.search(r'<a [^>]*href="https?://jobs\.example\.com', page_html)


def test_a_pasted_posting_is_checked_by_nothing(app, browser_a):
    record_id = record_of(keep(browser_a, token_of(paste(browser_a))))

    for action in ("recheck", "apply-check"):
        response = post(browser_a, f"/job-records/postings/{record_id}/{action}", revision="1")

        assert response.status_code == 403 and updates.PASTED in text_of(response.text)

    assert rows(app, JobUpdateRun) == [] and rows(app, JobPostingRecheck) == []


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"title": ""}, "请填写职位名"),
        ({"company": "合" * 101}, "公司最多 100 字"),
        ({"text": "合" * 8001}, "岗位原文最多 8000 字"),
        ({"link": "javascript:alert(1)"}, "原始链接"),
    ],
)
def test_what_cannot_be_held_is_said_and_nothing_is_held(app, browser_a, fields, said):
    response = paste(browser_a, **fields)

    assert response.status_code == 422 and said in text_of(response.text) and held(app) == 0


def test_a_paste_like_one_of_the_users_is_saved_only_as_another_and_the_old_one_is_untouched(app, browser_a):
    first = record_of(keep(browser_a, token_of(paste(browser_a))))
    before = rows(app, JobPosting)[0]
    token = token_of(paste(browser_a, company="另一家合成公司"))
    form = keep_form(browser_a, token)
    shown = page_text(browser_a, f"{SUPPLY}/{token}")

    assert "职位名、链接、原文相同，公司不同" in shown and form.fields["choice"] == "separate"

    # A form saying it is the same posting, or naming an older page, saves nothing.
    wrong = keep(browser_a, token, choice="add")
    stale = keep(browser_a, token, form)

    assert wrong.status_code == 409 and "你按的按钮和这一页列出的相同或部分相同的岗位对不上" in text_of(wrong.text)
    assert stale.status_code == 409 and "这一页在你打开以后更新过" in text_of(stale.text) and len(rows(app, JobPosting)) == 1

    second = record_of(keep(browser_a, token))
    after = [row for row in rows(app, JobPosting) if row.id == first][0]

    assert second != first and (after.facts, after.revision) == (before.facts, before.revision)



def test_what_is_kept_comes_from_memory_never_from_the_form(app, browser_a):
    token = token_of(paste(browser_a, link=FORM_LINK))
    keep(browser_a, token, title="伪造的职位", company="伪造的公司", text="伪造的原文", link="https://evil.example/x")
    [record] = rows(app, JobPosting)
    [text] = rows(app, JobPostingText)

    assert (record.facts["title"], record.facts["company"], record.facts["via"]["link"]) == ("数据分析实习生", "合成数据科技", SHOWN_LINK)
    assert text.text == supplied.kept_text(TEXT)

def test_a_match_saved_after_the_page_was_shown_stops_the_save_and_is_shown(app, browser_a):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)
    assert form.fields["choice"] == "add"

    # Meanwhile, another tab saves the same posting.
    elsewhere = record_of(keep(browser_a, token_of(paste(browser_a))))
    response = keep(browser_a, token, form)

    assert response.status_code == 409 and "这一页没列出的相同或部分相同的岗位" in text_of(response.text)
    assert f"/job-records/postings/{elsewhere}" in response.text and len(rows(app, JobPosting)) == 1


def test_a_supply_is_kept_once_however_often_it_is_sent(app, browser_a):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)
    record_id = record_of(keep(browser_a, token, form))
    again = keep(browser_a, token, form)

    assert again.headers["location"] == f"/job-records/postings/{record_id}?notice=supplied_again"
    assert len(rows(app, JobPosting)) == 1


def test_after_its_record_is_deleted_an_old_form_keeps_nothing_and_its_text_is_gone(app, browser_a):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)
    record_id = record_of(keep(browser_a, token, form))

    assert post(browser_a, f"/job-records/postings/{record_id}/delete", expected_revision="1").status_code == 303

    response = keep(browser_a, token, form)
    filled = browser_a.get(f"/job-analysis?supplied={token}").text

    assert response.status_code == 409 and "同一份结果不会再保存一次" in text_of(response.text)
    assert len(rows(app, JobPosting)) == 1 and "已经过期或不在了" in text_of(filled)
    assert "整理合成数据" not in filled


def test_a_lapsed_or_discarded_supply_keeps_nothing(app, browser_a, ticks):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)
    ticks.advance(30 * 60)

    assert keep(browser_a, token, form).status_code == 410 and browser_a.get(f"{SUPPLY}/{token}").status_code == 410

    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)

    assert post(browser_a, f"{SUPPLY}/{token}/discard").headers["location"] == f"{SUPPLY}?notice=discarded"
    assert keep(browser_a, token, form).status_code == 410 and rows(app, JobPosting) == []


def test_a_supply_is_its_owners_session_alone(app, browser_a, browser_b):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)

    assert browser_b.get(f"{SUPPLY}/{token}").status_code == 410
    assert browser_b.post(form.action, {**form.fields, "csrf_token": browser_b.csrf_token()}).status_code == 410
    assert "整理合成数据" not in browser_b.get(f"/job-analysis?supplied={token}").text and rows(app, JobPosting) == []


def test_every_supply_entry_needs_the_sessions_token(app, browser_a):
    token = token_of(paste(browser_a))

    for path, fields in (
        (f"{SUPPLY}/link", {"link": LINK}),
        (f"{SUPPLY}/text", {"title": "合成", "company": "合成", "text": TEXT}),
        (f"{SUPPLY}/{token}/keep", {"review": "x", "choice": "add"}),
        (f"{SUPPLY}/{token}/discard", {}),
    ):
        assert browser_a.post(path, fields).status_code == 403

    assert held(app) == 1 and rows(app, JobPosting) == []


# --- 去分析: only filled in --------------------------------------------------------------------------------------------------


def test_the_analysis_page_is_only_filled_in(app, browser_a, browser_b):
    token = token_of(paste(browser_a))
    filled = browser_a.get(f"/job-analysis?supplied={token}").text

    assert "整理合成数据" in filled and "已填入你补充的岗位原文" in text_of(filled)
    # Filling in starts nothing: no analysis task, no request.
    assert app.state.job_analysis.store._tasks == {}

    record_id = record_of(keep(browser_a, token))

    assert "整理合成数据" in browser_a.get(f"/job-analysis?record={record_id}").text
    assert "整理合成数据" not in browser_b.get(f"/job-analysis?record={record_id}").text
    assert app.state.job_analysis.store._tasks == {}


# --- the records page, and the export ---------------------------------------------------------------------------------------


def test_the_records_page_offers_supply_and_says_a_pasted_posting_is_the_users(app, browser_a):
    record_of(keep(browser_a, token_of(paste(browser_a))))
    page = page_text(browser_a, "/job-records")

    assert "补充岗位…" in page and "用户提供，系统未独立核验" in page and "用户提供，系统不核对招聘状态" in page


def test_the_export_says_how_each_came_and_has_the_pasted_text_only(app, browser_a, board):
    record_of(keep(browser_a, token_of(read_link(browser_a))))
    record_of(keep(browser_a, token_of(paste(browser_a, title="合成岗位二", text="合成原文 ``` 三个反引号", link=FORM_LINK))))
    data, jobs = exported(browser_a)
    entries = {entry["supplied"]["kind"]: entry for entry in data["job_records"]["postings"]}

    assert data["format_version"] == 14 and set(entries) == {"link", "pasted"}
    assert entries["link"]["user_text"] is None and entries["link"]["kept_from_search"] is None
    assert entries["pasted"]["user_text"]["text"] == "合成原文 ``` 三个反引号" and entries["pasted"]["supplied"]["link"] == SHOWN_LINK
    assert "````\n合成原文 ``` 三个反引号\n````" in jobs and "来自你补充的链接" in jobs and "pa55" not in jobs


def test_nothing_of_a_supply_reaches_another_user(app, browser_a, browser_b):
    record_id = record_of(keep(browser_a, token_of(paste(browser_a))))
    token = token_of(paste(browser_b))

    assert "/job-records/postings/" + record_id not in browser_b.get(f"{SUPPLY}/{token}").text
    assert browser_b.get(f"/job-records/postings/{record_id}").status_code == 404
    assert record_of(keep(browser_b, token)) != record_id

    with app.state.library.database.transaction() as db:
        owners = {row.owner_id for row in db.scalars(select(JobPosting))}

    assert len(owners) == 2


def test_the_link_save_still_refuses_a_posting_the_user_got_meanwhile(app, browser_a, board):
    token = token_of(read_link(browser_a))
    form = keep_form(browser_a, token)
    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))
    calls = len(board.calls)
    response = keep(browser_a, token, form)

    assert response.headers["location"] == f"/job-records/postings/{record_id}?notice=supplied_already"
    assert len(rows(app, JobPosting)) == 1 and len(rows(app, JobPostingRecheck)) == 1 and len(board.calls) == calls
    assert records.SUPPLIED_SOURCE == "user-provided"


# --- D4d F1–F3: Codex's review probes, made part of the suite ----------------------------------------------------------------


def test_a_save_needs_the_page_before_saving_to_have_been_shown(app, browser_a, board):
    token = token_of(paste(browser_a, link="https://jobs.example/job?code=possible-credential"))
    unseen = post(browser_a, f"{SUPPLY}/{token}/keep", page="0", choice="add")

    assert unseen.status_code == 409 and "还没有给你看过要保存的内容" in text_of(unseen.text) and rows(app, JobPosting) == []

    # That refusal showed the page: a save must name its snapshot from now on; an empty, made-up or forged one is refused.
    for review in ("", "0", "forged"):
        response = post(browser_a, f"{SUPPLY}/{token}/keep", review=review, choice="add")

        assert response.status_code == 409 and "这一页在你打开以后更新过" in text_of(response.text)

    assert rows(app, JobPosting) == [] and record_of(keep(browser_a, token))

    # A link's read too: never saved from a page not shown, and saved as before from its page.
    linked = token_of(read_link(browser_a))

    assert post(browser_a, f"{SUPPLY}/{linked}/keep", review="", choice="add").status_code == 409
    assert record_of(keep(browser_a, linked)) and len(rows(app, JobPosting)) == 2


def test_a_link_save_that_found_the_users_posting_never_brings_it_back_once_gone(app, browser_a, board, clock):
    # Two result pages of the same posting, opened before either is saved.
    older = token_of(read_link(browser_a))
    old_form = keep_form(browser_a, older)
    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))

    assert keep(browser_a, older, old_form).headers["location"] == f"/job-records/postings/{record_id}?notice=supplied_already"
    assert post(browser_a, f"/job-records/postings/{record_id}/delete", expected_revision="1").status_code == 303
    assert post(browser_a, f"/job-records/postings/{record_id}/purge", expected_revision="2").status_code == 303
    purge_due(app.state.library.database, clock.now())
    assert rows(app, JobPosting) == []

    replay = keep(browser_a, older, old_form)

    assert replay.status_code == 409 and "同一份结果不会再保存一次" in text_of(replay.text) and rows(app, JobPosting) == []
    # A new read is a new result, and saves as any other.
    assert record_of(keep(browser_a, token_of(read_link(browser_a)))) and len(rows(app, JobPosting)) == 1


def test_a_link_save_that_found_the_posting_in_the_bin_ends_there_and_a_restore_makes_no_second(app, browser_a, board):
    older = token_of(read_link(browser_a))
    old_form = keep_form(browser_a, older)
    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))

    assert post(browser_a, f"/job-records/postings/{record_id}/delete", expected_revision="1").status_code == 303

    binned = keep(browser_a, older, old_form)

    assert binned.status_code == 409 and "在你的回收站里" in text_of(binned.text)
    assert post(browser_a, f"/job-records/postings/{record_id}/restore", expected_revision="2").status_code == 303
    assert keep(browser_a, older, old_form).headers["location"] == f"/job-records/postings/{record_id}?notice=supplied_again"
    assert len(rows(app, JobPosting)) == 1


def test_a_save_claimed_in_time_finishes_past_its_time_but_new_readers_get_nothing(app, browser_a, ticks, monkeypatch):
    token = token_of(paste(browser_a))
    form = keep_form(browser_a, token)
    store = app.state.discovery.supply
    entered, proceed = Event(), Event()
    original = records.keep_pasted

    def held_at_the_write(*args, **kwargs):
        # The save has claimed what it saves, and has not yet bound or committed.
        entered.set()
        assert proceed.wait(5), "the test's own synchronisation failed"

        return original(*args, **kwargs)

    monkeypatch.setattr(records, "keep_pasted", held_at_the_write)
    entry = store._entries[token]
    owner, session = entry.user_id, entry.session

    with ThreadPoolExecutor(max_workers=1) as executor:
        saving = executor.submit(keep, browser_a, token, form)

        try:
            assert entered.wait(5), "the save never reached the write"
            ticks.advance(30 * 60)
            fetched = (store.get(token, owner, session), store.prefill(token, owner, session), store.show(token, owner, session, frozenset()))
        finally:
            proceed.set()

        saved = saving.result(timeout=5)

    assert saved.status_code == 303 and fetched == (None, None, None)
    assert len(rows(app, JobPosting)) == 1 and "整理合成数据" not in browser_a.get(f"/job-analysis?supplied={token}").text


@pytest.mark.parametrize(
    ("decision", "wrong", "right"),
    [
        ("skip", "下次查找不再列出这个岗位", "跳过它不影响以后的查找"),
        ("apply", "申请前可以用上面的“申请前核对”", "申请前请自己到原网站确认要求"),
    ],
)
def test_a_pasted_postings_decision_says_only_what_can_be_done(app, browser_a, decision, wrong, right):
    record_id = record_of(keep(browser_a, token_of(paste(browser_a))))

    assert post(browser_a, f"/job-records/postings/{record_id}/decide", revision="1", decision=decision).status_code == 303

    page = page_text(browser_a, f"/job-records/postings/{record_id}")

    # Neither the hint under the decision nor the shared decision forms promise a search or a check.
    assert wrong not in page and right in page and "申请前核对在岗位记录页上单独做" not in page


def test_a_source_postings_decisions_still_say_what_they_do(app, browser_a, board):
    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))

    assert post(browser_a, f"/job-records/postings/{record_id}/decide", revision="1", decision="skip").status_code == 303

    page = page_text(browser_a, f"/job-records/postings/{record_id}")

    assert "下次查找不再列出这个岗位" in page and "申请前核对在岗位记录页上单独做" in page and "跳过它不影响以后的查找" not in page


def test_a_lever_replay_link_is_read_kept_and_filled_in_without_any_transport(app, browser_a):
    from app.discovery.web import LEVER_REPLAY

    app.state.discovery.sources = LEVER_REPLAY
    token = token_of(read_link(browser_a, "https://jobs.lever.co/rr-demo-alpha/a0000001-0000-4000-8000-000000000001"))
    record_id = record_of(keep(browser_a, token))

    assert [row.id for row in rows(app, JobPosting)] == [record_id]
    assert browser_a.get(f"/job-analysis?supplied={token}").status_code == 200
