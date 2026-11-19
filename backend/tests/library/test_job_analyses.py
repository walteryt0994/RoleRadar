"""U3-D3b: keeping a posting's analysis from a finished search, and the analyses kept, through the pages.

An analysis is kept only when the user asks, apart from the posting, its decision and the search's summary; what is
kept is the search's analysis in memory, by whitelist, never what a form says: each experience quote with what the
analysis read it with then, never the posting's text, a whole experience, a prompt or a model's answer. Quotes are
kept whole and shown folded. An analysis whose basis changed since the search is not kept; one kept is kept once, and
never again from that search once deleted for good. Deleting an experience or a version keeps the analyses resting on
it unless the user chooses to delete them along, under the same deletion; kept ones say how their basis stands and
keep their quotes, read as kept, never again from the experience as it is now. A review made under another notice
is never confirmed. The search runs on synthetic postings with the app's simulated decisions and analyses; nothing
leaves the process.
"""
import html
import json
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import analysis_words
from app.discovery import records
from app.discovery import web as discovery_web
from app.discovery.contracts import CATEGORY_LABELS
from app.discovery.web import SYNTHETIC_SOURCES
from app.experience_analysis import EXPERIENCES_START, PREFERENCE_TOPICS, PREFERENCES_START, REQUIREMENTS_START
from app.job_analysis import presentation as page_words
from app.library.job_choices import CATEGORY_WORDS
from app.library.job_export import IN_USE, JobExport, jobs_markdown, preference_name
from app.library.lifecycle import purge_due
from app.library.models import (
    ExperienceItem,
    ExperienceVersion,
    JobAnalysis,
    JobAnalysisFeedback,
    JobAnalysisSource,
    JobPosting,
    JobSearch,
)
from app.library.web import ALONG_CHANGED
from app.web_main import create_app
from job_helpers import InlineExecutor, confirmed, text_of
from library_helpers import LOCAL_BASE_URL, Browser, form_for
from test_discovery_pages import PREFERENCES, PROJECT, confirm, review, search_of, stored, task_id
from test_job_record_pages import exported, finished, inline, keep, library, rows  # noqa: F401 (fixtures)

COUNTED = (JobAnalysis, JobAnalysisSource, JobAnalysisFeedback, JobPosting, JobSearch)
# One sentence well over 300 characters naming a skill the synthetic postings ask for: the simulated analysis cites it
# whole, so a quote this long is kept, shown and exported whole.
LONG = "用 Python " + "整理并核对每一张订单表的字段格式、缺失值和重复记录，" * 14 + "最后汇总成每周的销量看板"
# A line of the synthetic posting no part of the analysis quotes.
UNQUOTED_POSTING_LINE = "制作每周报表"


def counts(app) -> tuple[int, ...]:
    with app.state.library.database.transaction() as db:
        return tuple(db.scalar(select(func.count()).select_from(model)) for model in COUNTED)


def row(app, model, record_id):
    with app.state.library.database.transaction() as db:
        return db.scalar(select(model).where(model.id == record_id))


def post(browser, path, **fields):
    return browser.post(path, {"csrf_token": browser.csrf_token(), **fields})


def first_result(app, browser, url):
    return stored(app, browser, url).finished.outcome.results[0]


def kept_one(app, browser, item):
    """A finished search on this experience and its first result's analysis kept: (search page, result, record)."""
    url = finished(browser, item)
    result = first_result(app, browser, url)
    response = keep(browser, url, result, action="analysis")
    assert (response.status_code, response.headers["location"]) == (303, f"{url}?notice=analysis_kept"), response.text
    [record] = rows(app, JobAnalysis)

    return url, result, record


def keep_page(browser, url, result):
    return browser.get(f"{url}/keep?source={result.ref.source_id}&posting={result.ref.posting_id}")


def deletion(browser, page):
    response = browser.get(page)
    assert response.status_code == 200, response.text

    return response.text, form_for(response.text, page)


def reconfirm(browser, item, **changes):
    assert browser.submit(f"/experiences/{item}/edit", f"/experiences/{item}/draft", **changes).status_code == 303
    assert browser.submit(f"/experiences/{item}", f"/experiences/{item}/confirm").status_code == 303


# --- kept only when asked, on its own ----------------------------------------------------------------------------------


