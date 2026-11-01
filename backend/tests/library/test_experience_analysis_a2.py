"""U2-A2 additions to the evidence analysis: chosen fields, preferences compared
with the posting, the outbound review before sending, and a real recipient
named by its endpoint. Simulated service and synthetic material only."""
import json
from dataclasses import replace

import pytest

from app.ai_provider import ProviderIdentity
from app.analysis_contracts import (
    AnalysisStatus,
    Recipient,
    SelectedPreference,
    SelectedSource,
    task_fingerprint,
)
from app.experience_analysis import (
    PREFERENCE_INSTRUCTIONS,
    PREFERENCE_RULES_VERSION,
    REQUIREMENTS_END,
    REQUIREMENTS_START,
    analyze_selected_experiences,
    build_output_format,
)
from app.library.content import PREFERENCES, parse_experience_form
from app.library.service import active_item, save_draft
from analysis_helpers import (
    POSTING,
    ScriptedProvider,
    approved,
    assessed,
    cite,
    confirmed,
    parsed_posting,
    request_for,
    saved_preferences,
)

ACTIONS = "用 Python 清洗订单数据，并用 SQL 汇总每周销量。"
OUTCOME = "看板在课程展示中使用。"
SKILLS_ONLY = dict(preferred_skills=[], hard_constraints=[], uncertain_requirements=[], education_requirement=None)


def run(database, request, *answers, provider=None):
    provider = provider or ScriptedProvider(*answers)

    return analyze_selected_experiences(database, request, approved(request), provider), provider


def with_preferences(answer: str, *comparisons) -> str:
    """comparisons: (preference id, relation, quotes, aspect of the preference, aspect of the quotes)."""
    payload = json.loads(answer)
    payload["preferences"] = [
        {
            "preference_id": preference_id,
            "aspect": aspect,
            "relation": relation,
            "job_quotes": list(quotes),
            "job_quotes_aspect": quotes_aspect,
        }
        for preference_id, relation, quotes, aspect, quotes_aspect in comparisons
    ]

    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def project(database, owners, clock):
    return confirmed(database, owners[0], clock, "project", title="校园订单看板（合成）", actions=ACTIONS, outcome=OUTCOME)


def test_only_the_chosen_fields_go_out_and_only_they_can_be_cited(database, owners, project):
    item_id, version_id = project
    request = replace(request_for(owners[0], [project]), selection=(SelectedSource(item_id, version_id, ("title", "outcome")),))
    result, provider = run(database, request, parsed_posting(**SKILLS_ONLY), assessed(R1=[], R2=[]))
    analysis_prompt = provider.prompts[1]

    assert result.status is AnalysisStatus.COMPLETE
    assert OUTCOME in analysis_prompt and ACTIONS not in analysis_prompt
    assert result.sources[0].fields == ("title", "outcome")

    # A quote from a field that was not sent fails the whole answer, even if it is in the version.
    cited, _ = run(database, request, parsed_posting(**SKILLS_ONLY), assessed(R1=[cite("S1", "actions", "用 Python 清洗订单数据", "practice")], R2=[]))

    assert (cited.status, cited.problem) == (AnalysisStatus.FAILED, "citation")


def test_without_preferences_the_posting_is_not_sent_again(database, owners, project):
    result, provider = run(database, request_for(owners[0], [project]), parsed_posting(**SKILLS_ONLY), assessed(R1=[], R2=[]))

    assert result.preferences == "not_analyzed" and result.preference_findings == ()
    assert result.analysis.preference_rules_version is None
    assert "合成科技 - 上海" not in provider.prompts[1] and "<<<PREFERENCES_START>>>" not in provider.prompts[1]


