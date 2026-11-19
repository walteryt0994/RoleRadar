"""Owner redesign batch (2026-11-17, unified UI plan): what the new pages promise beyond their looks.

- an experience's time in a year-and-month picker that never rewrites a time saved back as it was (plan 3.1);
- the job preferences edited in three steps as rows, folded back losslessly (plan 3.3);
- the library's list beside one record, with its state chips (plan 3.1); a history of compact rows (plan 3.1, 3.3);
- a resume import that reads only the way chosen (plan 3.4);
- 发现岗位 started in one protected post, with a one-time key and nothing widened (plan 5.1, 5.2);
- the add-a-job page without the five notes (plan 6.1, 9); the workspace's four tasks and 最近保存 (plan 8.1).
Synthetic data only; nothing is sent anywhere."""
import re

import pytest

from app import i18n
from app.discovery.tasks import Stage
from app.library.months import MonthRange, parse_period, period_text
from app.library.preference_rows import fold_rows, rows_of, start_of
from job_helpers import InlineExecutor, confirmed
from library_helpers import form_for, item_id_from

PREFERENCES = dict(
    cities_ideal="北京",
    cities_ok="上海",
    cities_no="深圳",
    job_types_ideal="实习",
    directions_ideal="数据分析",
    work_content_no="销售",
    must_haves="最早 2026 年 7 月入职",
)


# --- the year-and-month picker (plan 3.1) --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "read"),
    [
        ("", MonthRange(None)),
        ("2026年3月", MonthRange((2026, 3))),
        ("2026.03-2026.06", MonthRange((2026, 3), (2026, 6))),
        ("2026年3月至今", MonthRange((2026, 3), None, True)),
        ("2026-03 – present", MonthRange((2026, 3), None, True)),
        # A year alone, a season, a sentence or a range ending before it starts is left as its own words.
        ("2026", None),
        ("2025 年春季学期", None),
        ("2026 年暑期；还在进行中", None),
        ("2026年6月-2026年3月", None),
        ("2026-13", None),
    ],
)
def test_only_unambiguous_year_and_month_writing_is_read_into_the_picker(text, read):
    assert parse_period(text) == read


def test_a_chosen_range_is_written_one_way_that_reads_back_the_same():
    for chosen in (MonthRange((2026, 3)), MonthRange((2026, 3), (2026, 6)), MonthRange((2026, 3), None, True)):
        for language in ("zh", "en"):
            assert parse_period(period_text(chosen, language)) == chosen


def test_a_new_experience_takes_its_time_from_the_picker_and_never_a_day(browser_a):
    item_id = browser_a.create(
        title="年月", body="合成内容", period_start_year="2026", period_start_month="3", period_end_year="2026", period_end_month="6"
    )
    page = browser_a.get(f"/experiences/{item_id}").text

    assert "2026-03 – 2026-06" in page
    # The form offers years and months only, and fills nothing in for the person.
    form = browser_a.get("/experiences/new").text
    assert 'name="period_start_year"' in form and 'name="period_start_month"' in form and 'type="date"' not in form
    assert not re.search(r'name="period_start_year".*?<option value="\d+" selected', form, re.S)


def test_a_time_saved_back_as_it_was_keeps_its_words_and_makes_no_new_version(browser_a):
    readable = browser_a.create(title="可读", body="合成内容", period_mode="text", period="2026年3月 - 2026年6月")
    unreadable = browser_a.create(title="不可读", body="合成内容", period_mode="text", period="2025 年春季学期")

    for item_id, words in ((readable, "2026年3月 - 2026年6月"), (unreadable, "2025 年春季学期")):
        saved = browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft")

        assert saved.headers["location"].endswith("notice=unchanged"), saved.headers["location"]
        assert words in browser_a.get(f"/experiences/{item_id}").text

    # A time the picker cannot read is offered in its own words, with the choice to switch to the picker.
    edit = browser_a.get(f"/experiences/{unreadable}/edit").text
    assert 'value="2025 年春季学期"' in edit and 'name="period_mode" value="months"' in edit


