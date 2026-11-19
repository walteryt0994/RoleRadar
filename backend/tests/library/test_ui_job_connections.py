"""UI bilingual batch: the job connections of plan 5.3 and 5.4, the history page, and the result language of job
analyses and searches (6.3); the multi-reason quota display of 4.3.

A standalone analysis is kept on its own, once per task, linked to a posting only by a source the server verified
when the analysis was reviewed; it saves and bookmarks no posting; the bin and a purge keep an old page from keeping it
again. A posting page offers one Refresh job, the full single check under its own name. Synthetic data and the local
simulation only; nothing leaves the process.
"""
import json
import re
from types import SimpleNamespace

import openai
import pytest
from sqlalchemy import func, select

from app.analysis_contracts import task_fingerprint
from app.discovery import agent, records
from app.discovery.batch_live import Batch, OperationKind
from app.job_analysis.tasks import TaskStore
from app.library.models import JobAnalysis, JobPosting
from job_helpers import InlineExecutor, RecordingProvider, confirmed, offered, start, task_of, text_of, use_provider
from library_helpers import forms_in
from test_job_supplied_pages import keep, keep_form, paste, record_of, token_of

ANALYSIS = "/job-analysis"
POSTING_TEXT = """数据分析实习生（合成）
岗位要求：
- 熟练使用 SQL 和 Python
- 能整理每周销售报表
工作地点：上海
"""


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


@pytest.fixture
def inline(app):
    app.state.job_analysis.executor = InlineExecutor()

    return app


@pytest.fixture
def project(browser_a):
    return confirmed(browser_a, title="销售报表看板（合成）", actions="用 SQL 汇总每周销售数据，并用 Python 画报表。")


def speak(browser, language):
    assert browser.post("/language", {"lang": language, "next": "/workspace"}).status_code == 303


def analysis_review(browser, items, *, posting=POSTING_TEXT, **extra):
    values = offered(browser)
    data = {"csrf_token": browser.csrf_token(), "posting": posting, "source": [values[item] for item in items], **extra}

    return browser.post(f"{ANALYSIS}/review", data)


def analysed(browser, items, **extra) -> str:
    task = task_of(analysis_review(browser, items, **extra))
    response = start(browser, task)
    assert response.status_code == 303, response.text

    return task


def task_in(app, task_url):
    return app.state.job_analysis.store._tasks[task_url.rsplit("/", 1)[1]]


def save(browser, task_url):
    page = browser.get(task_url).text
    form = next(form for form in forms_in(page) if form.action == f"{task_url}/save")

    return browser.post(form.action, form.fields)


def kept_analyses(app):
    with app.state.library.database.transaction() as db:
        return db.scalars(select(JobAnalysis)).all()


def postings(app) -> int:
    with app.state.library.database.transaction() as db:
        return db.scalar(select(func.count()).select_from(JobPosting))


# --- the result language of an analysis ------------------------------------------------------------------------


def test_an_analysis_draft_asks_for_the_interface_language(browser_a, project):
    page = browser_a.get(ANALYSIS).text
    # A select (owner redesign, image 01) with the interface language chosen; any other can be picked.
    assert re.search(r'name="result_language"', page) and re.search(r'<option value="zh-CN" lang="zh-CN" selected>', page)
    speak(browser_a, "en")
    assert re.search(r'<option value="en" lang="en" selected>', browser_a.get(ANALYSIS).text)


def test_the_result_language_is_part_of_an_analysis_confirmation_and_its_requests_stay_the_same(app, inline, browser_a, project):
    recorder = use_provider(app, RecordingProvider())
    chinese = task_of(analysis_review(browser_a, [project], result_language="zh-CN"))
    english = task_of(analysis_review(browser_a, [project], result_language="en"))
    zh_task, en_task = task_in(app, chinese), task_in(app, english)

    assert (zh_task.request.result_language, en_task.request.result_language) == ("zh-CN", "en")
    assert zh_task.fingerprint != en_task.fingerprint
    assert "English" in text_of(browser_a.get(english).text)

    start(browser_a, chinese)
    sent_zh = list(recorder.prompts)
    start(browser_a, english)
    sent_en = recorder.prompts[len(sent_zh):]

    # A job analysis writes no prose of its own: the same requests go out in either language.
    assert sent_zh and sent_zh == sent_en


