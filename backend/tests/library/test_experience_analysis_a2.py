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
from app.experience_analysis import PREFERENCE_RULES_VERSION, analyze_selected_experiences
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
SKILLS_ONLY = dict(preferred_skills=[], hard_constraints=[], uncertain_requirements=[])


def run(database, request, *answers, provider=None):
    provider = provider or ScriptedProvider(*answers)

    return analyze_selected_experiences(database, request, approved(request), provider), provider


def with_preferences(answer: str, *comparisons) -> str:
    payload = json.loads(answer)
    payload["preferences"] = [
        {"preference_id": preference_id, "relation": relation, "job_quotes": list(quotes)}
        for preference_id, relation, quotes in comparisons
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
        ("F1", "consistent", ["合成科技 - 上海（混合办公）"]),
        ("F2", "not_stated", []),
        ("F3", "conflicts", ["合成科技 - 上海（混合办公）"]),
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
        (("F1", "consistent", ["工作地点：北京"]), "citation"),
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
