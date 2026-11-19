"""The owner redesign, revised after Codex's review (2026-11-18): what Codex reproduced, as product tests.

- A decision chosen again changes nothing, another one keeps the note, and one that would leave reasons behind asks
  first; reasons and note are edited beside the decision; taking it back says what goes with it.
- The preferences editor's rows stay rows: two of one stance are two rows again when the editor is opened, while a text
  saved before (or by another tool) stays one row, never split.
- A failed save opens the step of its first error, with the field said to be invalid and named by its error.
- 我的岗位's filters are a decision and a job status (never the source's note as a "type").
All synthetic; nothing is sent anywhere."""
import re

from app.library.models import ExperienceItem, ExperienceVersion, JobPosting
from app.library.preference_rows import ROWS_KEY, fold_rows, rows_of
from job_helpers import text_of
from library_helpers import form_for
from test_job_record_pages import rows
from test_job_supplied_pages import board, keep, paste, post, record_of, token_of  # noqa: F401 (board: a fixture)
from test_ui_owner_redesign import ready  # noqa: F401 (a fixture)


def kept_pasted(browser) -> str:
    return record_of(keep(browser, token_of(paste(browser))))


def posting(app) -> JobPosting:
    [row] = rows(app, JobPosting)

    return row


# --- a decision never clears what it did not show ----------------------------------------------------------------------


def test_choosing_the_current_decision_again_keeps_its_note_and_reasons(app, browser_a):
    record_id = kept_pasted(browser_a)
    path = f"/job-records/postings/{record_id}"
    action = path + "/decide"
    first = browser_a.post(action, {**form_for(browser_a.get(path).text, action).fields, "decision": "favorite", "reason": ["company"], "note": "合成备注：需先了解团队"})
    assert first.status_code == 303
    before = posting(app)
    assert (before.decision, before.reasons, before.note) == ("favorite", ["company"], "合成备注：需先了解团队")

    # The prominent choice buttons send the decision alone (Codex review): no note, no reasons.
    top = form_for(browser_a.get(path).text, action)
    assert "note" not in top.fields and "reason" not in top.fields and "reasons_shown" not in top.fields
    again = browser_a.post(action, {**top.fields, "decision": "favorite"})

    after = posting(app)
    assert (again.status_code, again.headers["location"]) == (303, f"{path}?notice=decision_same")
    assert (after.decision, after.reasons, after.note, after.revision) == ("favorite", ["company"], "合成备注：需先了解团队", before.revision)
    assert "决定没有变化" in text_of(browser_a.get(again.headers["location"]).text)


def test_another_decision_keeps_the_note_and_asks_first_when_reasons_would_go(app, browser_a):
    record_id = kept_pasted(browser_a)
    path = f"/job-records/postings/{record_id}"
    action = path + "/decide"
    # A decision with a note and no reasons: switching keeps the note, nothing to ask.
    post(browser_a, action, revision="1", decision="defer", reasons_shown="1", note="合成：等下周再看")
    switched = browser_a.post(action, {**form_for(browser_a.get(path).text, action).fields, "decision": "skip"})
    assert switched.status_code == 303
    assert (posting(app).decision, posting(app).reasons, posting(app).note) == ("skip", [], "合成：等下周再看")

    # With reasons for skip, a quick switch writes nothing and opens the page that asks.
    row = posting(app)
    post(browser_a, action, revision=str(row.revision), decision="skip", reason=["direction"], note="合成：方向不合适")
    row = posting(app)
    asked = browser_a.post(action, {**form_for(browser_a.get(path).text, action).fields, "decision": "favorite"})
    assert (asked.status_code, asked.headers["location"]) == (303, f"{path}?change=favorite#decision-change")
    unchanged = posting(app)
    assert (unchanged.decision, unchanged.reasons, unchanged.note, unchanged.revision) == ("skip", ["direction"], "合成：方向不合适", row.revision)

    page = browser_a.get(asked.headers["location"]).text
    shown = text_of(page)
    assert "改为「收藏」" in shown and "原来的原因只适用于「跳过」" in shown and "备注会保留" in shown
    confirm = form_for(page, action)
    assert confirm.fields["decision"] == "favorite" and confirm.fields["reasons_shown"] == "1" and confirm.fields["note"] == "合成：方向不合适"
    assert f'href="/job-records/postings/{record_id}">取消</a>' in page

    saved = browser_a.post(action, {**confirm.fields, "reason": ["company"]})
    assert saved.status_code == 303
    done = posting(app)
    assert (done.decision, done.reasons, done.note) == ("favorite", ["company"], "合成：方向不合适")