def test_chosen_preferences_are_compared_with_the_posting_word_for_word(database, owners, clock, project):
    saved_preferences(database, owners[0], clock, cities_ideal="上海", work_style_no="频繁出差", must_haves="每周至少两天远程")
    chosen = (
        SelectedPreference("cities_ideal", "上海"),
        SelectedPreference("work_style_no", "频繁出差"),
        SelectedPreference("must_haves", "每周至少两天远程"),
    )
    request = replace(request_for(owners[0], [project]), preferences=chosen)
    answer = with_preferences(
        assessed(R1=[], R2=[]),
        ("F1", "consistent", ["合成科技 - 上海（混合办公）"], "location", "location"),
        ("F2", "not_stated", [], "travel", "travel"),
        ("F3", "conflicts", ["合成科技 - 上海（混合办公）"], "work_mode", "work_mode"),
    )
    result, provider = run(database, request, parsed_posting(**SKILLS_ONLY), answer)
    findings = {finding.preference_id: finding for finding in result.preference_findings}

    assert result.status is AnalysisStatus.COMPLETE and result.preferences == "compared"
    assert result.analysis.preference_rules_version == PREFERENCE_RULES_VERSION
    # The call got the preferences and the posting itself, which the quotes come from.
    assert "<<<PREFERENCES_START>>>" in provider.prompts[1] and "合成科技 - 上海" in provider.prompts[1]
    assert (findings["F1"].field, findings["F1"].conclusion, findings["F1"].firm) == ("cities_ideal", "consistent", False)
    assert (findings["F2"].conclusion, findings["F2"].job_quotes) == ("not_stated", ())
    # A must-have the posting goes against is a conflict, shown apart; it does not touch the score.
    assert (findings["F3"].conclusion, findings["F3"].firm) == ("conflict", True)
    assert [finding.preference_id for finding in result.preference_conflicts] == ["F3"]
    assert result.coverage.overall_score == 0


@pytest.mark.parametrize(
    "comparison, failure",
    [
        (("F1", "consistent", ["工作地点：北京"], "location", "location"), "citation"),
        (None, "coverage"),
    ],
)
def test_a_comparison_that_cannot_be_trusted_fails_the_whole_answer(database, owners, clock, project, comparison, failure):
    saved_preferences(database, owners[0], clock, cities_ideal="上海")
    request = replace(request_for(owners[0], [project]), preferences=(SelectedPreference("cities_ideal", "上海"),))
    answer = with_preferences(assessed(R1=[], R2=[]), *([comparison] if comparison else []))
    result, _ = run(database, request, parsed_posting(**SKILLS_ONLY), answer)

    assert (result.status, result.problem, result.preference_findings) == (AnalysisStatus.FAILED, failure, ())


def test_preferences_changed_since_they_were_chosen_stop_the_task(database, owners, clock, project):
    record = saved_preferences(database, owners[0], clock, cities_ideal="上海")
    request = replace(request_for(owners[0], [project]), preferences=(SelectedPreference("cities_ideal", "上海"),))

    with database.transaction() as db:
        item = active_item(db, owners[0], record, kinds=(PREFERENCES,))
        save_draft(db, owners[0], record, item.revision, parse_experience_form({"cities_ideal": "杭州"}, PREFERENCES), clock.now())

    result, provider = run(database, request, parsed_posting(), assessed())

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.REJECTED, "selection_invalid", 0)
    assert [(problem.item_id, problem.reason) for problem in result.selection_problems] == [("preferences", "preferences_changed")]
    assert provider.prompts == []


@pytest.mark.parametrize("where", ["experience", "preference", "posting"])
def test_material_that_fails_the_outbound_review_is_never_sent(database, owners, clock, where):
    actions = ACTIONS + ("联系我：13800138000" if where == "experience" else "")
    chosen = confirmed(database, owners[0], clock, "project", title="合成项目", actions=actions)
    request = request_for(owners[0], [chosen], posting=POSTING + ("简历请发 hr@example.com" if where == "posting" else ""))

    if where == "preference":
        saved_preferences(database, owners[0], clock, cities_ideal="上海，电话 13800138000")
        request = replace(request, preferences=(SelectedPreference("cities_ideal", "上海，电话 13800138000"),))

    result, provider = run(database, request, parsed_posting(), assessed())

    assert (result.status, result.problem, result.requests_sent) == (AnalysisStatus.REJECTED, "outbound_blocked", 0)
    assert provider.prompts == []


