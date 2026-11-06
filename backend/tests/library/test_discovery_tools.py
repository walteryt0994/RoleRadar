"""U3-A1: the search tools over frozen synthetic postings, and the analyses they start.

Everything stays in this process: the sources are the synthetic ones
(app.discovery.synthetic), the analysis service is the app's own simulation
or a script, and the one gate opened here has synthetic terms and a
synthetic provider. These tests prove the tools' contracts and rules, not
any real source, search service or model. All material is synthetic.

The A1-R1 part keeps, as regressions, what the A1 acceptance found handled
unsafely (its report, section 8, F1-F5), next to what must still hold: a
real repost is analysed once, and a search that found nothing suitable may
still say so. The A1-R2 and A1-R3 parts do the same for what the re-checks
found left (sections 10 and 12), and the A2-0 part for a source giving back
another source's records (A1 acceptance, section 14; A2 pre-check, 2.3).
"""
import hashlib
import json
import threading
from dataclasses import replace
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.ai_provider import AIProvider, ProviderIdentity
from app.analysis_contracts import Recipient, SelectedPreference, SelectedSource, TaskApproval, task_fingerprint
from app.discovery import synthetic, tools
from app.discovery.contracts import (
    NOT_PERSONALISED_WHY,
    RECOMMENDED,
    Category,
    CheckStatus,
    DimensionScope,
    EndReason,
    EvidenceLevel,
    Limits,
    Others,
    Outcome,
    ParentConfirmation,
    PostingRef,
    SearchApproval,
    SearchScope,
    Unlisted,
    search_fingerprint,
)
from app.discovery.sources import Access, Permission, Query, Register, SourceEntry, SourceError, SourceTimeout
from app.discovery.synthetic import CAREERS, LEADS, SyntheticSource
from app.discovery.tools import (
    StartRefused,
    analyze_posting,
    check_posting,
    child_request,
    derive_approval,
    finish,
    get_posting,
    search_postings,
    start,
)
from app.job_analysis.simulation import RECIPIENT, SIMULATION_MODEL, SimulatedProvider
from app.library.content import PREFERENCES, parse_experience_form
from app.library.service import active_item, save_draft
from app.live_gate import ApprovalTerms, Blocked, InputBound, LiveBlocked, PriceBasis, open_gate
from app.outbound_review import review
from analysis_helpers import ScriptedProvider, cite, confirmed, parsed_posting, saved_preferences
from job_helpers import RecordingProvider

SENTINEL = "没有选择发送的栏目-u3a1-7c4e"
PROJECT_ACTIONS = "用 Python 清洗订单数据，并用 SQL 汇总每周销量。"
NOTICE = "discovery-test-1"
SALES = SelectedPreference("work_content_no", "销售")
START_DATE = SelectedPreference("must_haves", "最早 2026 年 7 月入职")
SCOPE = SearchScope(
    city=DimensionScope(("北京", "上海"), Others.EXCLUDED, ("北京",), ("深圳",)),
    job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope(("数据分析",), Others.INCLUDED, ("数据分析",)),
    targets=("数据分析实习生",),
)
WITHIN = dict(job_type="实习", direction="数据分析")


def ref(posting_id: str, source: str = CAREERS) -> PostingRef:
    return PostingRef(source, posting_id)


@pytest.fixture
def library(database, owners, clock):
    owner, other = owners
    project = confirmed(
        database, owner, clock, "project", private_note=f"私有备注{SENTINEL}",
        title="校园订单看板（合成）", actions=PROJECT_ACTIONS, outcome="看板在课程展示中使用。", reflection=f"反思{SENTINEL}",
    )
    education = confirmed(database, owner, clock, "education", school="合成大学", major="数据科学", graduation="2027 年 6 月（预计）")
    preferences = saved_preferences(
        database, owner, clock, cities_ideal="北京", cities_ok="上海", cities_no="深圳", job_types_ideal="实习",
        directions_ideal="数据分析", work_content_no="销售", must_haves="最早 2026 年 7 月入职",
    )

    return SimpleNamespace(owner=owner, other=other, project=project, education=education, preferences=preferences)


def confirmation_for(
    library, *, selection=None, preferences=(SALES,), recipient=RECIPIENT, scope=SCOPE, limits=Limits(), sources=synthetic.GRANTS
) -> ParentConfirmation:
    """What the user confirmed for a search: two experiences and some of their fields unless said, the
    preferences given, and both synthetic sources as they are registered."""
    if selection is None:
        selection = (
            SelectedSource(*library.project, ("title", "actions", "outcome")),
            SelectedSource(*library.education, ("school", "major", "graduation")),
        )

    return ParentConfirmation(
        owner_id=library.owner,
        scope=scope,
        selection=selection,
        preferences=preferences,
        recipient=recipient,
        sources=sources,
        notice_version=NOTICE,
        limits=limits,
    )


def begin(database, clock, confirmation, provider=None, *, register=None, cancel=None, monotonic=lambda: 0.0):
    """A search of exactly this confirmation, approved as a test approves it (not a user's permission)."""
    return start(
        database,
        confirmation,
        SearchApproval(search_fingerprint(confirmation)),
        provider or RecordingProvider(),
        register=register or synthetic.register(),
        clock=clock,
        cancel=cancel,
        monotonic=monotonic,
    )


def ready(run, *posting_ids, city="北京"):
    """Search, read and check these official postings, as a search does before analysing them."""
    assert search_postings(run, CAREERS, "数据分析 实习生", city=city, **WITHIN).outcome is Outcome.DONE
    assert get_posting(run, [ref(posting_id) for posting_id in posting_ids]).outcome is Outcome.DONE
    assert check_posting(run, [ref(posting_id) for posting_id in posting_ids]).outcome is Outcome.DONE


def edit(database, owner, item_id, clock, **fields):
    """Save new content: the experience waits for review again."""
    with database.transaction() as db:
        item = active_item(db, owner, item_id)
        save_draft(db, owner, item_id, item.revision, parse_experience_form({**item.content, **fields}, item.category), clock.now())


def edit_preferences(database, library, clock, **fields):
    """Save new words in the job preferences."""
    with database.transaction() as db:
        item = active_item(db, library.owner, library.preferences, kinds=(PREFERENCES,))
        form = parse_experience_form({**item.content, **fields}, PREFERENCES)
        save_draft(db, library.owner, library.preferences, item.revision, form, clock.now())


# --- the case reported for A1 --------------------------------------------------------


def test_one_frozen_search_tells_grounded_results_from_leads_and_from_what_is_left_out(database, library, clock):
    """Three searches, ten reads, six checks and four analyses over the frozen synthetic postings."""
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    steps = [
        search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN),
        search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN),
        search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN),
        get_posting(run, map(ref, ("c-bj-analyst", "c-bj-sales", "c-choice", "c-two-cities", "c-address"))),
        get_posting(run, map(ref, ("c-sh-analyst", "c-injection", "c-no-requirements", "c-unreachable", "c-closed"))),
        check_posting(run, map(ref, ("c-bj-analyst", "c-sh-analyst", "c-bj-sales", "c-address", "c-closed"))),
    ]

    assert [step.outcome for step in steps] == [Outcome.DONE] * 6

    unanswered = check_posting(run, [ref("c-unreachable")])
    blocked = analyze_posting(run, ref("c-address"))
    analysed = [analyze_posting(run, ref(posting_id)) for posting_id in ("c-bj-analyst", "c-sh-analyst", "c-bj-sales")]

    assert unanswered.outcome is Outcome.FAILED
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()
    # Nothing of the posting with an office address went out, and it used no analysis.
    assert (blocked.outcome, blocked.reason) == (Outcome.BLOCKED, "address")
    assert [result.outcome for result in analysed] == [Outcome.DONE] * 3
    assert (len(provider.prompts), run.analyses, run.reads, run.checks) == (6, 3, 10, 6)
    # All the next decision saw passes the outbound rules: no address, no contact details (F1).
    assert all(review(step.summary) == () for step in [*steps, unanswered, blocked, *analysed])
    assert outcome.personalised

    # Grounded: analysed official postings, a conflict last and never among the recommended.
    assert [(result.ref.posting_id, result.category) for result in outcome.results] == [
        ("c-bj-analyst", Category.STRONG),
        ("c-sh-analyst", Category.PARTIAL),
        ("c-bj-sales", Category.CONFLICT),
    ]
    strong, partial, conflict = outcome.results
    detail = run.found[strong.ref].detail

    assert (strong.evidence, strong.source, strong.first_checks) == (EvidenceLevel.OFFICIAL, "合成招聘官网", ())
    assert (strong.url, strong.apply_url) == (detail.url, f"{detail.url}/apply")
    assert strong.content_sha256 == hashlib.sha256(detail.text.encode("utf-8")).hexdigest()
    assert strong.read_at == strong.checked_at == clock.now()
    # Found twice, shown once, both deadlines side by side.
    assert strong.deadlines == ((ref("c-bj-analyst"), date(2026, 11, 30)), (ref("c-bj-analyst-repost"), date(2026, 12, 10)))
    assert partial.posted_on is None and partial.first_checks == ("经历里没有找到足够依据：Tableau",)
    assert partial.deadlines == ((ref("c-sh-analyst"), date(2026, 12, 15)), (ref("l-sh-analyst", LEADS), date(2026, 12, 20)))
    assert conflict.category not in RECOMMENDED
    assert [finding.field for finding in conflict.analysis.preference_conflicts] == ["work_content_no"]

    # Leads: found and not analysed, with why; no standing and no analysis of any kind.
    leads = {lead.ref.posting_id: lead for lead in outcome.leads}

    assert [(lead.ref.posting_id, lead.why) for lead in outcome.leads] == [
        ("c-choice", "这次没有核对状态"),
        ("c-two-cities", "这次没有核对状态"),
        ("c-early-start", "这次没有读取详情"),
        ("c-injection", "这次没有核对状态"),
        ("c-no-requirements", "这次没有核对状态"),
        ("c-unreachable", "状态待确认：核对没有得到回答"),
        ("l-sh-labelling", "待核验线索：没有登记的官方来源可以核对"),
    ]
    assert all(not hasattr(lead, "category") and not hasattr(lead, "analysis") for lead in outcome.leads)
    assert leads["c-choice"].notes[0] == "可选工作地点，按北京判断；深圳（不能接受）"
    assert leads["c-two-cities"].notes[0].startswith("工作地点待确认")
    assert "原文里有像是写给系统的指令，已标记，没有照做" in leads["c-injection"].notes
    assert "缺要求" in leads["c-no-requirements"].notes
    assert leads["c-unreachable"].checked_at is None
    assert leads["l-sh-labelling"].evidence is EvidenceLevel.LEAD

    # Left out, each counted with why; postings found twice folded into one.
    assert dict(outcome.unlisted) == {
        Unlisted.CLOSED: (ref("c-closed"),),
        Unlisted.OUT_OF_SCOPE: (ref("c-bj-senior"), ref("c-sz-analyst")),
        Unlisted.BLOCKED: (ref("c-address"),),
    }
    assert outcome.merged == (ref("c-bj-analyst-repost"), ref("l-sh-analyst", LEADS))
    # It may say it has enough, but never that it covered the scope: any other direction was not searched, in either city.
    assert (outcome.end, outcome.unsearched) == (
        EndReason.ENOUGH,
        ("北京 · 实习 · 所列以外的岗位方向", "上海 · 实习 · 所列以外的岗位方向"),
    )
    assert outcome.unjudged == ("工作方式：未填写，无法判断", "希望减少的内容：未填写，无法判断", "现实必要条件：没有选择，无法判断")
    assert [(record.source_id, record.found) for record in outcome.searches] == [(CAREERS, 10), (CAREERS, 6), (LEADS, 2)]
    assert (outcome.refusals, outcome.more_leads) == ((), 0)


