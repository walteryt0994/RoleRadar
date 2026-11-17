"""U3-D6 R4-R9: the local reading of listed requirements (F1) and of conditional posting quotes (F2), and how they are
shown and kept (Claude, 2026-11-16). The D6-2 cases themselves are in test_analysis_d6_2_repro.py; Codex's R4 review
cases (R5), R5 review cases (R6), R6 review cases (R7), R7 review cases (R8) and R8 review cases (R9) are here word for
word, its files left as they are.

R9: a passage of practice shows a requirement read as one item only when it names every part of what it asks for, so
a requirement that must be supported is given a passage that does (one that is about the requirement, as written);
PASSAGE, about a Python web service, still stands for Python and for what asks nothing in particular.

Everything here is local: fixed rules over words, a scripted or no service at all. They show what the rules make of
the citations they are given; they do not show what a real model will cite, nor that it reads a requirement well.
"""
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import analysis_words
from app.analysis_contracts import (
    CitationBasis,
    ConditionConclusion,
    EvidenceCitation,
    RequirementFinding,
    RequirementGroup,
)
from app.discovery import records
from app.discovery.tools import child_request
from app.experience_analysis import EVIDENCE_RULES_VERSION, PREFERENCE_RULES_VERSION, _finding, analyze_selected_experiences
from app.library.job_export import IN_USE, JobExport, jobs_markdown
from app.library.web import build_templates
from app.requirement_reading import ALL, ANY, UNCLEAR, UNREAD, Listing, condition, condition_state, listing, named, reading
from app.schemas import JobRequirement
from analysis_helpers import ScriptedProvider, approved, assessed, confirmed, request_for

FAMILIARITY = "Familiarity with data structures, storage systems, cloud infrastructure, front-end frameworks, and other technical tools"
LANGUAGES = "Experience coding in programming languages such as Java, C++, Python, JavaScript, or similar languages"
ONSITE = "If the posting is specified as Onsite, you are required to work from an office."
PASSAGE = "Built a small Python web service for course scheduling and wrote unit tests for its rules."
# R9: passages that name all a requirement asks for.
CLEAN_CODE = "Wrote clean, effective code for a synthetic course service."
COMMUNICATION = "Kept communication with the client team going through written and verbal weekly updates."


def cited(quote: str, basis: str = "practice", context: tuple[str, ...] | None = None, flags=()) -> EvidenceCitation:
    return EvidenceCitation(
        item_id="11111111-2222-3333-4444-555555555555",
        version_id="v1",
        category="project",
        field="actions",
        quote=quote,
        basis=CitationBasis(basis),
        context=context or (quote,),
        flags=tuple(flags),
    )


def judged(text: str, citations=(), group=RequirementGroup.REQUIRED, quote: str | None = None, posting: str = "") -> RequirementFinding:
    return _finding("R1", group, JobRequirement(text=text, evidence_text=quote or text), tuple(citations), posting)


# --- reading a list --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "words, expected",
    [
        (FAMILIARITY, Listing(ALL, ("data structures", "storage systems", "cloud infrastructure", "front-end frameworks"))),
        (LANGUAGES, Listing(ANY, ("Java", "C++", "Python", "JavaScript"))),
        ("Experience in data analysis, visualization, and reporting", Listing(ALL, ("data analysis", "visualization", "reporting"))),
        ("Proficiency with Python or R", Listing(ANY, ("Python", "R"))),
        ("Knowledge of one of Python, R, SQL", Listing(ANY, ("Python", "R", "SQL"))),
        ("Experience with SQL, Python, Tableau, etc.", Listing(ALL, ("SQL", "Python", "Tableau"))),
        # R7: a requirement before the lead-in stays an item (R6 listed only the three after "with").
        ("Communicate with engineers, users and non-technical team members", Listing(ALL, ("Communicate", "engineers", "users", "non-technical team members"))),
        ("熟悉 Python、SQL 和 Tableau", Listing(ALL, ("Python", "SQL", "Tableau"))),
        ("掌握 Python、R 或 SQL 之一", Listing(ANY, ("Python", "R", "SQL"))),
        ("例如 Python、R", Listing(ANY, ("Python", "R"))),
        # R5: two items after a lead-in are a list too, and both are asked for (Codex's R4 review).
        ("Experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Experience with Python, SQL", Listing(ALL, ("Python", "SQL"))),
        ("熟悉 Python 和 SQL", Listing(ALL, ("Python", "SQL"))),
        ("Experience with both Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("熟练使用 Python、SQL 等工具", Listing(ALL, ("Python", "SQL"))),
        ("Experience with either Python or R", Listing(ANY, ("Python", "R"))),
        # R5: "all of" is never read as any one, whatever lead-in follows (Codex's R4 review).
        ("Proficiency in all of these required tools, including Python, SQL, and Tableau", Listing(ALL, ("Python", "SQL", "Tableau"))),
        # R5: unclear, so only every item named can support it: examples said to be required; "and" and "or" together;
        # a list inside another; something listed that does not part into plain items.
        ("Required tools, including Python, SQL, and Tableau", Listing(UNCLEAR, ("Python", "SQL", "Tableau"))),
        ("Active US Security clearance, or eligibility and willingness to obtain one", Listing(UNCLEAR, ("Active US Security clearance", "eligibility", "willingness to obtain one"))),
        ("Experience with Python, SQL, and data tools such as Tableau or Power BI", Listing(UNCLEAR, ())),
        ("Ability to communicate and collaborate with engineers, users, and non-technical team members", Listing(UNCLEAR, ())),
        ("Experience with Python, which you will use to build data pipelines for our analytics team, and SQL", Listing(UNCLEAR, ())),
        # R5: without a lead-in, a plain series of three or more ending in a conjunction.
        ("Strong analytical, communication, and teamwork skills", Listing(ALL, ("Strong analytical", "communication", "teamwork skills"))),
        ("Willingness to learn, make decisions independently, and ask questions when needed", Listing(ALL, ("Willingness to learn", "make decisions independently", "ask questions when needed"))),
        # Not a list: one skill; describing words of the fixed set joined (R6: DESCRIBING_WORDS).
        ("Python", None),
        ("Ability to write clean, effective code", None),
        # R6: a year bound after a full-width comma is no item, and 及 in 及以后 joins nothing (test_experience_analysis_a2's
        # posting).
        ("必须掌握 Python，2027 年及以后毕业", None),
        # R6: things joined without a lead-in that are not plain items read unclear, never one item (R5 read it as one).
        ("Ability to communicate and collaborate", Listing(UNCLEAR, ())),
        # R6: full-width commas between short terms part items (Codex's R5 review; R5 read it as one item).
        ("熟悉 Python，SQL，Tableau", Listing(ALL, ("Python", "SQL", "Tableau"))),
        ("掌握 Python 或 R", Listing(ANY, ("Python", "R"))),
        ("熟悉 Python、SQL 及以上版本的工具和 Tableau", Listing(ALL, ("Python", "SQL 及以上版本的工具", "Tableau"))),
        # A bound or an open end with an article is no item: what is left is read as it stands.
        ("Experience with Python, graduating in 2027 or later", Listing(ALL, ("Python", "graduating in 2027"))),
        # R7: "Degree" asks for something in its own right; with a list any one of which is asked for, only every
        # item named shows both (R6: any one of the fields).
        ("Degree in Computer Science, Engineering, or a related field", Listing(UNCLEAR, ("Degree", "Computer Science", "Engineering"))),
    ],
)
def test_a_list_is_read_only_where_the_words_plainly_list_items(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize(
    "item, passage, expected",
    [
        ("data structures", "implemented a data structure for the rules", True),
        ("front-end frameworks", "used front end frameworks", True),
        ("front-end frameworks", "built a frontend framework", True),
        ("front-end frameworks", "built the front end in React", False),
        ("C++", "wrote the parser in C++", True),
        ("Java", "wrote the page in JavaScript", False),
        ("Python", PASSAGE, True),
        ("cloud infrastructure", PASSAGE, False),
        ("Tableau", "用 Tableau 做看板", True),
        ("数据可视化", "做过数据 可视化", True),
        ("数据可视化", "做过报表", False),
    ],
)
def test_an_item_is_named_by_its_words_never_by_another_way_of_saying_it(item, passage, expected):
    assert named(item, [passage]) is expected


# --- reading a condition ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote, expected",
    [
        (ONSITE, "If the posting is specified as Onsite"),
        ("Unless stated otherwise, the role is based in New York.", "Unless stated otherwise"),
        ("You must relocate to New York if the role is onsite.", "if the role is onsite"),
        ("Travel up to 25% provided that the project needs it.", "provided that the project needs it"),
        ("如果岗位是现场办公，需要到办公室工作。", "如果岗位是现场办公"),
        # No condition: a plain demand, "when" in a clause of its own, no "if" at all.
        ("You must work from our New York office five days a week.", None),
        ("Willingness to learn and make decisions independently, and the ability to ask questions when needed.", None),
        ("An updated resume / CV - please do so in PDF format.", None),
    ],
)
def test_a_condition_is_the_posting_quote_s_own_words(quote, expected):
    assert condition(quote) == expected


