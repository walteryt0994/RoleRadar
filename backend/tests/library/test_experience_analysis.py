"""U2-A1: analysing chosen, confirmed experiences against one job posting.

Everything runs against the simulated service in analysis_helpers: these
tests prove the pipeline, its checks and its rules, not the accuracy of any
real model. All material is synthetic.
"""
import json
import logging
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app import experience_analysis
from app.ai_provider import AIProviderRateLimitError, AIProviderTimeoutError
from app.analysis_contracts import (
    AnalysisStatus,
    CitationBasis,
    Recipient,
    task_fingerprint,
)
from app.experience_analysis import EVIDENCE_RULES_VERSION, analyze_selected_experiences
from app.jd_outcome import JobDescriptionOutcome, ParseOutcomeSource
from app.jd_record import fingerprint_job_posting
from app.jd_service import PARSER_VERSION, PROMPT_VERSION, parse_job_description
from app.library.content import parse_experience_form
from app.library.db import LibraryBase
from app.library.lifecycle import delete_experience
from app.library.models import Attachment, AttachmentState
from app.library.service import active_item, confirm_experience, create_document, save_draft
from app.match_service import SCORING_VERSION
from app.schemas import SCHEMA_VERSION
from analysis_helpers import (
    POSTING,
    SIMULATION,
    ScriptedProvider,
    approved,
    assessed,
    cite,
    confirmed,
    parsed_posting,
    request_for,
)

SENTINEL = "分析外哨兵-u2a1-5d21"
PROJECT_ACTIONS = "用 Python 清洗订单数据，并用 SQL 汇总每周销量。"
COMPLETE = dict(
    R1=[cite("S1", "actions", "用 Python 清洗订单数据", "practice")],
    R2=[cite("S1", "actions", "用 SQL 汇总每周销量", "practice")],
    P1=[],
    H1=[cite("S2", "graduation", "2027 年 6 月（预计）", "fact")],
    U1=[],
)


@pytest.fixture
def library(database, owners, clock):
    owner, other = owners

    return SimpleNamespace(
        owner=owner,
        other=other,
        project=confirmed(
            database, owner, clock, "project", private_note=f"私有备注{SENTINEL}",
            title="校园订单看板（合成）", actions=PROJECT_ACTIONS, outcome="看板在课程展示中使用。",
        ),
        education=confirmed(database, owner, clock, "education", school="合成大学", major="数据科学", graduation="2027 年 6 月（预计）"),
        unchosen=confirmed(database, owner, clock, "other", title=f"没有选择的经历{SENTINEL}"),
    )


def run(database, request, *answers, approval="match", cancel=None, provider=None):
    provider = provider or ScriptedProvider(*answers)
    approval = approved(request) if approval == "match" else approval

    return analyze_selected_experiences(database, request, approval, provider, cancel=cancel), provider


def edit(database, owner, item_id, clock, **fields):
    """Save new content: the experience waits for review again."""
    with database.transaction() as db:
        item = active_item(db, owner, item_id)
        save_draft(db, owner, item_id, item.revision, parse_experience_form({**item.content, **fields}, item.category), clock.now())


# --- a complete analysis and what it rests on -----------------------------------------