def test_a_real_recipient_is_also_named_by_its_endpoint(database, owners, project):
    recipient = Recipient("simulation", "simulation", "https://api.example.invalid/v1")

    class Fixed(ScriptedProvider):
        def __init__(self, *answers, endpoint=None):
            super().__init__(*answers)
            self.endpoint = endpoint

        def identity(self):
            return ProviderIdentity("simulation", "simulation", self.endpoint)

    request = request_for(owners[0], [project], recipient=recipient)
    unfixed, unfixed_provider = run(database, request, provider=Fixed(parsed_posting(), assessed()))
    elsewhere, _ = run(database, request, provider=Fixed(parsed_posting(), assessed(), endpoint="https://proxy.example.invalid/v1"))
    named, _ = run(database, request, provider=Fixed(parsed_posting(**SKILLS_ONLY), assessed(R1=[], R2=[]), endpoint=recipient.endpoint))

    assert (unfixed.status, unfixed.problem, unfixed_provider.prompts) == (AnalysisStatus.REJECTED, "recipient_mismatch", [])
    assert (elsewhere.status, elsewhere.problem) == (AnalysisStatus.REJECTED, "recipient_mismatch")
    assert named.status is AnalysisStatus.COMPLETE


def test_the_fingerprint_covers_the_fields_the_preferences_and_the_notice(owners):
    base = request_for(owners[0], [("item", "version")])
    changes = {
        "fields": replace(base, selection=(SelectedSource("item", "version", ("title",)),)),
        "other fields": replace(base, selection=(SelectedSource("item", "version", ("outcome",)),)),
        "a preference": replace(base, preferences=(SelectedPreference("cities_ideal", "上海"),)),
        "its words": replace(base, preferences=(SelectedPreference("cities_ideal", "杭州"),)),
        "the notice": replace(base, notice_version="2"),
        "the endpoint": replace(base, recipient=Recipient("simulation", "simulation", "https://api.example.invalid/v1")),
    }
    prints = {name: task_fingerprint(request) for name, request in changes.items()}

    assert len(set(prints.values()) | {task_fingerprint(base)}) == len(changes) + 1
    # The order fields are named in does not matter.
    assert task_fingerprint(replace(base, selection=(SelectedSource("item", "version", ("title", "outcome")),))) == task_fingerprint(
        replace(base, selection=(SelectedSource("item", "version", ("outcome", "title")),))
    )


# --- U2-A2 R5 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides, answer, unlisted",
    [
        # Kept only in its field, word for word in the posting: assessed with the uncertain ones.
        (dict(hard_constraints=[]), dict(R1=[], R2=[], U1=[cite("S2", "graduation", "2025 年 6 月", "conflict")]), ["2027 年及以后毕业"]),
        # Listed as a hard condition as well: assessed there, not twice.
        ({}, dict(R1=[], R2=[], H1=[cite("S2", "graduation", "2025 年 6 月", "conflict")]), []),
        # Worded otherwise than the posting: it cannot be traced to it, so it stays out.
        (dict(hard_constraints=[], education_requirement="2027届及以后毕业生"), dict(R1=[], R2=[]), []),
    ],
)
def test_an_education_requirement_only_in_its_field_is_assessed_when_the_posting_says_it(
    database, owners, clock, project, overrides, answer, unlisted
):
    graduated = confirmed(database, owners[0], clock, "education", school="合成学院", graduation="2025 年 6 月")
    posting = parsed_posting(preferred_skills=[], uncertain_requirements=[], **overrides)
    result, _ = run(database, request_for(owners[0], [project, graduated]), posting, assessed(**answer))
    uncertain = [finding for finding in result.findings if finding.group == "uncertain"]

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert [(finding.text, finding.job_posting_quote) for finding in uncertain] == [(text, text) for text in unlisted]
    # Never a hard condition there, never scored: the conflict it cites only needs confirming.
    assert all(finding.conclusion == "needs_confirmation" for finding in uncertain)