@pytest.mark.parametrize(
    "wanted, posting, expected",
    [
        ("If the posting is specified as Onsite", "Title\n工作方式：onsite\nBody", ("met", "工作方式：onsite")),
        ("If the posting is specified as Onsite", "Title\n工作方式：remote\n", ("not_met", "工作方式：remote")),
        ("If the posting is specified as Onsite", "Title\n工作方式：hybrid", ("not_met", "工作方式：hybrid")),
        ("if the role is onsite", "Title\n工作方式：onsite\n", ("met", "工作方式：onsite")),
        ("If the position is a remote role", "Title\n工作方式：remote\n", ("met", "工作方式：remote")),
        ("如果岗位是现场办公", "Title\n工作方式：onsite\n", ("met", "工作方式：onsite")),
        # Unknown: no set line, a value that is no work mode, a line not of its own, a condition about something else.
        ("If the posting is specified as Onsite", "Title\nBody says the role is onsite.", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title\n工作方式：unspecified\n", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title · 工作方式：onsite\n", ("unknown", "")),
        ("If you are applying to the London office", "Title\n工作方式：onsite\n", ("unknown", "")),
        ("If the role is remote or hybrid", "Title\n工作方式：remote\n", ("unknown", "")),
        # R5 (Codex's R4 review): unless, not and a second condition are not read; only the adapter's exact values are
        # facts, only once and only in the head, before the blank line and the source's own description.
        ("Unless the role is remote", "Title\n工作方式：remote\n", ("unknown", "")),
        ("Unless the role is remote", "Title\n工作方式：onsite\n", ("unknown", "")),
        ("If the role is not remote", "Title\n工作方式：onsite\n", ("unknown", "")),
        ("If the role is onsite and based in London", "Title\n工作方式：onsite\n", ("unknown", "")),
        ("如果岗位不是远程", "Title\n工作方式：onsite\n", ("unknown", "")),
        ("If the role is remote", "Title\n工作方式：not remote\n", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title\n工作方式：on-site\n", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title\n工作方式：Onsite\n", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title\n工作方式：onsite\n工作方式：remote\n", ("unknown", "")),
        ("If the posting is specified as Onsite", "Title\n\nThe description:\n工作方式：onsite\n", ("unknown", "")),
    ],
)
def test_a_condition_holds_or_fails_only_on_the_posting_s_set_work_mode_line(wanted, posting, expected):
    assert condition_state(wanted, posting) == expected


# --- F1: what a passage of practice supports ------------------------------------------------------------------------


def test_a_listed_requirement_is_supported_when_its_practice_names_every_item():
    finding = judged("Experience with Python, SQL, and Tableau", [cited("Built dashboards in Tableau using Python and SQL.")])

    assert (finding.conclusion, finding.reasons) == ("supported", ("practice",))
    assert (finding.listed, finding.listed_mode, finding.named) == (("Python", "SQL", "Tableau"), "all", ("Python", "SQL", "Tableau"))


def test_items_may_be_named_across_passages_and_in_what_a_quote_is_read_with():
    finding = judged(
        "Experience with Python, SQL, and Tableau",
        [cited("Cleaned data with Python"), cited("Tableau", context=("Summed sales with SQL and drew them in Tableau",))],
    )

    assert finding.conclusion == "supported" and finding.named == ("Python", "SQL", "Tableau")


def test_a_listed_requirement_partly_named_needs_confirming_and_keeps_what_is_unproven():
    finding = judged("Experience with Python, SQL, and Tableau", [cited("Cleaned data with Python and SQL.")])

    assert (finding.conclusion, finding.reasons, finding.named) == ("needs_confirmation", ("listed_partly",), ("Python", "SQL"))
    assert analysis_words.listed_parts(finding.listed, finding.named) == (["Python", "SQL"], ["Tableau"])


def test_a_passage_unrelated_to_the_listed_items_supports_none_of_them():
    finding = judged("Experience with Python, SQL, and Tableau", [cited("Organised a campus club event.")])

    assert (finding.conclusion, finding.reasons, finding.named) == ("needs_confirmation", ("listed_unnamed",), ())


def test_any_one_item_is_enough_where_any_one_is_asked_for():
    supported = judged(LANGUAGES, [cited(PASSAGE)])
    unnamed = judged(LANGUAGES, [cited("Wrote the course rules in Go.")])

    assert (supported.conclusion, supported.named) == ("supported", ("Python",))
    assert (unnamed.conclusion, unnamed.reasons) == ("needs_confirmation", ("listed_unnamed",))


def test_chinese_lists_are_read_as_written():
    partly = judged("熟悉 Python、SQL 和 Tableau", [cited("用 Python 和 SQL 清洗订单数据")])
    any_one = judged("掌握 Python、R 或 SQL 之一", [cited("用 SQL 汇总每周销量")])

    assert (partly.conclusion, partly.named) == ("needs_confirmation", ("Python", "SQL"))
    assert (any_one.conclusion, any_one.named) == ("supported", ("SQL",))


@pytest.mark.parametrize(
    "words, passage",
    [
        # R9: each with a passage that names what it asks for (R4-R8 gave all of them PASSAGE).
        ("Ability to write clean, effective code", CLEAN_CODE),
        ("Python", PASSAGE),
        # R6: the protections kept, and describing words joined however the head before them is worded.
        ("必须掌握 Python，2027 年及以后毕业", PASSAGE),
        ("Write clean, effective code", CLEAN_CODE),
        ("Excellent written and verbal communication skills", COMMUNICATION),
    ],
)
def test_a_requirement_that_lists_nothing_is_supported_by_practice_as_before(words, passage):
    finding = judged(words, [cited(passage)])

    assert (finding.conclusion, finding.reasons, finding.listed, finding.named) == ("supported", ("practice",), (), ())


@pytest.mark.parametrize("words", ["Experience with Python and SQL", "Experience with Python, SQL", "熟悉 Python 和 SQL"])
def test_two_listed_items_both_need_naming_r5(words):
    partly = judged(words, [cited(PASSAGE)])
    both = judged(words, [cited("Built a Python service that runs SQL queries.")])

    assert (partly.conclusion, partly.reasons, partly.named) == ("needs_confirmation", ("listed_partly",), ("Python",))
    assert (both.conclusion, both.named) == ("supported", ("Python", "SQL"))


def test_an_unclear_list_is_supported_only_when_every_item_is_named_r5():
    words = "Required tools, including Python, SQL, and Tableau"
    every = judged(words, [cited("Built dashboards using Python, SQL, and Tableau.")])
    partly = judged(words, [cited(PASSAGE)])
    nested = judged("Experience with Python, SQL, and data tools such as Tableau or Power BI", [cited("Drew charts in Tableau with Python and SQL.")])

    assert (every.conclusion, every.listed_mode) == ("supported", UNCLEAR)
    assert (partly.conclusion, partly.reasons, partly.named) == ("needs_confirmation", ("listed_unclear",), ("Python",))
    # A list inside another: its items are not told apart, so no passage can show them all.
    assert (nested.conclusion, nested.reasons, nested.listed) == ("needs_confirmation", ("listed_unclear",), ())


def test_all_of_is_never_read_as_any_one_r5():
    finding = judged("Proficiency in all of these required tools, including Python, SQL, and Tableau", [cited(PASSAGE)])

    assert (finding.conclusion, finding.listed_mode, finding.named) == ("needs_confirmation", ALL, ("Python",))


def test_a_requirement_the_parse_split_from_a_listing_line_is_one_requirement():
    """A posting line listing three skills, split by the parse into three, each quoting the whole line: each is read by
    its own words, never by the line's."""
    line = "Experience with Python, SQL, and Tableau."
    found = [judged(skill, [cited(f"Cleaned data with {skill}")], quote=line) for skill in ("Python", "SQL", "Tableau")]

    assert [(finding.conclusion, finding.listed) for finding in found] == [("supported", ())] * 3


def test_the_other_rules_hold_for_a_listed_requirement_as_before():
    flagged = judged(FAMILIARITY, [cited("plan to learn data structures", flags=("plan",))])
    denied = judged(FAMILIARITY, [cited(PASSAGE), cited("did not use cloud infrastructure", basis="negated")])
    nothing = judged(FAMILIARITY)

    assert (flagged.conclusion, flagged.reasons, flagged.named) == ("needs_confirmation", ("flag:plan",), ())
    assert (denied.conclusion, denied.reasons) == ("needs_confirmation", ("contradiction",))
    assert (nothing.conclusion, nothing.reasons, nothing.named) == ("no_evidence", ("no_material",), ())
    # What its words list is kept all the same, to be shown.
    assert nothing.listed == ("data structures", "storage systems", "cloud infrastructure", "front-end frameworks")


def test_a_hard_constraint_is_not_read_as_a_list_of_skills():
    degrees = "Degree in fields such as Computer Science, Mathematics, Software Engineering, and Physics"
    finding = judged(degrees, [cited("Computer Science", basis="fact")], group=RequirementGroup.HARD_CONSTRAINT)

    assert (finding.conclusion, finding.listed, finding.named) == ("met", (), ())


# --- F2: what a condition does to a judgement -------------------------------------------------------------------------


def test_a_skill_under_a_condition_keeps_its_judgement_with_the_condition_beside_it():
    quote = "If you join the data team, experience with SQL is required."
    unknown = judged("SQL", [cited("Summed weekly sales with SQL.")], quote=quote)
    not_met = judged("SQL", [], quote="If the posting is specified as Onsite, SQL is required.", posting="t\n工作方式：remote\n")

    assert (unknown.conclusion, unknown.reasons, unknown.condition) == ("supported", ("condition_unknown", "practice"), "If you join the data team")
    assert (not_met.conclusion, not_met.reasons) == ("no_evidence", ("condition_not_met", "no_material"))
    assert not_met.condition_fact == "工作方式：remote"


def test_a_hard_constraint_met_by_the_material_stays_met_whatever_its_condition():
    quote = "If you are applying for the research track, a Master's degree is required."
    finding = judged("A Master's degree is required", [cited("硕士", basis="fact")], group=RequirementGroup.HARD_CONSTRAINT, quote=quote)

    assert (finding.conclusion, finding.reasons) == ("met", ("condition_unknown", "fact"))


def test_a_hard_constraint_whose_condition_does_not_hold_does_not_apply_whatever_the_material_says():
    finding = judged(
        "Onsite work from an office is required",
        [cited("本科", basis="conflict")],
        group=RequirementGroup.HARD_CONSTRAINT,
        quote=ONSITE,
        posting="t\n工作方式：remote\n",
    )

    assert (finding.conclusion, finding.reasons) == (ConditionConclusion.NOT_APPLICABLE.value, ("condition_not_met",))
    assert analysis_words.conclusion_label(finding) == "不适用"


def test_the_versions_say_the_rules_changed():
    # R4: 3, 4, 2; R5: the evidence and preference rules again, the kept content as it was; R6-R9: the evidence rules
    # only (the preference rules read no lists; an unread finding, or one item not all named, is kept in the same shape).
    # R10 (U3-D6-3): the evidence rules again (F1, F2); a regrouped requirement is kept in the same shape.
    # R11: again, for how far a denial reaches (F1 of Codex's review of R10).
    # R12: again, for where a soft word is on the whole of what a quote asks (F1 of Codex's review of R11).
    assert (EVIDENCE_RULES_VERSION, PREFERENCE_RULES_VERSION, records.ANALYSIS_CONTENT_VERSION) == ("11", "5", 2)


# --- R5: whose text the set line may be read in ------------------------------------------------------------------------


HEAD = "Software Engineer, Internship (synthetic)\nSynthetic Co · 地点：New York · 类型：实习\n工作方式：remote\n\n"
OFFICE = {"text": "Onsite work from an office is required", "evidence_text": ONSITE}


def parsed_office() -> str:
    return json.dumps(
        {
            "job_title": None, "company": None, "location": None, "work_mode": None, "seniority": None,
            "minimum_experience": None, "education_requirement": None, "work_authorization": None, "responsibilities": [],
            "required_skills": [], "preferred_skills": [], "hard_constraints": [OFFICE], "uncertain_requirements": [],
        },
        ensure_ascii=False,
    )


def test_a_pasted_posting_s_words_are_never_taken_for_the_adapter_s_line(database, owners, clock):
    project = confirmed(database, owners[0], clock, "project", title="Course scheduling web service (synthetic)", actions=PASSAGE)
    posting = HEAD + ONSITE + "\n"
    pasted = request_for(owners[0], [project], posting=posting)
    read = replace(pasted, posting_from_source=True)
    results = [
        analyze_selected_experiences(database, request, approved(request), ScriptedProvider(parsed_office(), assessed(H1=[])))
        for request in (pasted, read)
    ]
    (from_paste,), (from_source,) = (result.findings for result in results)

    # The same words: pasted, the condition stays unknown and the requirement is still to confirm with its condition;
    # read by a source adapter, its line shows the condition does not hold.
    assert (from_paste.conclusion, from_paste.condition_state, from_paste.condition_fact) == ("unknown", "unknown", "")
    assert (from_source.conclusion, from_source.condition_state, from_source.condition_fact) == ("not_applicable", "not_met", "工作方式：remote")
    assert not pasted.posting_from_source


def test_a_search_s_analysis_is_of_a_posting_a_source_adapter_read():
    confirmation = SimpleNamespace(owner_id="o", selection=(), recipient=None, preferences=(), notice_version="n")

    assert child_request(confirmation, "text").posting_from_source is True


# --- Codex's R4 review cases, word for word (/private/tmp/roleradar-d6-r4-review-8XAnci/test_r4_boundaries.py) ---------


def codex_citation(quote, basis="practice"):
    return EvidenceCitation(
        item_id="synthetic-project", version_id="synthetic-v1",
        category="project", field="actions", quote=quote,
        basis=CitationBasis(basis), context=(quote,), flags=(),
    )


def codex_skill(words, passage):
    return _finding("R1", RequirementGroup.REQUIRED, JobRequirement(text=words, evidence_text=words), (codex_citation(passage),), "")


def codex_constrained(condition_words, mode):
    quote = f"{condition_words}, you must work from an office."
    posting = f"Synthetic posting\n工作方式：{mode}\n{quote}"
    return _finding(
        "H1", RequirementGroup.HARD_CONSTRAINT,
        JobRequirement(text="Must work from an office", evidence_text=quote),
        (codex_citation("I can work from an office.", "fact"),), posting,
    )


@pytest.mark.parametrize("words", ["Experience with Python and SQL", "Experience with Python, SQL", "熟悉 Python 和 SQL"])
def test_r4_review_two_explicit_required_skills_are_not_fully_supported_by_one(words):
    assert codex_skill(words, "Built a small Python service and wrote unit tests.").conclusion != "supported"


def test_r4_review_including_does_not_turn_explicitly_required_tools_into_alternatives():
    result = codex_skill("Proficiency in all of these required tools, including Python, SQL, and Tableau", "Built a small Python service and wrote unit tests.")
    assert result.conclusion != "supported"


@pytest.mark.parametrize("condition_words,mode,expected", [
    ("Unless the role is remote", "remote", "not_met"),
    ("Unless the role is remote", "onsite", "met"),
    ("If the role is not remote", "remote", "not_met"),
    ("If the role is not remote", "onsite", "met"),
])
def test_r4_review_negation_and_unless_must_not_reverse_conditional_applicability(condition_words, mode, expected):
    result = codex_constrained(condition_words, mode)
    assert result.condition_state in (expected, "unknown")
    if expected == "met":
        assert result.conclusion != "not_applicable"


def test_r4_review_onsite_alone_does_not_prove_a_compound_location_condition():
    assert codex_constrained("If the role is onsite and based in London", "onsite").condition_state == "unknown"


def test_r4_review_a_negated_work_mode_value_is_not_a_positive_fact():
    assert codex_constrained("If the role is remote", "not remote").condition_state != "met"


def test_r4_review_original_multiskill_case_remains_guarded():
    assert codex_skill(FAMILIARITY, PASSAGE).conclusion == "needs_confirmation"


def test_r4_review_complete_three_skill_support_still_passes():
    assert codex_skill("Experience with Python, SQL, and Tableau", "Built dashboards using Python, SQL, and Tableau.").conclusion == "supported"


def test_r4_review_explicit_one_of_support_still_passes():
    assert codex_skill("Knowledge of one of Python, R, SQL", "Built a data service with Python.").conclusion == "supported"


@pytest.mark.parametrize("mode,expected", [("onsite", "met"), ("remote", "not_met")])
def test_r4_review_simple_positive_condition_still_works(mode, expected):
    assert codex_constrained("If the posting is specified as Onsite", mode).condition_state == expected


# --- Codex's R5 review cases, word for word (/private/tmp/roleradar-d6-r5-review-rvIW2o/test_r5_boundaries.py) ---------
# Its helpers citation() and skill() are the R4 file's, word for word: codex_citation() and codex_skill() above.


@pytest.mark.parametrize("words", [
    "Python and SQL",
    "Python, SQL",
    "Python、SQL",
    "熟悉 Python，SQL，Tableau",
])
def test_r5_review_explicit_skill_items_without_leadin_or_with_chinese_commas_are_not_one_item(words):
    found = codex_skill(words, "Built a Python service and wrote unit tests.")
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("prefix", ["Workplace policy. ", "Workplace policy:\n"])
def test_r5_review_condition_after_short_heading_or_sentence_is_not_lost(prefix):
    quote = prefix + "If the role is onsite, you must work from an office."
    found = _finding(
        "H1", RequirementGroup.HARD_CONSTRAINT,
        JobRequirement(text="Must work from an office", evidence_text=quote),
        (codex_citation("I cannot work from an office.", "conflict"),), "",
    )
    assert found.conclusion != "conflict", found
    assert found.condition_state == "unknown", found
    assert "If the role is onsite" in found.condition, found


@pytest.mark.parametrize("words, passage", [
    ("Python", "Built a Python service."),
    ("Experience with Python and SQL", "Built a Python service with SQL queries."),
    ("Proficiency in all of these required tools, including Python, SQL, and Tableau",
     "Built dashboards with Python, SQL, and Tableau."),
    ("Knowledge of one of Python, R, SQL", "Built a Python service."),
])
def test_r5_review_known_complete_and_alternative_support_remains_supported(words, passage):
    assert codex_skill(words, passage).conclusion == "supported"


def test_r5_review_plain_unconditional_office_constraint_still_can_conflict():
    quote = "You must work from an office."
    found = _finding("H1", RequirementGroup.HARD_CONSTRAINT,
                     JobRequirement(text=quote, evidence_text=quote),
                     (codex_citation("I cannot work from an office.", "conflict"),), "")
    assert found.conclusion == "conflict" and not found.condition


def test_r5_review_original_onsite_conditional_stays_unknown_without_source_facts():
    quote = "If the role is onsite, you must work from an office."
    found = _finding("H1", RequirementGroup.HARD_CONSTRAINT,
                     JobRequirement(text="Must work from an office", evidence_text=quote),
                     (codex_citation("I cannot work from an office.", "conflict"),), "")
    assert found.conclusion == "needs_confirmation" and found.condition_state == "unknown"


# --- R6: what may list items is never read as one item ------------------------------------------------------------------

# The same items with a lead-in and without one, parted by and, a comma, 、, 和, a full-width comma or semicolon, & or
# "as well as"; and any one of them, by or.
SAME_ITEMS = [
    ("Experience with Python and SQL", "Python and SQL"),
    ("Experience with Python, SQL", "Python, SQL"),
    ("熟悉 Python、SQL", "Python、SQL"),
    ("熟悉 Python 和 SQL", "Python 和 SQL"),
    ("熟悉 Python，SQL", "Python，SQL"),
    ("熟悉 Python；SQL", "Python；SQL"),
    ("熟悉 Python，SQL，Tableau", "Python，SQL，Tableau"),
    ("熟悉 Python、SQL 和 Tableau", "Python、SQL 和 Tableau"),
    ("Experience with Python & SQL", "Python & SQL"),
    ("Experience with Python as well as SQL", "Python as well as SQL"),
    ("Proficiency with Python or R", "Python or R"),
    ("掌握 Python 或 R", "Python 或 R"),
]
EVERY_ONE = [unled for _, unled in SAME_ITEMS if " or " not in unled and "或" not in unled]


@pytest.mark.parametrize("led, unled", SAME_ITEMS)
def test_the_same_items_read_the_same_with_a_lead_in_or_without_one_r6(led, unled):
    found = listing(unled)

    assert found == listing(led) and found is not None and len(found.items) >= 2 and found.mode != UNCLEAR


@pytest.mark.parametrize("words", EVERY_ONE + ["Must know Python and SQL", "需要 Python 和 SQL", "Ability to use Python, SQL"])
def test_items_without_a_lead_in_each_need_naming_r6(words):
    partly = judged(words, [cited(PASSAGE)])
    every = judged(words, [cited("Built a Python service with SQL queries and Tableau dashboards.")])

    assert (partly.conclusion, partly.reasons, partly.named) == ("needs_confirmation", ("listed_partly",), ("Python",))
    assert every.conclusion == "supported"


@pytest.mark.parametrize("words", ["Python or R", "Python 或 R", "Python and/or R"])
def test_any_one_item_without_a_lead_in_is_enough_where_any_one_is_asked_for_r6(words):
    assert judged(words, [cited(PASSAGE)]).conclusion == "supported"
    assert judged(words, [cited("Wrote unit tests for the scheduler.")]).conclusion == "needs_confirmation"


@pytest.mark.parametrize(
    "words, expected",
    [
        # An English comma: items; describing words of the fixed set; or unclear. A word not in the set is never
        # taken for one, the same shape though it is.
        ("Python, SQL", Listing(ALL, ("Python", "SQL"))),
        ("Data analysis, data visualization", Listing(ALL, ("Data analysis", "data visualization"))),
        ("Ability to use Python, SQL", Listing(ALL, ("Python", "SQL"))),
        ("Ability to write clean, effective code", None),
        ("Write clean, effective code", None),
        ("Ability to write clean, effective, and maintainable code", None),
        ("Ability to build dashboards, data pipelines", Listing(UNCLEAR, ())),
        ("Ability to apply machine learning, deep learning", Listing(UNCLEAR, ())),
        # 、 parts items with a lead-in or without one, the words before the first item's names aside.
        ("Python、SQL", Listing(ALL, ("Python", "SQL"))),
        ("数据分析、可视化", Listing(ALL, ("数据分析", "可视化"))),
        ("要求 Python、SQL", Listing(ALL, ("Python", "SQL"))),
        # A full-width comma or semicolon: items between short terms; a clause that is only a year bound is no item;
        # any other clause may ask for more, so the words read unclear.
        ("熟悉 Python，SQL，Tableau", Listing(ALL, ("Python", "SQL", "Tableau"))),
        ("Python，SQL", Listing(ALL, ("Python", "SQL"))),
        ("数据分析，可视化，报表", Listing(ALL, ("数据分析", "可视化", "报表"))),
        ("熟悉 Python，SQL，Tableau 等工具", Listing(ALL, ("Python", "SQL", "Tableau"))),
        ("熟悉 Python，SQL，2026 届毕业生", Listing(ALL, ("Python", "SQL"))),
        ("必须掌握 Python，2027 年及以后毕业", None),
        ("Python，2027 年及以后毕业", None),
        ("2027 年及以后毕业，熟悉 Python", None),
        ("掌握 Python，熟悉 SQL", Listing(UNCLEAR, ())),
        ("熟悉 Python，3 年以上数据分析经验", Listing(UNCLEAR, ())),
    ],
)
def test_commas_part_items_only_where_they_plainly_do_r6(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize(
    "words",
    [
        "Ability to communicate and collaborate",
        "Ability to build dashboards, data pipelines",
        "掌握 Python，熟悉 SQL",
        "熟悉 Python，3 年以上数据分析经验",
        "Python and experience with SQL",
    ],
)
def test_what_may_list_items_but_does_not_read_plainly_needs_confirming_r6(words):
    finding = judged(words, [cited(PASSAGE)])

    assert (finding.conclusion, finding.reasons, finding.listed_mode) == ("needs_confirmation", ("listed_unclear",), UNCLEAR)


@pytest.mark.parametrize(
    "words, passage",
    [
        ("Python and SQL", "Built a Python service with SQL queries."),
        ("Python, SQL", "Built a Python service with SQL queries."),
        ("Python、SQL", "用 Python 和 SQL 做了报表。"),
        ("Python，SQL，Tableau", "Built dashboards using Python, SQL, and Tableau."),
        ("Must know Python and SQL", "Built a Python service with SQL queries."),
        ("需要 Python 和 SQL", "用 Python 和 SQL 做了报表。"),
        ("数据分析、可视化", "做过数据分析和可视化。"),
    ],
)
def test_items_without_a_lead_in_all_named_are_supported_r6(words, passage):
    assert judged(words, [cited(passage)]).conclusion == "supported"


# --- Codex's R6 review cases, word for word (/private/tmp/roleradar-d6-r6-review-L5r70E/test_r6_boundaries.py) ---------


def codex_judge(words, passage):
    citation = EvidenceCitation(
        item_id="synthetic-project", version_id="synthetic-v1", category="project",
        field="actions", quote=passage, basis=CitationBasis.PRACTICE,
        context=(passage,), flags=(),
    )
    return _finding(
        "R1", RequirementGroup.REQUIRED,
        JobRequirement(text=words, evidence_text=words), (citation,), "",
    )


@pytest.mark.parametrize("words", [
    "Python plus SQL",
    "Python along with SQL",
    "Python/SQL",
])
def test_r6_review_additional_or_ambiguous_join_is_not_proof_of_the_whole(words):
    found = codex_judge(words, "Built a Python web service and wrote unit tests.")
    # Plus / along with explicitly add another skill; slash's all/any ambiguity
    # is not a basis for a confident whole-requirement conclusion.
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words", [
    "machine learning with Python and SQL",
    "database administration using Python and SQL",
])
def test_r6_review_tool_names_do_not_prove_the_substantive_requirement_before_leadin(words):
    found = codex_judge(words, "Built a course-scheduling web service with Python and SQL.")
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words, passage", [
    ("Python", "Built a Python web service."),
    ("CI/CD", "Built and operated a CI/CD pipeline."),
    ("A/B testing", "Ran A/B testing for the synthetic site."),
    ("Knowledge of one of Python, R, SQL", "Built a Python service."),
    ("Experience with Python and SQL", "Built a Python service with SQL queries."),
    ("machine learning with Python and SQL", "Built a machine learning workflow with Python and SQL."),
])
def test_r6_review_atomic_terms_explicit_alternatives_and_complete_evidence_still_pass(words, passage):
    assert codex_judge(words, passage).conclusion == "supported"


