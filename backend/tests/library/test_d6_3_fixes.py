"""U3-D6-3 R10: what Codex's acceptance of the D6-3 run found (F1-F3), fixed offline (Claude, 2026-11-16).

Built from what D6-3 kept, never fetched again (ROLERADAR_V0_1_U3_D6_EVIDENCE/D6-3/06-run/pages/): the sentence "This
should be your final internship before graduating." that came back as a "must" hard constraint (13), the requirement
"Curiosity about connectivity in cloud systems" read as "所列各项都要: about connectivity、cloud systems" (13), a check
step whose timeout followed its first, answered, posting (13), the result page's retention line after a skip (the
page as seen after it), and the keep page's check time from the search, not the later 现在核对 (08 against 07).

Everything here is synthetic: scripted services, synthetic sources and the app's simulated decisions and analyses;
nothing leaves the process and no model is called. What a real model will write is not shown by these tests: only that
the local rules no longer take a "must" from a "should", make up items, or put a failure on the wrong posting.

F1: a requirement whose own posting quote only expects or prefers is no hard constraint and no required skill, and is
never shown with a "must" its quote lacks (app.requirement_reading.strength, app.experience_analysis._requirements).
F2: a stretch before a lead-in that is a phrase of its own makes no items (requirement_reading.reading).
F3: a step names the postings that came to no answer on a line of its own; the result page says what its search's end
held, then what is held now; the keep page says its checks are the search's own.

The first five tests are Codex's (/private/tmp/roleradar-d63-review-w4rifR/test_d63_review.py, ea6982e4…), their
bodies as written, renamed test_d63_review_*.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.analysis_contracts import AnalysisStatus, RequirementGroup
from app.discovery import presentation, records, rules, tools
from app.discovery.agent import ScriptedDecider, failed_lines, make_action, run_agent
from app.discovery.contracts import RECOMMENDED, Category, EndReason, Outcome, Screen, Screening, Tool
from app.discovery.sources import SourceTimeout
from app.discovery.synthetic import CAREERS
from app.experience_analysis import analyze_selected_experiences
from app.library import job_export
from app.library.models import JobRecommendation
from app.requirement_reading import EXPECTED, OBLIGED, STATED, UNREAD, obliges, reading, strength, without_obligation
from analysis_helpers import ScriptedProvider, approved, assessed, cite, confirmed, parsed_posting, request_for
from discovery_helpers import WITHIN, begin, confirmation_for, make_library, ref
from job_helpers import text_of
from test_discovery_pages import confirm, review, search_of, stored
from test_job_apply_check import board, current, post  # noqa: F401 (fixtures)
from test_job_record_pages import inline, library  # noqa: F401 (fixtures)
from test_job_recommendation_pages import markers, ticked_search


# --- Codex's independent tests, as written ------------------------------------------------------------------------


@pytest.mark.parametrize('words',[
    'Curiosity about connectivity in cloud systems',
    'Interest in data analysis',
])
def test_d63_review_prepositions_do_not_make_independent_requirements(words):
    listing,asks=reading(words)
    # Keeping the whole wording unread is acceptable. Inventing several independent
    # required items from an ordinary prepositional phrase is not.
    assert listing is None or not listing.items, listing


@pytest.mark.parametrize('quote,summary,is_hard',[
    ('This should be your final internship before graduating.',
     "This must be the candidate's final internship before graduating",False),
    ('This must be your final internship before graduating.',
     "This must be the candidate's final internship before graduating",True),
])
def test_d63_review_requirement_strength_not_upgraded_by_summary(database, owners, clock, quote, summary, is_hard):
    project=confirmed(database,owners[0],clock,'project',title='Synthetic project',actions='Built a small Python service.',outcome='Synthetic tests passed.')
    request=request_for(owners[0],[project],posting='Synthetic internship\n'+quote)
    payload=parsed_posting(job_title='Synthetic internship',company='Synthetic Co',
        education_requirement=None,work_authorization=None,required_skills=[],preferred_skills=[],
        uncertain_requirements=[],hard_constraints=[{'text':summary,'evidence_text':quote}])
    def assessment(prompt):
        # Accept a safe local reclassification before the scripted analysis call.
        values=[{'requirement_id':prefix+'1','citations':[]} for prefix in ['H','U'] if '"'+prefix+'1"' in prompt or prefix+'1' in prompt]
        return json.dumps({'assessments':values or [{'requirement_id':'H1','citations':[]}]})
    result=analyze_selected_experiences(database,request,approved(request),ScriptedProvider(payload,assessment))
    assert result.status is AnalysisStatus.COMPLETE,(result.status,result.problem)
    assert len(result.findings)==1
    found=result.findings[0]
    assert (found.group is RequirementGroup.HARD_CONSTRAINT)==is_hard,found
    if not is_hard:
        assert 'must' not in found.text.lower(),found
    assert found.job_posting_quote==quote


def test_d63_review_genuine_multi_item_requirement_is_preserved():
    listing,_=reading('Experience with Python and SQL')
    assert listing and listing.items==('Python','SQL')


# --- F1: how firmly a posting's own words ask ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote, expected",
    [
        # D6-3 (13): the posting's own sentence, and the same sentence with "must".
        ("This should be your final internship before graduating.", EXPECTED),
        ("This must be your final internship before graduating.", OBLIGED),
        # What D6-3's analyses rested on: required, stated plainly, or conditional and required.
        ("Must be planning on graduating in 2028.", OBLIGED),
        ("Engineering background in fields such as Computer Science, Mathematics, Software Engineering, and Physics.", STATED),
        ("If the posting is specified as Onsite, you are required to work from an office.", OBLIGED),
        # Preferences, plainly.
        ("Experience with Go is a plus.", EXPECTED),
        ("Ideally, you have shipped a web service.", EXPECTED),
        ("GPA 3.0 or above preferred.", EXPECTED),
        ("Docker would be nice to have.", EXPECTED),
        # Said not to be required: nothing firm is left.
        ("A degree is not required.", EXPECTED),
        ("No degree required; Python preferred.", EXPECTED),
        ("You do not need a degree, but you should know Python.", EXPECTED),
        # "must not" still requires.
        ("You must not have graduated before 2027.", OBLIGED),
        # Mixed: a firm sentence beside a soft one keeps the quote firm, never weakened.
        ("Must have a degree; experience with Go is a plus.", OBLIGED),
        ("You must be enrolled in a degree program and should be graduating in 2028.", OBLIGED),
        # A plain statement beside a "should": the quote stands as it is, neither firm nor soft.
        ("Bachelor's degree in Computer Science. You should have Go experience.", STATED),
        # Conditional, and only expected.
        ("If you live in New York, you should work from our office.", EXPECTED),
        # Chinese.
        ("需要有效的工作许可", OBLIGED),
        ("Python 工程师须持有效工作签证", OBLIGED),
        ("最好是 2028 届毕业生", EXPECTED),
        ("有 SQL 经验者优先", EXPECTED),
        ("不要求工作经验", EXPECTED),
        ("2027 年及以后毕业", STATED),
        # Curly marks read as plain ones (R9).
        ("This should be your final internship — ideally before graduating.", EXPECTED),
    ],
)
def test_the_strength_of_a_quote_is_read_sentence_by_sentence(quote, expected):
    assert strength(quote) == expected


@pytest.mark.parametrize(
    "words, firm",
    [
        ("This must be the candidate's final internship before graduating", True),
        ("Final internship before graduating", False),
        ("Python is required", True),
        ("Degree not required", False),
        ("必须 2028 届毕业", True),
        ("2028 届毕业", False),
    ],
)
def test_whether_words_require(words, firm):
    assert obliges(words) is firm


@pytest.mark.parametrize(
    "words, left",
    [
        ("Must know Python", "Python"),
        ("Candidates must have a bachelor's degree", "bachelor's degree"),
        ("Python is required", "Python"),
        ("Python (required)", "Python"),
        ("Required: SQL", "SQL"),
        ("Go experience is a must", "Go experience"),
        ("必须掌握 SQL", "SQL"),
        # What cannot be taken out plainly is not guessed at: the caller shows the posting's own words.
        ("This must be the candidate's final internship before graduating", None),
    ],
)
def test_the_firmness_is_taken_out_of_the_parses_words_only_in_fixed_forms(words, left):
    assert without_obligation(words) == left


# A synthetic posting around the lines below: every requirement quotes it word for word.
SHOULD = "This should be your final internship before graduating."
MUST = "This must be your final internship before graduating."
STATED_DEGREE = "Bachelor's degree in Computer Science."
NEIGHBOURS = "Bachelor's degree in Computer Science. You should have Go experience."
MIXED = "You must be enrolled in a degree program and should be graduating in 2028."
NOT_REQUIRED = "A degree is not required."
LOCAL = "If you live in New York, you should work from our office."
GO_PLUS = "Experience with Go is a plus."
SHOULD_SQL = "You should know SQL."
STATED_SQL = "Strong SQL skills."
DOCKER = "Docker is a plus."
TOOLS_LINE = "Python, SQL and Tableau would be a plus."
CHINESE = "最好是 2028 届毕业生"
PROJECT = {"title": "Synthetic project", "actions": "Built a small Python service.", "outcome": "Synthetic tests passed."}
ANY_ID = r'"requirement_id": "([RPHU]\d+)"'


def posting(*lines: str) -> str:
    return "\n".join(["Synthetic internship (synthetic)", "Synthetic Co · New York", "", *lines, ""])


def parse(required=(), preferred=(), hard=(), uncertain=()) -> str:
    return parsed_posting(
        job_title="Synthetic internship (synthetic)",
        company="Synthetic Co",
        location="New York",
        work_mode=None,
        education_requirement=None,
        work_authorization=None,
        required_skills=[{"text": text, "evidence_text": quote} for text, quote in required],
        preferred_skills=[{"text": text, "evidence_text": quote} for text, quote in preferred],
        hard_constraints=[{"text": text, "evidence_text": quote} for text, quote in hard],
        uncertain_requirements=[{"text": text, "evidence_text": quote} for text, quote in uncertain],
    )


def nothing_cited(prompt: str) -> str:
    """An analysis answer citing nothing, for every requirement the prompt sends, whatever its id."""
    import re

    return json.dumps({"assessments": [{"requirement_id": rid, "citations": []} for rid in re.findall(ANY_ID, prompt)]})


@pytest.fixture
def project(database, owners, clock):
    return confirmed(database, owners[0], clock, "project", **PROJECT)


def analysed(database, owners, project, lines, answer=nothing_cited, **groups):
    request = request_for(owners[0], [project], posting=posting(*lines))
    provider = ScriptedProvider(parse(**groups), answer)
    result = analyze_selected_experiences(database, request, approved(request), provider)

    assert result.status is AnalysisStatus.COMPLETE, (result.status, result.problem)

    return result, provider


def by_quote(result) -> dict:
    return {finding.job_posting_quote: finding for finding in result.findings}


def test_a_hard_condition_only_expected_is_assessed_with_the_uncertain_ones_in_the_postings_words(database, owners, project):
    result, provider = analysed(
        database, owners, project, [SHOULD], hard=[("This must be the candidate's final internship before graduating", SHOULD)]
    )
    found = by_quote(result)[SHOULD]

    # D6-3 (13) had it as a hard constraint, "must". Here: uncertain, in the posting's own words, its quote as it was.
    assert (found.requirement_id, found.group, found.text) == ("U1", RequirementGroup.UNCERTAIN, SHOULD)
    # And it is sent so: the analysis is never told "must" or "hard_constraint" for it.
    sent = provider.prompts[-1]
    assert "This must be the candidate" not in sent and '"group": "hard_constraint"' not in sent
    # Never a "招聘条件" to confirm before applying, never a hard conflict.
    assert rules.to_confirm(result, Screening(Screen.IN)) == ()
    assert found.conclusion == "no_evidence"


def test_a_hard_condition_required_by_its_quote_stays_one(database, owners, project):
    result, _ = analysed(database, owners, project, [MUST], hard=[("This must be the candidate's final internship before graduating", MUST)])
    found = by_quote(result)[MUST]

    assert (found.group, found.text) == (RequirementGroup.HARD_CONSTRAINT, "This must be the candidate's final internship before graduating")
    assert rules.to_confirm(result, Screening(Screen.IN)) == ("招聘条件：This must be the candidate's final internship before graduating",)


@pytest.mark.parametrize(
    "quote, words",
    [
        # Mixed: required somewhere in its quote, it keeps its group and its words.
        (MIXED, "Must be enrolled and graduating in 2028"),
        # A "should" beside a plain statement: nothing says the plain one is only expected.
        (NEIGHBOURS, "Bachelor's degree in Computer Science"),
        # Stated plainly, with a "must" the parse added: neither firmer nor softer than the posting says (left as it is).
        (STATED_DEGREE, "Must have a bachelor's degree in Computer Science"),
    ],
)
def test_a_hard_condition_is_never_weakened_by_a_should_beside_a_firm_or_plain_one(database, owners, project, quote, words):
    result, _ = analysed(database, owners, project, [quote], hard=[(words, quote)])
    found = by_quote(result)[quote]

    assert (found.group, found.text) == (RequirementGroup.HARD_CONSTRAINT, words)


def test_a_hard_condition_said_not_to_be_required_or_only_expected_under_a_condition_is_no_hard_constraint(database, owners, project):
    result, _ = analysed(
        database, owners, project, [NOT_REQUIRED, LOCAL],
        hard=[("Must have a degree", NOT_REQUIRED), ("Must work from the New York office", LOCAL)],
    )
    degree, office = by_quote(result)[NOT_REQUIRED], by_quote(result)[LOCAL]

    # Their own words, the firmness taken out; uncertain, never hard.
    assert (degree.group, degree.text) == (RequirementGroup.UNCERTAIN, "degree")
    assert (office.group, office.text) == (RequirementGroup.UNCERTAIN, "work from the New York office")
    # The condition its quote lays down is kept with it, word for word (R4).
    assert office.condition == "If you live in New York"
    assert rules.to_confirm(result, Screening(Screen.IN)) == ()


def test_a_required_skill_only_expected_is_no_required_skill_and_no_gap(database, owners, project):
    result, _ = analysed(
        database, owners, project, [GO_PLUS, SHOULD_SQL, STATED_SQL],
        required=[("Must know Go", GO_PLUS), ("SQL", SHOULD_SQL), ("Strong SQL skills", STATED_SQL)],
    )
    found = by_quote(result)

    assert (found[GO_PLUS].group, found[GO_PLUS].text) == (RequirementGroup.UNCERTAIN, "Go")
    assert (found[SHOULD_SQL].group, found[SHOULD_SQL].text) == (RequirementGroup.UNCERTAIN, "SQL")
    # A required skill stated plainly stays one.
    assert (found[STATED_SQL].group, found[STATED_SQL].text) == (RequirementGroup.REQUIRED, "Strong SQL skills")
    # Only that one is a required gap; the coverage counts only it.
    assert rules.gaps(result) == ("经历里没有找到足够依据：Strong SQL skills",)
    assert result.coverage.required_total == 1


def test_a_preferred_or_uncertain_requirement_keeps_its_group_without_a_must_its_quote_lacks(database, owners, project):
    result, _ = analysed(
        database, owners, project, [DOCKER, CHINESE],
        preferred=[("Must know Docker", DOCKER)], uncertain=[("必须 2028 届毕业", CHINESE)],
    )
    found = by_quote(result)

    assert (found[DOCKER].group, found[DOCKER].text) == (RequirementGroup.PREFERRED, "Docker")
    assert (found[CHINESE].group, found[CHINESE].text) == (RequirementGroup.UNCERTAIN, "2028 届毕业")


def test_a_posting_line_split_into_several_requirements_keeps_each_its_own_words(database, owners, project):
    # The parse split one line into three, each quoting the whole line, each worded "must": each is read as one item.
    # The whole line is never the list of each of them.
    # R12 (more conservative on purpose): the line is a list parted by commas, so whether "would be a plus" is on all
    # three or on the last is not read (app.requirement_reading, part 3, R12): the quote states, and each requirement
    # keeps the group and the words the parse gave it. Each keeping its own words less the "must" under a soft line is
    # now shown with a line plainly soft on the whole (test_d6_3_r12.py).
    result, _ = analysed(
        database, owners, project, [TOOLS_LINE],
        required=[("Must know Python", TOOLS_LINE), ("Must know SQL", TOOLS_LINE), ("Must know Tableau", TOOLS_LINE)],
    )

    assert [(finding.group, finding.text, finding.listed) for finding in result.findings] == [
        (RequirementGroup.REQUIRED, "Must know Python", ()),
        (RequirementGroup.REQUIRED, "Must know SQL", ()),
        (RequirementGroup.REQUIRED, "Must know Tableau", ()),
    ]


def test_what_is_kept_and_exported_says_the_same_as_the_page(database, owners, project):
    result, _ = analysed(
        database, owners, project, [SHOULD], hard=[("This must be the candidate's final internship before graduating", SHOULD)]
    )
    shown = SimpleNamespace(analysis=result, category=Category.PARTIAL, first_checks=rules.first_checks(result, Screening(Screen.IN)), notes=())
    content = records.analysis_content(shown)
    entry = {
        "facts": {"title": "Synthetic internship (synthetic)", "company": "Synthetic Co", "source_name": "合成来源", "check": {"status": "open"}},
        "kept_as": "in_use",
        "restore_deadline": None,
        "basis": "当前",
        "posting_id": "synthetic-1",
        "analysed_at": None,
        "saved_at": None,
        "content": content,
        "feedback": [],
    }
    exported = "\n".join(job_export._analysis_lines(1, entry))

    # Kept as shown: uncertain, the posting's own words; and the export reads it so.
    assert [(finding["group"], finding["text"]) for finding in content["findings"]] == [("uncertain", SHOULD)]
    assert "招聘条件" not in "".join(content["first_checks"])
    assert f"招聘条件：{SHOULD}（写得不明确的要求；岗位原文：「{SHOULD}」）" in exported
    assert "must" not in exported.casefold()


# --- F2: a phrase of its own before a lead-in makes no items ------------------------------------------------------


@pytest.mark.parametrize(
    "words",
    [
        # One item after the lead-in (reading's first branch).
        "Curiosity about connectivity in cloud systems",
        "Experience across teams with Python",
        "Background regarding security in distributed systems",
        # A list after the lead-in (its second branch).
        "Knowledge about networking with Python and SQL",
        # No lead-in: the words before the first item's names (_list).
        "Knowledge about networking Python and SQL",
    ],
)
def test_a_phrase_of_its_own_before_a_lead_in_is_unread_with_no_items(words):
    assert reading(words) == (reading(words)[0], ()) and reading(words)[0].mode == UNREAD and reading(words)[0].items == ()


@pytest.mark.parametrize(
    "words, items",
    [
        # R7, R8: what asks for something in its own right before a lead-in still does.
        ("machine learning with Python and SQL", ("machine learning", "Python", "SQL")),
        ("database administration with automation using Python and SQL", ("database administration", "automation", "Python", "SQL")),
        ("Strong understanding of statistics with Python", ("statistics", "Python")),
        ("Machine learning via Python and SQL", ("Machine learning", "Python", "SQL")),
        ("Experience with Python and SQL", ("Python", "SQL")),
    ],
)
def test_requirements_before_a_lead_in_that_ask_in_their_own_right_stay_items(words, items):
    assert reading(words)[0].items == items


def test_d6_3s_curiosity_requirement_lists_nothing_and_its_practice_needs_confirming(database, owners, project):
    words = "Curiosity about connectivity in cloud systems"
    quote = "Curiosity about connectivity in cloud systems, from networking to productization to security and authentication"
    cited = assessed(U1=[cite("S1", "actions", PROJECT["actions"], "practice")])
    result, _ = analysed(database, owners, project, [quote], answer=cited, uncertain=[(words, quote)])
    found = by_quote(result)[quote]

    # D6-3 (13) showed "所列各项都要: about connectivity、cloud systems". No items are made up; the practice alone
    # needs confirming, and nothing says the user lacks it.
    assert (found.listed, found.listed_mode) == ((), UNREAD)
    assert (found.conclusion, found.reasons) == ("needs_confirmation", ("listed_unread",))


# --- F3: which postings of a step came to no answer -----------------------------------------------------------------


@pytest.fixture
def agent_library(database, owners, clock):
    return make_library(database, owners, clock)


def checked(database, clock, agent_library, faults, read_faults=()):
    """A search on the synthetic careers site that reads and checks two postings, its sources failing as told; the
    check step (or the read step when read_faults are given) and its page view. No experience is chosen, so it may end
    with "够了" having analysed nothing (A1-R1 F5; U3-Q2 refuses that only with experiences chosen)."""
    run = begin(database, clock, confirmation_for(agent_library, selection=(), preferences=()))
    careers = run.register.adapter(CAREERS)

    for posting_id in faults:
        careers.faults[("check", posting_id)] = SourceTimeout("synthetic")

    for posting_id in read_faults:
        careers.faults[("read", posting_id)] = SourceTimeout("synthetic")

    finished = run_agent(
        run,
        ScriptedDecider(
            [
                make_action(Tool.SEARCH, source_id=CAREERS, keywords="数据分析 实习生", city="北京", **WITHIN, note="检索"),
                make_action(Tool.READ, postings=[ref("c-bj-analyst"), ref("c-choice")], note="读取"),
                make_action(Tool.CHECK, postings=[ref("c-bj-analyst"), ref("c-choice")], note="核对"),
                make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束"),
            ]
        ),
    )
    tool = Tool.READ if read_faults else Tool.CHECK
    step = next(step for step in finished.trace.steps if step.tool is tool)

    return step, presentation.step_view(step)


def shown(posting_id: str) -> str:
    return tools.shown_ref(ref(posting_id))


def test_a_check_that_answers_first_and_fails_second_names_the_second_on_a_line_apart(database, clock, agent_library):
    step, view = checked(database, clock, agent_library, faults=["c-choice"])

    assert step.outcome == Outcome.DONE.value and step.failed == (f"{shown('c-choice')}（来源没有在时限内回答）",)
    # The first line is the first posting's, answered; the failure never follows it.
    assert view["summary"].startswith(f"- [{shown('c-bj-analyst')}] 核对时仍公开发布，截止 ")
    assert "原因" not in view["summary"] and "c-choice" not in view["summary"]
    assert view["failed"] == f"这一步其中 1 个岗位没有结果：{shown('c-choice')}（来源没有在时限内回答）"


def test_a_check_that_fails_first_and_answers_second_names_the_first(database, clock, agent_library):
    step, view = checked(database, clock, agent_library, faults=["c-bj-analyst"])

    assert step.outcome == Outcome.DONE.value
    assert view["summary"].startswith(f"- [{shown('c-bj-analyst')}] 来源没有回答：状态待确认") and "原因" not in view["summary"]
    assert view["failed"] == f"这一步其中 1 个岗位没有结果：{shown('c-bj-analyst')}（来源没有在时限内回答）"


def test_a_check_that_answers_for_all_names_none(database, clock, agent_library):
    step, view = checked(database, clock, agent_library, faults=[])

    assert (step.outcome, step.failed, view["failed"]) == (Outcome.DONE.value, (), "")
    assert "原因" not in view["summary"]


def test_a_check_that_fails_for_all_says_so_and_names_them(database, clock, agent_library):
    step, view = checked(database, clock, agent_library, faults=["c-bj-analyst", "c-choice"])

    assert step.outcome == Outcome.FAILED.value
    assert view["title"].endswith(presentation.STEP_OUTCOMES[Outcome.FAILED.value])
    assert view["failed"] == (
        f"这一步问到的岗位都没有结果：{shown('c-bj-analyst')}（来源没有在时限内回答）；{shown('c-choice')}（来源没有在时限内回答）"
    )


def test_a_read_that_fails_for_one_names_it(database, clock, agent_library):
    step, view = checked(database, clock, agent_library, faults=[], read_faults=["c-choice"])

    assert step.outcome == Outcome.DONE.value and "原因" not in view["summary"]
    assert view["failed"] == f"这一步其中 1 个岗位没有结果：{shown('c-choice')}（来源没有在时限内回答）"


def test_a_failed_search_keeps_its_reason_after_its_line():
    # A search is one call: what failed is the line itself, as before R10 (U3-D2-E2).
    step = SimpleNamespace(number=1, tool=Tool.SEARCH, params=(), outcome=Outcome.FAILED.value, summary="合成招聘官网没有回答。", note="", failure="来源没有在时限内回答")

    assert presentation.step_view(step)["summary"] == "合成招聘官网没有回答。 原因：来源没有在时限内回答"
    assert presentation.step_view(step)["failed"] == ""


def test_failed_lines_name_postings_as_the_summaries_do():
    result = SimpleNamespace(failed=((ref("c-choice"), "来源没有在时限内回答"),))

    assert failed_lines(result) == (f"{shown('c-choice')}（来源没有在时限内回答）",)


def test_the_result_page_names_the_posting_its_check_step_got_no_answer_for(app, browser_a, library, inline, board):
    # A first search on the board, to see which postings the app's simulated decisions read and check.
    first = search_of(review(browser_a, sources=[library], use_preferences=True))
    assert confirm(browser_a, first).status_code == 303
    step = next(step for step in stored(app, browser_a, first).finished.trace.steps if step.tool is Tool.CHECK)
    asked = [shown_ref.split("/", 1)[1] for shown_ref in dict(step.params)["岗位"].split("、")]
    assert len(asked) >= 2

    # The same search again, the board not answering the check of its second posting.
    board.faults[("check", asked[1])] = SourceTimeout("synthetic")
    url = search_of(review(browser_a, sources=[library], use_preferences=True))
    assert confirm(browser_a, url).status_code == 303
    page = text_of(browser_a.get(url).text)

    assert f"这一步其中 1 个岗位没有结果：{shown(asked[1])}（来源没有在时限内回答）" in page
    assert f"- [{shown(asked[0])}] 核对时仍公开发布" in page
    assert "原因：来源没有在时限内回答" not in page


# --- F3: the result page's retention line, and the keep page's checks ----------------------------------------------


def held_records(app, owner) -> list[str]:
    return [marker.posting_record_id for marker in markers(app, owner)]


def test_the_result_page_says_what_its_end_held_and_what_is_held_now(app, browser_a, library, inline):
    url = ticked_search(browser_a, library)
    task = stored(app, browser_a, url)
    recommended = [item for item in task.finished.outcome.results if item.category in RECOMMENDED]
    first, second = held_records(app, task.owner_id)[:2]
    assert len(recommended) == 2

    found = text_of(browser_a.get(url).text)
    assert "查找结束时，这次的推荐临时留存跟踪了 2 条（当时都还没加入我的岗位）" in found
    assert "现在这 2 条仍在临时留存跟踪中。" in found

    # Skipped: added to 我的岗位, held no more, never deleted with the recommendations.
    assert post(browser_a, f"/job-records/postings/{first}/decide", decision="skip", expected_revision=str(current(app, first).revision)).status_code == 303
    found = text_of(browser_a.get(url).text)
    assert "查找结束时，这次的推荐临时留存跟踪了 2 条（当时都还没加入我的岗位）" in found
    assert "现在其中 1 条仍在临时留存跟踪中；其余的以下面各条和岗位记录为准：加入了我的岗位的（包括做了跳过等决定的）不会随推荐删除" in found
    assert "已加入我的岗位：跳过" in found

    # Untracked: removed, and held no more either.
    assert post(browser_a, f"/job-records/postings/{second}/untrack", expected_revision=str(current(app, second).revision)).status_code == 303
    found = text_of(browser_a.get(url).text)
    assert "现在其中 0 条仍在临时留存跟踪中" in found and "临时留存跟踪中（到" not in found
    # The one skipped stays 我的岗位.
    assert current(app, first) is not None and "已加入我的岗位：跳过" in found


def test_the_result_page_says_its_recommendations_came_to_their_time(app, browser_a, library, inline, clock):
    url = ticked_search(browser_a, library)
    owner = stored(app, browser_a, url).owner_id

    # Their time come, as if seven days had passed (the page itself lasts thirty minutes).
    with app.state.library.database.transaction() as db:
        for marker in db.scalars(select(JobRecommendation).where(JobRecommendation.owner_id == owner)):
            marker.until = clock.now() - timedelta(seconds=1)

    found = text_of(browser_a.get(url).text)
    assert "查找结束时，这次的推荐临时留存跟踪了 2 条" in found
    assert "现在其中 0 条仍在临时留存跟踪中" in found and "不再跟踪或已到期的已经删除" in found
    assert "临时留存跟踪中（到" not in found


def keep_page(browser, url, item) -> str:
    return text_of(browser.get(f"{url}/keep?source={item.ref.source_id}&posting={item.ref.posting_id}").text)


def test_the_keep_page_says_its_checks_are_the_searchs_own(app, browser_a, library, inline, board, clock):
    url = ticked_search(browser_a, library)
    task = stored(app, browser_a, url)
    result = next(item for item in task.finished.outcome.results if item.category in RECOMMENDED)
    # A posting of the search with no record: leads are never held.
    lead = task.finished.outcome.leads[0]
    record_id = held_records(app, task.owner_id)[0]

    page = keep_page(browser_a, url, result)
    assert "本次查找时的来源核对" in page and "本次查找时最后一次有回答：" in page
    assert "查找以后的核对记在岗位记录里，以记录为准" in page
    # Never "最后一次有回答" with no word of whose checks they are.
    assert "来源核对 最后一次有回答" not in page

    # A later check (现在核对), kept in the record: the keep page still says the search's, and where the later one is;
    # showing it reads nothing from the source.
    clock.advance(minutes=11)
    assert post(browser_a, "/job-records/refresh").status_code == 303
    calls = len(board.calls)
    page = keep_page(browser_a, url, result)

    assert "本次查找时最后一次有回答：" in page and "查找以后的核对记在岗位记录里，以记录为准" in page
    assert len(board.calls) == calls
    record = text_of(browser_a.get(f"/job-records/postings/{record_id}").text)
    assert "现在核对" in record

    # A posting with no record: the search's own lines, and no word of a record.
    page = keep_page(browser_a, url, lead)
    assert "本次查找时的来源核对" in page and "以记录为准" not in page
    assert "本次查找时最后一次有回答：" in page or "这次查找没有得到回答的核对" in page