def nothing_cited(prompt: str) -> str:
    """An analysis answer naming every requirement the call got, citing nothing."""
    requirements = json.loads(prompt.split(REQUIREMENTS_START)[1].split(REQUIREMENTS_END)[0])

    return assessed(**{requirement["requirement_id"]: [] for requirement in requirements})


GRADUATION_LINE = "任职要求：熟练使用 SQL；2027 年及以后毕业"
OTHER_WORDING = "岗位要求：熟悉 SQL，2028届及以后毕业生"
PERMIT_LINE = "任职要求：2027 年及以后毕业；需要有效的工作许可"


@pytest.mark.parametrize(
    "posting, education, listed, unlisted",
    [
        # R6 F1: a skill quoting the whole line is another requirement; the graduation one stays.
        (GRADUATION_LINE, "2027 年及以后毕业", dict(required_skills=[{"text": "SQL", "evidence_text": GRADUATION_LINE}]), ["2027 年及以后毕业"]),
        (OTHER_WORDING, "2028届及以后毕业生", dict(required_skills=[{"text": "SQL", "evidence_text": OTHER_WORDING}]), ["2028届及以后毕业生"]),
        # So is another hard condition quoting the same line.
        (PERMIT_LINE, "2027 年及以后毕业", dict(hard_constraints=[{"text": "有效的工作许可", "evidence_text": PERMIT_LINE}]), ["2027 年及以后毕业"]),
        # Listed already, by its very quote or as a condition in its own words: not added twice.
        (
            OTHER_WORDING,
            "2028届及以后毕业生",
            dict(required_skills=[{"text": "SQL", "evidence_text": "熟悉 SQL"}], hard_constraints=[{"text": "毕业届别", "evidence_text": "2028届及以后毕业生"}]),
            [],
        ),
        (
            GRADUATION_LINE,
            "2027 年及以后毕业",
            dict(
                required_skills=[{"text": "SQL", "evidence_text": GRADUATION_LINE}],
                hard_constraints=[{"text": "2027 年及以后毕业", "evidence_text": GRADUATION_LINE}],
            ),
            [],
        ),
    ],
)
def test_a_requirement_kept_only_in_its_field_is_dropped_only_when_a_listed_one_is_it(
    database, owners, clock, project, posting, education, listed, unlisted
):
    graduated = confirmed(database, owners[0], clock, "education", school="合成学院", graduation="2025 年 6 月")
    groups = dict(required_skills=[], preferred_skills=[], hard_constraints=[], uncertain_requirements=[])
    parse = parsed_posting(job_title=None, company=None, location=None, work_mode=None, education_requirement=education, **{**groups, **listed})
    result, _ = run(database, request_for(owners[0], [project, graduated], posting=posting), parse, nothing_cited)
    uncertain = [finding for finding in result.findings if finding.group == "uncertain"]

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert [finding.text for finding in uncertain] == unlisted
    # A requirement still to look at: not met, not a conflict, not scored.
    assert all(finding.conclusion == "no_evidence" for finding in uncertain) and result.condition_conflicts == ()
    assert result.coverage.required_total == len(listed.get("required_skills", []))


NO_START_DATE = "数据工程实习生（合成岗位）\n任职要求：熟练使用 SQL；2027 年及以后毕业\n工作说明：需要经常出差，每月约两周在外地"
WITH_START_DATE = NO_START_DATE + "\n入职要求：需在 2026 年 7 月前到岗"
MUST_HAVE = "2026 年 9 月之后才能全职入职"
GRADUATION = "2027 年及以后毕业"