def test_an_analysis_is_kept_only_when_asked_and_keeps_nothing_else(app, browser_a, library, inline):
    url = finished(browser_a, library)
    result = first_result(app, browser_a, url)
    page = keep_page(browser_a, url, result)

    # Seeing the results and the keep page writes nothing; the keep page offers the analysis on its own.
    assert counts(app) == (0, 0, 0, 0, 0) and page.status_code == 200
    assert "保存这份分析" in page.text and "单独保存这个岗位的分析，不会同时保存岗位、决定或查找摘要" in text_of(page.text)
    assert "留到你删除为止；删除后在回收站留 7 天，然后清除" in text_of(page.text)

    response = keep(browser_a, url, result, action="analysis")
    assert (response.status_code, response.headers["location"]) == (303, f"{url}?notice=analysis_kept")
    # The analysis and what it rests on (the experience and the preferences sent), nothing else.
    assert counts(app) == (1, 2, 0, 0, 0)
    assert {source.kind for source in rows(app, JobAnalysisSource)} == {"experience", "preferences"}
    [record] = rows(app, JobAnalysis)
    shown = text_of(browser_a.get(f"{url}?notice=analysis_kept").text)
    assert "这个岗位的分析已保存到岗位记录" in shown and "分析已保存" in shown

    # Keeping the posting keeps no analysis, and leaves the one kept as it was.
    assert keep(browser_a, url, result).status_code == 303
    assert counts(app) == (1, 2, 0, 1, 0) and rows(app, JobAnalysis)[0].revision == record.revision
    # What a search ran with and when it analysed it, by its own clock.
    assert record.analysed_at == result.analysed_at is not None and record.search_id == task_id(url)


def test_what_is_kept_is_the_searchs_analysis_by_whitelist_never_the_forms(app, browser_a, library, inline):
    url = finished(browser_a, library)
    result = first_result(app, browser_a, url)
    forged = {"content": '{"category": "strong"}', "title": "伪造的职位", "quote": "伪造的引文", "context": "伪造的上下文"}
    assert keep(browser_a, url, result, action="analysis", **forged).status_code == 303
    [record] = rows(app, JobAnalysis)
    kept = json.dumps([record.content, record.facts, record.generation], ensure_ascii=False)

    assert record.content == records.analysis_content(result) and record.facts["title"] == result.title
    assert not any(word in kept for word in forged.values())
    # display_metrics: the 岗位要求依据覆盖 count made from these very findings (owner review, plan 7.3), counts only.
    assert set(record.content) == {
        "version", "category", "first_checks", "notes", "findings", "preferences", "preference_findings", "sources", "display_metrics"
    }
    assert set(record.content["display_metrics"]) == {"evidence_coverage"}
    assert set(record.content["display_metrics"]["evidence_coverage"]) <= {"version", "supported", "total", "needs_confirmation", "no_evidence", "none"}
    citations = [citation for finding in record.content["findings"] for citation in finding["citations"]]
    assert citations
    # Each quote with what the analysis read it with then, word for word from its field; never more of the record.
    for citation in citations:
        assert set(citation) == {"item_id", "version_id", "category", "field", "quote", "basis", "context", "flags"}
        assert citation["context"] and citation["quote"] in PROJECT[citation["field"]]
        assert all(passage in PROJECT[citation["field"]] for passage in citation["context"])
    assert PROJECT["outcome"] not in kept
    # Never the posting's text, a prompt or a model's answer as such; the posting's text is known by its digest only.
    assert UNQUOTED_POSTING_LINE not in kept
    assert not any(marker in kept for marker in (REQUIREMENTS_START, EXPERIENCES_START, PREFERENCES_START, '"assessments"'))
    assert record.content_sha256 == result.content_sha256 and len(record.content_sha256) == 64
    assert record.generation["recipient"]["provider"] == "simulation" and record.generation["rules_version"] == "8"


def test_a_long_quote_is_kept_whole_folded_on_the_page_and_exported_whole(app, browser_a, inline):
    assert len(LONG) > 300
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    item = confirmed(browser_a, title="长句项目（合成）", actions=LONG, outcome="合成结果。")
    _, _, record = kept_one(app, browser_a, item)
    quotes = [citation["quote"] for finding in record.content["findings"] for citation in finding["citations"]]

    assert LONG in quotes
    page = browser_a.get(f"/job-records/analyses/{record.id}").text
    # On the page each requirement is folded, its quotes whole inside.
    folded = page.split('<details class="fold finding">')
    assert len(folded) > 1 and any(LONG in part for part in folded[1:]) and "<details class=\"fold finding\" open" not in page

    data, jobs = exported(browser_a)
    [entry] = data["job_records"]["analyses"]
    assert LONG in json.dumps(entry["content"], ensure_ascii=False) and LONG in jobs


# Each requirement row's group and words, as the shared row (_findings.html) writes them (U3-D6 close-out).
ROW = re.compile(r'<summary><span class="fold-title finding-text"><span class="finding-group">([^<]*)</span>([^<]*)</span>')


def row_groups(page: str) -> list[tuple[str, str]]:
    return [(html.unescape(group), html.unescape(words)) for group, words in ROW.findall(page)]


def shown_order(content: dict) -> list[tuple[str, str]]:
    """The rows a page shows for kept content: the hard conditions first, then the rest, each in the content's order."""
    findings = content["findings"]
    hard = [finding for finding in findings if finding["group"] == "hard_constraint"]
    rest = [finding for finding in findings if finding["group"] != "hard_constraint"]

    return [(analysis_words.requirement_group(finding["group"]), finding["text"]) for finding in hard + rest]