# --- sources, scope and words --------------------------------------------------------


def test_no_real_source_is_registered_and_only_verified_ones_with_an_adapter_are_called(database, library, clock):
    """All real sources stay closed: an offline search knows synthetic sources only."""
    assert {entry.access for entry in synthetic.register().entries} == {Access.SYNTHETIC}

    careers = SyntheticSource(CAREERS, synthetic.POSTINGS)
    entries = tuple(
        SourceEntry(source_id, name, access, permission, EvidenceLevel.OFFICIAL, "测试", "2026-11-06")
        for source_id, name, access, permission in (
            ("prohibited", "禁止的来源", Access.LOCAL_FETCH, Permission.PROHIBITED),
            ("unverified", "未核实的来源", Access.SEARCH_SERVICE, Permission.UNVERIFIED),
            ("no-adapter", "没有适配的来源", Access.SYNTHETIC, Permission.VERIFIED),
        )
    )
    register = Register(entries, {"prohibited": careers, "unverified": careers})

    for source_id, reason in (
        ("prohibited", "禁止的来源禁止调用"),
        ("unverified", "未核实的来源未核实，不能调用"),
        ("no-adapter", "没有适配的来源没有可用的读取方式"),
        ("unregistered", "来源没有登记"),
    ):
        result = search_postings(begin(database, clock, confirmation_for(library), register=register), source_id, "数据分析", city="北京", **WITHIN)

        assert (result.outcome, result.reason) == (Outcome.REFUSED, reason)

    assert careers.calls == []

    with pytest.raises(ValueError):
        Register(entries, {"unregistered": careers})


def test_a_search_outside_the_scope_is_refused_and_two_refusals_end_the_search(database, library, clock):
    """Case 3: Shenzhen cannot be accepted, and nothing is asked of any source for it."""
    run = begin(database, clock, confirmation_for(library))
    careers = run.register.adapter(CAREERS)

    assert search_postings(run, CAREERS, "数据分析", city="深圳", **WITHIN).reason == "“深圳”是不能接受的城市"
    assert search_postings(run, CAREERS, "数据分析", city=None, **WITHIN).reason == "本次只看所列的城市，检索时必须指定一个"
    assert run.ended is EndReason.REFUSED

    after = search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN)

    assert (after.outcome, after.reason) == (Outcome.STOPPED, "refused")
    assert careers.calls == []
    assert run.outcome().refusals == ("“深圳”是不能接受的城市", "本次只看所列的城市，检索时必须指定一个")


@pytest.mark.parametrize(
    ("keywords", "reason"),
    [
        ("数据分析 hr-synthetic@example.invalid", "检索词只能是岗位、技能、城市这类条件词，不能带标点或整句话"),
        ("数据分析 13800138000", "检索词里有像联系方式、证件号、地址或保密内容的文字"),
        (PROJECT_ACTIONS, "检索词只能是岗位、技能、城市这类条件词，不能带标点或整句话"),
        ("数据分析" * 11, "检索词最多 40 个字"),
        ("   ", "检索词是空的"),
    ],
)
def test_search_words_are_words_of_conditions_only(database, library, clock, keywords, reason):
    run = begin(database, clock, confirmation_for(library))
    result = search_postings(run, CAREERS, keywords, city="北京", **WITHIN)

    assert (result.outcome, result.reason) == (Outcome.REFUSED, reason)
    assert run.register.adapter(CAREERS).calls == []


def test_a_search_out_of_searches_says_what_it_left_and_never_that_nothing_was_found(database, library, clock):
    """Case 4: only Beijing searched before the searches ran out."""
    run = begin(database, clock, confirmation_for(library, limits=Limits(searches=1)))

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE
    assert search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN).reason == "检索次数已用完（1 次）"
    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.DONE

    outcome = run.outcome()

    assert outcome.end is EndReason.LIMIT
    assert outcome.unsearched == ("北京 · 实习 · 所列以外的岗位方向", "上海 · 实习 · 数据分析", "上海 · 实习 · 所列以外的岗位方向")


def test_nothing_suitable_may_be_said_only_once_the_whole_scope_was_searched_and_every_posting_settled(database, library, clock):
    """Not searched yet, not analysed yet and nothing suitable are three things (A1-R1, F3)."""
    run = begin(database, clock, confirmation_for(library, limits=Limits(refusals=3)))

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE

    refused = finish(run, EndReason.SCOPE_DONE)

    assert (refused.outcome, refused.reason) == (
        Outcome.REFUSED,
        "范围里还有 3 个组合没查：北京 · 实习 · 所列以外的岗位方向；上海 · 实习 · 数据分析；上海 · 实习 · 所列以外的岗位方向",
    )
    assert run.ended is None
    # Any direction, in each city: every combination the scope holds.
    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", job_type="实习").outcome is Outcome.DONE
    assert search_postings(run, CAREERS, "数据分析 实习生", city="上海", job_type="实习").outcome is Outcome.DONE

    # The scope is covered, but the postings found in it were never analysed: not "nothing suitable".
    unsettled = finish(run, EndReason.NO_RESULTS)

    assert (unsettled.outcome, unsettled.reason) == (
        Outcome.REFUSED,
        "还有 11 个岗位没有结论（没有分析、待核验，或者分析没有完成），不能说没有合适结果",
    )
    assert finish(run, EndReason.SCOPE_DONE).outcome is Outcome.DONE

    outcome = run.outcome()

    # Ten listed, the eleventh counted (Limits.leads).
    assert (outcome.end, outcome.unsearched, outcome.results) == (EndReason.SCOPE_DONE, (), ())
    assert (len(outcome.leads), outcome.more_leads) == (10, 1)


def test_nothing_suitable_may_be_said_when_all_the_scope_held_was_out_of_it(database, library, clock):
    """Guangzhou only: the source gives only the two postings it lists for every search, and both are out."""
    scope = SearchScope(
        city=DimensionScope(("广州",), Others.EXCLUDED, (), ("深圳",)),
        job_type=DimensionScope(("实习",), Others.EXCLUDED),
        direction=DimensionScope(("数据分析",), Others.EXCLUDED),
        targets=("数据分析实习生",),
    )
    run = begin(database, clock, confirmation_for(library, scope=scope))

    assert search_postings(run, CAREERS, "数据分析 实习生", city="广州", **WITHIN).outcome is Outcome.DONE
    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.DONE

    outcome = run.outcome()

    assert (outcome.end, outcome.unsearched, outcome.results, outcome.leads) == (EndReason.NO_RESULTS, (), (), ())
    assert dict(outcome.unlisted) == {Unlisted.OUT_OF_SCOPE: (ref("c-bj-senior"), ref("c-sz-analyst"))}


def test_a_source_that_does_not_answer_is_reported_once_and_never_asked_again(database, library, clock):
    register = synthetic.register()
    careers = register.adapter(CAREERS)
    careers.faults[("search", None)] = SourceTimeout("synthetic")
    run = begin(database, clock, confirmation_for(library), register=register)
    result = search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN)

    assert result.outcome is Outcome.FAILED and "没有在时限内回答" in result.summary
    assert (len(careers.calls), len(run.searches), run.searched) == (1, 1, set())
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE
    assert run.outcome().end is EndReason.SOURCES_UNAVAILABLE


# --- checks, closed, leads and the same posting twice -------------------------------