def test_a_changed_month_is_written_in_the_one_form_and_a_reversed_range_is_refused(browser_a):
    item_id = browser_a.create(title="改动", body="合成内容", period_mode="text", period="2026年3月 - 2026年6月")
    saved = browser_a.submit(f"/experiences/{item_id}/edit", f"/experiences/{item_id}/draft", period_end_month="9")

    assert saved.headers["location"].endswith("notice=saved")
    assert "2026-03 – 2026-09" in browser_a.get(f"/experiences/{item_id}").text

    refused = browser_a.submit("/experiences/new", "/experiences", title="倒序", body="合成内容", period_start_year="2026",
                               period_start_month="6", period_end_year="2026", period_end_month="3")

    assert refused.status_code == 422 and "结束年月不能早于开始年月" in refused.text


# --- the job preferences as rows (plan 3.3) --------------------------------------------------------------------------


def test_each_stored_stance_is_one_row_and_nothing_is_split_by_guessing():
    rows = rows_of({"cities_ideal": "北京、上海", "cities_ok": "杭州\n苏州", "job_types_no": "全职"})

    assert rows["cities"] == [("ideal", "北京、上海"), ("ok", "杭州\n苏州")]
    assert rows["job_types"] == [("no", "全职")]
    assert rows["directions"] == []


def test_rows_fold_back_by_stance_one_per_line_and_an_unknown_stance_is_refused():
    form = {
        "pref_rows": "1",
        "cities_r0": "北京", "cities_r0_stance": "ideal",
        "cities_r1": "杭州", "cities_r1_stance": "ideal",
        "cities_r2": "", "cities_r2_stance": "ok",
        "job_types_r0": "实习", "job_types_r0_stance": "no",
        "must_original": "", "must_start_year": "", "must_start_month": "", "must_other": "",
    }
    folded = fold_rows(form)

    assert (folded["cities_ideal"], folded["cities_ok"], folded["job_types_no"]) == ("北京\n杭州", "", "实习")

    from app.library.errors import ContentError

    with pytest.raises(ContentError):
        fold_rows({**form, "cities_r0_stance": "maybe"})

    with pytest.raises(ContentError):
        fold_rows({**form, "cities_r12": "太多", "cities_r12_stance": "ideal"})


def test_the_start_month_is_read_only_in_its_exact_forms_and_kept_in_its_words():
    assert start_of("最早 2026 年 7 月入职\n需要签证担保") == ((2026, 7), "需要签证担保")
    assert start_of("Earliest start: 2026-07") == ((2026, 7), "")
    # Any other words: no date is read out of them.
    assert start_of("2026 年 7 月以后都可以入职") == (None, "2026 年 7 月以后都可以入职")

    original = "最早 2026 年 7 月入职\n需要签证担保"
    unchanged = {"pref_rows": "1", "must_original": original, "must_start_year": "2026", "must_start_month": "7", "must_other": "需要签证担保"}
    assert fold_rows(unchanged)["must_haves"] == original

    with i18n.using("zh-CN"):
        moved = fold_rows({**unchanged, "must_start_month": "9"})["must_haves"]
    assert moved == "最早 2026 年 9 月入职\n需要签证担保"
    assert fold_rows({**unchanged, "must_start_year": "", "must_start_month": ""})["must_haves"] == "需要签证担保"