def test_reasons_and_note_are_edited_beside_the_decision_and_undo_says_what_goes(app, browser_a):
    record_id = kept_pasted(browser_a)
    path = f"/job-records/postings/{record_id}"
    action = path + "/decide"
    post(browser_a, action, revision="1", decision="favorite", reason=["direction"], note="合成备注")
    page = browser_a.get(path).text

    # The editor sits in the decision bar, filled in as saved: the reasons ticked, the note written.
    bar = page[page.index('class="decision-bar"'):page.index('data-tab-area')]
    assert 'name="reason" value="direction" checked' in bar and ">合成备注</textarea>" in bar
    assert "撤销决定" in bar and "撤销后，原因和备注一起清除。" in text_of(bar)

    # Saving the editor with every reason unticked and the note emptied clears them: they were shown.
    editor = [form for form in re.findall(r'<form method="post" action="[^"]+/decide" class="keep-form">.*?</form>', page, re.S)][0]
    assert 'name="reasons_shown" value="1"' in editor
    row = posting(app)
    cleared = post(browser_a, action, revision=str(row.revision), decision="favorite", reasons_shown="1", note="")
    assert cleared.status_code == 303 and (posting(app).reasons, posting(app).note) == ([], None)


def test_an_older_page_or_another_user_changes_no_decision(app, browser_a, browser_b):
    record_id = kept_pasted(browser_a)
    path = f"/job-records/postings/{record_id}"
    action = path + "/decide"
    post(browser_a, action, revision="1", decision="favorite", note="合成备注")

    # The same decision from a page seen before the last write: a conflict, not a quiet no-op.
    stale = post(browser_a, action, revision="1", decision="favorite")
    assert stale.status_code == 409 and posting(app).note == "合成备注"

    other = post(browser_b, action, revision=str(posting(app).revision), decision="skip")
    assert other.status_code == 404 and posting(app).decision == "favorite"


def test_deciding_from_the_list_comes_back_to_the_list_and_asks_there_too(app, browser_a):
    record_id = kept_pasted(browser_a)
    action = f"/job-records/postings/{record_id}/decide"
    listed = browser_a.get("/job-records").text
    quick = form_for(listed, action)
    assert quick.fields.get("back") == "list" and "note" not in quick.fields

    first = browser_a.post(action, {**quick.fields, "decision": "skip"})
    assert (first.status_code, first.headers["location"]) == (303, f"/job-records?job={record_id}&notice=decided")
    row = posting(app)
    post(browser_a, action, revision=str(row.revision), decision="skip", reason=["place"], back="list")

    quick = form_for(browser_a.get("/job-records").text, action)
    asked = browser_a.post(action, {**quick.fields, "decision": "apply"})
    assert asked.headers["location"] == f"/job-records/postings/{record_id}?change=apply&back=list#decision-change"
    page = browser_a.get(asked.headers["location"]).text
    assert form_for(page, action).fields.get("back") == "list"
    assert f'href="/job-records?job={record_id}">取消</a>' in page


# --- the preferences editor's rows stay rows ---------------------------------------------------------------------------


def test_two_rows_of_one_stance_are_two_rows_again():
    saved = fold_rows({
        "pref_rows": "1",
        "cities_r0": "北京", "cities_r0_stance": "ideal",
        "cities_r1": "上海", "cities_r1_stance": "ideal",
        "must_original": "", "must_start_year": "", "must_start_month": "", "must_other": "",
    })

    assert saved["cities_ideal"] == "北京\n上海"
    assert rows_of(saved)["cities"] == [("ideal", "北京"), ("ideal", "上海")]
    # A text with several lines and nothing kept beside it (saved before, or by another tool) stays one row.
    assert rows_of({"cities_ideal": "北京\n上海"})["cities"] == [("ideal", "北京\n上海")]
    # Rows kept beside a text that is no longer theirs are not used: the text is one row as it stands.
    assert rows_of({"cities_ideal": "北京\n广州", ROWS_KEY: {"cities": {"ideal": ["北京", "上海"]}}})["cities"] == [("ideal", "北京\n广州")]