# --- R7: joins the reading did not know, and what stands before a lead-in ----------------------------------------------

TOOLS_ONLY = "Built a course-scheduling web service with Python and SQL."
PYTHON_ONLY = "Built a Python web service and wrote unit tests."


@pytest.mark.parametrize(
    "words",
    [
        # Joins that add an item, with a lead-in and without one.
        "Python plus SQL", "Python, plus SQL", "Python + SQL", "Python along with SQL", "Python together with SQL",
        "Python combined with SQL", "Experience with Python plus SQL", "Experience with Python along with SQL",
    ],
)
def test_joins_that_add_an_item_list_both_r7(words):
    partly = judged(words, [cited(PYTHON_ONLY)])
    both = judged(words, [cited("Built a Python service with SQL queries.")])

    assert listing(words) == Listing(ALL, ("Python", "SQL"))
    assert (partly.conclusion, partly.reasons, partly.named) == ("needs_confirmation", ("listed_partly",), ("Python",))
    assert both.conclusion == "supported"


@pytest.mark.parametrize(
    "words", ["Familiarity with Docker is a plus", "Docker is a plus", "Docker is a plus for this role", "Experience with Docker is a plus for the team"]
)
def test_a_plus_is_no_join_r7(words):
    assert listing(words) is None


@pytest.mark.parametrize(
    "words, expected",
    [
        # A label before a colon is no requirement, whatever lead-in follows; other words before a colon are.
        ("岗位要求：熟练使用 Python", None),
        ("岗位要求：熟悉 Python 和 SQL", Listing(ALL, ("Python", "SQL"))),
        ("任职要求：熟练使用 SQL；2026 届及以后毕业", None),
        ("Requirements: Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Nice to have: Docker and Kubernetes", Listing(ALL, ("Docker", "Kubernetes"))),
        ("Machine learning: Python and SQL", Listing(ALL, ("Machine learning", "Python", "SQL"))),
        # Who is asked, and list marks, are no requirement either.
        ("You will work with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Candidates should be proficient in Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("你需要熟悉 Python 和 SQL", Listing(ALL, ("Python", "SQL"))),
        ("- Familiarity with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        # An address is no list.
        ("https://careers.example.com/jobs", None),
        ("www.example.com/careers", None),
        # The dot a lead-in leaves behind is no part of an item.
        ("Statistics, e.g. regression and hypothesis testing", Listing(UNCLEAR, ("Statistics", "regression", "hypothesis testing"))),
    ],
)
def test_labels_who_is_asked_list_marks_and_addresses_are_no_items_r7(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize(
    "words, expected",
    [
        # A slash between two words that are not one term: both named, whichever it means.
        ("Python/SQL", Listing(UNCLEAR, ("Python", "SQL"))),
        ("Python / SQL", Listing(UNCLEAR, ("Python", "SQL"))),
        ("Experience with Python/SQL", Listing(UNCLEAR, ("Python", "SQL"))),
        ("熟悉 Python/SQL", Listing(UNCLEAR, ("Python", "SQL"))),
        ("熟悉 Python／SQL", Listing(UNCLEAR, ("Python", "SQL"))),
        ("数据分析/可视化", Listing(UNCLEAR, ("数据分析", "可视化"))),
        ("CI/CD", Listing(UNCLEAR, ("CI", "CD"))),
        # Unless the words say which.
        ("Knowledge of one of Python/R/SQL", Listing(ANY, ("Python", "R", "SQL"))),
        # One term: two single letters, a number, "and/or".
        ("A/B testing", None),
        ("I/O", None),
        ("24/7 on-call support", None),
        ("Python and/or SQL", Listing(ANY, ("Python", "SQL"))),
    ],
)
def test_a_slash_joins_items_unless_it_stands_in_one_term_r7(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize("words", ["Python/SQL", "熟悉 Python／SQL", "Experience with Python/SQL"])
def test_a_slash_list_needs_every_item_named_r7(words):
    partly = judged(words, [cited(PYTHON_ONLY)])
    both = judged(words, [cited("Built a Python service with SQL queries.")])

    assert (partly.conclusion, partly.reasons, partly.named) == ("needs_confirmation", ("listed_unclear",), ("Python",))
    assert both.conclusion == "supported"


@pytest.mark.parametrize(
    "words, expected",
    [
        # A requirement before a lead-in stays an item, with one item after it or several, whatever lead-in it is.
        ("machine learning with Python and SQL", Listing(ALL, ("machine learning", "Python", "SQL"))),
        ("database administration using Python and SQL", Listing(ALL, ("database administration", "Python", "SQL"))),
        ("machine learning with Python", Listing(ALL, ("machine learning", "Python"))),
        ("Experience in machine learning with Python and SQL", Listing(ALL, ("machine learning", "Python", "SQL"))),
        ("Python experience with Django", Listing(ALL, ("Python", "Django"))),
        ("数据库管理使用 Python 和 SQL", Listing(ALL, ("数据库管理", "Python", "SQL"))),
        # Without a lead-in, before the first item's names.
        ("Machine learning via Python and SQL", Listing(ALL, ("Machine learning", "Python", "SQL"))),
        # A lead-in at the end of what stands before belongs to what follows.
        ("machine learning in one of Python or R", Listing(UNCLEAR, ("machine learning", "Python", "R"))),
        # With any one of a list: only every item named shows both.
        ("Statistical analysis in R or Python", Listing(UNCLEAR, ("Statistical analysis", "R", "Python"))),
        # Before a lead-in of examples: a requirement unless its last word names a kind of thing.
        ("Data analysis including SQL and Excel", Listing(UNCLEAR, ("Data analysis", "SQL", "Excel"))),
        ("Data analysis including SQL", Listing(UNCLEAR, ("Data analysis", "SQL"))),
        ("Experience with data visualization tools such as Tableau or Power BI", Listing(ANY, ("Tableau", "Power BI"))),
        ("Machine learning frameworks such as TensorFlow or PyTorch", Listing(ANY, ("TensorFlow", "PyTorch"))),
        (LANGUAGES, Listing(ANY, ("Java", "C++", "Python", "JavaScript"))),
        # Only the wording of a lead-in before it: left out, as before.
        ("Experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("3+ years of professional experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Hands-on experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Strong working knowledge of Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Requirements: experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Experience with one of Python or R", Listing(ANY, ("Python", "R"))),
        ("熟练使用 Python 和 SQL", Listing(ALL, ("Python", "SQL"))),
        ("Must know Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Experience with Python", None),
        ("Knowledge of Python", None),
        ("Ability to work in a team", None),
    ],
)
def test_what_stands_before_a_lead_in_is_an_item_when_it_asks_for_something_r7(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize(
    "words",
    [
        "machine learning with Python and SQL",
        "database administration using Python and SQL",
        "machine learning with Python",
        "Experience in machine learning with Python and SQL",
        "Machine learning via Python and SQL",
        "Data analysis including SQL and Excel",
    ],
)
def test_naming_the_tools_does_not_show_the_requirement_before_them_r7(words):
    finding = judged(words, [cited(TOOLS_ONLY)])

    assert finding.conclusion == "needs_confirmation"
    assert "Python" in finding.named or "SQL" in finding.named


@pytest.mark.parametrize(
    "words, passage",
    [
        ("machine learning with Python and SQL", "Built a machine learning workflow with Python and SQL."),
        ("database administration using Python and SQL", "Did database administration with Python and SQL scripts."),
        ("machine learning with Python", "Trained a machine learning model in Python."),
        ("Machine learning via Python and SQL", "Built a machine learning workflow with Python and SQL."),
        ("Data analysis including SQL and Excel", "Did data analysis with SQL and Excel."),
        ("CI/CD", "Built and operated a CI/CD pipeline."),
        ("Python/SQL", "Built a Python service with SQL queries."),
    ],
)
def test_complete_evidence_still_supports_r7(words, passage):
    assert judged(words, [cited(passage)]).conclusion == "supported"


# --- Codex's R7 review cases, word for word (/private/tmp/roleradar-d6-r7-review-PhmuOA/test_r7_boundaries.py) ---------
# Its helper judge() is the R6 file's, word for word: codex_judge() above.


@pytest.mark.parametrize("words, passage", [
    ("Python for data analysis", "Built a Python web service."),
    ("SQL for reporting", "Wrote SQL queries for a course scheduling service."),
    ("使用 Python 进行数据分析", "使用 Python 开发了课程安排网页服务。"),
    ("Python alongside SQL", "Built a Python web service."),
    ("Python besides SQL", "Built a Python web service."),
])
def test_r7_review_reported_unread_structure_does_not_prove_the_whole(words, passage):
    found = codex_judge(words, passage)
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words, passage", [
    (
        "data analysis with machine learning using Python and SQL",
        "Built a machine learning course-scheduling service using Python and SQL.",
    ),
    (
        "database administration with automation using Python and SQL",
        "Built course-scheduling automation using Python and SQL.",
    ),
])
def test_r7_review_nested_known_leadins_do_not_drop_an_earlier_substantive_head(words, passage):
    found = codex_judge(words, passage)
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words, passage", [
    ("Python", "Built a Python web service."),
    ("CI/CD", "Built and operated a CI/CD pipeline."),
    ("A/B testing", "Ran A/B testing for a synthetic web service."),
    ("Experience with one of Python, SQL, R", "Built a Python web service."),
    (
        "data analysis with machine learning using Python and SQL",
        "Performed data analysis with machine learning using Python and SQL.",
    ),
])
def test_r7_review_atomic_alternative_and_complete_evidence_remain_supported(words, passage):
    assert codex_judge(words, passage).conclusion == "supported"


# --- R8: "one item" only for what plainly is; every stretch before a lead-in --------------------------------------------

UNREAD_WORDS = [
    # Codex's R7 review, and the same shapes otherwise worded.
    "Python for data analysis", "SQL for reporting", "使用 Python 进行数据分析", "Python alongside SQL", "Python besides SQL",
    "Experience with Python for data analysis", "Python to automate reports", "Python vs R",
    "Experience building data pipelines at scale", "Python for data analysis is a plus", "Kubernetes clusters on GCP",
    "用 Python 做数据分析", "熟练使用 Python 进行数据处理", "Python 数据分析",
    # Longer than one short phrase, with nothing in it that may part items.
    "Python data cleaning feature engineering model deployment", "数据清洗特征工程模型训练部署上线",
    # Two runs of names with no joining word between them; a Chinese joining word in Chinese words alone.
    "Python scripting SQL queries", "通过数据分析支持业务决策",
]
# R9: with a passage that names what each asks for (R8 gave all of them PASSAGE).
ONE_THING = [
    ("Python", PASSAGE),
    ("A/B testing", "Ran A/B testing for a synthetic course site."),
    ("I/O", "Tuned disk I/O for a synthetic course service."),
    ("24/7 on-call support", "Provided 24/7 on-call support for a synthetic course service."),
    ("Ability to write clean, effective code", CLEAN_CODE),
    ("Write clean, effective code", CLEAN_CODE),
    ("Excellent written and verbal communication skills", COMMUNICATION),
    ("必须掌握 Python，2027 年及以后毕业", PASSAGE),
    ("Experience with Python", PASSAGE),
    ("Knowledge of Python", PASSAGE),
    ("Proficiency in Python", PASSAGE),
    ("Experience in a fast-paced environment", "Shipped weekly releases in a fast-paced environment."),
    ("Ability to work in a team", "Worked in a team of four on a synthetic course service."),
    ("Familiarity with Docker is a plus", "Packaged a synthetic course service with Docker."),
    ("Docker is a plus for this role", "Packaged a synthetic course service with Docker."),
    ("Strong SQL skills", "Wrote SQL queries for a synthetic course service."),
    ("Attention to detail", "Checked every detail of a synthetic course schedule."),
    ("Willingness to travel", "Traveled to three synthetic campuses for user interviews."),
    ("Passion for technology", "Ran a technology club at a synthetic school."),
    ("Strong understanding of data structures", "Implemented data structures for a synthetic course scheduler."),
    ("Experience working with cross-functional teams", "Worked with cross-functional teams on a synthetic launch."),
    ("Experience developing REST APIs", "Developed REST APIs for a synthetic course service."),
    ("Hands-on experience with AWS services", "Deployed services on AWS for a synthetic course site."),
    ("Bachelor's degree", "Earned a Bachelor's degree in Data Science (synthetic)."),
    ("熟练使用 Excel", "用 Excel 整理了合成报名数据。"),
    ("具备良好的沟通能力", "每周和合成客户沟通需求。"),
    ("熟悉 Python 编程", "用 Python 编程实现了合成排课服务。"),
    ("掌握 SQL 查询", "写了 SQL 查询统计合成报名人数。"),
    ("数据分析", "完成了合成报名数据的数据分析。"),
    ("岗位要求：熟练使用 Python", "用 Python 写了合成排课服务。"),
    ("能够独立完成数据分析", "独立完成数据分析并写了合成报告。"),
    ("Microsoft Power BI", "Built a synthetic dashboard in Microsoft Power BI."),
    ("Relevant experience", PASSAGE),
    ("Python 开发经验", "用 Python 开发了合成排课服务。"),
]


@pytest.mark.parametrize("words", UNREAD_WORDS)
def test_words_read_as_neither_one_item_nor_a_list_are_unread_r8(words):
    assert listing(words) == Listing(UNREAD, ())


@pytest.mark.parametrize("words", UNREAD_WORDS)
def test_a_passage_of_practice_alone_never_supports_unread_words_r8(words):
    finding = judged(words, [cited(PASSAGE)])

    assert (finding.conclusion, finding.reasons, finding.listed, finding.listed_mode, finding.named) == (
        "needs_confirmation", ("listed_unread",), (), UNREAD, ()
    )


@pytest.mark.parametrize("words, passage", ONE_THING)
def test_words_that_plainly_ask_for_one_thing_stay_one_item_r8(words, passage):
    finding = judged(words, [cited(passage)])

    assert listing(words) is None
    assert (finding.conclusion, finding.reasons) == ("supported", ("practice",))


@pytest.mark.parametrize(
    "words, expected",
    [
        # Every stretch before a lead-in that asks for something stays an item, not only the last one (R7).
        ("database administration with automation using Python and SQL", Listing(ALL, ("database administration", "automation", "Python", "SQL"))),
        ("data analysis with machine learning using Python and SQL", Listing(ALL, ("data analysis", "machine learning", "Python", "SQL"))),
        ("data analysis with machine learning using Python", Listing(ALL, ("data analysis", "machine learning", "Python"))),
        ("database administration with tools such as Python and SQL", Listing(UNCLEAR, ("database administration", "Python", "SQL"))),
        # Stretches that are only wording are left out, as before.
        ("Experience in data analysis with Python and SQL", Listing(ALL, ("data analysis", "Python", "SQL"))),
        ("3+ years of professional experience with Python and SQL", Listing(ALL, ("Python", "SQL"))),
        ("Proficiency in all of these required tools, including Python, SQL, and Tableau", Listing(ALL, ("Python", "SQL", "Tableau"))),
        (LANGUAGES, Listing(ANY, ("Java", "C++", "Python", "JavaScript"))),
    ],
)
def test_every_stretch_before_a_lead_in_that_asks_for_something_stays_an_item_r8(words, expected):
    assert listing(words) == expected


@pytest.mark.parametrize(
    "words, partial, complete",
    [
        (
            "database administration with automation using Python and SQL",
            "Built course-scheduling automation using Python and SQL.",
            "Did database administration with automation using Python and SQL.",
        ),
        (
            "data analysis with machine learning using Python and SQL",
            "Built a machine learning course-scheduling service using Python and SQL.",
            "Performed data analysis with machine learning using Python and SQL.",
        ),
    ],
)
def test_naming_the_later_stretches_does_not_show_the_earlier_one_r8(words, partial, complete):
    assert judged(words, [cited(partial)]).conclusion == "needs_confirmation"
    assert judged(words, [cited(complete)]).conclusion == "supported"


@pytest.mark.parametrize(
    "words, expected",
    [
        # The kind of thing asked for and one example of it: one ask, when the example reads plainly.
        ("Experience with ML frameworks such as TensorFlow", None),
        ("Data visualization tools such as Tableau", None),
        ("Data visualization tools such as Python for data analysis", Listing(UNREAD, ())),
    ],
)
def test_one_example_of_a_kind_of_thing_is_one_ask_when_it_reads_plainly_r8(words, expected):
    assert listing(words) == expected


def test_unread_words_are_said_as_they_stand_with_why_they_need_confirming_r8(make_config):
    finding = judged("Python for data analysis", [cited(PASSAGE)])
    page = rendered(make_config, finding)
    content = records.analysis_content(kept_item(finding))
    entry = {
        "kept_as": IN_USE, "restore_deadline": None, "basis": "依据的资料和当时一致", "posting_id": "SYN-1",
        "analysed_at": None, "saved_at": None, "feedback": [], "content": content,
        "facts": {"title": "合成岗位", "company": "合成公司", "source_name": "合成来源", "check": {"status": "open", "answered_at": None}},
    }
    jobs = jobs_markdown(JobExport(analyses=[entry]), datetime(2026, 11, 16, tzinfo=timezone.utc))
    said = analysis_words.REASONS["listed_unread"]

    assert "Python for data analysis" in page and said in page and "待确认" in page
    assert "这一条列出的几项" not in page and "有经历支持" not in page
    assert content["findings"][0]["listed_mode"] == UNREAD and content["findings"][0]["listed"] == []
    assert records.shown_analysis(content).findings == (finding,)
    assert said in jobs and "这一条列出的几项" not in jobs


# --- Codex's R8 review cases, word for word (/private/tmp/roleradar-d6-r8-review-e1aHr4/test_r8_boundaries.py) ---------
# Its helper judge() is the R6 file's, word for word: codex_judge() above.


@pytest.mark.parametrize("words, passage", [
    ("Python SQL", "Built a Python web service."),
    ("SQL Excel Tableau", "Wrote SQL queries for a scheduling service."),
    ("data analysis amid reporting", "Performed data analysis for a synthetic experiment."),
    ("数据分析报表制作", "完成了合成数据的数据分析。"),
])
def test_r8_review_reported_ambiguous_short_phrases_do_not_support_unmentioned_parts(words, passage):
    found = codex_judge(words, passage)
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words", [
    "“Python for data analysis”",
    "Python for data analysis – required",
    "Python for client’s data analysis",
])
def test_r8_review_unicode_punctuation_does_not_bypass_unread_structure_guard(words):
    found = codex_judge(words, "Built a Python web service.")
    assert found.conclusion != "supported", found