def test_preferences_opened_and_saved_unchanged_change_nothing_and_rows_add_a_value(browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    page = browser_a.get("/job-preferences/edit").text
    action = re.search(r'action="(/job-preferences/[^"]+/save)"', page).group(1)
    form = form_for(page, action)

    # Three steps in one form; moving between them is never a save.
    assert 'name="pref_rows" value="1"' in page
    for step in ("求职目标", "工作偏好", "求职要求"):
        assert step in page

    unchanged = browser_a.post(action, form.fields)
    assert unchanged.headers["location"].endswith("notice=preferences_unchanged")

    added = browser_a.post(action, {**form.fields, "cities_r3": "杭州", "cities_r3_stance": "ok"})
    assert added.headers["location"].endswith("notice=preferences_saved")
    overview = browser_a.get("/job-preferences").text
    assert "杭州" in overview and "上海" in overview and "最早 2026 年 7 月入职" in overview


def test_a_failed_preferences_save_shows_the_rows_as_posted(browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    page = browser_a.get("/job-preferences/edit").text
    action = re.search(r'action="(/job-preferences/[^"]+/save)"', page).group(1)
    form = form_for(page, action)
    refused = browser_a.post(action, {**form.fields, "cities_r3": "杭州", "cities_r3_stance": "sometimes"})

    assert refused.status_code == 422 and 'value="杭州"' in refused.text


# --- the library beside one record, its history (plans 3.1, 3.3) ---------------------------------------------------


def test_the_library_lists_records_beside_the_first_with_state_chips(browser_a):
    draft = browser_a.create(title="草稿经历", body="合成内容")
    kept = browser_a.create(title="确认经历", body="合成内容")
    browser_a.submit(f"/experiences/{kept}", f"/experiences/{kept}/confirm")
    page = browser_a.get("/experiences").text

    assert 'class="md"' in page and 'class="md-detail"' in page
    assert re.search(r"全部<span class=\"chip-count\">2</span>", page)
    assert re.search(r"已确认<span class=\"chip-count\">1</span>", page)
    assert re.search(r"待核对<span class=\"chip-count\">1</span>", page)
    review = browser_a.get("/experiences?state=review").text
    assert "草稿经历" in review and "确认经历" not in review.split('class="md-detail"')[0]
    # A record's own page shows it beside the list, with the way back on narrow screens.
    record = browser_a.get(f"/experiences/{draft}").text
    assert "md-has-selection" in record and "返回经历库" in record


def test_the_overview_keeps_the_opened_records_private_note_back_until_that_record_is_opened(browser_a):
    noted = browser_a.create(title="有备注的经历", body="合成内容", private_note="备注-overview-sentinel-7c1")
    overview = browser_a.get("/experiences").text
    record = browser_a.get(f"/experiences/{noted}").text

    # The list opens its only record by itself: the note stays out, said held (its row in the list opens it).
    assert "备注-overview-sentinel-7c1" not in overview and "有私有备注，打开这条经历才显示。" in overview
    # Opened, the record shows its note to its owner as before.
    assert "备注-overview-sentinel-7c1" in record and "有私有备注，打开这条经历才显示。" not in record


def test_a_history_lists_versions_closed_one_open_at_a_time_and_events_apart(browser_a):
    item_id = browser_a.create(title="历史", body="合成内容")
    browser_a.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
    page = browser_a.get(f"/experiences/{item_id}/history").text

    rows = re.findall(r'<details class="history-row"([^>]*)>', page)
    assert rows and all(' name="history-version"' in row and " open" not in row for row in rows)
    assert "初始版" in page and "确认版 1" in page
    assert "历史版本" in page and "操作记录" in page
    events = browser_a.get(f"/experiences/{item_id}/history?tab=events").text
    assert 'class="event-list history-events"' in events and 'class="history-row"' not in events


# --- resume import: the way chosen only (plan 3.4) -------------------------------------------------------------------


def test_the_import_page_switches_ways_and_keeps_one_line_on_how_long(browser_a):
    page = browser_a.get("/imports").text

    assert 'name="mode" value="file"' in page and 'name="mode" value="text"' in page
    assert "文件和文字暂存最多 7 天，到期自动删除且不可恢复。" in page
    assert re.search(r'<input type="checkbox" name="consent" value="yes" required', page)
    for gone in ("把已有简历的文字读进来", "不会发给外部 AI", "读取在本机完成"):
        assert gone not in page


def test_only_the_way_chosen_is_read(browser_a):
    form = form_for(browser_a.get("/imports").text, "/imports")
    fields = {name: value for name, value in form.fields.items() if name not in ("file", "text", "consent")}

    # Text sent while the file way is chosen, with no file: nothing to read, never the text instead.
    refused = browser_a.client.post(
        "/imports",
        data={**fields, "mode": "file", "text": "合成简历文字", "consent": "yes"},
        files={"file": ("", b"", "application/octet-stream")},
    )
    assert refused.status_code == 422

    # A file sent while the text way is chosen is set aside: the text is what is kept.
    fields = {name: value for name, value in form_for(browser_a.get("/imports").text, "/imports").fields.items() if name not in ("file", "text", "consent")}
    kept = browser_a.client.post(
        "/imports",
        data={**fields, "mode": "text", "text": "合成简历文字：项目经历", "consent": "yes"},
        files={"file": ("简历.pdf", b"%PDF-1.4 not really", "application/pdf")},
    )
    assert kept.status_code == 303
    assert "合成简历文字：项目经历" in browser_a.get(kept.headers["location"].split("?")[0]).text


# --- 发现岗位 in one protected post (plans 5.1, 5.2) -----------------------------------------------------------------


@pytest.fixture
def ready(app, browser_a):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    confirmed(browser_a, title="校园订单看板（合成）", actions="用 Python 清洗订单数据，并用 SQL 汇总每周销量。")
    app.state.discovery.executor = InlineExecutor()

    return app.state.discovery


def start_form(browser, **changes):
    """The setup form as a browser sends it: every chip ticked by default under its repeated name."""
    page = browser.get("/discover").text
    form = form_for(page, "/discover/start")
    data = dict(form.fields)

    for name in ("city_pick", "job_type_pick", "direction_pick"):
        data[name] = re.findall(rf'name="{name}" value="([^"]*)" checked', page)

    data.update({"targets": "数据分析实习生"})
    data.update(changes)

    return page, data


def test_start_search_checks_binds_and_starts_in_one_post_with_no_review_page(browser_a, ready):
    page, data = start_form(browser_a)

    # No result language choice and no "也查" / "所列以外" controls on the setup page.
    for gone in ('name="result_language"', "_others", "_added"):
        assert gone not in page
    started = browser_a.post("/discover/start", {**data, "direction_pick": "数据分析"})

    assert started.status_code == 303 and started.headers["location"].startswith("/discover/tasks/")
    task_id = started.headers["location"].split("?")[0].rsplit("/", 1)[1]
    task = ready.store.get(task_id, *_owner(ready, task_id))
    assert task.stage is not Stage.REVIEW
    # The values chosen are the scope and everything else is left out; the language is the interface's.
    scope = task.confirmation.scope
    assert scope.city.values == ("北京", "上海") and scope.city.others.value == "excluded"
    assert task.confirmation.result_language == "zh-CN"


def _owner(searches, task_id):
    task = searches.store._tasks[task_id]

    return task.owner_id, task.session


def test_the_same_start_key_starts_one_search_only(browser_a, ready):
    _, data = start_form(browser_a, direction_pick="数据分析")
    first = browser_a.post("/discover/start", data)
    again = browser_a.post("/discover/start", data)

    assert first.status_code == again.status_code == 303
    assert first.headers["location"].split("?")[0] == again.headers["location"].split("?")[0]
    assert len(ready.store._tasks) == 1


def test_a_dimension_with_nothing_chosen_is_asked_for_and_nothing_starts(browser_a, ready):
    _, data = start_form(browser_a)
    data = {name: value for name, value in data.items() if name != "city_pick"}
    refused = browser_a.post("/discover/start", {**data, "direction_pick": "数据分析"})

    assert refused.status_code == 422 and "城市：请至少选一项" in refused.text
    assert len(ready.store._tasks) == 0


def test_start_search_refuses_a_post_without_its_token_or_key(browser_a, ready):
    _, data = start_form(browser_a, direction_pick="数据分析")

    assert browser_a.post("/discover/start", {**data, "csrf_token": "forged"}).status_code == 403
    assert browser_a.post("/discover/start", {**data, "start_key": ""}).status_code == 400
    assert len(ready.store._tasks) == 0


# --- 补充岗位, 工作台 (plans 6.1, 8.1, 9) -----------------------------------------------------------------------------


FIVE_NOTES = (
    ("现在能读取的来源", "Sources that can be read now"),
    ("读取只读这个岗位的公开信息", "This reads only this job's public information"),
    ("这样补充的岗位标", "Jobs added this way are marked"),
    ("只作为出处的文字保存，不会打开", "Saved only as text for reference"),
    ("下一步只给你看，不保存，也不发送", "The next step only shows you the content"),
)


def test_the_add_a_job_page_has_two_ways_and_none_of_the_five_notes(browser_a):
    chinese = browser_a.get("/job-records/supply").text
    browser_a.post("/language", {"lang": "en", "next": "/job-records/supply", "csrf_token": browser_a.csrf_token()})
    english = browser_a.get("/job-records/supply").text

    for page in (chinese, english):
        assert 'action="/job-records/supply/link"' in page and 'action="/job-records/supply/text"' in page
    for zh, en in FIVE_NOTES:
        assert zh not in chinese and en not in english


def test_the_workspace_starts_four_tasks_and_lists_what_was_saved(browser_a):
    page = browser_a.get("/workspace").text

    for href, label in (("/experiences/new", "记录经历"), ("/directions", "探索方向"), ("/discover", "发现岗位"), ("/job-analysis", "分析岗位")):
        assert f'href="{href}"' in page and label in page
    assert "最近保存" in page and "最近查看" not in page


# --- 我的岗位: a decision made on the list comes back to the same job (plan 5.3, 8.3) ---------------------------------


def test_a_decision_made_on_the_list_comes_back_to_that_job_on_the_list(browser_a, ready):
    _, data = start_form(browser_a, direction_pick="数据分析")
    task = browser_a.post("/discover/start", data).headers["location"].split("?")[0]
    page = browser_a.get(task).text
    keep_path = re.search(r'href="(/discover/tasks/[^"]+/keep\?[^"]+)">加入我的岗位</a>', page).group(1).replace("&amp;", "&")
    keep = form_for(browser_a.get(keep_path).text, f"{task}/keep")
    assert browser_a.post(f"{task}/keep", {**keep.fields, "action": "keep"}).status_code == 303

    listing = browser_a.get("/job-records").text
    decide = form_for(listing, re.search(r'action="(/job-records/postings/[^"]+/decide)"', listing).group(1))
    record_id = decide.action.split("/")[3]
    back = browser_a.post(decide.action, {**decide.fields, "decision": "favorite"})

    # Back to the list with this job chosen by its record (its row moved to another group), never another row.
    assert (back.status_code, back.headers["location"]) == (303, f"/job-records?job={record_id}&notice=decided")
    shown = browser_a.get(back.headers["location"]).text
    detail = re.search(r'<article class="result-detail" id="job-(\d+)"[^>]*aria-labelledby="job-\d+-title">', shown)
    assert detail and "决定已保存" in shown and f"/job-records/postings/{record_id}/decide" in shown
    # A decision from the job's own page still comes back to that page.
    own = form_for(browser_a.get(f"/job-records/postings/{record_id}").text, decide.action)
    assert browser_a.post(own.action, {**own.fields, "decision": "defer"}).headers["location"] == f"/job-records/postings/{record_id}?notice=decided"


# --- 发现的岗位: a posting is "still listed" only when its last check said so (plan 5.3) ---------------------------------


def test_a_found_posting_is_called_still_listed_only_when_its_last_check_said_so():
    from datetime import datetime, timezone

    from app.discovery import presentation
    from app.discovery.contracts import CheckStatus
    from types import SimpleNamespace

    when = datetime(2026, 11, 18, 9, 30, tzinfo=timezone.utc)
    never = SimpleNamespace(status=None, checked_at=None, answered_status=None, check_failed_at=None)
    missed = SimpleNamespace(status=CheckStatus.UNKNOWN, checked_at=when, answered_status=CheckStatus.OPEN, check_failed_at=when)
    unanswered_only = SimpleNamespace(status=CheckStatus.UNKNOWN, checked_at=None, answered_status=None, check_failed_at=when)
    open_now = SimpleNamespace(status=CheckStatus.OPEN, checked_at=when, answered_status=CheckStatus.OPEN, check_failed_at=None)

    with i18n.using("zh-CN"):
        assert presentation.found_check_text(never) == "这次没有核对状态"
        assert presentation.found_check_text(missed).startswith("岗位状态待确认：最近一次核对")
        assert "最后一次有回答是" in presentation.found_check_text(missed)
        assert presentation.found_check_text(unanswered_only).startswith("岗位状态待确认：最近一次核对")
        assert presentation.found_check_text(open_now).startswith("核对时仍公开发布 · ")

        # Never "still listed" as where it stands: at most what its last answered check said, said as that.
        for item in (never, unanswered_only):
            assert "仍公开发布" not in presentation.found_check_text(item)
        assert not presentation.found_check_text(missed).startswith("核对时仍公开发布")