def test_a_check_without_an_answer_never_refreshes_the_time_and_is_not_sent_again(database, library, clock):
    run = begin(database, clock, confirmation_for(library, limits=Limits(refusals=3)))
    careers = run.register.adapter(CAREERS)
    ready(run, "c-bj-analyst")
    found = run.found[ref("c-bj-analyst")]

    assert (found.status, found.checked_at) == (CheckStatus.OPEN, clock.now())

    # A posting whose last check was answered may be checked again, and the answer refreshes the time.
    clock.advance(hours=1)

    assert check_posting(run, [ref("c-bj-analyst")]).outcome is Outcome.DONE

    answered = found.checked_at

    assert answered == clock.now()

    clock.advance(hours=1)
    careers.faults[("check", "c-bj-analyst")] = SourceTimeout("synthetic")
    again = check_posting(run, [ref("c-bj-analyst")])

    assert again.outcome is Outcome.FAILED and "核对时间不更新" in again.summary
    assert (found.status, found.checked_at) == (CheckStatus.UNKNOWN, answered)
    assert analyze_posting(run, ref("c-bj-analyst")).reason == "岗位状态待确认（核对没有得到回答），先不分析"

    # The same check that failed is not sent again in this search, even once the site would answer (A1-R1, F2).
    del careers.faults[("check", "c-bj-analyst")]
    checks = len(careers.calls)
    retried = check_posting(run, [ref("c-bj-analyst")])

    assert (retried.outcome, retried.reason) == (Outcome.REFUSED, "[synthetic-careers/c-bj-analyst] 上次核对没有得到回答，这次查找不再重试")
    assert len(careers.calls) == checks
    assert (found.status, found.checked_at) == (CheckStatus.UNKNOWN, answered)


def test_a_closed_posting_is_never_analysed_or_shown(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    ready(run, "c-bj-analyst", "c-closed")

    assert analyze_posting(run, ref("c-closed")).reason == "岗位已关闭，不分析"
    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE

    # Closed after its analysis: no longer shown either.
    run.register.adapter(CAREERS).closed.add("c-bj-analyst")

    assert check_posting(run, [ref("c-bj-analyst")]).outcome is Outcome.DONE
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()

    assert outcome.results == ()
    assert dict(outcome.unlisted)[Unlisted.CLOSED] == (ref("c-bj-analyst"), ref("c-closed"))


def test_a_lead_is_analysed_only_through_the_official_posting_it_points_to(database, library, clock):
    run = begin(database, clock, confirmation_for(library))

    assert search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN).outcome is Outcome.DONE
    assert get_posting(run, [ref("l-sh-analyst", LEADS)]).outcome is Outcome.DONE
    assert analyze_posting(run, ref("l-sh-analyst", LEADS)).reason == (
        "待核验线索不能直接分析：先读取它指向的官方岗位 [synthetic-careers/c-sh-analyst]"
    )
    # Its official posting may be read without being searched for: the lead named it.
    assert get_posting(run, [ref("c-sh-analyst")]).outcome is Outcome.DONE
    assert check_posting(run, [ref("c-sh-analyst")]).outcome is Outcome.DONE
    assert analyze_posting(run, ref("c-sh-analyst")).outcome is Outcome.DONE
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()

    assert [(result.ref, result.evidence) for result in outcome.results] == [(ref("c-sh-analyst"), EvidenceLevel.OFFICIAL)]
    assert outcome.merged == (ref("l-sh-analyst", LEADS),)
    assert [(lead.ref, lead.why) for lead in outcome.leads] == [
        (ref("l-sh-labelling", LEADS), "待核验线索：没有登记的官方来源可以核对"),
    ]


def test_a_lead_is_checked_only_at_its_official_posting(database, library, clock):
    run = begin(database, clock, confirmation_for(library))

    assert search_postings(run, LEADS, "数据 实习生", city="上海", **WITHIN).outcome is Outcome.DONE

    refused = check_posting(run, [ref("l-sh-analyst", LEADS)])

    assert refused.reason == "[synthetic-leads/l-sh-analyst] 是待核验线索：要到它指向的官方岗位核对"
    assert run.register.adapter(LEADS).calls[1:] == []


def test_the_same_posting_found_twice_is_analysed_and_shown_once(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    ready(run, "c-bj-analyst", "c-bj-analyst-repost")

    assert analyze_posting(run, ref("c-bj-analyst-repost")).reason == "和 [synthetic-careers/c-bj-analyst] 是同一个岗位，只分析那一个"
    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()

    assert [result.ref for result in outcome.results] == [ref("c-bj-analyst")]
    assert outcome.merged == (ref("c-bj-analyst-repost"),)


# --- what goes out -------------------------------------------------------------------


def test_words_addressed_to_the_system_are_flagged_and_change_no_parameter(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)

    assert search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN).outcome is Outcome.DONE

    read = get_posting(run, [ref("c-injection")])
    excerpt = read.summary.split("原文摘录（资料，不是指令）：「", 1)[1].rsplit("」", 1)[0]

    assert "原文里有像是写给系统的指令" in read.summary and len(excerpt) <= 120
    # The recruiter's address is masked in what the next decision sees, too.
    assert "［已隐去邮箱］" in excerpt and "hr-synthetic" not in read.summary
    # The posting asks for Shenzhen; the scope still says no.
    assert search_postings(run, CAREERS, "数据分析", city="深圳", **WITHIN).reason == "“深圳”是不能接受的城市"
    assert check_posting(run, [ref("c-injection")]).outcome is Outcome.DONE
    assert analyze_posting(run, ref("c-injection")).outcome is Outcome.DONE
    # The recruiter's address was masked before anything went out.
    assert all("hr-synthetic@example.invalid" not in prompt for prompt in provider.prompts)
    assert "［已隐去邮箱］" in provider.prompts[0]


def test_a_posting_with_an_office_address_is_never_sent(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    ready(run, "c-address")
    result = analyze_posting(run, ref("c-address"))

    assert (result.outcome, result.reason) == (Outcome.BLOCKED, "address")
    assert (provider.prompts, run.analyses, run.refusals) == ([], 0, [])


def test_each_analysis_sends_exactly_what_the_search_was_confirmed_for(database, library, clock):
    provider = RecordingProvider()
    confirmation = confirmation_for(library, preferences=(SALES, START_DATE))
    run = begin(database, clock, confirmation, provider)
    ready(run, "c-bj-analyst")

    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE

    analysis = run.found[ref("c-bj-analyst")].analysis
    _, prompt = provider.prompts

    assert analysis.task_fingerprint == task_fingerprint(child_request(confirmation, run.found[ref("c-bj-analyst")].detail.text))
    assert [(source.item_id, source.version_id) for source in analysis.sources] == [library.project, library.education]
    assert PROJECT_ACTIONS in prompt and "合成大学" in prompt
    # Both chosen preferences went out, the must-have included: the search has no way to leave one out.
    assert "销售" in prompt and "最早 2026 年 7 月入职" in prompt
    # Neither unchosen fields nor the scope's preferences: those are rules here, never sent.
    assert all(SENTINEL not in sent and "深圳" not in sent for sent in provider.prompts)


@pytest.mark.parametrize(
    "change", ["owner", "another_source", "another_version", "more_fields", "preference_words", "preference_left_out", "recipient", "notice", "posting"]
)
def test_an_analysis_is_approved_only_for_what_the_search_was_confirmed_for(library, change):
    confirmation = confirmation_for(library, preferences=(SALES, START_DATE))
    request = child_request(confirmation, "数据分析实习生（合成岗位，不是真实招聘）")
    first, *rest = request.selection

    assert derive_approval(confirmation, request) == TaskApproval(task_fingerprint(request))

    changed = {
        "owner": dict(owner_id=library.other),
        "another_source": dict(selection=request.selection + (SelectedSource("合成经历", "v1"),)),
        "another_version": dict(selection=(replace(first, version_id="older"), *rest)),
        "more_fields": dict(selection=(replace(first, fields=None), *rest)),
        "preference_words": dict(preferences=(SALES, replace(START_DATE, text="随时可以入职"))),
        "preference_left_out": dict(preferences=(SALES,)),
        "recipient": dict(recipient=Recipient("synthetic", "synthetic-model")),
        "notice": dict(notice_version="discovery-test-2"),
        "posting": dict(job_posting="   "),
    }[change]

    assert derive_approval(confirmation, replace(request, **changed)) is None


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ("approval", "这次查找没有经过确认，或者确认的不是这次查找"),
        ("undecided", "城市：所列以外的是否也查还没有选择（本次不限，或只看所列的）"),
        ("scope_preference", "城市、求职类型和岗位方向由规则按查找范围核对，不随分析发送"),
        ("recipient", "分析服务不是确认里写明的接收方"),
        ("experience", "所选经历已经修改、重新确认或删除"),
        ("preference", "所选偏好已经修改"),
        ("no_source", "这次查找没有确认可以调用的岗位来源"),
        ("preferences_only", "没有选择经历时不做个性化分析，偏好也不会发送：请不要选择偏好"),
        ("contact_in_material", "所选经历或偏好里有疑似联系方式、证件号、地址或保密内容，不能随分析发送"),
    ],
)
def test_a_search_starts_only_from_a_confirmation_that_holds(database, library, clock, change, problem):
    confirmation = confirmation_for(library)
    provider = RecordingProvider()

    if change == "undecided":
        confirmation = replace(confirmation, scope=replace(SCOPE, city=replace(SCOPE.city, others=Others.UNDECIDED)))
    elif change == "scope_preference":
        confirmation = replace(confirmation, preferences=(SelectedPreference("cities_no", "深圳"),))
    elif change == "recipient":
        provider = ScriptedProvider(provider="synthetic", model="synthetic-model")
    elif change == "experience":
        edit(database, library.owner, library.project[0], clock, actions="换了一种说法。")
    elif change == "preference":
        confirmation = replace(confirmation, preferences=(replace(SALES, text="销售和客服"),))
    elif change == "no_source":
        confirmation = replace(confirmation, sources=())
    elif change == "preferences_only":
        confirmation = replace(confirmation, selection=())
    elif change == "contact_in_material":
        reachable = confirmed(database, library.owner, clock, "project", title="合成项目", actions="项目联系人电话 13800138000")
        confirmation = replace(confirmation, selection=(SelectedSource(*reachable, ("actions",)),))

    approval = SearchApproval(search_fingerprint(replace(confirmation, notice_version="another") if change == "approval" else confirmation))

    with pytest.raises(StartRefused) as refused:
        start(database, confirmation, approval, provider, register=synthetic.register(), clock=clock)

    assert problem in refused.value.problems
    assert getattr(provider, "prompts", []) == []