def test_a_complete_analysis_traces_every_conclusion_to_its_sources(database, library):
    request = request_for(library.owner, [library.project, library.education])
    result, _ = run(database, request, parsed_posting(), assessed(**COMPLETE))
    findings = {finding.requirement_id: finding for finding in result.findings}

    assert result.status is AnalysisStatus.COMPLETE and result.is_current
    assert (result.mode, result.requests_sent, result.preferences) == ("simulation", 2, "not_analyzed")
    assert [(source.item_id, source.version_id, source.category) for source in result.sources] == [
        (*library.project, "project"),
        (*library.education, "education"),
    ]
    # The posting: this text's fingerprint and the versions that parsed it.
    assert result.job.job_posting_sha256 == fingerprint_job_posting(POSTING)
    assert (result.job.prompt_version, result.job.parser_version, result.job.schema_version) == (PROMPT_VERSION, PARSER_VERSION, SCHEMA_VERSION)
    assert (result.job.provider, result.analysis.provider, result.analysis.returned_model) == ("simulation", "simulation", "simulation")
    assert (result.analysis.rules_version, result.analysis.scoring_version) == (EVIDENCE_RULES_VERSION, SCORING_VERSION)
    assert {key: finding.conclusion for key, finding in findings.items()} == {
        "R1": "supported", "R2": "supported", "P1": "no_evidence", "H1": "met", "U1": "no_evidence",
    }
    # Each conclusion names the posting's words and the chosen version, field and words it rests on.
    assert findings["R1"].job_posting_quote == "熟练使用 Python 进行数据处理"
    assert [
        (citation.item_id, citation.version_id, citation.category, citation.field, citation.quote, citation.basis)
        for citation in findings["R1"].citations
    ] == [(*library.project, "project", "actions", "用 Python 清洗订单数据", CitationBasis.PRACTICE)]
    assert findings["P1"].reasons == ("no_material",)
    coverage = result.coverage
    assert (coverage.required_score, coverage.preferred_score, coverage.overall_score) == (100, 0, 80)
    assert (coverage.required_total, coverage.preferred_total) == (2, 1)
    assert result.condition_conflicts == ()


def test_only_the_chosen_fields_of_the_chosen_experiences_go_out(database, library, clock):
    with database.transaction() as db:
        create_document(db, library.owner, parse_experience_form({"name": f"姓名{SENTINEL}", "phone": "13800138000"}, "profile"), clock.now())
        create_document(db, library.owner, parse_experience_form({"cities_ideal": f"城市{SENTINEL}"}, "preferences"), clock.now())
        db.add(Attachment(
            id=str(uuid.uuid4()), owner_id=library.owner, item_id=library.project[0], storage_key="a" * 32,
            filename=f"附件{SENTINEL}.pdf", media_type="application/pdf", size=1, sha256="0" * 64,
            state=AttachmentState.READY, upload_key="k" * 24, revision=1, created_at=clock.now(), updated_at=clock.now(),
            deleted_at=None, purge_after=None, deletion_id=None,
        ))

    request = request_for(library.owner, [library.project, library.education])
    result, provider = run(database, request, parsed_posting(), assessed(**COMPLETE))
    parse_prompt, analysis_prompt = provider.prompts
    sent = parse_prompt + analysis_prompt

    assert result.status is AnalysisStatus.COMPLETE
    # The posting goes to the parse; the analysis gets its requirements, not the whole text.
    assert POSTING.strip() in parse_prompt and PROJECT_ACTIONS not in parse_prompt
    assert PROJECT_ACTIONS in analysis_prompt and "合成大学" in analysis_prompt
    assert "有云平台经验更好" in analysis_prompt and "合成科技 - 上海" not in analysis_prompt
    # Private notes, unchosen experiences, the profile, the preferences and attachments never go out.
    assert SENTINEL not in sent and "13800138000" not in sent
    # Nor the library's own ids: sources are S1, S2.
    assert not any(identifier in sent for identifier in (*library.project, *library.education))


# --- what stops a task before anything goes out ----------------------------------------


@pytest.mark.parametrize(
    "change, problem, reason",
    [
        ("nothing_chosen", "empty_selection", None),
        ("edited_since", "selection_invalid", "not_confirmed"),
        ("confirmed_again", "selection_invalid", "version_changed"),
        ("deleted", "selection_invalid", "unavailable"),
        ("another_owners", "selection_invalid", "unavailable"),
        ("unknown", "selection_invalid", "unavailable"),
    ],
)
def test_a_choice_that_no_longer_holds_stops_the_task_before_anything_goes_out(database, library, clock, change, problem, reason):
    chosen = [library.project]

    if change == "nothing_chosen":
        chosen = []
    elif change == "edited_since":
        edit(database, library.owner, library.project[0], clock, outcome="改过还没确认")
    elif change == "confirmed_again":
        edit(database, library.owner, library.project[0], clock, outcome="改过并重新确认")

        with database.transaction() as db:
            confirm_experience(db, library.owner, library.project[0], active_item(db, library.owner, library.project[0]).revision, clock.now())
    elif change == "deleted":
        with database.transaction() as db:
            delete_experience(db, library.owner, library.project[0], active_item(db, library.owner, library.project[0]).revision, clock.now())
    elif change == "another_owners":
        chosen = [confirmed(database, library.other, clock, "project", title="B 的经历", actions="B 的内容")]
    else:
        chosen = [(str(uuid.uuid4()), str(uuid.uuid4()))]

    result, provider = run(database, request_for(library.owner, chosen), parsed_posting(), assessed(**COMPLETE))

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.REJECTED, problem, 0)
    assert provider.prompts == []
    assert [(item.item_id, item.reason) for item in result.selection_problems] == ([(chosen[0][0], reason)] if reason else [])