def assert_rows_say_their_groups(page: str, content: dict) -> None:
    shown, wanted = row_groups(page), shown_order(content)

    assert len(shown) == len(wanted) > 0
    for (group, words), (expected, text) in zip(shown, wanted):
        assert group == expected and words.startswith(text), (group, words, expected, text)


def test_every_row_says_its_group_on_the_result_and_the_kept_page_and_on_one_kept_before(app, browser_a, library, inline):
    """U3-D6 close-out: on the search result and on the kept analysis alike, each requirement row says the group it was
    judged in, in the words of analysis_words.requirement_group. A record kept in the first content version (before
    U3-D6 R4) reads with its groups the same; reading it rewrites nothing."""
    url, result, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"
    groups = {finding["group"] for finding in record.content["findings"]}
    assert "required" in groups

    # On the result page, the card of the result kept (each result is an article of its own; titles may repeat, the
    # posting's own address does not).
    articles = [part for part in browser_a.get(url).text.split("<article")[1:] if html.escape(result.url) in part.split("</article>")[0]]
    assert len(articles) == 1
    assert_rows_say_their_groups(articles[0].split("</article>")[0], record.content)
    assert_rows_say_their_groups(browser_a.get(analysis).text, record.content)

    # Made as version 1 kept it: none of the fields R4 added. Its rows still say their groups, from what was kept.
    old = json.loads(json.dumps(record.content))
    old["version"] = 1
    for finding in old["findings"]:
        for key in ("listed", "listed_mode", "named", "condition", "condition_state", "condition_fact"):
            finding.pop(key, None)

    with app.state.library.database.transaction() as db:
        db.get(JobAnalysis, record.id).content = old

    page = browser_a.get(analysis)
    assert page.status_code == 200
    assert_rows_say_their_groups(page.text, old)
    assert row(app, JobAnalysis, record.id).content == old


def test_a_row_of_a_group_no_page_knows_is_an_error_never_shown_as_it_is():
    with pytest.raises(KeyError):
        analysis_words.requirement_group("made_up_group")

    assert [analysis_words.requirement_group(group) for group in ("required", "preferred", "uncertain", "hard_constraint")] == [
        "必要要求", "加分项", "写得不明确的要求", "硬性条件",
    ]


# --- what the server lets keep ------------------------------------------------------------------------------------------


def test_a_set_that_does_not_let_keep_analyses_keeps_none_by_any_entry(make_config, clock):
    application = create_app(make_config(), clock=clock, discovery_sources=replace(SYNTHETIC_SOURCES, keep_analyses=frozenset()))

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        application.state.discovery.executor = InlineExecutor()
        browser = Browser.signed_in(application, "合成用户 A")
        assert browser.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
        url = finished(browser, confirmed(browser, **PROJECT))
        result = first_result(application, browser, url)
        page = keep_page(browser, url, result)

        assert page.status_code == 200 and "保存这份分析" not in page.text
        response = keep(browser, url, result, action="analysis")
        assert response.status_code == 403 and "没有保存分析的许可" in text_of(response.text)
        # The posting itself may still be kept, apart.
        assert keep(browser, url, result).status_code == 303
        assert counts(application) == (0, 0, 0, 1, 0)


def test_the_hard_rule_holds_whatever_a_set_lists(app, browser_a, library, inline, monkeypatch):
    url = finished(browser_a, library)
    result = first_result(app, browser_a, url)
    monkeypatch.setattr(records, "ANALYSIS_KEEPABLE_ACCESS", frozenset())

    response = keep(browser_a, url, result, action="analysis")
    assert response.status_code == 403 and counts(app)[:3] == (0, 0, 0)


def test_a_lead_has_no_analysis_to_keep(app, browser_a, library, inline):
    url = finished(browser_a, library)
    lead = stored(app, browser_a, url).finished.outcome.leads[0]

    assert "保存这份分析" not in keep_page(browser_a, url, lead).text
    response = keep(browser_a, url, lead, action="analysis")
    assert response.status_code == 409 and "没有完整分析结果" in text_of(response.text) and counts(app)[0] == 0


@pytest.mark.parametrize("change", ["edited", "preferences", "deleted"])
def test_an_analysis_whose_basis_changed_since_the_search_is_not_kept(app, browser_a, library, inline, change):
    url = finished(browser_a, library)
    result = first_result(app, browser_a, url)

    if change == "edited":
        assert browser_a.submit(f"/experiences/{library}/edit", f"/experiences/{library}/draft", actions="改过之后的做法。").status_code == 303
    elif change == "preferences":
        page = browser_a.get("/job-preferences/edit").text
        action = re.search(r'action="(/job-preferences/[^"]+/save)"', page).group(1)
        assert browser_a.submit("/job-preferences/edit", action, **{**PREFERENCES, "work_content_no": "客服"}).status_code == 303
    else:
        assert browser_a.submit(f"/experiences/{library}/delete", f"/experiences/{library}/delete").status_code == 303

    response = keep(browser_a, url, result, action="analysis")
    assert response.status_code == 409 and "有了变化" in text_of(response.text)
    assert counts(app)[:3] == (0, 0, 0)