def test_a_chinese_analysis_fingerprint_is_as_it_was_before():
    from app.analysis_contracts import AnalysisRequest, Recipient

    request = AnalysisRequest(owner_id="u", selection=(), job_posting="x", recipient=Recipient("simulation", "m", "local"))
    english = AnalysisRequest(
        owner_id="u", selection=(), job_posting="x", recipient=Recipient("simulation", "m", "local"), result_language="en"
    )

    assert request.result_language == "zh-CN"
    assert task_fingerprint(request) != task_fingerprint(english)


# --- keeping a standalone analysis (5.3) ---------------------------------------------------------------------


def test_a_standalone_analysis_is_kept_once_on_its_own_and_saves_no_posting(app, inline, browser_a, project):
    task = analysed(browser_a, [project], result_language="en")
    assert "保存分析" in text_of(browser_a.get(task).text)

    response = save(browser_a, task)
    assert response.status_code == 303 and response.headers["location"].endswith("?notice=analysis_saved")
    again = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    assert again.status_code == 303

    rows = kept_analyses(app)
    assert len(rows) == 1 and postings(app) == 0
    row = rows[0]
    assert row.search_id == records.STANDALONE_PREFIX + task.rsplit("/", 1)[1]
    assert (row.source_id, row.posting_id) == (records.PASTED_ANALYSIS_SOURCE, records.PASTED_ANALYSIS_POSTING)
    assert row.generation["result_language"] == "en" and row.content["kind"] == "standalone"

    page = text_of(browser_a.get(f"/job-records/analyses/{row.id}").text)
    assert "单独分析" in page and "English" in page
    history = text_of(browser_a.get("/job-records/history").text)
    assert "单独分析" in history


def test_a_standalone_analysis_in_the_bin_or_gone_is_never_kept_again_from_its_page(app, inline, browser_a, project, clock):
    task = analysed(browser_a, [project])
    assert save(browser_a, task).status_code == 303
    row = kept_analyses(app)[0]

    delete = next(form for form in forms_in(browser_a.get(f"/job-records/analyses/{row.id}/delete").text) if form.action.endswith("/delete"))
    assert browser_a.post(delete.action, delete.fields).status_code == 303
    assert "回收站" in text_of(browser_a.get(task).text)
    refused = browser_a.post(f"{task}/save", {"csrf_token": browser_a.csrf_token()})
    assert refused.status_code == 409
    assert len(kept_analyses(app)) == 1


def test_an_analysis_of_a_kept_pasted_posting_is_linked_to_it(app, inline, browser_a, project):
    record = record_of(keep(browser_a, token_of(paste(browser_a, text=POSTING_TEXT)), choice="add"))
    page = browser_a.get(f"{ANALYSIS}?record={record}").text
    origin = re.search(r'name="origin" value="([^"]+)"', page).group(1)
    assert origin == f"record:{record}"

    task = analysed(browser_a, [project], origin=origin)
    assert task_in(app, task).origin.record_id == record
    assert save(browser_a, task).status_code == 303

    with app.state.library.database.transaction() as db:
        posting = db.get(JobPosting, record)
        identity = (posting.source_id, posting.posting_id)

    kept = kept_analyses(app)[0]
    assert (kept.source_id, kept.posting_id) == identity
    assert kept.generation["standalone"] == task.rsplit("/", 1)[1]
    # It shows on the posting's own page, by that identity.
    assert f"/job-records/analyses/{kept.id}" in browser_a.get(f"/job-records/postings/{record}").text
    assert postings(app) == 1