@pytest.mark.parametrize(
    "posting, relation, aspect, quotes, quotes_aspect, conclusion, reasons",
    [
        # Quotes about another aspect leave the judgement without a basis: it needs confirming.
        # (R5 had these as "not stated", which a wrong quote cannot show; R6 F2.)
        (NO_START_DATE, "conflicts", "start_date", [GRADUATION], "qualification", "needs_confirmation", ("other_aspect",)),
        (NO_START_DATE, "conflicts", "start_date", ["数据工程实习生（合成岗位）"], "employment_type", "needs_confirmation", ("other_aspect",)),
        (NO_START_DATE, "conflicts", "start_date", ["需要经常出差，每月约两周在外地"], "travel", "needs_confirmation", ("other_aspect",)),
        # R6 F2: the posting does state a start date, but the model quoted the graduation line.
        (WITH_START_DATE, "conflicts", "start_date", [GRADUATION], "qualification", "needs_confirmation", ("other_aspect",)),
        # A preference is never about what the posting asks of the candidate.
        (NO_START_DATE, "conflicts", "qualification", [GRADUATION], "qualification", "needs_confirmation", ("other_aspect",)),
        # Words about the start date itself carry the conflict, shown apart.
        (WITH_START_DATE, "conflicts", "start_date", ["需在 2026 年 7 月前到岗"], "start_date", "conflict", ("conflicts",)),
        # Nothing about it in the posting, and the model says so: not stated, as before.
        (NO_START_DATE, "not_stated", "start_date", [], "start_date", "not_stated", ("not_stated",)),
    ],
)
def test_a_relation_stands_only_on_the_postings_words_about_its_own_aspect(
    database, owners, clock, project, posting, relation, aspect, quotes, quotes_aspect, conclusion, reasons
):
    saved_preferences(database, owners[0], clock, must_haves=MUST_HAVE)
    request = replace(request_for(owners[0], [project], posting=posting), preferences=(SelectedPreference("must_haves", MUST_HAVE),))
    parse = parsed_posting(
        job_title="数据工程实习生（合成岗位）",
        company=None,
        location=None,
        work_mode=None,
        required_skills=[{"text": "SQL", "evidence_text": "熟练使用 SQL"}],
        preferred_skills=[],
        uncertain_requirements=[],
    )
    answer = with_preferences(assessed(R1=[], H1=[]), ("F1", relation, quotes, aspect, quotes_aspect))
    result, _ = run(database, request, parse, answer)
    (finding,) = result.preference_findings

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert (finding.conclusion, finding.reasons) == (conclusion, reasons)
    assert [item.preference_id for item in result.preference_conflicts] == (["F1"] if conclusion == "conflict" else [])
    # The words the judgement rested on stay to be checked; "not stated" has none.
    assert finding.job_quotes == (() if conclusion == "not_stated" else tuple(quotes))


# --- U2-A2 R7 ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule",
    [
        "A graduation year or class the posting asks for says who may apply, not when the job starts",
        "on its own it states no start date",
        "never assume the work starts after it",
        "gives the earliest the student can start, not the month they must start",
        "only one that requires starting before that date goes against it",
        "only asks for graduates of a given year has not stated a start date",
        "start before a given date is a direct basis to compare",
        "without guessing a date",
    ],
)
def test_the_preference_instructions_keep_a_graduation_year_apart_from_a_start_date(rule):
    # What the call says; whether a real model follows it only a real run can show.
    assert rule in PREFERENCE_INSTRUCTIONS


def test_the_answer_schema_tells_the_model_what_each_aspect_means():
    described = " ".join(build_output_format(with_preferences=True).schema["$defs"]["PostingAspect"]["description"].split())

    assert "start_date, when the job starts or by when the hire must be available" in described
    assert "qualification, what the posting asks of the candidate" in described and "a graduation date or year" in described
    assert "never a start date" in described
    # Notes for developers stay out of what the model reads.
    assert "U2-A2" not in described


# --- U2-A2 R8 ----------------------------------------------------------------------------

REPEATS_POSTING = "岗位要求：必须掌握 Python；2027 年及以后毕业\nPython 项目经验是硬性要求"