@pytest.mark.parametrize(
    "choice, status, problem, sent",
    [
        ("stale_then_current", "rejected", "repeated_selection", 0),
        ("current_then_stale", "rejected", "repeated_selection", 0),
        ("same_version_twice", "rejected", "repeated_selection", 0),
        ("two_experiences", "complete", None, 2),
    ],
)
def test_each_experience_is_chosen_once(database, library, choice, status, problem, sent):
    # Both orders of the first two are one task by its fingerprint: neither may keep one version in silence.
    stale = (library.project[0], str(uuid.uuid4()))
    chosen = {
        "stale_then_current": [stale, library.project],
        "current_then_stale": [library.project, stale],
        "same_version_twice": [library.project, library.project],
        "two_experiences": [library.project, library.education],
    }[choice]
    result, provider = run(database, request_for(library.owner, chosen), parsed_posting(), assessed(**COMPLETE))

    assert (result.status, result.problem, result.requests_sent, len(provider.prompts)) == (status, problem, sent, sent)


def test_a_confirmation_covers_exactly_one_task(database, library):
    request = request_for(library.owner, [library.project])
    approval = approved(request)
    others = {
        "more sources": request_for(library.owner, [library.project, library.education]),
        "another posting": request_for(library.owner, [library.project], posting=POSTING + "\n需要经常出差。"),
        "another model": request_for(library.owner, [library.project], recipient=Recipient("simulation", "another-model")),
        "another service": request_for(library.owner, [library.project], recipient=Recipient("openai", "simulation")),
        "another purpose": replace(request, purpose="direction_exploration"),
        "kept running": replace(request, mode="background"),
        "another owner": replace(request, owner_id=library.other),
    }

    # The order of the chosen sources is not part of the scope.
    assert task_fingerprint(request_for(library.owner, [library.project, library.education])) == task_fingerprint(
        request_for(library.owner, [library.education, library.project])
    )

    missing, provider = run(database, request, parsed_posting(), assessed(**COMPLETE), approval=None)
    assert (missing.status, missing.problem, provider.prompts) == (AnalysisStatus.REJECTED, "approval_missing", [])

    for name, other in others.items():
        assert task_fingerprint(other) != approval.task_fingerprint, name

        result, provider = run(database, other, parsed_posting(), assessed(**COMPLETE), approval=approval)

        assert result.status is AnalysisStatus.REJECTED and result.requests_sent == 0, name
        assert provider.prompts == [], name


class UnnamedProvider(ScriptedProvider):
    """Answers like the simulated service but cannot say where its requests go."""

    def identity(self):
        return None


ELSEWHERE = {
    "task_names_another_service": (Recipient("openai", "gpt-synthetic"), ScriptedProvider, "recipient_mismatch"),
    "configured_for_another_service": (SIMULATION, partial(ScriptedProvider, provider="openai"), "recipient_mismatch"),
    "configured_for_another_model": (SIMULATION, partial(ScriptedProvider, model="another-model"), "recipient_mismatch"),
    "cannot_tell": (SIMULATION, UnnamedProvider, "recipient_unverified"),
}


@pytest.mark.parametrize("case", list(ELSEWHERE))
def test_a_provider_configured_for_another_recipient_is_sent_nothing(database, library, case):
    recipient, make, problem = ELSEWHERE[case]
    request = request_for(library.owner, [library.project], recipient=recipient)
    result, provider = run(database, request, provider=make(parsed_posting(), assessed(**COMPLETE)))

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.REJECTED, problem, 0)
    assert provider.prompts == []