@pytest.mark.parametrize("words, passage", [
    ("Python", "Built a Python web service."),
    ("Microsoft Power BI", "Built a dashboard using Microsoft Power BI."),
    ("Google Cloud Platform", "Deployed a synthetic web service on Google Cloud Platform."),
    ("Experience with one of Python, SQL, R", "Built a Python web service."),
    (
        "data analysis with machine learning using Python and SQL",
        "Performed data analysis with machine learning using Python and SQL.",
    ),
])
def test_r8_review_single_names_explicit_alternatives_and_complete_evidence_remain_supported(words, passage):
    assert codex_judge(words, passage).conclusion == "supported"


# --- R9: marks typed either way; one item shown only when all of it is named --------------------------------------------

MARKED_UNREAD = [
    # Codex's R8 review, and the same marks otherwise typed or placed.
    "“Python for data analysis”", "Python for data analysis – required", "Python for client’s data analysis",
    "Python for data analysis — required", "‘Python for data analysis’", "「Python for data analysis」",
    "Python for data analysis", "Python　for data analysis", "Ｐｙｔｈｏｎ for data analysis", "Ｒ for data analysis",
    # The words beside a short Chinese word are held as English words are.
    "Python for data 分析",
    # A mark still not plain may join two asks.
    "Python → SQL", "Python • SQL", "Python·SQL", "Python – SQL",
]


