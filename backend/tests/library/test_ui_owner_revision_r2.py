"""The owner redesign, revision R2 after Codex's review of R1 (2026-11-18): what Codex found, as product tests.

- Supplementing an experience written in its one experience details field (经历内容) with a resume item sets the
  item's joined text against that field's real words, kept, appended or put in their place; never an empty-looking
  "做了什么" filled beside it, never a guess at splitting, never a silent dedupe. Records kept in the older split fields
  are supplemented field by field, as before; a mixed one keeps its older fields untouched.
- 我的岗位's job beside the list shows what its own page shows, read only: 岗位概览 (or the text pasted) and 已保存分析,
  分析岗位 where a text is kept, 补充岗位原文 for a job from a source; nothing invented, nothing scored again.
- A normal direction result keeps no line of requests handed on; failures and refusals still say what went out.
- The start month's error sits right under the year and month.
The tab kept in the records page's address is covered by js/results_page.test.cjs (test_results_page_script.py).
All synthetic; nothing is sent anywhere.
"""
import re

from sqlalchemy import select

from app.library.content import parse_experience_form
from app.library.models import CandidateState, ExperienceItem, JobAnalysis, JobPosting
from app.library.service import create_experience
from job_helpers import text_of
from library_helpers import item_id_from
from test_job_analyses import first_result, kept_one  # noqa: F401
from test_job_record_pages import finished, inline, keep, library, rows  # noqa: F401 (fixtures)
from test_job_supplied_pages import board, keep as keep_supplied, paste as paste_posting, record_of, token_of  # noqa: F401
from test_library_adoption import adopt_url, adoptions, fields_url, imported, item, write_fields
from test_library_candidates import one, owner_id, rt

# What the resume's internship item says it did (test_library_candidates.RESUME), joined as the item gives it.
DONE = "参与搭建销售看板，团队共 4 人\n负责每周的数据清洗\n没有使用 Docker，部署由同事完成"