@pytest.mark.parametrize(
    "required, hard, kept",
    [
        # The same words and the same quote: one requirement, assessed once, with the skills.
        ([("Python", "必须掌握 Python")], [("Python", "必须掌握 Python")], []),
        # Up to case and whitespace.
        ([("Python", "必须掌握 Python")], [("python", "必须掌握  Python")], []),
        # The same quote, other words: another requirement, kept.
        (
            [("Python", "必须掌握 Python；2027 年及以后毕业")],
            [("2027 年及以后毕业", "必须掌握 Python；2027 年及以后毕业")],
            ["2027 年及以后毕业"],
        ),
        # The same words over another quote: not certain to be the same, so kept.
        ([("Python", "必须掌握 Python")], [("Python", "Python 项目经验是硬性要求")], ["Python"]),
        # Nothing repeated: nothing changes.
        ([("Python", "必须掌握 Python")], [("2027 年及以后毕业", "2027 年及以后毕业")], ["2027 年及以后毕业"]),
    ],
)
def test_a_hard_condition_is_left_out_only_when_it_repeats_a_skill_word_for_word(database, owners, project, required, hard, kept):
    parse = parsed_posting(
        job_title=None,
        company=None,
        location=None,
        work_mode=None,
        education_requirement=None,
        required_skills=[{"text": text, "evidence_text": quote} for text, quote in required],
        preferred_skills=[],
        hard_constraints=[{"text": text, "evidence_text": quote} for text, quote in hard],
        uncertain_requirements=[],
    )
    result, provider = run(database, request_for(owners[0], [project], posting=REPEATS_POSTING), parse, nothing_cited)

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert [finding.text for finding in result.findings if finding.group == "required"] == [text for text, _ in required]
    assert [finding.text for finding in result.findings if finding.group == "hard_constraint"] == kept
    # What is left out is not sent either: the call names each requirement it assesses.
    assert provider.prompts[1].count('"group": "hard_constraint"') == len(kept)


# --- U2-A2 R9 ----------------------------------------------------------------------------

GRADUATION_ONLY = "2027 年及以后毕业"
PYTHON_AND_GRADUATION = "必须掌握 Python，2027 年及以后毕业"
PERMIT_ONLY = "需要有效的工作许可"
SQL_AND_PERMIT = "必须掌握 SQL，需要有效的工作许可"
CONDITIONS_POSTING = f"岗位要求：{PYTHON_AND_GRADUATION}；{SQL_AND_PERMIT}\n加分项：Python 为加分项"


def pair(text):
    return [{"text": text, "evidence_text": text}]