@pytest.mark.parametrize("words", MARKED_UNREAD)
def test_marks_typed_either_way_get_no_words_past_the_checks_r9(words):
    finding = judged(words, [cited(PASSAGE)])

    assert listing(words) == Listing(UNREAD, ())
    # The words are kept and shown as written; only the reading takes them as plain marks.
    assert (finding.conclusion, finding.reasons, finding.text) == ("needs_confirmation", ("listed_unread",), words)


MARKED_ONE = [
    ("“Python”", PASSAGE),
    ("‘A/B testing’", "Ran A/B testing for a synthetic course site."),
    ("Docker – a plus", "Packaged a synthetic course service with Docker."),
    ("Microsoft® Excel", "Built synthetic reports in Microsoft Excel."),
    ("Ｐｙｔｈｏｎ", PASSAGE),
    ("Bachelor’s degree", "Earned a Bachelor’s degree in Data Science (synthetic)."),
    ("Wi‑Fi troubleshooting", "Did Wi-Fi troubleshooting for a synthetic campus."),
]


@pytest.mark.parametrize("words, passage", MARKED_ONE)
def test_marks_that_change_nothing_keep_one_item_and_the_words_as_written_r9(words, passage):
    finding = judged(words, [cited(passage)])

    assert listing(words) is None
    assert (finding.conclusion, finding.reasons, finding.text) == ("supported", ("practice",), words)