@pytest.mark.parametrize("part", ["quote", "context"])
def test_a_quote_or_what_it_was_read_with_not_in_its_version_is_not_kept(app, browser_a, library, inline, part):
    """The basis is current, yet a quote, or what it was read with, does not stand in the version it names: nothing is
    kept (the second guard, behind the state of the basis)."""
    url = finished(browser_a, library)
    task = app.state.discovery.store._tasks[task_id(url)]
    outcome = task.finished.outcome
    result = outcome.results[0]
    finding = next(finding for finding in result.analysis.findings if finding.citations)
    citation = finding.citations[0]
    changed = replace(citation, quote="从来没有写过的一句话") if part == "quote" else replace(citation, context=("从来没有写过的一句话",))
    findings = tuple(
        replace(item, citations=(changed, *item.citations[1:])) if item is finding else item for item in result.analysis.findings
    )
    forged = replace(result, analysis=replace(result.analysis, findings=findings))
    task.finished = replace(task.finished, outcome=replace(outcome, results=(forged, *outcome.results[1:])))

    response = keep(browser_a, url, forged, action="analysis")
    assert response.status_code == 409 and "有了变化" in text_of(response.text) and counts(app)[:3] == (0, 0, 0)


# --- kept once ------------------------------------------------------------------------------------------------------------


def test_an_analysis_is_kept_once_and_never_again_once_deleted_for_good(app, browser_a, library, inline, clock):
    url, result, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"

    # The same page posted again keeps nothing new.
    assert keep(browser_a, url, result, action="analysis").status_code == 409 and counts(app)[0] == 1
    assert "这份分析已保存于" in text_of(keep_page(browser_a, url, result).text)

    # In the bin it is restored, never kept again.
    assert post(browser_a, f"{analysis}/delete", expected_revision=record.revision).status_code == 303
    refused = keep(browser_a, url, result, action="analysis")
    assert refused.status_code == 409 and "现在在回收站里" in text_of(refused.text) and counts(app)[0] == 1
    assert "删除的岗位记录" in browser_a.get("/experiences?tab=trash").text

    # Deleted for good: not kept again from this search, before the clean-up nor after it.
    trashed = row(app, JobAnalysis, record.id)
    assert post(browser_a, f"{analysis}/purge", expected_revision=trashed.revision).status_code == 303
    refused = keep(browser_a, url, result, action="analysis")
    assert refused.status_code == 409 and "已经永久删除" in text_of(refused.text)
    purge_due(app.state.library.database, clock.now())
    assert counts(app)[:3] == (0, 0, 0)
    refused = keep(browser_a, url, result, action="analysis")
    assert refused.status_code == 409 and "已经永久删除" in text_of(refused.text) and counts(app)[:3] == (0, 0, 0)
    assert "已经永久删除" in text_of(keep_page(browser_a, url, result).text)


def test_an_analysis_deleted_on_its_own_waits_seven_days_then_is_gone(app, browser_a, library, inline, clock):
    _, result, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"

    assert post(browser_a, f"{analysis}/delete", expected_revision=record.revision).status_code == 303
    trashed = row(app, JobAnalysis, record.id)
    assert post(browser_a, f"{analysis}/restore", expected_revision=trashed.revision).status_code == 303
    assert row(app, JobAnalysis, record.id).deleted_at is None

    restored = row(app, JobAnalysis, record.id)
    assert post(browser_a, f"{analysis}/delete", expected_revision=restored.revision).status_code == 303
    trashed = row(app, JobAnalysis, record.id)
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")

    assert post(browser_a, f"{analysis}/restore", expected_revision=trashed.revision).status_code == 404
    assert result.title not in browser_a.get("/experiences?tab=trash").text
    data, jobs = exported(browser_a)
    assert data["job_records"]["analyses"] == [] and "已保存的分析" not in jobs


# --- what it rests on: changed, deleted, deleted along, purged -----------------------------------------------------------


def test_a_kept_analysis_says_how_its_basis_stands_and_is_never_read_again_from_the_experience(app, browser_a, library, inline):
    _, _, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"
    [quote] = {citation["quote"] for finding in record.content["findings"] for citation in finding["citations"]}

    page = text_of(browser_a.get(analysis).text)
    assert "依据和保存时一致" in page and quote in page and "只供回看，不会用于新的分析或找岗" in page

    # Changed and confirmed again: marked, and still the words kept then, never the record's words now.
    reconfirm(browser_a, library, actions="后来改写的做法，和当时完全不同。")
    page = text_of(browser_a.get(analysis).text)
    assert "依据已变更" in page and quote in page and "后来改写的做法" not in page
    assert rows(app, JobAnalysis)[0].content == record.content