@pytest.mark.parametrize(
    "extracted, skills, hard, kept, unlisted",
    [
        # R9 (R8-F1): a condition the parse extracted, repeated in the skills and the hard
        # conditions, alone or within a longer requirement: the hard condition stays.
        (dict(education_requirement=GRADUATION_ONLY), dict(required_skills=pair(GRADUATION_ONLY)), pair(GRADUATION_ONLY), [GRADUATION_ONLY], []),
        (dict(education_requirement=GRADUATION_ONLY), dict(required_skills=pair(PYTHON_AND_GRADUATION)), pair(PYTHON_AND_GRADUATION), [PYTHON_AND_GRADUATION], []),
        # The same for a work authorisation.
        (dict(work_authorization=PERMIT_ONLY), dict(required_skills=pair(PERMIT_ONLY)), pair(PERMIT_ONLY), [PERMIT_ONLY], []),
        (dict(work_authorization=PERMIT_ONLY), dict(required_skills=pair(SQL_AND_PERMIT)), pair(SQL_AND_PERMIT), [SQL_AND_PERMIT], []),
        # Carried by a skill alone, it is not assessed as a condition: it is added, apart.
        (dict(education_requirement=GRADUATION_ONLY), dict(required_skills=pair(PYTHON_AND_GRADUATION)), [], [], [GRADUATION_ONLY]),
        (dict(work_authorization=PERMIT_ONLY), dict(required_skills=pair(PERMIT_ONLY)), [], [], [PERMIT_ONLY]),
        # A pure skill repeat, required or preferred, is still assessed once; the other condition stays.
        (
            dict(education_requirement=GRADUATION_ONLY),
            dict(required_skills=[{"text": "Python", "evidence_text": "必须掌握 Python"}]),
            [{"text": "Python", "evidence_text": "必须掌握 Python"}, *pair(GRADUATION_ONLY)],
            [GRADUATION_ONLY],
            [],
        ),
        (
            dict(education_requirement=GRADUATION_ONLY),
            dict(preferred_skills=[{"text": "Python", "evidence_text": "Python 为加分项"}]),
            [{"text": "Python", "evidence_text": "Python 为加分项"}, *pair(GRADUATION_ONLY)],
            [GRADUATION_ONLY],
            [],
        ),
    ],
)
def test_a_repeat_bearing_an_extracted_condition_keeps_the_condition(database, owners, project, extracted, skills, hard, kept, unlisted):
    parse = parsed_posting(
        job_title=None,
        company=None,
        location=None,
        work_mode=None,
        **{"education_requirement": None, "work_authorization": None, **extracted},
        **{"required_skills": [], "preferred_skills": [], **skills},
        hard_constraints=hard,
        uncertain_requirements=[],
    )
    result, _ = run(database, request_for(owners[0], [project], posting=CONDITIONS_POSTING), parse, nothing_cited)
    skill_texts = [item["text"] for group in ("required_skills", "preferred_skills") for item in skills.get(group, [])]

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    # The skills are left as the parse gave them.
    assert [finding.text for finding in result.findings if finding.group in ("required", "preferred")] == skill_texts
    assert [finding.text for finding in result.findings if finding.group == "hard_constraint"] == kept
    assert [finding.text for finding in result.findings if finding.group == "uncertain"] == unlisted


# With the education field filled (R9) or left empty (R10).
@pytest.mark.parametrize("education", [GRADUATION_ONLY, None])
def test_a_graduation_conflict_within_a_repeated_requirement_is_still_shown_apart(database, owners, clock, project, education):
    graduated = confirmed(database, owners[0], clock, "education", school="合成学院", graduation="2025 年 6 月")
    parse = parsed_posting(
        job_title=None,
        company=None,
        location=None,
        work_mode=None,
        education_requirement=education,
        required_skills=pair(PYTHON_AND_GRADUATION),
        preferred_skills=[],
        hard_constraints=pair(PYTHON_AND_GRADUATION),
        uncertain_requirements=[],
    )

    def answer(prompt):
        requirements = json.loads(prompt.split(REQUIREMENTS_START)[1].split(REQUIREMENTS_END)[0])

        return assessed(
            **{
                requirement["requirement_id"]: (
                    [cite("S2", "graduation", "2025 年 6 月", "conflict")]
                    if requirement["group"] == "hard_constraint"
                    else [cite("S1", "actions", "用 Python 清洗订单数据", "practice")]
                )
                for requirement in requirements
            }
        )

    result, _ = run(database, request_for(owners[0], [project, graduated], posting=CONDITIONS_POSTING), parse, answer)

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    # Not only a high skill score: the graduation conflict stands on its own.
    assert [(finding.requirement_id, finding.conclusion) for finding in result.condition_conflicts] == [("H1", "conflict")]
    assert result.coverage.overall_score == 100


# --- U2-A2 R10 ---------------------------------------------------------------------------

ENGLISH_COMPOUND = "Proficiency in Python and a degree in statistics"
JOINED_POSTING = f"岗位要求：{PYTHON_AND_GRADUATION}；{SQL_AND_PERMIT}\nRequired: {ENGLISH_COMPOUND}\n熟悉 Python 和 SQL"