# Words read as one item whose ask has two or more parts: a passage naming part of it, and one naming all of it.
PARTLY_NAMED = [
    # Codex's R8 review, and the same shapes otherwise worded.
    ("Python SQL", "Built a Python web service.", "Wrote Python and SQL for a synthetic scheduling service."),
    ("SQL Excel Tableau", "Wrote SQL queries for a scheduling service.", "Built SQL queries, Excel sheets and Tableau dashboards."),
    ("data analysis amid reporting", "Performed data analysis for a synthetic experiment.", "Did data analysis amid weekly reporting."),
    ("数据分析报表制作", "完成了合成数据的数据分析。", "负责合成门店的数据分析报表制作。"),
    ("Kubernetes Docker", "Ran Docker containers for a synthetic course service.", "Ran Docker containers on Kubernetes."),
    ("Strong understanding of data structures", "Studied data science.", "Implemented data structures for a synthetic course scheduler."),
    ("熟悉 Python 编程", "用 Java 编程实现了合成排课服务。", "用 Python 编程实现了合成排课服务。"),
    ("Experience building data pipelines", "Used data pipelines for a synthetic shop.", "Built data pipelines for a synthetic shop."),
    ("Ability to write clean, effective code", PASSAGE, CLEAN_CODE),
]


@pytest.mark.parametrize("words, partly, wholly", PARTLY_NAMED)
def test_one_item_is_shown_only_when_a_passage_names_all_of_it_r9(words, partly, wholly):
    part = judged(words, [cited(partly)])
    whole = judged(words, [cited(wholly)])

    assert listing(words) is None
    # Never said to be lacking: no items are made up, and the words stay as written.
    assert (part.conclusion, part.reasons, part.listed, part.named, part.text) == ("needs_confirmation", ("single_unnamed",), (), (), words)
    assert (whole.conclusion, whole.reasons) == ("supported", ("practice",))