def test_deleting_an_experience_keeps_its_analyses_unless_chosen_and_brings_them_back(app, browser_a, library, inline):
    _, _, record = kept_one(app, browser_a, library)
    page_url = f"/experiences/{library}/delete"
    page, form = deletion(browser_a, page_url)

    # Offered, never ticked, apart from any explorations.
    assert "以这条项目经历为依据的岗位分析" in text_of(page)
    assert form.fields["analyses_along"] == f"{record.id}.{record.revision}" and "delete_analyses_along" not in form.fields
    assert "有 1 份已保存的岗位分析以它为依据：默认不会一起删除" in text_of(page)

    # Kept: the analysis stays in use and says its experience is in the bin.
    assert browser_a.post(page_url, form.fields).status_code == 303
    assert row(app, JobAnalysis, record.id).deleted_at is None
    assert "依据的经历或偏好在回收站里" in text_of(browser_a.get(f"/job-records/analyses/{record.id}").text)
    item = row(app, ExperienceItem, library)
    assert post(browser_a, f"/experiences/{library}/restore", expected_revision=item.revision).status_code == 303

    # Deleted along: in the bin under the experience's deletion, back only with it.
    page, form = deletion(browser_a, page_url)
    assert browser_a.post(page_url, {**form.fields, "delete_analyses_along": "yes"}).status_code == 303
    item, analysis = row(app, ExperienceItem, library), row(app, JobAnalysis, record.id)
    assert analysis.deletion_id == item.deletion_id is not None
    assert "连同 1 份岗位分析" in browser_a.get("/experiences?tab=trash").text
    alone = post(browser_a, f"/job-records/analyses/{record.id}/restore", expected_revision=analysis.revision)
    assert alone.status_code == 409 and "只能随那份资料一起恢复" in text_of(alone.text)
    assert post(browser_a, f"/experiences/{library}/restore", expected_revision=item.revision).status_code == 303
    assert row(app, JobAnalysis, record.id).deleted_at is None


def test_a_deletion_page_older_than_the_analysis_deletes_nothing(app, browser_a, library, inline):
    _, _, record = kept_one(app, browser_a, library)
    page_url = f"/experiences/{library}/delete"
    _, form = deletion(browser_a, page_url)

    # The analysis changes after the page froze it: feedback added.
    parts = records.analysis_parts(record.content)
    assert post(
        browser_a, f"/job-records/analyses/{record.id}/feedback", revision=record.revision, part=parts[0][0], analysis_reason="wrong"
    ).status_code == 303

    refused = browser_a.post(page_url, {**form.fields, "delete_analyses_along": "yes"})
    assert refused.status_code == 409 and str(ALONG_CHANGED) in text_of(refused.text)
    assert row(app, ExperienceItem, library).deleted_at is None and row(app, JobAnalysis, record.id).deleted_at is None


def test_a_version_takes_along_only_the_analyses_resting_on_it(app, browser_a, library, inline):
    _, _, record = kept_one(app, browser_a, library)
    [first] = [source.version_id for source in rows(app, JobAnalysisSource) if source.kind == "experience"]
    reconfirm(browser_a, library, actions="后来改写的做法。")
    page_url = f"/experiences/{library}/versions/{first}/delete"
    page, form = deletion(browser_a, page_url)

    assert "以这个版本为依据的岗位分析" in text_of(page)
    assert browser_a.post(page_url, {**form.fields, "delete_analyses_along": "yes"}).status_code == 303
    version = row(app, ExperienceVersion, first)
    assert row(app, JobAnalysis, record.id).deletion_id == version.deletion_id is not None
    assert "连同 1 份岗位分析" in browser_a.get(f"/experiences/{library}/history").text

    restore = form_for(browser_a.get(f"/experiences/{library}/history").text, f"/experiences/{library}/versions/{first}/restore")
    assert browser_a.post(f"/experiences/{library}/versions/{first}/restore", restore.fields).status_code == 303
    assert row(app, JobAnalysis, record.id).deleted_at is None