def test_the_text_a_browser_sends_back_with_its_own_line_endings_is_the_same_text(app, inline, browser_a, project):
    # A browser sends a textarea's line breaks as CR LF: the text filled in from the posting comes back so.
    record = record_of(keep(browser_a, token_of(paste(browser_a, text=POSTING_TEXT)), choice="add"))
    task = analysed(browser_a, [project], posting=POSTING_TEXT.replace("\n", "\r\n"), origin=f"record:{record}")

    assert task_in(app, task).origin.record_id == record


def test_an_edited_or_someone_elses_origin_links_nothing(app, inline, browser_a, browser_b, project):
    record = record_of(keep(browser_a, token_of(paste(browser_a, text=POSTING_TEXT)), choice="add"))

    edited = analysed(browser_a, [project], posting=POSTING_TEXT + "\n另加一行", origin=f"record:{record}")
    assert task_in(app, edited).origin == records.PASTED_ORIGIN

    other = confirmed(browser_b, title="另一个项目（合成）", actions="整理数据。")
    forged = task_of(analysis_review(browser_b, [other], origin=f"record:{record}"))
    assert task_in(app, forged).origin == records.PASTED_ORIGIN


# --- one Refresh job (5.4) and the history page ---------------------------------------------------------------


def test_a_pasted_posting_page_offers_no_check_and_points_to_adding_a_link(app, browser_a):
    record = record_of(keep(browser_a, token_of(paste(browser_a, text=POSTING_TEXT)), choice="add"))
    page = browser_a.get(f"/job-records/postings/{record}").text

    assert "/recheck" not in page and "/apply-check" not in page
    assert 'href="/job-records/supply"' in page


def test_the_history_page_keeps_analyses_and_searches_apart(browser_a):
    analyses = browser_a.get("/job-records/history").text
    searches = browser_a.get("/job-records/history?tab=searches").text

    # The two tabs are 岗位分析 and 查找记录 (owner redesign, plan 5.4).
    assert 'aria-current="page">岗位分析' in analyses and 'aria-current="page">查找记录' in searches
    assert browser_a.get("/job-records/history?tab=anything").status_code == 200


# --- the result language of a search ------------------------------------------------------------------------


def test_an_english_search_asks_its_decisions_for_english_notes_and_a_chinese_one_is_unchanged():
    observation = SimpleNamespace(text="OBSERVATION", result_language="zh-CN")
    chinese = agent.decision_prompt(observation)
    assert chinese == f"{agent.DECISION_INSTRUCTIONS}\n\nOBSERVATION"

    observation.result_language = "en"
    english = agent.decision_prompt(observation)
    assert english.replace(agent.OUTPUT_LANGUAGE_LINES["en"] + "\n\n", "", 1) == chinese


def test_the_simulated_decider_writes_english_notes_for_an_english_search():
    assert agent.SIMULATED_NOTES_EN["检索次数已经用完，按规则结束"].startswith("No searches")
    match = agent.SIMULATED_RETRY.match("上次结果都是资深或超出条件的岗位，把“实习”写进检索词再查一次")
    # UI R4: the note is shorter so a step shows it whole; the type stays as the scope writes it.
    assert match and agent.SIMULATED_RETRY_EN.format(type=match["type"]).endswith('retry with "实习"')


def test_a_searchs_analyses_inherit_its_language_and_agree_only_with_it():
    from app.discovery import tools
    from app.discovery.contracts import DimensionScope, Limits, ParentConfirmation, SearchScope, search_fingerprint
    from app.analysis_contracts import Recipient

    confirmation = ParentConfirmation(
        owner_id="u",
        scope=SearchScope(city=DimensionScope(), job_type=DimensionScope(), direction=DimensionScope(), targets=("数据分析",)),
        selection=(),
        preferences=(),
        recipient=Recipient("simulation", "m", "local"),
        sources=(),
        notice_version="n",
        limits=Limits(),
        result_language="en",
    )
    request = tools.child_request(confirmation, "posting")

    assert request.result_language == "en" and tools.derive_approval(confirmation, request) is not None
    chinese = ParentConfirmation(**{**confirmation.__dict__, "result_language": "zh-CN"})
    assert tools.derive_approval(chinese, request) is None
    assert search_fingerprint(chinese) != search_fingerprint(confirmation)