# --- cancelling, changed material, time and the gate ---------------------------------


def test_a_cancelled_search_starts_no_analysis_and_sends_nothing(database, library, clock):
    cancel = threading.Event()
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider, cancel=cancel)
    ready(run, "c-bj-analyst", "c-bj-sales")
    calls = len(run.register.adapter(CAREERS).calls)
    cancel.set()

    # No step runs once it is cancelled, not only the analyses, which check it themselves as well.
    assert check_posting(run, [ref("c-bj-analyst")]).reason == "cancelled"
    assert len(run.register.adapter(CAREERS).calls) == calls

    for posting_id in ("c-bj-analyst", "c-bj-sales"):
        result = analyze_posting(run, ref(posting_id))

        assert (result.outcome, result.reason) == (Outcome.STOPPED, "cancelled")

    assert (provider.prompts, run.ended, run.outcome().results) == ([], EndReason.CANCELLED, ())


def test_a_cancellation_during_an_analysis_counts_what_went_out_and_starts_nothing_more(database, library, clock):
    cancel = threading.Event()

    class Cancelling(RecordingProvider):
        def generate_text(self, prompt, **options):
            answer = super().generate_text(prompt, **options)
            cancel.set()

            return answer

    provider = Cancelling()
    run = begin(database, clock, confirmation_for(library), provider, cancel=cancel)
    ready(run, "c-bj-analyst", "c-bj-sales")

    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.STOPPED
    assert analyze_posting(run, ref("c-bj-sales")).outcome is Outcome.STOPPED
    assert (len(provider.prompts), run.analyses, run.ended) == (1, 1, EndReason.CANCELLED)
    assert dict(run.outcome().unlisted)[Unlisted.INCOMPLETE] == (ref("c-bj-analyst"),)


def test_a_change_of_the_chosen_experiences_ends_the_search_before_another_analysis(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    ready(run, "c-bj-analyst", "c-bj-sales")
    edit(database, library.owner, library.project[0], clock, actions="换了一种说法。")
    changed = analyze_posting(run, ref("c-bj-analyst"))

    assert (changed.outcome, changed.reason, run.ended) == (Outcome.STOPPED, "selection_invalid", EndReason.SOURCES_CHANGED)
    assert analyze_posting(run, ref("c-bj-sales")).outcome is Outcome.STOPPED
    assert (provider.prompts, run.analyses) == ([], 0)


def test_a_search_out_of_time_does_nothing_more(database, library, clock):
    now = [0.0]
    run = begin(database, clock, confirmation_for(library), monotonic=lambda: now[0])

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE

    now[0] = Limits().seconds
    late = search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN)

    assert (late.outcome, late.reason, run.ended) == (Outcome.STOPPED, "timeout", EndReason.TIMEOUT)
    assert len(run.register.adapter(CAREERS).calls) == 1


SYNTHETIC_RECIPIENT = Recipient("synthetic", "synthetic-model", "https://synthetic.invalid/v1")
PRICES = PriceBasis(Decimal("2"), None, None, Decimal("8"), Decimal("2"), "synthetic", "2026-11-06", True)
BOUND = InputBound("synthetic-bytes", lambda prompt, output_format: len(prompt.encode("utf-8")) + 50, "synthetic", "2026-11-06", True)


def gate_terms() -> ApprovalTerms:
    """Made-up terms: three requests of the analysis purpose, synthetic prices, nothing real."""
    return ApprovalTerms(
        approval_id="synthetic-discovery-a1",
        purposes=frozenset({"analysis"}),
        provider="synthetic",
        model="synthetic-model",
        endpoint="https://synthetic.invalid/v1",
        service_tier="default",
        reasoning_effort="low",
        max_output_tokens=2_000,
        structuring_max_output_tokens=2_000,
        timeout_seconds=30.0,
        structuring_max_bytes=8_000,
        max_requests=3,
        max_cost_micro_usd=10_000_000,
        prices=PRICES,
        input_bound=BOUND.name,
        rules_checked_on="2026-11-06",
        request_unit="generation",
        max_input_tokens=32_000,
    )


class GatedSimulation(AIProvider):
    """The app's simulation answering as a synthetic service behind a gate, with made-up usage."""

    SYNTHETIC = True

    def __init__(self):
        self.inner = SimulatedProvider()
        self.prompts = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.prompts.append(prompt)
        answer = self.inner.generate_text(prompt, json_schema_format=json_schema_format)

        return replace(
            answer,
            provider="synthetic",
            requested_model="synthetic-model",
            model="synthetic-model",
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            service_tier="default",
        )


def test_analyses_go_through_the_one_provider_the_parent_gate_admitted_and_never_enter_it_again(database, library, clock, tmp_path):
    """The parent enters the gate once (here for the analysis purpose: A2 adds its own); its search only uses what it was given."""
    gate = open_gate(database, tmp_path / "service.db", gate_terms(), BOUND, clock)
    provider = GatedSimulation()

    try:
        with gate.task("analysis", library.owner, provider) as gated:
            run = begin(database, clock, confirmation_for(library, recipient=SYNTHETIC_RECIPIENT), gated)
            ready(run, "c-bj-analyst", "c-bj-sales", "c-choice")

            assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE
            # Both of its requests went through the parent's provider: the analysis entered no gate of its own.
            assert (gated.attempts, gated.blocked, len(provider.prompts)) == (2, 0, 2)

            with pytest.raises(LiveBlocked) as nested:
                with gate.task("analysis", library.owner, provider):
                    pass

            assert nested.value.reason is Blocked.BUSY

            # The approval had three requests: the next analysis is stopped at its second, and the search ends.
            stopped = analyze_posting(run, ref("c-bj-sales"))

            assert (stopped.outcome, stopped.reason, run.ended) == (Outcome.STOPPED, "requests", EndReason.GATE)
            assert analyze_posting(run, ref("c-choice")).outcome is Outcome.STOPPED
            assert (gated.attempts, gated.blocked, len(provider.prompts), run.analyses) == (4, 1, 3, 2)

        status = gate.status()

        assert (status.requests_used, status.requests_left, status.paused_reason) == (3, 0, None)
        assert dict(run.outcome().unlisted)[Unlisted.BLOCKED] == (ref("c-bj-sales"),)
    finally:
        gate.close()


def test_a_start_date_the_user_cannot_make_is_a_conflict_the_search_cannot_drop(database, library, clock):
    """Case 6: the must-have goes out as confirmed, and the conflict found is shown apart."""
    parse = parsed_posting(
        job_title="数据分析实习生（合成岗位，不是真实招聘）",
        company="合成咨询",
        location="北京",
        work_mode=None,
        education_requirement=None,
        required_skills=[{"text": "Python", "evidence_text": "熟练使用 Python 进行数据处理"}],
        preferred_skills=[],
        hard_constraints=[],
        uncertain_requirements=[],
    )
    answer = json.dumps(
        {
            "assessments": [{"requirement_id": "R1", "citations": [cite("S1", "actions", "用 Python 清洗订单数据", "practice")]}],
            "preferences": [
                {"preference_id": "F1", "aspect": "work_content", "relation": "not_stated", "job_quotes": [], "job_quotes_aspect": "work_content"},
                {
                    "preference_id": "F2",
                    "aspect": "start_date",
                    "relation": "conflicts",
                    "job_quotes": ["到岗时间：需在 2026 年 3 月前到岗"],
                    "job_quotes_aspect": "start_date",
                },
            ],
        },
        ensure_ascii=False,
    )
    provider = ScriptedProvider(parse, answer, model=SIMULATION_MODEL)
    run = begin(database, clock, confirmation_for(library, preferences=(SALES, START_DATE)), provider)
    ready(run, "c-early-start")
    result = analyze_posting(run, ref("c-early-start"))

    assert result.outcome is Outcome.DONE and result.summary.endswith("已分析：有明确冲突，需谨慎考虑。")
    assert "推荐" not in result.summary
    assert "最早 2026 年 7 月入职" in provider.prompts[1]
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    (conflict,) = run.outcome().results

    assert conflict.category is Category.CONFLICT and conflict.category not in RECOMMENDED
    assert [finding.field for finding in conflict.analysis.preference_conflicts] == ["must_haves"]


# --- A1-R1, F1: what each source and each step may be given ---------------------------