def test_rows_saved_from_the_editor_reopen_as_separate_rows_and_read_as_before(app, browser_a):
    response = browser_a.submit(
        "/job-preferences/edit", "/job-preferences", pref_rows="1",
        cities_r0="北京", cities_r0_stance="ideal",
        cities_r1="上海", cities_r1_stance="ideal",
        cities_r2="杭州", cities_r2_stance="ok",
    )
    assert response.status_code == 303

    page = browser_a.get("/job-preferences/edit").text
    assert re.search(r'<input[^>]+name="cities_r0"[^>]+value="北京"', page)
    assert re.search(r'<input[^>]+name="cities_r1"[^>]+value="上海"', page)
    assert re.search(r'<input[^>]+name="cities_r2"[^>]+value="杭州"', page)
    assert "北京\n上海</textarea>" not in page

    [item] = [row for row in rows(app, ExperienceItem) if row.category == "preferences"]
    # Stored as before (one text per stance), with the rows beside it; the overview lists each row.
    assert item.content["cities_ideal"] == "北京\n上海" and item.content["cities_ok"] == "杭州"
    assert item.content[ROWS_KEY] == {"cities": {"ideal": ["北京", "上海"]}}
    overview = text_of(browser_a.get("/job-preferences").text)
    assert overview.count("理想") >= 2 and "北京" in overview and "上海" in overview

    # Saved back unchanged: the same content, no new version.
    versions = len(rows(app, ExperienceVersion))
    again = browser_a.submit(f"/job-preferences/edit", f"/job-preferences/{item.id}/save")
    assert again.status_code == 303 and len(rows(app, ExperienceVersion)) == versions


def test_a_text_saved_before_stays_one_row_and_unchanged(app, browser_a):
    created = browser_a.submit("/job-preferences/edit", "/job-preferences", pref_rows="1", directions_r0="教育科技", directions_r0_stance="ideal")
    assert created.status_code == 303
    [item] = [row for row in rows(app, ExperienceItem) if row.category == "preferences"]

    # Written with several lines by another tool (its stored field by name): no rows kept beside it.
    other = browser_a.post(f"/job-preferences/{item.id}/save", {
        "csrf_token": browser_a.csrf_token(), "expected_revision": str(item.revision), "cities_ideal": "北京\n上海",
        "directions_ideal": "教育科技",
    })
    assert other.status_code == 303
    page = browser_a.get("/job-preferences/edit").text
    start = page.index('data-pref-dim="cities"')
    cities = page[start:page.index("data-pref-template", start)]
    # One row as it stands (a textarea), and only the empty row offered to add one beside it.
    assert re.search(r'<textarea name="cities_r0"[^>]*>北京\n上海</textarea>', cities)
    assert 'name="cities_r1"' in cities and 'name="cities_r2"' not in cities and "pref-row pref-row-new" in cities


# --- a failed save opens the step of its error -------------------------------------------------------------------------


def test_a_failed_save_opens_the_step_of_its_first_error_with_everything_kept(app, browser_a):
    third = browser_a.submit(
        "/job-preferences/edit", "/job-preferences", pref_rows="1",
        cities_r0="北京", cities_r0_stance="ideal",
        must_start_year="2026", must_start_month="", must_other="合成要求",
    )
    assert third.status_code == 422
    assert 'data-step="3"' in third.text and 'data-error-step="3"' in third.text
    assert re.search(r'name="must_start_year"[^>]+aria-invalid="true" aria-describedby="e-must"', third.text)
    assert 'id="e-must"' in third.text and ">合成要求</textarea>" in third.text
    assert re.search(r'<input[^>]+name="cities_r0"[^>]+value="北京"', third.text)

    second = browser_a.submit(
        "/job-preferences/edit", "/job-preferences", pref_rows="1",
        work_style_r0="合" * 1001, work_style_r0_stance="ok",
    )
    assert second.status_code == 422 and 'data-step="2"' in second.text


def test_nothing_is_saved_by_a_failed_save(app, browser_a):
    failed = browser_a.submit("/job-preferences/edit", "/job-preferences", pref_rows="1", must_start_year="2026", must_start_month="")

    assert failed.status_code == 422
    assert [row for row in rows(app, ExperienceItem) if row.category == "preferences"] == []


# --- 我的岗位's filters --------------------------------------------------------------------------------------------------


def test_my_jobs_filters_a_decision_and_a_job_status_never_the_sources_note(app, browser_a):
    record_id = kept_pasted(browser_a)
    post(browser_a, f"/job-records/postings/{record_id}/decide", revision="1", decision="favorite")
    listed = browser_a.get("/job-records").text
    filters = listed[listed.index("data-result-filters"):listed.index("data-results=")]

    assert 'data-filter="decision"' in filters and 'data-filter="status"' in filters
    assert "用户提供，系统未独立核验" not in filters and 'data-filter-type' not in filters
    assert ">手动添加，未核实</option>" in filters and ">收藏</option>" in filters
    assert 'data-decision="favorite" data-status="unchecked"' in listed
    # Back to the list is a link that results.js keeps in place, with the filters as they were.
    assert 'data-md-back' in listed