# --- every shortfall at once (4.3) -----------------------------------------------------------------------------


def test_every_shortfall_of_a_batch_is_said_together_and_nothing_is_taken():
    batch = Batch.__new__(Batch)
    batch.source = SimpleNamespace(max_requests=5)
    batch.sent = 4
    batch.robots_read = True
    batch.model_status = lambda: SimpleNamespace(requests_left=1, micro_usd_left=10)
    batch.model_need = lambda kind: (3, 1_000_000)

    short, requests, robots = batch._shortfalls(OperationKind.SEARCH)
    codes = [code for code, _ in short]

    assert codes == ["requests", "model", "model"] and "预算" in short[2][1]
    assert batch.sent == 4


def test_a_model_status_that_cannot_be_read_is_said_as_such():
    batch = Batch.__new__(Batch)
    batch.source = SimpleNamespace(max_requests=50)
    batch.sent = 0
    batch.robots_read = True

    def unreadable():
        raise OSError("ledger")

    batch.model_status = unreadable
    batch.model_need = lambda kind: (3, 1_000_000)
    short, _, _ = batch._shortfalls(OperationKind.SEARCH)

    assert [code for code, _ in short] == ["model"] and "读不出" in short[0][1]


def test_an_english_analysis_names_the_preferences_in_english(app, inline, browser_a):
    """The review lists the preferences, and the result compares with them, by their names in the page's language;
    the prompt keeps its own words for them."""
    assert browser_a.submit(
        "/job-preferences/edit", "/job-preferences", cities_ideal="Shanghai", must_haves="Start in July 2027",
    ).status_code == 303
    item = confirmed(browser_a, title="Weekly sales dashboard", body="I cleaned the order data with Python and summarised weekly sales with SQL.")
    speak(browser_a, "en")
    english = "Data analyst intern\nRequirements:\n- Strong SQL and Python skills\nLocation: Shanghai\n"
    task = task_of(analysis_review(browser_a, [item], posting=english, result_language="en", use_preferences="yes"))
    review_page = text_of(browser_a.get(task).text)
    assert start(browser_a, task).status_code == 303
    result_page = text_of(browser_a.get(task).text)

    for page in (review_page, result_page):
        assert re.findall(r".{0,20}[一-鿿].{0,20}", page.replace("简体中文", "").replace("合成用户 A", "")) == []

    assert "Target cities: Ideal" in review_page and "Must-have conditions" in review_page
    assert "Target cities (Ideal)" in result_page


# --- a supplied posting analysed before or after it is kept (Codex review F2) -----------------------------------------


def delete_record(browser, path):
    form = browser.form(f"{path}/delete", f"{path}/delete")
    assert browser.post(form.action, form.fields).status_code == 303