def test_a_search_asks_only_the_sources_it_was_confirmed_for_as_they_were_confirmed(database, library, clock):
    """Another recipient, way of reaching or evidence under the same id is another source: nothing is asked of it."""
    confirmation = confirmation_for(library)
    careers = SyntheticSource(CAREERS, synthetic.POSTINGS)
    entry, leads = synthetic.ENTRIES

    for changed in (
        replace(entry, name="另一个接收方（合成）"),
        replace(entry, access=Access.CACHED_SEARCH),
        replace(entry, evidence=EvidenceLevel.LEAD),
    ):
        with pytest.raises(StartRefused) as refused:
            begin(database, clock, confirmation, register=Register((changed, leads), {CAREERS: careers}))

        assert refused.value.problems == ("合成招聘官网：登记的接收方、读取方式或证据等级和确认时不一致",)

    # Registered and callable, but not confirmed for this search: never asked either.
    extra = SourceEntry("synthetic-extra", "合成另一个官网", Access.SYNTHETIC, Permission.VERIFIED, EvidenceLevel.OFFICIAL, "测试", "2026-11-06")
    run = begin(database, clock, confirmation, register=Register((*synthetic.ENTRIES, extra), {"synthetic-extra": careers}))
    result = search_postings(run, "synthetic-extra", "数据分析", city="北京", **WITHIN)

    assert (result.outcome, result.reason) == (Outcome.REFUSED, "合成另一个官网不在这次查找确认的来源里")
    assert careers.calls == []
    # The sources are part of what was confirmed: other sources need another confirmation.
    assert search_fingerprint(replace(confirmation, sources=synthetic.GRANTS[:1])) != search_fingerprint(confirmation)


@pytest.mark.parametrize(
    ("filters", "reason"),
    [
        (dict(direction="candidate@example.invalid"), "岗位方向只能是岗位、技能、城市这类条件词，不能带标点或整句话"),
        (dict(direction="运营 13800138000"), "岗位方向里有像联系方式、证件号、地址或保密内容的文字"),
        (dict(direction="数据" * 21), "岗位方向最多 40 个字"),
        (dict(direction="   "), "岗位方向是空的"),
        (dict(city="北京海淀区合成路18号"), "城市里有像联系方式、证件号、地址或保密内容的文字"),
        (dict(keywords="数据分析 深圳"), "检索词里有不能接受的城市：深圳"),
    ],
)
def test_every_condition_sent_to_a_source_is_words_of_conditions_within_the_scope(database, library, clock, filters, reason):
    """The direction is open in this scope, so only the rules for words stand between it and the source."""
    run = begin(database, clock, confirmation_for(library))
    given = {"keywords": "数据分析", "city": "北京", "job_type": "实习", "direction": None, **filters}
    result = search_postings(run, CAREERS, given.pop("keywords"), **given)

    assert (result.outcome, result.reason) == (Outcome.REFUSED, reason)
    assert run.register.adapter(CAREERS).calls == []


def test_an_open_direction_goes_out_as_plain_words(database, library, clock):
    run = begin(database, clock, confirmation_for(library))

    assert search_postings(run, CAREERS, "数据分析", city="北京", job_type="实习", direction=" 运营 ").outcome is Outcome.DONE
    assert run.register.adapter(CAREERS).calls == [("search", Query("数据分析", "北京", "实习", "运营"))]


def test_what_the_next_decision_sees_passes_the_outbound_rules_and_holds_none_of_the_users_words(database, library, clock):
    """What a page shows stays with the search's own records; only the summaries may go on to a model."""
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library, preferences=(SALES, START_DATE)), provider)
    steps = [
        search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN),
        search_postings(run, CAREERS, "数据分析 实习生", city="上海", **WITHIN),
        get_posting(run, map(ref, ("c-address", "c-injection", "c-bj-analyst"))),
        check_posting(run, map(ref, ("c-address", "c-injection", "c-bj-analyst"))),
        analyze_posting(run, ref("c-address")),
        analyze_posting(run, ref("c-bj-analyst")),
    ]
    read, analysed = steps[2].summary, steps[5].summary

    assert [step.outcome for step in steps] == [Outcome.DONE] * 4 + [Outcome.BLOCKED, Outcome.DONE]
    assert all(review(step.summary) == () for step in steps)
    # The office address the analysis stops is not shown to the next decision either.
    assert "合成路 18 号" not in read and "原文里有疑似地址，摘录不交给下一步。" in read
    assert "hr-synthetic" not in read and "［已隐去邮箱］" in read
    # The user's words went to the analysis as confirmed, and never into a summary.
    assert "最早 2026 年 7 月入职" in provider.prompts[1]
    assert all(words not in step.summary for step in steps for words in (PROJECT_ACTIONS, "合成大学", "最早 2026 年 7 月入职", SENTINEL))
    assert analysed.endswith("申请前先确认：现实必要条件：待确认")
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    # The page has them, from the search's own record.
    (result,) = run.outcome().results

    assert result.first_checks == ("现实必要条件：最早 2026 年 7 月入职",)


def test_one_analysis_sends_no_more_than_u2_allows(database, library, clock):
    """U2's limits on one analysis: ten experiences, and 24000 characters of fields, preferences and posting together."""

    def projects(count, length):
        return tuple(
            SelectedSource(
                *confirmed(database, library.owner, clock, "project", title=f"合成课程{index}", actions="数据" * (length // 2)),
                ("actions",),
            )
            for index in range(count)
        )

    # 24,500 characters of fields and two of a preference: not even the shortest posting would fit.
    with pytest.raises(StartRefused) as refused:
        begin(database, clock, confirmation_for(library, selection=projects(5, 4900)))

    assert refused.value.problems == (
        "所选经历和偏好共 24502 字，加上岗位原文会超过一次分析 24000 字的上限：请少选几段经历，或排除一些栏目",
    )

    with pytest.raises(StartRefused) as refused:
        begin(database, clock, confirmation_for(library, selection=projects(11, 2)))

    assert refused.value.problems == ("一次分析最多发送 10 段经历，现在选了 11 段",)

    # The fields and preference fit, the posting does not: nothing is sent, and no analysis is used.
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library, selection=projects(5, 4790)), provider)
    ready(run, "c-bj-analyst")
    blocked = analyze_posting(run, ref("c-bj-analyst"))

    assert (blocked.outcome, blocked.reason, provider.prompts, run.analyses) == (Outcome.BLOCKED, "outbound_limit", [], 0)
    assert "超过 24000 字的上限" in blocked.summary

    # Within the limit, it goes out as U2 would send it.
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library, selection=projects(5, 4700)), provider)
    ready(run, "c-bj-analyst")

    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE
    assert len(provider.prompts) == 2


# --- A1-R1, F2: cancelling, time and no retries --------------------------------------


@pytest.mark.parametrize("operation", ["search", "read", "unread", "check", "unanswered"])
def test_what_a_source_gives_back_after_a_cancellation_counts_and_is_never_taken(database, library, clock, monkeypatch, operation):
    """The search is cancelled while its source answers the first of two postings (or fails to): the second is never asked."""
    cancel = threading.Event()
    run = begin(database, clock, confirmation_for(library), cancel=cancel)
    careers = run.register.adapter(CAREERS)
    first, second = ref("c-bj-analyst"), ref("c-bj-sales")
    asked = {"unread": "read", "unanswered": "check"}.get(operation, operation)

    if asked != "search":
        assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE

    if asked == "check":
        assert get_posting(run, [first, second]).outcome is Outcome.DONE

    if operation != asked:
        careers.faults[(asked, "c-bj-analyst")] = SourceTimeout("synthetic")

    answer = getattr(careers, asked)

    def cancelled_while_answering(*question):
        cancel.set()

        return answer(*question)

    monkeypatch.setattr(careers, asked, cancelled_while_answering)
    calls = len(careers.calls)
    result = {
        "search": lambda: search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN),
        "read": lambda: get_posting(run, [first, second]),
        "check": lambda: check_posting(run, [first, second]),
    }[asked]()

    assert (result.outcome, result.reason, run.ended) == (Outcome.STOPPED, "cancelled", EndReason.CANCELLED)
    assert len(careers.calls) == calls + 1

    if asked == "search":
        assert (run.found, run.searched, [record.outcome for record in run.searches]) == ({}, set(), [Outcome.STOPPED])
    elif asked == "read":
        assert (run.reads, run.found[first].detail, run.found[second].detail) == (1, None, None)
    else:
        assert (run.checks, run.found[first].status, run.found[first].checked_at, run.found[second].status) == (1, None, None, None)


@pytest.mark.parametrize("calls", [1, 2])
def test_an_analysis_that_runs_out_of_time_sends_nothing_more_and_its_answer_is_not_taken(database, library, clock, calls):
    """The time runs out while the posting is parsed (1) or while it is analysed (2)."""
    now = [0.0]

    class OutOfTime(RecordingProvider):
        def generate_text(self, prompt, **options):
            answer = super().generate_text(prompt, **options)

            if len(self.prompts) == calls:
                now[0] = Limits().seconds

            return answer

    provider = OutOfTime()
    run = begin(database, clock, confirmation_for(library), provider, monotonic=lambda: now[0])
    ready(run, "c-bj-analyst", "c-bj-sales")
    stopped = analyze_posting(run, ref("c-bj-analyst"))

    assert (stopped.outcome, stopped.reason, run.ended) == (Outcome.STOPPED, "timeout", EndReason.TIMEOUT)
    assert (len(provider.prompts), run.analyses, run.found[ref("c-bj-analyst")].analysis) == (calls, 1, None)
    assert analyze_posting(run, ref("c-bj-sales")).outcome is Outcome.STOPPED
    assert len(provider.prompts) == calls
    assert dict(run.outcome().unlisted)[Unlisted.INCOMPLETE] == (ref("c-bj-analyst"),)


def test_a_complete_analysis_back_after_the_search_ran_out_of_time_is_not_taken(database, library, clock, monkeypatch):
    """The tools ask again once the analysis entry has returned, whatever it checked itself."""
    now = [0.0]
    entry = tools.analyze_selected_experiences

    def returned_late(*args, **kwargs):
        result = entry(*args, **kwargs)
        now[0] = Limits().seconds

        return result

    monkeypatch.setattr(tools, "analyze_selected_experiences", returned_late)
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider, monotonic=lambda: now[0])
    ready(run, "c-bj-analyst")
    late = analyze_posting(run, ref("c-bj-analyst"))

    assert (late.outcome, late.reason, len(provider.prompts), run.analyses) == (Outcome.STOPPED, "timeout", 2, 1)
    assert run.found[ref("c-bj-analyst")].analysis is None and run.outcome().results == ()