def test_purging_an_experience_purges_what_went_along_and_keeps_what_was_kept(app, browser_a, inline):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    along_item, kept_item = confirmed(browser_a, **PROJECT), confirmed(browser_a, **{**PROJECT, "title": "另一个合成项目"})
    _, _, along = kept_one(app, browser_a, along_item)
    url = finished(browser_a, kept_item)
    result = first_result(app, browser_a, url)
    assert keep(browser_a, url, result, action="analysis").status_code == 303
    kept = next(record for record in rows(app, JobAnalysis) if record.id != along.id)
    assert post(
        browser_a, f"/job-records/analyses/{along.id}/feedback", revision=along.revision, part="category", analysis_reason="other"
    ).status_code == 303

    # One deleted along, one kept: purging each experience.
    _, form = deletion(browser_a, f"/experiences/{along_item}/delete")
    assert browser_a.post(f"/experiences/{along_item}/delete", {**form.fields, "delete_analyses_along": "yes"}).status_code == 303
    _, form = deletion(browser_a, f"/experiences/{kept_item}/delete")
    assert browser_a.post(f"/experiences/{kept_item}/delete", form.fields).status_code == 303

    purge = deletion(browser_a, f"/experiences/{along_item}/purge")[0]
    assert "一起删除的 1 份岗位分析也会永久删除" in text_of(purge)
    purge = deletion(browser_a, f"/experiences/{kept_item}/purge")[0]
    assert "这些分析里保存的引文和判断时读的上下文不会随之清除" in text_of(purge)

    for item in (along_item, kept_item):
        _, form = deletion(browser_a, f"/experiences/{item}/purge")
        assert browser_a.post(f"/experiences/{item}/purge", {**form.fields, "acknowledge": "yes"}).status_code == 303

    # What went along is gone, with what it rested on and the feedback on it; what was kept stays, said to be so.
    assert row(app, JobAnalysis, along.id) is None
    with app.state.library.database.transaction() as db:
        assert db.scalar(select(func.count()).select_from(JobAnalysisFeedback)) == 0
        assert {source.analysis_id for source in db.scalars(select(JobAnalysisSource))} == {kept.id}

    page = text_of(browser_a.get(f"/job-records/analyses/{kept.id}").text)
    [quote] = {citation["quote"] for finding in kept.content["findings"] for citation in finding["citations"]}
    assert "依据已永久删除" in page and "已永久删除的经历" in page and quote in page
    data, _ = exported(browser_a)
    assert [entry["id"] for entry in data["job_records"]["analyses"]] == [kept.id]


# --- feedback, owners, export ---------------------------------------------------------------------------------------------


def test_feedback_names_a_part_of_the_analysis_and_is_only_added(app, browser_a, library, inline):
    _, _, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"
    parts = dict(records.analysis_parts(record.content))
    requirement = next(value for value in parts if value.startswith("requirement:"))

    assert post(browser_a, f"{analysis}/feedback", revision=record.revision, part=requirement, analysis_reason="wrong", note="合成备注").status_code == 303
    later = row(app, JobAnalysis, record.id)
    assert later.revision == record.revision + 1 and later.content == record.content

    for fields, status in (
        ({"part": "requirement:不存在", "analysis_reason": "wrong"}, 422),
        ({"part": "category", "analysis_reason": "不在列表"}, 422),
        ({"part": "category", "analysis_reason": "other", "note": "长" * 201}, 422),
    ):
        assert post(browser_a, f"{analysis}/feedback", revision=later.revision, **fields).status_code == status, fields

    # A page older than the record changes nothing.
    assert post(browser_a, f"{analysis}/feedback", revision=record.revision, part="category", analysis_reason="other").status_code == 409

    [feedback] = rows(app, JobAnalysisFeedback)
    assert (feedback.target, feedback.target_ref, feedback.reason, feedback.note) == ("requirement", requirement.split(":", 1)[1], "wrong", "合成备注")
    assert "招聘条件的判断：判断不对" in text_of(browser_a.get(analysis).text)


def test_a_kept_analysis_is_its_owners_alone(app, browser_a, browser_b, library, inline):
    url, result, record = kept_one(app, browser_a, library)
    analysis = f"/job-records/analyses/{record.id}"

    assert browser_b.get(analysis).status_code == 404
    assert post(browser_b, f"{analysis}/feedback", revision=record.revision, part="category", analysis_reason="wrong").status_code == 404
    assert browser_b.get(f"{analysis}/delete").status_code == 404
    assert post(browser_b, f"{analysis}/delete", expected_revision=record.revision).status_code == 404
    # Another session cannot even find the search to keep from it.
    response = browser_b.post(
        f"{url}/keep",
        {"csrf_token": browser_b.csrf_token(), "source": result.ref.source_id, "posting": result.ref.posting_id, "action": "analysis", "record": "", "revision": ""},
    )
    assert response.status_code == 404
    assert record.id not in browser_b.get("/job-records").text and counts(app)[:3] == (1, 2, 0)


def test_the_export_holds_the_analysis_as_kept_with_how_its_basis_stands(app, browser_a, library, inline):
    _, result, record = kept_one(app, browser_a, library)
    data, jobs = exported(browser_a)
    [entry] = data["job_records"]["analyses"]

    assert data["format_version"] == 14
    assert entry["content"] == record.content and entry["facts"] == record.facts and entry["basis"] == "依据和保存时一致"
    assert {source["kind"] for source in entry["sources"]} == {"experience", "preferences"}
    assert all(source["state"] == "current" for source in entry["sources"])
    quotes = [citation["quote"] for finding in record.content["findings"] for citation in finding["citations"]]
    assert "已保存的分析" in jobs and result.title in jobs and quotes and all(quote in jobs for quote in quotes)
    assert CATEGORY_WORDS[record.content["category"]] in jobs


