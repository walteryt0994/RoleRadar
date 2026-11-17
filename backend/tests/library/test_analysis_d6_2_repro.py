"""U3-D6 R4: the two analysis defects Codex found in the D6-2 run, reproduced offline (Claude, 2026-11-16).

Built from what D6-2 kept, never fetched again: the requirement words, the posting quotes and the practice passage are
those of the result pages' "分析依据" (ROLERADAR_V0_1_U3_D6_EVIDENCE/D6-2/02-run/pages/06-search1-result-NHQU.json and
08-search2-result-irJl.json); the passage is the frozen synthetic project's own sentence. The posting around them is
synthetic, and so is every service here: a scripted one, nothing leaves the process. What the real model will cite is
not shown by these tests; only what the local rules make of the citations it gave.

This file imports only what was there before R4, so it runs unchanged on the code before the fix (where the defects
show) and after it. A new attribute is read with getattr for the same reason.

R5: D6-2's postings were read by the Lever adapter, which writes the set line "工作方式：…" in a posting's head; an
analysis may read that line as a fact only when its request says the text is a source adapter's (posting_from_source,
set by every search). The requests here that stand for those postings say so (from_source), where the field exists.

F1: a requirement listing several items ("data structures, storage systems, cloud infrastructure, front-end
frameworks, and other technical tools") was shown as supported by one passage naming none of them.
F2: a posting quote laying down a condition ("If the posting is specified as Onsite, ...") was shown, and held, as
an unconditional requirement.

R9: "Ability to write clean, effective code" is no longer supported by that passage, which names no code (see the F1
test); on the code before R9 this one assertion fails, as it should.
"""
import json
from dataclasses import replace

import pytest

from app.analysis_contracts import AnalysisStatus, SelectedPreference
from app.discovery import rules
from app.discovery.contracts import Category, Screen, Screening
from app.experience_analysis import analyze_selected_experiences
from analysis_helpers import ScriptedProvider, approved, assessed, cite, confirmed, request_for, saved_preferences

# The frozen synthetic project of D6-1 and D6-2 (R3/00-launch/seed_d6.py), word for word.
PROJECT = {
    "title": "Course scheduling web service (synthetic)",
    "actions": "Built a small Python web service for course scheduling and wrote unit tests for its rules.",
    "outcome": "A synthetic class team used it to plan one semester project; it had no real users.",
}
PASSAGE = PROJECT["actions"]

# The posting's words, as D6-2 kept them (06 and 08, "岗位原文").
FAMILIARITY = "Familiarity with data structures, storage systems, cloud infrastructure, front-end frameworks, and other technical tools."
LANGUAGES = "Experience coding in programming languages, such as Java, C++, Python, JavaScript, or similar languages."
CLEAN_CODE = "it does matter that you can write clean, effective code"
ONSITE = "If the posting is specified as Onsite, you are required to work from an office."
# And their requirements as the parse worded them (06, the summaries).
REQUIRED = [
    {"text": "Familiarity with data structures, storage systems, cloud infrastructure, front-end frameworks, and other technical tools", "evidence_text": FAMILIARITY},
    {"text": "Experience coding in programming languages such as Java, C++, Python, JavaScript, or similar languages", "evidence_text": LANGUAGES},
    {"text": "Ability to write clean, effective code", "evidence_text": CLEAN_CODE},
]
OFFICE = {"text": "Onsite work from an office is required", "evidence_text": ONSITE}

# Synthetic lines around them, as a source adapter writes a posting's facts (app.discovery.lever).
RESEARCH = "If you are applying for the research track, a Master's degree is required."
EVERY_DAY = "You must work from our New York office five days a week."


def posting(mode: str | None = None) -> str:
    head = ["Software Engineer, Internship (synthetic)", "Synthetic Co · 地点：New York · 类型：实习"]

    if mode:
        head.append(f"工作方式：{mode}")

    return "\n".join([*head, "", FAMILIARITY, f"{LANGUAGES} Also, {CLEAN_CODE} and learn new languages quickly.", ONSITE, RESEARCH, EVERY_DAY, ""])