def test_a_source_is_never_given_more_time_than_the_search_has_left(database, library, clock):
    now = [0.0]
    run = begin(database, clock, confirmation_for(library), monotonic=lambda: now[0])

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE

    now[0] = Limits().seconds - 5

    assert get_posting(run, [ref("c-bj-analyst")]).outcome is Outcome.DONE
    assert run.register.adapter(CAREERS).timeouts == [Limits().source_seconds, 5.0]


def test_a_request_that_failed_is_not_sent_again_but_one_with_other_conditions_is(database, library, clock):
    run = begin(database, clock, confirmation_for(library, limits=Limits(refusals=5)))
    careers = run.register.adapter(CAREERS)
    careers.faults[("search", None)] = SourceTimeout("synthetic")

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.FAILED

    # The same request, however it is spaced or written, is not sent again in this search.
    again = search_postings(run, CAREERS, " 数据分析  实习生 ", city="北京市", **WITHIN)

    assert (again.outcome, again.reason) == (Outcome.REFUSED, "这次检索和之前没有成功的一次完全相同，不会重试")
    assert len(careers.calls) == 1

    # Other words are another request.
    del careers.faults[("search", None)]

    assert search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN).outcome is Outcome.DONE

    careers.faults[("read", "c-bj-analyst")] = SourceTimeout("synthetic")

    assert get_posting(run, [ref("c-bj-analyst")]).outcome is Outcome.FAILED

    del careers.faults[("read", "c-bj-analyst")]
    retried = get_posting(run, [ref("c-bj-analyst")])

    assert (retried.outcome, retried.reason) == (Outcome.REFUSED, "[synthetic-careers/c-bj-analyst] 上次没有读到，这次查找不再重试")
    assert [call for call in careers.calls if call[0] == "read"] == [("read", "c-bj-analyst")]
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE
    assert {lead.ref: lead.why for lead in run.outcome().leads}[ref("c-bj-analyst")] == "读取没有成功，这次查找不再重试"


# --- A1-R1, F3: a result stands only while its facts do ------------------------------


def test_a_result_whose_recheck_gets_no_answer_is_withdrawn_and_keeps_the_time_last_answered(database, library, clock):
    run = begin(database, clock, confirmation_for(library))
    ready(run, "c-bj-analyst")
    answered = clock.now()

    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE

    clock.advance(hours=1)
    run.register.adapter(CAREERS).faults[("check", "c-bj-analyst")] = SourceTimeout("synthetic")

    assert check_posting(run, [ref("c-bj-analyst")]).outcome is Outcome.FAILED
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()
    lead = {lead.ref: lead for lead in outcome.leads}[ref("c-bj-analyst")]

    assert outcome.results == ()
    assert (lead.why, lead.checked_at) == ("状态待确认：最近一次核对没有得到回答，之前的分析已撤下", answered)


@pytest.mark.parametrize("when", ["before_the_next_analysis", "before_finishing", "after_the_end", "preferences_after_the_end"])
def test_results_resting_on_material_that_has_changed_are_withdrawn(database, library, clock, when):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library), provider)
    ready(run, "c-bj-analyst", "c-bj-sales")

    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE

    if when == "before_the_next_analysis":
        edit(database, library.owner, library.project[0], clock, actions="换了一种说法。")
        stopped = analyze_posting(run, ref("c-bj-sales"))

        assert (stopped.outcome, stopped.reason, run.ended) == (Outcome.STOPPED, "selection_invalid", EndReason.SOURCES_CHANGED)
    elif when == "before_finishing":
        edit(database, library.owner, library.project[0], clock, actions="换了一种说法。")

        assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE
        assert run.ended is EndReason.SOURCES_CHANGED
    else:
        assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

        if when == "after_the_end":
            edit(database, library.owner, library.project[0], clock, actions="换了一种说法。")
        else:
            edit_preferences(database, library, clock, work_content_no="销售和客服")

    outcome = run.outcome()

    assert (outcome.results, len(provider.prompts)) == ((), 2)
    assert {lead.ref: lead.why for lead in outcome.leads}[ref("c-bj-analyst")] == "所选经历或偏好已经变化，这次查找的分析都不算数"


# --- A1-R1, F4: alike is not the same ------------------------------------------------


def test_postings_only_alike_stay_two_candidates_and_a_stated_job_number_makes_one(database, library, clock):
    first, repost = synthetic.POSTINGS[0], synthetic.POSTINGS[1]
    # The same company, title and city, with another job number and direction: another job.
    other = replace(
        first, posting=replace(first.posting, ref=ref("c-bj-analyst-ops"), direction="运营", identity="SYN-JOB-0002")
    )
    # A lead stating the first one's job number, which a lead's word cannot make the same job.
    hearsay = replace(first, posting=replace(first.posting, ref=ref("l-bj-analyst", LEADS)))
    register = Register(
        synthetic.ENTRIES, {CAREERS: SyntheticSource(CAREERS, (first, repost, other)), LEADS: SyntheticSource(LEADS, (hearsay,))}
    )
    run = begin(database, clock, confirmation_for(library), register=register)
    postings = [ref("c-bj-analyst"), ref("c-bj-analyst-repost"), ref("c-bj-analyst-ops")]

    assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", job_type="实习").outcome is Outcome.DONE
    assert search_postings(run, LEADS, "数据分析 实习生", city="北京", job_type="实习").outcome is Outcome.DONE
    assert get_posting(run, postings).outcome is Outcome.DONE
    assert check_posting(run, postings).outcome is Outcome.DONE
    # The real repost is still analysed once.
    assert analyze_posting(run, ref("c-bj-analyst-repost")).reason == "和 [synthetic-careers/c-bj-analyst] 是同一个岗位，只分析那一个"
    assert analyze_posting(run, ref("c-bj-analyst")).outcome is Outcome.DONE
    assert analyze_posting(run, ref("c-bj-analyst-ops")).outcome is Outcome.DONE
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()
    kept, apart = outcome.results

    assert (kept.ref, apart.ref, outcome.merged) == (ref("c-bj-analyst"), ref("c-bj-analyst-ops"), (ref("c-bj-analyst-repost"),))
    assert kept.deadlines == ((ref("c-bj-analyst"), date(2026, 11, 30)), (ref("c-bj-analyst-repost"), date(2026, 12, 10)))
    assert "和 [synthetic-careers/c-bj-analyst-ops] 公司、标题、城市相同，但没有可靠依据确认是同一个岗位，分开列出" in kept.notes
    assert [lead.ref for lead in outcome.leads] == [ref("l-bj-analyst", LEADS)]


# --- A1-R1, F5: a search without experiences ------------------------------------------


def test_a_search_without_experiences_finds_reads_and_checks_and_analyses_nothing(database, library, clock):
    provider = RecordingProvider()
    run = begin(database, clock, confirmation_for(library, selection=(), preferences=(), limits=Limits(refusals=3)), provider)
    ready(run, "c-bj-analyst")
    refused = analyze_posting(run, ref("c-bj-analyst"))

    assert (refused.outcome, refused.reason) == (Outcome.REFUSED, "没有选择经历：这次查找不做个性化分析")
    # What it found was never judged, so it may not say that nothing suits.
    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.REFUSED
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()

    assert (outcome.personalised, outcome.results, provider.prompts) == (False, (), [])
    assert {lead.ref: lead.why for lead in outcome.leads}[ref("c-bj-analyst")] == NOT_PERSONALISED_WHY


# --- A1-R2: what A1-R1 left (report, section 10) ---------------------------------------


def test_an_id_that_is_not_plain_is_never_taken_or_repeated_and_plain_ones_still_work(database, library, clock, monkeypatch):
    """A source's ids reach the next step too, so they pass the same rules as its words; plain ones work as before."""
    run = begin(database, clock, confirmation_for(library))
    careers = run.register.adapter(CAREERS)
    first, sales = synthetic.POSTINGS[0].posting, synthetic.POSTINGS[3].posting
    unplain = (
        replace(first, ref=ref("user@example.invalid"), identity=None),
        replace(first, ref=ref("13800138000"), identity=None),
        replace(first, ref=ref("c-points-away"), identity=None, official=ref("user@example.invalid")),
    )
    monkeypatch.setattr(careers, "search", lambda query, timeout: (*unplain, first, sales))
    searched = search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN)

    assert searched.outcome is Outcome.DONE and "另有 3 个岗位的编号不合要求" in searched.summary
    assert list(run.found) == [ref("c-bj-analyst"), ref("c-bj-sales")]

    # A plain id is named and used by the next steps as before.
    used = [get_posting(run, [ref("c-bj-analyst")]), check_posting(run, [ref("c-bj-analyst")])]

    assert [step.outcome for step in used] == [Outcome.DONE, Outcome.DONE]

    # A read that comes back pointing to an id that is not plain is not taken.
    read = careers.read

    def pointing_away(posting_ref, timeout):
        detail = read(posting_ref, timeout)

        return replace(detail, posting=replace(detail.posting, official=ref("user@example.invalid")))

    monkeypatch.setattr(careers, "read", pointing_away)
    unread = get_posting(run, [ref("c-bj-sales")])

    assert (unread.outcome, run.found[ref("c-bj-sales")].detail) == (Outcome.FAILED, None)
    assert "来源给的岗位编号不合要求" in unread.summary

    # One the next step names itself is refused without being repeated.
    refused = get_posting(run, [ref("user@example.invalid")])

    assert refused.reason == "[编号不合要求，不显示] 不是这次检索得到的岗位"

    for step in (searched, *used, unread, refused):
        assert review(step.summary) == ()
        assert all(raw not in f"{step.summary}{step.reason}" for raw in ("user@example.invalid", "13800138000"))