# --- the readable export says what was judged, as the pages do (U3-D3b F1) -------------------------------------------
# Every conclusion code there is: a check that the readable export says each in the pages' words, never as a code.
CODES = re.compile(r"\b(supported|needs_confirmation|no_evidence|met|conflict|unknown|consistent|different|not_stated|hard_constraint|required|preferred|uncertain|practice|claim)\b")


def analysis_part(jobs: str, record: dict) -> str:
    """The readable export's part on one kept analysis: from its title to the next analysis's or the end of the
    section, wherever the layout puts them."""
    start = jobs.index("已保存的分析")
    begin = jobs.index(f"{record['facts']['title']}（{record['facts']['company']}）", start)
    ends = [jobs.find(marker, begin + 1) for marker in ("\n### ", "\n## ")]

    return jobs[begin : min([end for end in ends if end != -1], default=len(jobs))]


def said_with(part: str, text: str, words: str) -> bool:
    """Whether a line of the part says both: a requirement or a preference and what was judged of it."""
    return any(text in line and words in line for line in part.splitlines())


def assert_said_as_kept(part: str, content: dict) -> None:
    """Everything the kept analysis judged is in its readable part, in the pages' words (app.analysis_words, which the
    pages read too): each requirement with its group, the posting's words, the judgment and the reasons; each quote
    whole with what it was read with and where it stands; each preference with the judgment and the reasons."""
    shown = records.shown_analysis(content)
    versions = {source["item_id"]: source["version_number"] for source in content["sources"]}

    for finding in shown.findings:
        assert said_with(part, finding.text, page_words.conclusion_label(finding)), (finding.text, page_words.conclusion_label(finding))
        assert analysis_words.group_heading(finding.group) in part and finding.job_posting_quote in part
        assert all(reason in part for reason in page_words.reason_labels(finding.reasons)), finding.reasons

        for citation in finding.citations:
            assert citation.quote in part and all(passage in part for passage in citation.context)
            assert f"确认版 {versions[citation.item_id]}" in part and page_words.BASIS_LABELS[citation.basis] in part

    for finding in shown.preference_findings:
        assert said_with(part, finding.text, page_words.preference_label(finding)), (finding.text, page_words.preference_label(finding))
        assert all(reason in part for reason in page_words.reason_labels(finding.reasons)) and all(quote in part for quote in finding.job_quotes)

    assert not CODES.search(part), CODES.search(part)


def test_the_readable_export_says_every_judgment_as_kept_and_as_the_pages_do(app, browser_a, inline, clock):
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    # Python only: the postings asking for SQL too find no material for it.
    item = confirmed(browser_a, title="只写了 Python 的项目（合成）", actions="用 Python 清洗订单数据。", outcome="合成结果。")
    url = finished(browser_a, item)

    for result in stored(app, browser_a, url).finished.outcome.results:
        assert keep(browser_a, url, result, action="analysis").status_code == 303

    kept = rows(app, JobAnalysis)
    _, jobs = exported(browser_a)
    said = set()

    for record in kept:
        part = analysis_part(jobs, {"facts": record.facts})
        assert_said_as_kept(part, record.content)
        shown = records.shown_analysis(record.content)
        said |= {page_words.conclusion_label(finding) for finding in shown.findings}
        said |= {page_words.preference_label(finding) for finding in shown.preference_findings}

    # A conflict, no material found, and what the posting does not say: each said apart, never as one another.
    assert {"冲突", "目前未找到支持材料", "岗位未说明"} <= said, said

    # What is no longer kept is no longer exported: one analysis deleted and past its restore deadline.
    gone, *others = kept
    assert post(browser_a, f"/job-records/analyses/{gone.id}/delete", expected_revision=gone.revision).status_code == 303
    clock.advance(days=7)
    browser_a.sign_in("合成用户 A")
    _, jobs = exported(browser_a)

    for record in others:
        assert_said_as_kept(analysis_part(jobs, {"facts": record.facts}), record.content)

    assert jobs.count("岗位编号") == len(others)