def body_record(browser, **content) -> str:
    """A work experience written the new way, in its experience details, confirmed."""
    fields = {"organization": "合成数据科技有限公司", "role": "", "period": "", "private_note": "私有备注-合成", **content}
    response = browser.submit("/experiences/new?kind=work", "/experiences", **fields)
    assert response.status_code == 303, response.text
    item_id = item_id_from(response.headers["location"])
    assert browser.submit(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm").status_code == 303
    assert item_id and ExperienceItem
    return item_id


def mixed_record(app, **content) -> str:
    """A work experience with text both in its experience details and in an older split field, as an earlier
    supplement could leave it (R1's reproduction): made through the library's own service, as no page makes one now."""
    owner, now = owner_id(app), rt(app).now()
    entry = parse_experience_form({"organization": "合成数据科技有限公司", "private_note": "", **content}, "work")

    with rt(app).database.transaction() as db:
        return create_experience(db, owner, entry, now).id


def field_page(browser, import_id, candidate_id, target):
    response = browser.get(fields_url(import_id, candidate_id, target))
    assert response.status_code == 200, response.text
    return response.text


def stored(app, item_id) -> dict:
    return {name: value for name, value in item(app, item_id).content.items() if value}


# --- supplementing an experience written in its experience details ------------------------------------------------------


def test_the_same_words_already_in_the_experience_details_are_said_to_be_there_and_nothing_is_offered(app, browser_a):
    target = body_record(browser_a, body=DONE)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = field_page(browser_a, import_id, work.id, target)
    shown = text_of(page)

    # The real words of the experience, never "(empty)" for a 做了什么 it does not use.
    assert "经历内容" in shown and "和现在的内容相同，不需要处理" in shown
    assert 'name="choice-actions"' not in page and 'name="choice-body"' not in page
    assert "导入的内容由这条候选的“做了什么”合成" in shown

    before = (item(app, target).content, item(app, target).revision)
    action = f"{adopt_url(import_id, work.id)}/fields"
    form = browser_a.form(fields_url(import_id, work.id, target), action)
    forged = browser_a.post(action, {**form.fields, "choice-actions": "fill"})

    # A 做了什么 written beside the experience details is never offered, so never taken.
    assert forged.status_code == 400
    assert (item(app, target).content, item(app, target).revision) == before
    assert one(app, import_id, "work").state == CandidateState.PENDING and adoptions(app) == []


def test_other_words_are_kept_appended_or_put_in_place_of_the_experience_details_as_chosen(app, browser_a):
    target = body_record(browser_a, body="整理周报")
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = field_page(browser_a, import_id, work.id, target)

    assert 'name="choice-body" value="keep" checked' in page
    assert 'name="choice-body" value="append"' in page and 'name="choice-body" value="replace"' in page
    assert 'name="choice-actions"' not in page
    # What appending would leave, said before anything is written: the words as they are, a blank line, the item's.
    assert "看追加后的全文" in page and "整理周报\n\n" + DONE in page

    response = write_fields(browser_a, import_id, work.id, target, body="append", role="fill")

    assert response.status_code == 303
    content = stored(app, target)
    assert content["body"] == "整理周报\n\n" + DONE and content["role"] == "数据分析实习生"
    # Nothing in the older split fields of a record written the new way.
    assert "actions" not in content and "outcome" not in content and "reflection" not in content
    new = item(app, target)
    assert new.state == "draft" and new.private_note == "私有备注-合成"
    (adoption,) = adoptions(app)
    assert (adoption.action, adoption.fields) == ("supplement", ["role", "body"])
    card = text_of(browser_a.get(f"/imports/{import_id}/review").text)
    assert "写入的栏目：岗位或角色、经历内容。" in card

    # The record's own page shows its experience details once, with no older-format note.
    detail = text_of(browser_a.get(f"/experiences/{target}").text)
    assert detail.count("负责每周的数据清洗") == 1


def test_replacing_puts_the_items_words_in_place_and_keeping_everything_writes_nothing(app, browser_a):
    target = body_record(browser_a, body="整理周报")
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    before = (item(app, target).content, item(app, target).revision)

    kept = write_fields(browser_a, import_id, work.id, target)
    assert kept.status_code == 422 and "还没有选择要写入的栏目" in kept.text
    assert (item(app, target).content, item(app, target).revision) == before

    replaced = write_fields(browser_a, import_id, work.id, target, body="replace")
    assert replaced.status_code == 303
    assert stored(app, target)["body"] == DONE and "actions" not in stored(app, target)


def test_an_experience_with_no_words_yet_takes_the_items_words_in_its_experience_details(app, browser_a):
    target = body_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = field_page(browser_a, import_id, work.id, target)

    assert 'name="choice-body" value="fill"' in page and 'name="choice-actions"' not in page

    assert write_fields(browser_a, import_id, work.id, target, body="fill").status_code == 303
    content = stored(app, target)
    assert content["body"] == DONE and "actions" not in content


def test_a_mixed_record_keeps_its_older_fields_untouched_and_says_so(app, browser_a):
    target = mixed_record(app, body="整理周报", actions="旧格式的合成内容")
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = field_page(browser_a, import_id, work.id, target)
    shown = text_of(page)

    assert "这条经历还有旧格式的分栏内容，这次补充不会改动它们" in shown and "旧格式的合成内容" in shown
    assert 'name="choice-body" value="append"' in page and 'name="choice-actions"' not in page

    assert write_fields(browser_a, import_id, work.id, target, body="append").status_code == 303
    content = stored(app, target)
    assert content["body"] == "整理周报\n\n" + DONE and content["actions"] == "旧格式的合成内容"


def test_a_record_kept_in_the_older_split_fields_is_still_supplemented_field_by_field(app, browser_a):
    from test_library_adoption import work_record

    target = work_record(browser_a)
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    page = field_page(browser_a, import_id, work.id, target)

    assert 'name="choice-actions" value="append"' in page and 'name="choice-body"' not in page
    assert "导入的内容由这条候选的" not in text_of(page)


def test_a_page_older_than_the_record_writes_nothing_and_a_repeat_writes_once(app, browser_a):
    target = body_record(browser_a, body="整理周报")
    import_id = imported(app, browser_a)
    work = one(app, import_id, "work")
    action = f"{adopt_url(import_id, work.id)}/fields"
    form = browser_a.form(fields_url(import_id, work.id, target), action)

    # The record changed after the page was shown (another tab): nothing is written over it.
    assert browser_a.submit(f"/experiences/{target}/edit", f"/experiences/{target}/draft", body="整理周报（另一处改过）").status_code == 303
    changed = (item(app, target).content, item(app, target).revision)
    late = browser_a.post(action, {**form.fields, "choice-body": "append"})
    assert late.status_code == 409 and (item(app, target).content, item(app, target).revision) == changed

    fresh = browser_a.form(fields_url(import_id, work.id, target), action)
    first = browser_a.post(action, {**fresh.fields, "choice-body": "append"})
    written = (item(app, target).content, item(app, target).revision)
    again = browser_a.post(action, {**fresh.fields, "choice-body": "append"})

    assert first.status_code == 303 and again.status_code == 303 and "adopted_already" in again.headers["location"]
    assert (item(app, target).content, item(app, target).revision) == written and len(adoptions(app)) == 1
    assert written[0]["body"] == "整理周报（另一处改过）\n\n" + DONE


# --- 我的岗位: the job beside the list ---------------------------------------------------------------------------------


def detail_of(page: str, record_id: str) -> str:
    """The article beside the list that shows this record."""
    for article in re.findall(r'<article class="result-detail".*?</article>', page, re.S):
        if f"/job-records/postings/{record_id}\"" in article:
            return article

    raise AssertionError(f"no detail for {record_id}")


def test_a_job_from_a_source_shows_its_overview_and_saved_analyses_beside_the_list(app, browser_a, library, inline):
    url, result, analysis = kept_one(app, browser_a, library)
    assert keep(browser_a, url, result, action="decide", decision="favorite").status_code == 303
    [record] = rows(app, JobPosting)
    detail = detail_of(browser_a.get("/job-records").text, record.id)
    shown = text_of(detail)

    assert 'data-detail-tabs' in detail and 'data-panel="about"' in detail and 'data-panel="analyses"' in detail
    assert "岗位概览" in shown and "地点" in shown and "截止" in shown
    assert f'href="/job-records/analyses/{analysis.id}"' in detail and "保存于" in shown
    # Its facts only were kept: the way on is the text the user adds, never an analysis of a text it does not have.
    assert "/job-records/supply?title=" in detail and "补充岗位原文" in shown
    assert f"/job-analysis?record={record.id}" not in detail
    assert f'href="/job-records/postings/{record.id}"' in detail and "打开完整详情" in shown
    # The four decisions stay at hand, and nothing scores or matches the job here.
    assert detail.count('name="decision" value=') >= 4 and "依据覆盖" not in shown


def test_a_pasted_job_shows_its_text_and_the_way_to_analyze_it_and_an_empty_analyses_tab(app, browser_a, board):
    record_id = record_of(keep_supplied(browser_a, token_of(paste_posting(browser_a))))
    detail = detail_of(browser_a.get("/job-records").text, record_id)
    shown = text_of(detail)
    text = rt(app).database
    with text.transaction() as db:
        posting = db.get(JobPosting, record_id)
        assert posting is not None

    assert "岗位原文" in shown and 'class="quote posting-text posting-text-short"' in detail
    assert f'href="/job-analysis?record={record_id}"' in detail and "分析岗位" in shown
    assert "还没有已保存的分析。" in shown and "/job-records/supply?title=" not in detail


def test_the_list_never_lists_an_analysis_of_another_job_beside_a_job(app, browser_a, library, inline):
    url, result, analysis = kept_one(app, browser_a, library)
    others = stored_results(app, browser_a, url)
    other = next(found for found in others if found.ref != result.ref)
    assert keep(browser_a, url, other, action="decide", decision="defer").status_code == 303
    [record] = rows(app, JobPosting)
    detail = detail_of(browser_a.get("/job-records").text, record.id)

    assert f"/job-records/analyses/{analysis.id}" not in detail and "还没有已保存的分析。" in text_of(detail)


def stored_results(app, browser, url):
    from test_discovery_pages import stored as search_of_url

    return search_of_url(app, browser, url).finished.outcome.results


# --- a normal direction result, and the start month's error ------------------------------------------------------------


def test_the_start_months_error_sits_right_under_the_year_and_month(app, browser_a):
    page = browser_a.submit(
        "/job-preferences/edit", "/job-preferences", pref_rows="1",
        cities_r0="北京", cities_r0_stance="ideal",
        must_start_year="2026", must_start_month="", must_other="合成要求",
    ).text
    date_row = page[page.index('id="l-start"'):page.index('id="must-other"')]

    assert 'id="e-must"' in date_row and date_row.index('id="e-must"') > date_row.index('name="must_start_month"')
    assert 'class="field-error req-error" id="e-must"' in page
    assert page.count('id="e-must"') == 1 and 'aria-describedby="e-must"' in date_row
    assert ">合成要求</textarea>" in page and 'data-error-step="3"' in page
    assert JobAnalysis and select