@pytest.mark.parametrize("call, sent", [("parse", 1), ("analysis", 2)])
def test_an_answer_reporting_another_model_is_not_used(database, library, call, sent):
    def misreported(answer):
        def answer_as_another_model(prompt):
            provider.answered_as = ("simulation", "another-model")
            return answer

        return answer_as_another_model

    # Configured as the task names; one answer then says another model was asked.
    answers = [parsed_posting(), assessed(**COMPLETE)]
    index = 0 if call == "parse" else 1
    answers[index] = misreported(answers[index])
    provider = ScriptedProvider(*answers)
    result, _ = run(database, request_for(library.owner, [library.project, library.education]), provider=provider)

    assert (result.status, result.problem, result.requests_sent, len(provider.prompts)) == (AnalysisStatus.FAILED, "recipient_mismatch", sent, sent)
    assert result.findings == () and result.coverage is None


def test_a_provider_reconfigured_after_the_first_request_gets_no_second(database, library):
    def reconfigured(prompt):
        provider.model = "another-model"
        return parsed_posting()

    # Its answers still report the model the task names: only its configuration changed.
    provider = ScriptedProvider(reconfigured, assessed(**COMPLETE), answered_as=("simulation", "simulation"))
    result, _ = run(database, request_for(library.owner, [library.project]), provider=provider)

    assert (result.status, result.problem, result.requests_sent, len(provider.prompts)) == (AnalysisStatus.REJECTED, "recipient_mismatch", 1, 1)


def test_a_more_specific_version_in_the_answers_is_recorded_not_refused(database, library):
    provider = ScriptedProvider(parsed_posting(), assessed(**COMPLETE), returned_model="simulation-2026-10-01")
    result, _ = run(database, request_for(library.owner, [library.project, library.education]), provider=provider)

    assert result.status is AnalysisStatus.COMPLETE
    assert (result.job.requested_model, result.job.returned_model) == ("simulation", "simulation-2026-10-01")
    assert (result.analysis.requested_model, result.analysis.returned_model) == ("simulation", "simulation-2026-10-01")


@pytest.mark.parametrize("when, sent", [("before", 0), ("during_the_parse", 1), ("during_the_analysis", 2)])
def test_a_cancelled_task_sends_nothing_more_and_counts_what_went_out(database, library, when, sent):
    cancel = threading.Event()

    def cancelling(answer):
        def answer_after_cancel(prompt):
            cancel.set()
            return answer

        return answer_after_cancel

    answers = [parsed_posting(), assessed(**COMPLETE)]

    if when == "before":
        cancel.set()
    elif when == "during_the_parse":
        answers[0] = cancelling(answers[0])
    else:
        answers[1] = cancelling(answers[1])

    request = request_for(library.owner, [library.project, library.education])
    result, provider = run(database, request, *answers, cancel=cancel)

    assert (result.status, result.requests_sent, len(provider.prompts)) == (AnalysisStatus.CANCELLED, sent, sent)
    # An answer that arrives after cancelling is not presented.
    assert result.findings == () and result.coverage is None


# --- what the answer has to be ----------------------------------------------------------


def test_a_posting_read_only_by_rules_is_degraded_and_never_scored(database, library):
    request = request_for(library.owner, [library.project])
    result, provider = run(database, request, AIProviderTimeoutError("synthetic timeout"))

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.DEGRADED, "timeout", 1)
    assert {"Python", "SQL", "Docker"} <= set(result.rule_skills)
    assert result.findings == () and result.coverage is None and not result.is_current
    assert len(provider.prompts) == 1


def _twice(answer: dict) -> str:
    payload = json.loads(assessed(**answer))
    payload["assessments"].append(payload["assessments"][0])

    return json.dumps(payload, ensure_ascii=False)