def parsed(required=(), hard=()) -> str:
    return json.dumps(
        {
            "job_title": "Software Engineer, Internship (synthetic)",
            "company": "Synthetic Co",
            "location": "New York",
            "work_mode": None,
            "seniority": "intern",
            "minimum_experience": None,
            "education_requirement": None,
            "work_authorization": None,
            "responsibilities": [],
            "required_skills": list(required),
            "preferred_skills": [],
            "hard_constraints": list(hard),
            "uncertain_requirements": [],
        },
        ensure_ascii=False,
    )


def run(database, request, *answers):
    return analyze_selected_experiences(database, request, approved(request), ScriptedProvider(*answers))


def from_source(request):
    """A posting a source adapter read, as D6-2's were (R5); unchanged on code without the field."""
    return replace(request, posting_from_source=True) if hasattr(request, "posting_from_source") else request


def findings(result) -> dict:
    assert result.status is AnalysisStatus.COMPLETE, (result.status, result.problem)

    return {finding.requirement_id: finding for finding in result.findings}


@pytest.fixture
def project(database, owners, clock):
    return confirmed(database, owners[0], clock, "project", **PROJECT)


@pytest.fixture
def bachelor(database, owners, clock):
    return confirmed(database, owners[0], clock, "education", school="合成大学", major="Computer Science", stage="本科")


# --- F1: a listed requirement, one passage ------------------------------------------------------------------------------


def test_d6_2_one_passage_naming_none_of_the_listed_items_does_not_support_them(database, owners, project):
    result = run(
        database,
        request_for(owners[0], [project], posting=posting()),
        parsed(required=REQUIRED),
        assessed(**{key: [cite("S1", "actions", PASSAGE, "practice")] for key in ("R1", "R2", "R3")}),
    )
    found = findings(result)

    # D6-2 showed all three as 有经历支持. The passage names Python, one of the languages any one of which is asked
    # for, and nothing of the four items the first one lists.
    assert found["R1"].conclusion == "needs_confirmation"
    assert found["R2"].conclusion == "supported"
    # R9: one item, but what it asks for has two parts ("write code"), and the passage names no code: it needs
    # confirming. R4-R8 had it supported on the model's practice label alone; stricter on purpose.
    assert (found["R3"].conclusion, found["R3"].reasons) == ("needs_confirmation", ("single_unnamed",))
    # And what is not shown stays: the listed items, none named.
    assert getattr(found["R1"], "listed", None) == ("data structures", "storage systems", "cloud infrastructure", "front-end frameworks")
    assert getattr(found["R1"], "named", None) == ()
    assert getattr(found["R2"], "named", None) == ("Python",)


# --- F2: a conditional quote -----------------------------------------------------------------------------------------


def test_d6_2_a_conditional_quote_keeps_its_condition_where_the_requirement_is_shown(database, owners, project):
    result = run(database, request_for(owners[0], [project], posting=posting()), parsed(hard=[OFFICE]), assessed(H1=[]))
    office = findings(result)["H1"]

    # The posting states no work mode: whether the condition holds is unknown, and it is kept.
    assert office.conclusion == "unknown"
    assert getattr(office, "condition", None) == "If the posting is specified as Onsite"
    assert getattr(office, "condition_state", None) == "unknown"
    # What to confirm before applying says it with its condition, never as an unconditional demand.
    assert rules.to_confirm(result, Screening(Screen.IN)) == (
        "招聘条件：Onsite work from an office is required（原文条件：If the posting is specified as Onsite）",
    )