def test_what_one_item_asks_a_passage_to_name_r9():
    assert reading("Python SQL") == (None, ("Python SQL",))
    assert reading("Ability to write clean, effective code") == (None, ("write code",))
    assert reading("岗位要求：熟练使用 Python") == (None, ())
    assert reading("Python") == (None, ()) and reading("Relevant experience") == (None, ())
    assert reading("Experience with Python and SQL")[1] == () and reading("Python for data analysis")[1] == ()
    # Quotes and brackets around a term with a slash in it are no part of it.
    assert listing("'A/B testing'") is None and listing("(I/O)") is None


def test_one_item_is_named_in_one_passage_not_across_two_r9():
    # A name split over two passages is no evidence of it ("Microsoft" in one, "Power BI" in another), nor are two
    # parts that may be a list: one passage must name all of it.
    finding = judged("Python SQL", [cited("Built a Python web service."), cited("Wrote SQL queries for a synthetic shop.")])

    assert (finding.conclusion, finding.reasons) == ("needs_confirmation", ("single_unnamed",))


def test_a_passage_names_a_word_in_another_form_or_marks_r9():
    assert named("write code", ["Wrote clean, effective code."])
    assert named("building data pipelines", ["Built data pipelines for a synthetic shop."])
    assert named("Bachelor’s degree", ["Earned a Bachelor's degree."]) and named("Python 编程", ["编程时用 Python。"])
    assert named("Bachelor's degree", ["Earned a bachelor degree in data science."])
    assert not named("data", ["A dated synthetic report."]) and not named("Python 编程", ["用 Java 编程。"])