@pytest.mark.parametrize("stop", ["cancelled", "timeout", None])
@pytest.mark.parametrize("operation", ["search", "read"])
def test_a_last_call_that_fails_once_the_search_stopped_is_a_stop_and_not_a_source_failure(
    database, library, clock, monkeypatch, operation, stop
):
    """The source fails after the search was cancelled or ran out of time: counted, stopped, nothing taken or remembered."""
    cancel, now = threading.Event(), [0.0]
    run = begin(database, clock, confirmation_for(library), cancel=cancel, monotonic=lambda: now[0])
    careers = run.register.adapter(CAREERS)

    if operation == "read":
        assert search_postings(run, CAREERS, "数据分析 实习生", city="北京", **WITHIN).outcome is Outcome.DONE

    asked = getattr(careers, operation)

    def stopped_then_failed(*question):
        asked(*question)

        if stop == "cancelled":
            cancel.set()
        elif stop == "timeout":
            now[0] = Limits().seconds

        raise SourceTimeout("synthetic")

    monkeypatch.setattr(careers, operation, stopped_then_failed)
    calls = len(careers.calls)

    if operation == "search":
        result = search_postings(run, CAREERS, "数据分析", city="上海", **WITHIN)
    else:
        result = get_posting(run, [ref("c-bj-analyst")])

    assert len(careers.calls) == calls + 1

    if stop is None:
        # Nothing stopped the search: the source failing is just that, and the search goes on.
        assert (result.outcome, run.ended, len(run.failed)) == (Outcome.FAILED, None, 1)
    else:
        assert (result.outcome, result.reason, run.ended) == (Outcome.STOPPED, stop, EndReason(stop))
        assert run.failed == set()

        if operation == "search":
            assert ([record.outcome for record in run.searches], len(run.searched)) == ([Outcome.STOPPED], 0)
        else:
            assert (run.reads, run.found[ref("c-bj-analyst")].detail) == (1, None)


def test_each_value_searched_somewhere_is_not_each_combination_searched(database, library, clock, monkeypatch):
    """Beijing or Shanghai, internship or full-time: Beijing internships and Shanghai full-time roles leave two combinations."""
    scope = SearchScope(
        city=DimensionScope(("北京", "上海"), Others.EXCLUDED, ("北京",), ("深圳",)),
        job_type=DimensionScope(("实习", "全职"), Others.EXCLUDED, ("实习",)),
        direction=DimensionScope(("数据分析",), Others.EXCLUDED, ("数据分析",)),
        targets=("数据分析实习生",),
    )
    shanghai = synthetic.POSTINGS[2].posting
    run = begin(database, clock, confirmation_for(library, scope=scope, limits=Limits(refusals=3)))
    careers = run.register.adapter(CAREERS)
    # The source has a Shanghai internship, which neither search asks for.
    monkeypatch.setattr(careers, "search", lambda query, timeout: (shanghai,) if (query.city, query.job_type) == ("上海", "实习") else ())

    for city, kind in (("北京", "实习"), ("上海", "全职")):
        assert search_postings(run, CAREERS, "数据分析", city=city, job_type=kind, direction="数据分析").outcome is Outcome.DONE

    left = "北京 · 全职 · 数据分析；上海 · 实习 · 数据分析"
    covered = finish(run, EndReason.SCOPE_DONE)

    assert (covered.outcome, covered.reason) == (Outcome.REFUSED, f"范围里还有 2 个组合没查：{left}")

    # Nothing suitable in what was searched is said as that, with no search more: the rest unchecked.
    assert finish(run, EndReason.NO_RESULTS).summary == "查找结束：已查查询无结果，仍有范围未核验。"

    outcome = run.outcome()

    assert (outcome.end, outcome.unsearched) == (EndReason.PARTLY_NO_RESULTS, tuple(left.split("；")))
    # No search was sent beyond the two made.
    assert [record.outcome for record in outcome.searches] == [Outcome.DONE, Outcome.DONE]


def test_nothing_suitable_may_be_said_once_every_combination_was_searched_and_nothing_found(database, library, clock, monkeypatch):
    """The same two cities and two job types, each pair searched with any direction: the whole scope, and nothing in it."""
    scope = SearchScope(
        city=DimensionScope(("北京", "上海"), Others.EXCLUDED, ("北京",), ("深圳",)),
        job_type=DimensionScope(("实习", "全职"), Others.EXCLUDED, ("实习",)),
        direction=DimensionScope(("数据分析",), Others.INCLUDED, ("数据分析",)),
        targets=("数据分析实习生",),
    )
    run = begin(database, clock, confirmation_for(library, scope=scope))
    monkeypatch.setattr(run.register.adapter(CAREERS), "search", lambda query, timeout: ())

    for city in ("北京", "上海"):
        for kind in ("实习", "全职"):
            assert search_postings(run, CAREERS, "数据分析", city=city, job_type=kind).outcome is Outcome.DONE

    assert finish(run, EndReason.NO_RESULTS).outcome is Outcome.DONE
    assert (run.outcome().end, run.outcome().unsearched) == (EndReason.NO_RESULTS, ())


# --- A1-R3: what A1-R2 left (report, section 12) ---------------------------------------

UNVERIFIABLE = "13800001111"


def with_words(part: str, words: str) -> SearchScope:
    """SCOPE with words added to one part: a dimension's values, ideal or excluded ones ("city.values"), or the targets."""
    if part == "targets":
        return replace(SCOPE, targets=(*SCOPE.targets, words))

    dimension, field = part.split(".")
    current = getattr(SCOPE, dimension)

    return replace(SCOPE, **{dimension: replace(current, **{field: (*getattr(current, field), words)})})


@pytest.mark.parametrize(
    ("part", "words", "label"),
    [
        # The re-check's case: an email among the cities.
        ("city.values", "candidate@example.invalid", "城市"),
        ("city.ideal", "13800138000", "城市"),
        ("city.excluded", "人民路 100 号", "城市"),
        # A phone number only once its spacing is evened out, as keywords are.
        ("job_type.values", "138  0013  8000", "求职类型"),
        ("direction.excluded", "内部资料分析", "岗位方向"),
        ("targets", "联系 candidate@example.invalid", "要找的岗位名称"),
    ],
)
def test_a_scope_with_words_the_outbound_rules_stop_never_starts_and_no_refusal_repeats_them(
    database, library, clock, part, words, label
):
    """A scope's words go to sources as conditions and to the next step as what is left to search."""
    provider, register = RecordingProvider(), synthetic.register()

    with pytest.raises(StartRefused) as refused:
        begin(database, clock, confirmation_for(library, scope=with_words(part, words)), provider, register=register)

    problems = refused.value.problems

    assert [problem for problem in problems if "不能作为查找条件" in problem] == [
        f"{label}里有疑似联系方式、证件号、地址或保密内容，不能作为查找条件：请改正后再确认"
    ]
    assert all(words not in problem and " ".join(words.split()) not in problem for problem in problems)
    # Nothing went out: no search, read or check, and no analysis.
    assert (provider.prompts, register.adapter(CAREERS).calls, register.adapter(LEADS).calls) == ([], [], [])


def test_plain_cities_left_unsearched_are_still_named_combination_by_combination(database, library, clock):
    """The same case with a plain third city: the search starts, and what is left is named as before."""
    run = begin(database, clock, confirmation_for(library, scope=with_words("city.values", "广州")))

    assert search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN).outcome is Outcome.DONE

    refused = finish(run, EndReason.SCOPE_DONE)
    left = (
        "北京 · 实习 · 所列以外的岗位方向；上海 · 实习 · 数据分析；上海 · 实习 · 所列以外的岗位方向；"
        "广州 · 实习 · 数据分析；广州 · 实习 · 所列以外的岗位方向"
    )

    assert (refused.outcome, refused.reason) == (Outcome.REFUSED, f"范围里还有 5 个组合没查：{left}")
    assert review(refused.summary) == ()