def test_d6_2_a_condition_the_posting_rules_out_makes_the_requirement_not_apply(database, owners, project):
    result = run(database, from_source(request_for(owners[0], [project], posting=posting("remote"))), parsed(hard=[OFFICE]), assessed(H1=[]))
    office = findings(result)["H1"]

    assert office.conclusion == "not_applicable"
    assert getattr(office, "condition_fact", None) == "工作方式：remote"
    assert rules.to_confirm(result, Screening(Screen.IN)) == ()


def test_d6_2_a_condition_the_posting_states_is_met_with_its_fact_shown(database, owners, project):
    result = run(database, from_source(request_for(owners[0], [project], posting=posting("onsite"))), parsed(hard=[OFFICE]), assessed(H1=[]))
    office = findings(result)["H1"]

    # The requirement holds as written; the material says nothing of it, so it is still to be confirmed.
    assert office.conclusion == "unknown"
    assert (getattr(office, "condition_state", None), getattr(office, "condition_fact", None)) == ("met", "工作方式：onsite")


def test_d6_2_a_conflict_under_an_unknown_condition_is_not_a_conflict(database, owners, project, bachelor):
    hard = [{"text": "A Master's degree is required", "evidence_text": RESEARCH}]
    result = run(
        database,
        request_for(owners[0], [project, bachelor], posting=posting()),
        parsed(hard=hard),
        assessed(H1=[cite("S2", "stage", "本科", "conflict")]),
    )

    assert findings(result)["H1"].conclusion == "needs_confirmation"
    assert result.condition_conflicts == ()
    assert rules.classify(result, Screening(Screen.IN)) is not Category.CONFLICT


def test_d6_2_a_preference_against_a_conditional_quote_alone_needs_confirming(database, owners, clock, project):
    saved_preferences(database, owners[0], clock, work_style_no="到办公室坐班")
    chosen = (SelectedPreference("work_style_no", "到办公室坐班"),)

    def answer(posted: str) -> str:
        payload = json.loads(assessed())
        payload["preferences"] = [
            {"preference_id": "F1", "aspect": "work_mode", "relation": "conflicts", "job_quotes": [posted], "job_quotes_aspect": "work_mode"}
        ]
        return json.dumps(payload, ensure_ascii=False)

    unknown = run(database, from_source(replace(request_for(owners[0], [project], posting=posting()), preferences=chosen)), parsed(), answer(ONSITE))
    onsite = run(database, from_source(replace(request_for(owners[0], [project], posting=posting("onsite")), preferences=chosen)), parsed(), answer(ONSITE))
    plain = run(database, from_source(replace(request_for(owners[0], [project], posting=posting()), preferences=chosen)), parsed(), answer(EVERY_DAY))

    # A conditional quote alone carries no conflict; the posting's own work mode line can make it one.
    assert [finding.conclusion for finding in unknown.preference_findings] == ["needs_confirmation"]
    assert [finding.conclusion for finding in onsite.preference_findings] == ["conflict"]
    assert [finding.conclusion for finding in plain.preference_findings] == ["conflict"]


# --- what stays as it was ---------------------------------------------------------------------------------------------


def test_an_unconditional_quote_is_held_as_before(database, owners, project, bachelor):
    every_day = {"text": "Must work from the New York office five days a week", "evidence_text": EVERY_DAY}
    research = {"text": "A Master's degree is required", "evidence_text": "a Master's degree is required"}
    result = run(
        database,
        request_for(owners[0], [project, bachelor], posting=posting()),
        parsed(hard=[every_day, research]),
        assessed(H1=[], H2=[cite("S2", "stage", "本科", "conflict")]),
    )
    found = findings(result)

    assert (found["H1"].conclusion, found["H2"].conclusion) == ("unknown", "conflict")
    assert getattr(found["H1"], "condition", "") == ""
    assert rules.to_confirm(result, Screening(Screen.IN)) == ("招聘条件：Must work from the New York office five days a week",)
    assert rules.classify(result, Screening(Screen.IN)) is Category.CONFLICT