def test_restoring_from_the_bin_comes_back_to_the_bin_and_says_so(app, browser_a):
    record_id = kept_pasted(browser_a)
    record = f"/job-records/postings/{record_id}"
    assert browser_a.post(f"{record}/delete", {"csrf_token": browser_a.csrf_token(), "expected_revision": posting(app).revision}).status_code == 303
    bin_page = browser_a.get("/data/trash").text
    assert "删除的岗位记录" in text_of(bin_page) and f'href="{record}/purge"' in bin_page

    form = form_for(bin_page, f"{record}/restore")
    assert form.fields["next"] == "/data/trash?notice=restored"
    restored = browser_a.post(f"{record}/restore", form.fields)

    assert (restored.status_code, restored.headers["location"]) == (303, "/data/trash?notice=restored")
    assert posting(app).deleted_at is None
    back = text_of(browser_a.get(restored.headers["location"]).text)
    assert "已从回收站恢复。" in back and "删除的岗位记录" not in back


def test_a_source_job_offers_its_text_to_be_added_never_an_analysis_it_cannot_run(app, browser_a, board):
    from test_job_supplied_pages import read_link

    record_id = record_of(keep(browser_a, token_of(read_link(browser_a))))
    page = browser_a.get(f"/job-records/postings/{record_id}").text

    assert f'/job-analysis?record={record_id}' not in page
    assert "/job-records/supply?title=" in page and "补充岗位原文" in text_of(page)
    filled = browser_a.get("/job-records/supply?title=%E5%90%88%E6%88%90%E5%B2%97%E4%BD%8D&company=%E5%90%88%E6%88%90%E5%85%AC%E5%8F%B8").text
    assert 'value="合成岗位"' in filled and 'value="合成公司"' in filled


# --- 开始查找 keeps the choice of what to send ------------------------------------------------------------------------


def test_the_setup_page_lets_a_field_be_left_out_and_it_is_never_sent(app, browser_a, ready, monkeypatch):
    from app.job_analysis.simulation import SimulatedProvider
    from test_ui_owner_redesign import _owner, start_form

    prompts = []
    original = SimulatedProvider.generate_text

    def recorded(self, prompt, *args, **kwargs):
        prompts.append(str(prompt))
        return original(self, prompt, *args, **kwargs)

    monkeypatch.setattr(SimulatedProvider, "generate_text", recorded)
    page, data = start_form(browser_a, direction_pick="数据分析")
    offered = re.findall(r'name="send_offered" value="([^"]*)"', page)
    source = re.search(r'name="source" value="([^"]*)"', page).group(1)
    item_id = source.split(":")[0]
    actions = f"{item_id}:actions"

    # Every field of the experience is offered and ticked; the user unticks 做了什么 before starting.
    assert actions in offered and f'name="send" value="{actions}" checked' in page and 'name="send_shown" value="1"' in page
    data.update({"source": source, "send_offered": offered, "send": [key for key in offered if key != actions]})
    started = browser_a.post("/discover/start", data)
    assert started.status_code == 303 and started.headers["location"].startswith("/discover/tasks/")

    task_id = started.headers["location"].split("?")[0].rsplit("/", 1)[1]
    task = ready.store.get(task_id, *_owner(ready, task_id))
    [chosen] = task.confirmation.selection
    assert chosen.fields is not None and "actions" not in chosen.fields and "title" in chosen.fields
    # What went out to the (simulated) service: the title, never the field left out.
    assert prompts and any("校园订单看板（合成）" in prompt for prompt in prompts)
    assert not any("用 Python 清洗订单数据" in prompt for prompt in prompts)


def test_a_setup_page_without_the_choice_sends_what_a_review_would(app, browser_a, ready):
    from test_ui_owner_redesign import _owner, start_form

    page, data = start_form(browser_a, direction_pick="数据分析")
    source = re.search(r'name="source" value="([^"]*)"', page).group(1)
    data = {name: value for name, value in data.items() if name not in ("send", "send_offered", "send_shown")}
    started = browser_a.post("/discover/start", {**data, "source": source})

    task_id = started.headers["location"].split("?")[0].rsplit("/", 1)[1]
    [chosen] = ready.store.get(task_id, *_owner(ready, task_id)).confirmation.selection
    assert chosen.fields is None or {"title", "actions"} <= set(chosen.fields)