@pytest.mark.parametrize(
    "words, passage, expected",
    [
        ("Experience with ML frameworks such as TensorFlow", "Trained models with TensorFlow for a synthetic study.", "supported"),
        ("Experience with ML frameworks such as TensorFlow", "Compared ML frameworks for a synthetic study.", "supported"),
        ("Experience with ML frameworks such as TensorFlow", PASSAGE, "needs_confirmation"),
        ("Data visualization tools such as Tableau", "Built Tableau dashboards for a synthetic shop.", "supported"),
        ("Data visualization tools such as Tableau", PASSAGE, "needs_confirmation"),
        ("databases such as PostgreSQL", "Stored synthetic orders in PostgreSQL.", "supported"),
    ],
)
def test_a_kind_of_thing_is_shown_by_naming_it_or_its_example_r9(words, passage, expected):
    assert listing(words) is None
    assert judged(words, [cited(passage)]).conclusion == expected


def test_one_item_not_all_named_is_said_with_why_it_needs_confirming_r9(make_config):
    finding = judged("Python SQL", [cited("Built a Python web service.")])
    page = rendered(make_config, finding)
    content = records.analysis_content(kept_item(finding))
    entry = {
        "kept_as": IN_USE, "restore_deadline": None, "basis": "依据的资料和当时一致", "posting_id": "SYN-1",
        "analysed_at": None, "saved_at": None, "feedback": [], "content": content,
        "facts": {"title": "合成岗位", "company": "合成公司", "source_name": "合成来源", "check": {"status": "open", "answered_at": None}},
    }
    jobs = jobs_markdown(JobExport(analyses=[entry]), datetime(2026, 11, 16, tzinfo=timezone.utc))
    said = analysis_words.REASONS["single_unnamed"]

    assert "Python SQL" in page and said in page and "待确认" in page
    assert "这一条列出的几项" not in page and "有经历支持" not in page
    assert content["findings"][0]["reasons"] == ["single_unnamed"] and content["findings"][0]["listed"] == []
    assert records.shown_analysis(content).findings == (finding,)
    assert said in jobs and "不具备" not in said


# --- how they are said, shown and kept ---------------------------------------------------------------------------------


def test_a_requirement_is_said_with_its_condition_and_one_kept_before_reads_as_it_did():
    office = judged("Onsite work from an office is required", group=RequirementGroup.HARD_CONSTRAINT, quote=ONSITE)
    met = judged("Onsite work from an office is required", group=RequirementGroup.HARD_CONSTRAINT, quote=ONSITE, posting="t\n工作方式：onsite\n")
    before = SimpleNamespace(text="Must be planning on graduating in 2028")

    assert analysis_words.requirement_words(office) == "Onsite work from an office is required（原文条件：If the posting is specified as Onsite）"
    assert analysis_words.requirement_words(met) == "Onsite work from an office is required（条件成立：If the posting is specified as Onsite）"
    assert analysis_words.requirement_words(before) == "Must be planning on graduating in 2028"


def rendered(make_config, finding) -> str:
    templates = build_templates(make_config())
    page = templates.env.from_string('{% import "_findings.html" as f with context %}{{ f.finding_row(finding) }}')
    owner = SimpleNamespace(title="Course scheduling web service (synthetic)")

    return page.render(
        finding=finding,
        titles={citation.item_id: owner for citation in finding.citations},
        versions={citation.item_id: 1 for citation in finding.citations},
        BASIS_LABELS=analysis_words.BASIS_LABELS,
        conclusion_label=analysis_words.conclusion_label,
        reason_labels=analysis_words.reason_labels,
    )


def test_the_shared_row_shows_the_condition_its_fact_and_what_is_unproven(make_config):
    office = rendered(
        make_config,
        judged("Onsite work from an office is required", group=RequirementGroup.HARD_CONSTRAINT, quote=ONSITE, posting="t\n工作方式：remote\n"),
    )
    listed = rendered(make_config, judged(FAMILIARITY, [cited(PASSAGE)]))

    assert "Onsite work from an office is required（原文条件：If the posting is specified as Onsite）" in office
    assert "原文条件" in office and "岗位写明的事实" in office and "工作方式：remote" in office and "不适用" in office
    assert "条件不成立，这一条不适用" in office
    assert "这一条列出的几项" in listed and "所列各项都要" in listed and "引文写到的：没有" in listed
    assert "还没有依据的：data structures、storage systems、cloud infrastructure、front-end frameworks" in listed
    assert "待确认" in listed and "有经历支持" not in listed


def test_a_row_of_a_requirement_with_neither_reads_as_it_did(make_config):
    page = rendered(make_config, judged("Ability to write clean, effective code", [cited(CLEAN_CODE)]))

    assert "有经历支持" in page and "原文条件" not in page and "这一条列出的几项" not in page


def kept_item(*findings) -> SimpleNamespace:
    analysis = SimpleNamespace(findings=findings, preferences="not_analyzed", preference_findings=(), sources=())

    return SimpleNamespace(category=SimpleNamespace(value="partial"), first_checks=(), notes=(), analysis=analysis)


def test_a_kept_analysis_keeps_and_reads_back_what_its_page_showed():
    office = judged("Onsite work from an office is required", group=RequirementGroup.HARD_CONSTRAINT, quote=ONSITE, posting="t\n工作方式：remote\n")
    listed = judged(FAMILIARITY, [cited(PASSAGE)])
    content = records.analysis_content(kept_item(listed, office))

    assert content["version"] == 2
    assert content["findings"][0]["listed"] == list(listed.listed) and content["findings"][0]["named"] == []
    assert (content["findings"][1]["condition"], content["findings"][1]["condition_fact"]) == (office.condition, "工作方式：remote")
    assert records.shown_analysis(content).findings == (listed, office)


def test_a_kept_analysis_of_the_first_version_reads_with_neither():
    content = records.analysis_content(kept_item(judged("Python", [cited(PASSAGE)])))
    content["version"] = 1

    for finding in content["findings"]:
        for key in ("listed", "listed_mode", "named", "condition", "condition_state", "condition_fact"):
            del finding[key]

    (finding,) = records.shown_analysis(content).findings

    assert (finding.listed, finding.named, finding.condition, finding.condition_fact) == ((), (), "", "")
    assert finding.conclusion == "supported"


def test_the_readable_export_says_the_condition_its_fact_and_the_listed_items():
    office = judged("Onsite work from an office is required", group=RequirementGroup.HARD_CONSTRAINT, quote=ONSITE, posting="t\n工作方式：remote\n")
    content = records.analysis_content(kept_item(judged(FAMILIARITY, [cited(PASSAGE)]), office))
    entry = {
        "kept_as": IN_USE, "restore_deadline": None, "basis": "依据的资料和当时一致", "posting_id": "SYN-1",
        "analysed_at": None, "saved_at": None, "feedback": [], "content": content,
        "facts": {"title": "合成岗位", "company": "合成公司", "source_name": "合成来源", "check": {"status": "open", "answered_at": None}},
    }
    jobs = jobs_markdown(JobExport(analyses=[entry]), datetime(2026, 11, 16, tzinfo=timezone.utc))

    assert "Onsite work from an office is required（原文条件：If the posting is specified as Onsite）" in jobs
    assert "岗位写明的事实：「工作方式：remote」" in jobs and "：不适用" in jobs
    assert "这一条列出的几项（所列各项都要）：引文写到的：没有；还没有依据的：data structures、storage systems、cloud infrastructure、front-end frameworks" in jobs