def test_a_supplied_posting_analysed_first_and_kept_after_shares_one_identity(app, inline, browser_a, project):
    token = token_of(paste(browser_a, text=POSTING_TEXT))
    task = analysed(browser_a, [project], origin=f"supplied:{token}")
    origin = task_in(app, task).origin

    # Its own id since it was supplied, under the user's own source: never an official source, never its names.
    assert origin.source_id == records.SUPPLIED_SOURCE and origin.access == "user_provided" and origin.posting_id
    page = browser_a.get(task).text
    assert f'href="/job-records/supply/{token}"' in page and "加入我的岗位…" in page
    # The same save sent twice (a double click, a resend) keeps one analysis.
    form = next(form for form in forms_in(page) if form.action == f"{task}/save")
    assert browser_a.post(form.action, form.fields).status_code == 303
    assert browser_a.post(form.action, form.fields).status_code == 303
    [analysis] = kept_analyses(app)
    # Saved: the page says so, and no longer that nothing is saved.
    saved_page = text_of(browser_a.get(task).text)
    assert "分析已经另外保存为记录" in saved_page and "不会保存，也不会投递" not in saved_page

    adding = keep_form(browser_a, token)
    record = record_of(keep(browser_a, token, form=adding))
    with app.state.library.database.transaction() as db:
        posting = db.get(JobPosting, record)
    assert (posting.source_id, posting.posting_id) == (analysis.source_id, analysis.posting_id)
    assert f"/job-records/analyses/{analysis.id}" in browser_a.get(f"/job-records/postings/{record}").text
    assert f"/job-records/postings/{record}" in browser_a.get(f"/job-records/analyses/{analysis.id}").text
    # The result page now leads to the posting in 我的岗位; keeping the same result again makes no second posting.
    assert f'href="/job-records/postings/{record}"' in browser_a.get(task).text
    assert browser_a.post(adding.action, adding.fields).status_code < 500 and postings(app) == 1


def test_a_supplied_posting_kept_first_and_analysed_after_names_its_record(app, inline, browser_a, project):
    token = token_of(paste(browser_a, text=POSTING_TEXT))
    record = record_of(keep(browser_a, token))
    task = analysed(browser_a, [project], origin=f"supplied:{token}")

    assert task_in(app, task).origin.record_id == record
    assert save(browser_a, task).status_code == 303
    [analysis] = kept_analyses(app)
    assert f"/job-records/analyses/{analysis.id}" in browser_a.get(f"/job-records/postings/{record}").text


def test_another_users_or_a_made_up_supplied_token_links_nothing(app, inline, browser_a, browser_b, project):
    token = token_of(paste(browser_a, text=POSTING_TEXT))
    other = confirmed(browser_b, title="另一个项目（合成）", body="整理数据。")

    for browser, items, origin in (
        (browser_b, [other], f"supplied:{token}"),
        (browser_a, [project], "supplied:not-a-token"),
    ):
        task = task_of(analysis_review(browser, items, origin=origin))
        assert task_in(app, task).origin == records.PASTED_ORIGIN, origin

    # The supplied text edited before the review is the user's own pasted text: no identity either.
    edited = analysed(browser_a, [project], posting=POSTING_TEXT + "\n另加一行", origin=f"supplied:{token}")
    assert task_in(app, edited).origin == records.PASTED_ORIGIN
    assert 'href="/job-records/supply"' in browser_a.get(edited).text


def test_the_analysis_and_the_posting_are_deleted_each_on_its_own(app, inline, browser_a, project):
    token = token_of(paste(browser_a, text=POSTING_TEXT))
    task = analysed(browser_a, [project], origin=f"supplied:{token}")
    assert save(browser_a, task).status_code == 303
    [analysis] = kept_analyses(app)
    record = record_of(keep(browser_a, token))

    delete_record(browser_a, f"/job-records/analyses/{analysis.id}")
    assert postings(app) == 1 and f"/job-records/analyses/{analysis.id}" not in browser_a.get(f"/job-records/postings/{record}").text

    delete_record(browser_a, f"/job-records/postings/{record}")
    with app.state.library.database.transaction() as db:
        assert db.get(JobAnalysis, analysis.id).deleted_at is not None
        assert db.get(JobPosting, record).deleted_at is not None


def test_a_result_whose_supplied_text_lapsed_says_it_can_no_longer_be_added_from_there(app, inline, browser_a, project):
    token = token_of(paste(browser_a, text=POSTING_TEXT))
    task = analysed(browser_a, [project], origin=f"supplied:{token}")
    discard = browser_a.form(f"/job-records/supply/{token}", f"/job-records/supply/{token}/discard")
    assert browser_a.post(discard.action, discard.fields).status_code == 303

    page = text_of(browser_a.get(task).text)
    assert "不能再从这里加入我的岗位" in page and f"/job-records/supply/{token}" not in browser_a.get(task).text