UNTRUSTED = {
    "timeout": (AIProviderTimeoutError("synthetic"), "timeout"),
    "rate_limit": (AIProviderRateLimitError("synthetic"), "rate_limit"),
    "not_json": ("这不是 JSON", "invalid_json"),
    "extra_score": (json.dumps({"assessments": [], "score": 99}), "schema"),
    "requirement_left_out": (assessed(**{key: value for key, value in COMPLETE.items() if key != "U1"}), "coverage"),
    "requirement_twice": (_twice(COMPLETE), "coverage"),
    "made_up_quote": (assessed(**{**COMPLETE, "R1": [cite("S1", "actions", "用 Python 训练了深度学习模型", "practice")]}), "citation"),
    "unchosen_source": (assessed(**{**COMPLETE, "R1": [cite("S3", "title", "没有选择的经历", "practice")]}), "citation"),
    "unsent_private_note": (assessed(**{**COMPLETE, "R1": [cite("S1", "private_note", "私有备注", "practice")]}), "citation"),
    "quote_from_another_field": (assessed(**{**COMPLETE, "R1": [cite("S1", "outcome", "用 Python 清洗订单数据", "practice")]}), "citation"),
}


@pytest.mark.parametrize("case", list(UNTRUSTED))
def test_an_answer_that_cannot_be_trusted_fails_as_a_whole(database, library, case):
    answer, failure = UNTRUSTED[case]
    request = request_for(library.owner, [library.project, library.education])
    result, _ = run(database, request, parsed_posting(), answer)

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.FAILED, failure, 2)
    assert result.findings == () and result.coverage is None and not result.is_current


READINGS = {
    # The field's text and the quoted words -> what the rules read them with, and what they found there.
    "negation_in_its_clause": ("我没有使用 Python，数据清洗由队友完成。", "使用 Python", ("我没有使用 Python",), ("negated",)),
    "negation_before_a_turn": ("没有用 Docker，但用 Python 做了分析。", "用 Python 做了分析", ("但用 Python 做了分析",), ()),
    "negation_in_a_later_clause": ("用 Python 做了分析，没有用 Docker。", "用 Python 做了分析", ("用 Python 做了分析",), ()),
    "list_under_one_negation": ("I did not use Docker, Kubernetes or AWS.", "Kubernetes or AWS", ("I did not use Docker, Kubernetes or AWS",), ("negated",)),
    "one_line_each": ("没有使用 Docker\n用 Python 清洗了数据", "用 Python 清洗了数据", ("用 Python 清洗了数据",), ()),
    "every_place_it_stands": (
        "我没有用 Python 做建模。后来用 Python 清洗了数据。", "用 Python",
        ("我没有用 Python 做建模", "后来用 Python 清洗了数据"), ("negated",),
    ),
}


@pytest.mark.parametrize("case", list(READINGS))
def test_a_quote_is_read_with_its_clause_and_the_reading_is_kept(database, library, clock, case):
    text, quote, context, flags = READINGS[case]
    chosen = confirmed(database, library.owner, clock, "project", title="合成作业", actions=text)
    posting = parsed_posting(
        required_skills=[{"text": "Python", "evidence_text": "熟练使用 Python 进行数据处理"}],
        preferred_skills=[], hard_constraints=[], uncertain_requirements=[], education_requirement=None,
    )
    # How a quote is read does not depend on the requirement it is cited for.
    result, _ = run(database, request_for(library.owner, [chosen]), posting, assessed(R1=[cite("S1", "actions", quote, "practice")]))
    (finding,) = result.findings
    (citation,) = finding.citations

    assert (citation.quote, citation.context, citation.flags) == (quote, context, flags)
    assert finding.conclusion == ("needs_confirmation" if flags else "supported")


def test_a_parsed_posting_of_another_text_is_not_used(database, library, monkeypatch):
    other = parse_job_description(ScriptedProvider(parsed_posting()), POSTING + "\n另一份合成岗位。")
    monkeypatch.setattr(
        experience_analysis,
        "parse_job_description_with_fallback",
        lambda provider, text: JobDescriptionOutcome(source=ParseOutcomeSource.LLM, record=other),
    )
    request = request_for(library.owner, [library.project])
    result, provider = run(database, request, assessed(**COMPLETE))

    assert (result.status, result.problem) == (AnalysisStatus.FAILED, "job_record")
    assert provider.prompts == []


# --- sources that change while the task runs -------------------------------------------


def test_a_change_between_the_calls_stops_the_task(database, library, clock):
    def edited_meanwhile(prompt):
        edit(database, library.owner, library.project[0], clock, outcome="解析期间改过")
        return parsed_posting()

    request = request_for(library.owner, [library.project, library.education])
    result, provider = run(database, request, edited_meanwhile, assessed(**COMPLETE))

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.REJECTED, "sources_changed", 1)
    assert [(item.item_id, item.reason) for item in result.selection_problems] == [(library.project[0], "not_confirmed")]
    assert len(provider.prompts) == 1