@pytest.mark.parametrize("given", ["nothing", "unverifiable", "both"])
@pytest.mark.parametrize("covered", ["whole", "part"])
def test_postings_given_back_that_cannot_be_verified_are_counted_and_never_make_nothing_suitable(
    database, library, clock, monkeypatch, covered, given
):
    """The re-check's case: Beijing internships in data analysis searched, and the one posting given back has an id
    like a phone number. Given back nothing, a search may say it found nothing suitable, or nothing in what it
    searched with the rest unchecked. Given back what it could not verify, it may say neither, and its outcome
    counts those postings without their ids.
    """
    cities = ("北京",) if covered == "whole" else ("北京", "上海")
    scope = replace(SCOPE, city=replace(SCOPE.city, values=cities), direction=replace(SCOPE.direction, others=Others.EXCLUDED))
    run = begin(database, clock, confirmation_for(library, scope=scope))
    first = synthetic.POSTINGS[0].posting
    unverifiable = replace(first, ref=ref(UNVERIFIABLE), identity=None)
    returned = {"nothing": (), "unverifiable": (unverifiable,), "both": (unverifiable, first)}[given]
    monkeypatch.setattr(run.register.adapter(CAREERS), "search", lambda query, timeout: returned)
    searched = search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN)
    claimed = finish(run, EndReason.NO_RESULTS)

    assert searched.outcome is Outcome.DONE
    assert ("无法核验" in searched.summary) == (given != "nothing")

    if given == "nothing":
        # The source gave back nothing at all: said as it is.
        assert claimed.outcome is Outcome.DONE
        assert run.ended is (EndReason.NO_RESULTS if covered == "whole" else EndReason.PARTLY_NO_RESULTS)
    else:
        unverified = "来源返回的岗位里{}有 1 个编号不合要求，无法核验，不能说没有合适结果"
        reason = (
            unverified.format("")
            if given == "unverifiable"
            else "还有 1 个岗位没有结论（没有分析、待核验，或者分析没有完成）；" + unverified.format("另")
        )

        assert (claimed.outcome, claimed.reason, run.ended) == (Outcome.REFUSED, reason, None)
        # Nothing more is sent for them: the search may still end as enough, with them counted.
        assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()
    [record] = outcome.searches
    count = 0 if given == "nothing" else 1

    assert (record.found, record.unverifiable, outcome.unverifiable) == (len(returned), count, count)
    assert outcome.unsearched == (() if covered == "whole" else ("上海 · 实习 · 数据分析",))
    assert [lead.ref for lead in outcome.leads] == ([ref("c-bj-analyst")] if given == "both" else [])
    # The id is never repeated or kept: not to the next step, not in what the search holds or tells.
    assert review(searched.summary) == () and review(claimed.summary) == ()
    assert UNVERIFIABLE not in repr((searched, claimed, outcome, run.found, run.known, run.failed, run.searched))


# --- A2-0: a source giving back another source's records (A1 acceptance, 14; A2 pre-check, 2.3) ---

# What a careers search gives back as another source's: a record of the other registered source, and one of a
# source not registered at all. Their ids are plain, so only the source they name tells them apart.
FOREIGN_LEAD = "l-foreign-4417"
FOREIGN_ELSEWHERE = ("unregistered-board", "x-foreign-9902")
CONTRACT_QUERIES = (
    Query("数据", None, None, None),
    Query("数据分析 实习生", "北京", "实习", "数据分析"),
    Query("数据分析 实习生", "上海", "实习", "数据分析"),
)


def foreign_records():
    first = synthetic.POSTINGS[0].posting

    return (
        replace(first, ref=PostingRef(LEADS, FOREIGN_LEAD), identity=None),
        replace(first, ref=PostingRef(*FOREIGN_ELSEWHERE), identity=None),
    )


@pytest.mark.parametrize("given", ["nothing", "foreign", "foreign_and_own", "every_kind"])
@pytest.mark.parametrize("covered", ["whole", "part"])
def test_records_of_another_source_are_a_counted_anomaly_and_never_make_nothing_suitable(
    database, library, clock, monkeypatch, covered, given
):
    """The acceptance's case: a careers search gives back records naming another source as theirs, a source
    anomaly. They are never taken or named, only counted, and while any came back neither "nothing suitable" nor
    "nothing in what was searched" may be said. A careers posting given back with them is taken as before; and
    a source that truly gave back nothing still lets a search say so, of the whole scope or the part searched.
    """
    cities = ("北京",) if covered == "whole" else ("北京", "上海")
    scope = replace(SCOPE, city=replace(SCOPE.city, values=cities), direction=replace(SCOPE.direction, others=Others.EXCLUDED))
    run = begin(database, clock, confirmation_for(library, scope=scope))
    first = synthetic.POSTINGS[0].posting
    lead_record, elsewhere_record = foreign_records()
    unverifiable = replace(first, ref=ref(UNVERIFIABLE), identity=None)
    returned = {
        "nothing": (),
        "foreign": (lead_record, elsewhere_record),
        "foreign_and_own": (lead_record, first),
        "every_kind": (elsewhere_record, unverifiable, first),
    }[given]
    monkeypatch.setattr(run.register.adapter(CAREERS), "search", lambda query, timeout: returned)
    searched = search_postings(run, CAREERS, "数据分析", city="北京", **WITHIN)
    claimed = finish(run, EndReason.NO_RESULTS)
    foreign = {"nothing": 0, "foreign": 2, "foreign_and_own": 1, "every_kind": 1}[given]
    unverified = 1 if given == "every_kind" else 0

    assert searched.outcome is Outcome.DONE
    assert ("来源异常" in searched.summary) == bool(foreign)
    assert not foreign or f"另有 {foreign} 条记录不属于这个来源（来源异常），没有采用。" in searched.summary

    if given == "nothing":
        # The source truly gave back nothing: said as before, of the whole scope or of the part searched.
        whole = covered == "whole"

        assert claimed.outcome is Outcome.DONE
        assert claimed.summary == ("查找结束：没有合适结果。" if whole else "查找结束：已查查询无结果，仍有范围未核验。")
        assert run.ended is (EndReason.NO_RESULTS if whole else EndReason.PARTLY_NO_RESULTS)
    else:
        anomaly = "来源返回的记录里{}有 {} 条不属于该来源（来源异常），不能说没有合适结果"
        unsettled = "还有 1 个岗位没有结论（没有分析、待核验，或者分析没有完成）；"
        reason = {
            "foreign": anomaly.format("", 2),
            "foreign_and_own": unsettled + anomaly.format("另", 1),
            "every_kind": unsettled + "来源返回的岗位里另有 1 个编号不合要求，无法核验；" + anomaly.format("另", 1),
        }[given]

        # Neither kind of "nothing" was said, whether or not part of the scope is left.
        assert (claimed.outcome, claimed.reason, run.ended) == (Outcome.REFUSED, reason, None)
        # Nothing more is sent for them: the search may still end as enough, with them counted.
        assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE and run.ended is EndReason.ENOUGH

    outcome = run.outcome()
    [record] = outcome.searches
    own = [ref("c-bj-analyst")] if given in ("foreign_and_own", "every_kind") else []

    assert (record.found, record.foreign, record.unverifiable) == (len(returned), foreign, unverified)
    assert (outcome.foreign, outcome.unverifiable) == (foreign, unverified)
    assert outcome.unsearched == (() if covered == "whole" else ("上海 · 实习 · 数据分析",))
    # The careers posting given back with them is taken as before, and only it.
    assert list(run.found) == own and [lead.ref for lead in outcome.leads] == own
    # Their ids are never repeated or kept: not to the next step, not in what the search holds or tells.
    assert review(searched.summary) == () and review(claimed.summary) == ()
    held = repr((searched, claimed, outcome, run.found, run.known, run.failed, run.searched))

    assert all(raw not in held for raw in (FOREIGN_LEAD, *FOREIGN_ELSEWHERE, UNVERIFIABLE))


def reads_back(register: Register, wanted: PostingRef) -> bool:
    """Whether the source a reference names reads it back as that very posting."""
    try:
        return register.adapter(wanted.source_id).read(wanted, 1.0).posting.ref == wanted
    except SourceError:
        return False


def source_contract_breaches(register: Register, source_id: str) -> list[str]:
    """How a registered adapter breaks the source contract the tools rest on: every record a search gives back is
    the source's own, a read gives back the posting asked for, and a lead points to a posting an official source
    the tools may call has. A new adapter is held to this before any search uses it."""
    adapter = register.adapter(source_id)
    returned = {posting.ref: posting for query in CONTRACT_QUERIES for posting in adapter.search(query, 1.0)}
    breaches = [] if returned else ["no search gave anything back to check"]

    for posting in returned.values():
        if posting.ref.source_id != source_id:
            breaches.append(f"a search gave back a record of {posting.ref.source_id}")
            continue

        if not reads_back(register, posting.ref):
            breaches.append("a read did not give back the posting asked for")

        if posting.official is not None:
            entry = register.entry(posting.official.source_id)

            if entry is None or entry.evidence is not EvidenceLevel.OFFICIAL or register.refusal(entry.source_id):
                breaches.append("a lead points to no official source the tools may call")
            elif not reads_back(register, posting.official):
                breaches.append("a lead points to a posting its official source does not have")

    return breaches


@pytest.mark.parametrize("source_id", [entry.source_id for entry in synthetic.register().entries])
def test_every_registered_adapter_keeps_to_its_own_source_and_its_leads_to_official_postings(source_id):
    """The contract the fix rests on, for every registered adapter, before a search relies on it."""
    assert source_contract_breaches(synthetic.register(), source_id) == []


def test_the_contract_check_finds_an_adapter_giving_back_another_sources_records(monkeypatch):
    """The check is not empty: an adapter giving back the leads source's record from careers is found."""
    register = synthetic.register()
    lead = synthetic.LEAD_POSTINGS[0].posting
    monkeypatch.setattr(register.adapter(CAREERS), "search", lambda query, timeout: (lead,))

    assert source_contract_breaches(register, CAREERS) == [f"a search gave back a record of {LEADS}"]


def test_a_lead_naming_its_official_posting_is_no_anomaly(database, library, clock):
    """A lead is the leads source's own record naming an official posting apart from its own id: it is taken as
    before, nothing is counted as an anomaly, and the official posting it names is read and checked there."""
    run = begin(database, clock, confirmation_for(library))
    searched = search_postings(run, LEADS, "数据分析", city="上海", **WITHIN)
    official = ref("c-sh-analyst")

    assert searched.outcome is Outcome.DONE and "来源异常" not in searched.summary
    assert list(run.found) == [ref("l-sh-analyst", LEADS)] and official in run.known

    steps = [get_posting(run, [official]), check_posting(run, [official])]

    assert [step.outcome for step in steps] == [Outcome.DONE, Outcome.DONE]
    assert finish(run, EndReason.ENOUGH).outcome is Outcome.DONE

    outcome = run.outcome()

    assert ([record.foreign for record in outcome.searches], outcome.foreign) == ([0], 0)