@pytest.mark.parametrize(
    "repeated, kept",
    [
        # Evidently one skill: a pure repeat, assessed once, with the skills.
        ({"text": "Python", "evidence_text": "必须掌握 Python"}, False),
        # Both descriptive fields empty: a repeat that joins clauses keeps its hard condition.
        (pair(PYTHON_AND_GRADUATION)[0], True),
        (pair(SQL_AND_PERMIT)[0], True),
        (pair(ENGLISH_COMPOUND)[0], True),
        # One clause that names no skill of the rule baseline: kept.
        (pair(PERMIT_ONLY)[0], True),
        # Two skills joined: not evidently one skill, so kept rather than guessed.
        ({"text": "Python 和 SQL", "evidence_text": "Python 和 SQL"}, True),
    ],
)
def test_without_descriptive_fields_a_repeat_is_left_out_only_when_evidently_one_skill(database, owners, project, repeated, kept):
    parse = parsed_posting(
        job_title=None,
        company=None,
        location=None,
        work_mode=None,
        education_requirement=None,
        work_authorization=None,
        required_skills=[repeated],
        preferred_skills=[],
        hard_constraints=[repeated],
        uncertain_requirements=[],
    )
    result, _ = run(database, request_for(owners[0], [project], posting=JOINED_POSTING), parse, nothing_cited)

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert [finding.text for finding in result.findings if finding.group == "required"] == [repeated["text"]]
    assert [finding.text for finding in result.findings if finding.group == "hard_constraint"] == ([repeated["text"]] if kept else [])


# --- U2-A2 R11 ---------------------------------------------------------------------------

SKILL_LINES_POSTING = (
    "Requirements:\nSQL analysts need a master's degree\nJava engineers must be authorized to work in the US\n"
    "Python 工程师须持有效工作签证\n熟练掌握 Python 的应届毕业生\n熟练使用 SQL\nStrong SQL skills\n"
    "Nice to have:\nFamiliarity with Docker is a plus"
)


@pytest.mark.parametrize(
    "group, repeated, kept",
    [
        # The words name a skill alone, the quote adds a degree or a work permit: the quote is
        # read too, so the hard condition stays.
        ("required_skills", {"text": "SQL", "evidence_text": "SQL analysts need a master's degree"}, True),
        ("required_skills", {"text": "Python", "evidence_text": "Python 工程师须持有效工作签证"}, True),
        # And the words too: a skill quoted alone, worded with a degree.
        ("required_skills", {"text": "Python with a bachelor's degree", "evidence_text": "Python"}, True),
        # One clause, words and quote alike, that names a skill and asks for more.
        ("required_skills", pair("Java engineers must be authorized to work in the US")[0], True),
        # Asked for in a known way, then more: a known start is not enough.
        ("required_skills", pair("熟练掌握 Python 的应届毕业生")[0], True),
        # Wholly one skill in a known way, words and quote alike: assessed once, with the skills.
        ("required_skills", {"text": "SQL", "evidence_text": "熟练使用 SQL"}, False),
        ("required_skills", pair("Strong SQL skills")[0], False),
        ("preferred_skills", pair("Familiarity with Docker is a plus")[0], False),
    ],
)
def test_a_repeat_is_one_skill_only_when_its_words_and_its_quote_are_wholly_one(database, owners, project, group, repeated, kept):
    parse = parsed_posting(
        job_title=None,
        company=None,
        location=None,
        work_mode=None,
        education_requirement=None,
        work_authorization=None,
        **{"required_skills": [], "preferred_skills": [], group: [repeated]},
        hard_constraints=[repeated],
        uncertain_requirements=[],
    )
    result, _ = run(database, request_for(owners[0], [project], posting=SKILL_LINES_POSTING), parse, nothing_cited)

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    assert [finding.text for finding in result.findings if finding.group in ("required", "preferred")] == [repeated["text"]]
    assert [finding.text for finding in result.findings if finding.group == "hard_constraint"] == ([repeated["text"]] if kept else [])