def test_every_judgment_code_is_said_in_the_pages_words_in_the_readable_export(clock):
    """Each code the analysis may keep, in each group, read out of a kept analysis's content alone."""
    citation = {
        "item_id": "11111111-2222-3333-4444-555555555555", "version_id": "v", "category": "project", "field": "actions",
        "quote": "用 Python 清洗数据", "basis": "plan", "context": ["打算以后用 Python 清洗数据"], "flags": ["plan"],
    }
    judged = [
        (group, code)
        for group, codes in (("hard_constraint", analysis_words.CONDITION_LABELS), ("required", analysis_words.EVIDENCE_LABELS),
                             ("preferred", analysis_words.EVIDENCE_LABELS), ("uncertain", analysis_words.EVIDENCE_LABELS))
        for code in codes
    ]
    # Texts of their own, numbered: none holds a code, so any code in the export is the export's.
    findings = [
        {"requirement_id": f"r{number}", "group": group, "text": f"合成条件{number}号", "job_posting_quote": f"合成原文{number}号",
         "conclusion": code, "reasons": ["plan", "flag:plan"] if code == "needs_confirmation" else ["no_material"], "citations": [citation]}
        for number, (group, code) in enumerate(judged, start=1)
    ]
    preferences = [
        {"preference_id": f"p{number}", "field": field, "text": f"合成偏好{number}号", "conclusion": code, "job_quotes": [f"合成岗位原文{number}号"],
         "reasons": ["not_stated"] if code == "not_stated" else ["conflicts"], "firm": code == "conflict"}
        for number, (code, field) in enumerate(
            zip(analysis_words.PREFERENCE_LABELS, ("cities_ideal", "work_content_ok", "work_content_no", "less_content", "must_haves")), start=1
        )
    ]
    content = {
        "version": 1, "category": "conflict", "first_checks": [], "notes": [], "findings": findings, "preferences": "compared",
        "preference_findings": preferences,
        "sources": [{"item_id": citation["item_id"], "version_id": "v", "category": "project", "version_number": 3, "fields": ["actions"]}],
    }
    entry = {
        "kept_as": IN_USE, "restore_deadline": None, "basis": "依据已永久删除，下面只剩保存时的原话", "posting_id": "SYN-1",
        "analysed_at": None, "saved_at": None, "feedback": [], "content": content,
        "facts": {"title": "合成岗位", "company": "合成公司", "source_name": "合成来源", "check": {"status": "open", "answered_at": None}},
    }
    jobs = jobs_markdown(JobExport(analyses=[entry]), clock.now())
    part = analysis_part(jobs, entry)

    assert_said_as_kept(part, content)
    # The page module says them from the same words: one mapping, not two.
    assert page_words.conclusion_label is analysis_words.conclusion_label and page_words.REASONS is analysis_words.REASONS
    assert "经历 11111111" in part and "项目/作品" in part and "我的贡献" in part and "计划" in part


def test_the_readable_export_names_preferences_as_the_pages_do():
    for field, (topic, _, stance, _) in PREFERENCE_TOPICS.items():
        assert preference_name(field) == (f"{topic}（{stance}）" if stance else topic), field


def test_the_category_words_of_the_library_are_the_searchs():
    assert CATEGORY_WORDS == {category.value: words for category, words in CATEGORY_LABELS.items()}


# --- the review: what it says is kept, and the notice it was made under ---------------------------------------------------


def test_the_review_says_nothing_is_kept_by_itself_and_what_may_be_kept(app, browser_a, library, make_config, clock):
    page = text_of(browser_a.get(search_of(review(browser_a, sources=[library], use_preferences=True))).text)

    assert "不会自动保存。查找结束后，你可以自己把岗位、它的分析（含所选经历的原话和分析时读的那句话）或这次查找的摘要保存到岗位记录" in page
    assert "删除后在回收站留 7 天" in page and "结果不会自动保存，只有你点了保存才会存" in page
    assert "不会存进经历库" not in page and "不保存结果" not in page

    # A set that lets keep nothing says so, as before.
    application = create_app(make_config(), clock=clock, discovery_sources=replace(SYNTHETIC_SOURCES, keep=frozenset(), keep_analyses=frozenset()))

    with TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)):
        browser = Browser.signed_in(application, "合成用户 A")
        item = confirmed(browser, **PROJECT)
        page = text_of(browser.get(search_of(review(browser, sources=[item]))).text)

    assert "不会存进经历库" in page and "不保存结果" in page and "不会自动保存" not in page


def test_a_review_made_under_another_notice_is_never_confirmed(app, browser_a, library, inline, monkeypatch):
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    # Version 6 since U3-D3c (the review says how many skipped postings are not listed again).
    assert stored(app, browser_a, url).confirmation.notice_version == "discovery-preview-7"
    page = browser_a.get(url).text
    monkeypatch.setattr(discovery_web, "NOTICE_VERSION", "discovery-preview-test")

    # The old page's own fingerprint matches its own task: still not confirmed, and nothing started.
    response = confirm(browser_a, url, fingerprint=form_for(page, f"{url}/confirm").fields["fingerprint"])
    assert response.status_code == 303 and response.headers["location"].endswith("?notice=service_changed")
    again = response.headers["location"].split("?")[0]
    assert again != url and task_id(url) not in app.state.discovery.store._tasks
    fresh = stored(app, browser_a, again)
    assert fresh.stage == "review" and fresh.confirmation.notice_version == "discovery-preview-test" and fresh.finished is None
    assert "原来的确认已经作废，没有开始" in text_of(browser_a.get(response.headers["location"]).text)

    # Confirmed under the notice of now, it starts.
    assert confirm(browser_a, again).status_code == 303
    assert stored(app, browser_a, again).finished is not None