def test_a_change_after_the_analysis_makes_its_result_stale(database, library, clock):
    def edited_meanwhile(prompt):
        edit(database, library.owner, library.education[0], clock, notes="分析期间改过")
        return assessed(**COMPLETE)

    request = request_for(library.owner, [library.project, library.education])
    result, _ = run(database, request, parsed_posting(), edited_meanwhile)

    assert result.status is AnalysisStatus.STALE and not result.is_current
    assert [(item.item_id, item.reason) for item in result.selection_problems] == [(library.education[0], "not_confirmed")]
    # Shown as what it was, based on the versions that were chosen.
    assert len(result.findings) == 5 and result.sources[1].version_id == library.education[1]


# --- scores --------------------------------------------------------------------------


def test_scores_count_a_requirement_once_and_keep_conflicts_apart(database, library, clock):
    graduated = confirmed(database, library.owner, clock, "education", school="合成大学", graduation="2025 年 6 月")
    posting = parsed_posting(
        required_skills=[
            {"text": "Python", "evidence_text": "熟练使用 Python 进行数据处理"},
            {"text": "SQL", "evidence_text": "掌握 SQL 查询"},
            {"text": "python", "evidence_text": "熟练使用 Python 进行数据处理"},
        ],
        preferred_skills=[],
    )
    answer = assessed(
        R1=[cite("S1", "actions", "用 Python 清洗订单数据", "practice"), cite("S1", "actions", "用 Python 清洗订单数据，并用 SQL", "practice")],
        R2=[cite("S1", "actions", "用 SQL 汇总每周销量", "practice")],
        H1=[cite("S2", "graduation", "2025 年 6 月", "conflict")],
        U1=[],
    )
    request = request_for(library.owner, [library.project, graduated])
    result, _ = run(database, request, posting, answer)

    # Python is required once, however often the posting or the evidence names it.
    assert [finding.text for finding in result.findings if finding.group == "required"] == ["Python", "SQL"]
    assert (result.coverage.required_score, result.coverage.overall_score) == (100, 100)
    # A full skill score does not hide the conflict, which is reported on its own.
    assert [finding.requirement_id for finding in result.condition_conflicts] == ["H1"]


def test_without_skill_requirements_there_is_no_score(database, library):
    posting = parsed_posting(required_skills=[], preferred_skills=[])
    answer = assessed(H1=[cite("S1", "graduation", "2027 年 6 月（预计）", "fact")], U1=[])
    request = request_for(library.owner, [library.education])
    result, _ = run(database, request, posting, answer)

    assert result.status is AnalysisStatus.COMPLETE
    assert (result.coverage.required_score, result.coverage.preferred_score, result.coverage.overall_score) == (None, None, None)


# --- side effects ----------------------------------------------------------------------


def everything(database) -> dict:
    with database.transaction() as db:
        return {table.name: db.execute(select(table)).all() for table in LibraryBase.metadata.sorted_tables}


def test_an_analysis_writes_nothing_and_logs_no_text(database, library, caplog):
    caplog.set_level(logging.DEBUG)
    posting = POSTING + f"\n公司内部代号：{SENTINEL}\n"
    before = everything(database)
    statements = []

    def record(conn, cursor, statement, *args):
        statements.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(database.engine, "before_cursor_execute", record)

    try:
        complete, _ = run(database, request_for(library.owner, [library.project, library.education], posting=posting), parsed_posting(), assessed(**COMPLETE))
        failed, _ = run(database, request_for(library.owner, [library.project], posting=posting), parsed_posting(), f"不是 JSON {SENTINEL}")
    finally:
        event.remove(database.engine, "before_cursor_execute", record)

    assert complete.status is AnalysisStatus.COMPLETE and failed.status is AnalysisStatus.FAILED
    assert everything(database) == before
    assert set(statements) <= {"BEGIN", "SELECT"}
    assert SENTINEL not in caplog.text and SENTINEL not in repr(failed)
