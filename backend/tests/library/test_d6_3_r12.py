"""U3-D6-3 R12: Codex's review of R11 found two firm hard conditions demoted by a soft word on another part of their
quote (F1), fixed offline (Claude, 2026-11-17).

"Bachelor's degree in Computer Science, Master's preferred." (only the Master's is preferred) and "Valid work
authorization for your preferred office location." ("preferred" is on the office location) were read as only expected,
so a hard constraint the parse had right went to the uncertain group: R11 read a sentence as soft wherever a soft word
stood in it. Now a sentence only expects when it asks softly for the whole of what it asks, clause by clause
(app.requirement_reading, part 3, R12).

Everything is synthetic: scripted services, no network, no model. The first three tests are Codex's
(/private/tmp/roleradar-d63-review-w4rifR/test_r11_scope.py, d1847695…), their bodies as written, renamed
test_r11_scope_*. The soft sentences R12 leaves in their group (a list parted by commas, "Candidates who live in New
York should ...") are not pinned as soft or stated here beyond what keeps a hard condition firm: a test must never fix a
misreading in place.
"""
import pytest

from app.analysis_contracts import RequirementGroup
from app.requirement_reading import EXPECTED, STATED, strength
from test_d6_3_fixes import analysed, by_quote, project  # noqa: F401 (project is a fixture)


# --- Codex's independent tests, as written ------------------------------------------------------------------------


@pytest.mark.parametrize('quote,summary', [
    ("Bachelor's degree in Computer Science, Master's preferred.", "Bachelor's degree in Computer Science"),
    ('Valid work authorization for your preferred office location.', 'Valid work authorization'),
])
def test_r11_scope_a_soft_word_about_another_part_cannot_demote_the_correct_hard_requirement(database, owners, project, quote, summary):
    result, _ = analysed(database, owners, project, [quote], hard=[(summary, quote)])
    found = by_quote(result)[quote]
    assert found.group is RequirementGroup.HARD_CONSTRAINT, found
    assert found.text == summary, found

def test_r11_scope_an_explicitly_preferred_whole_requirement_can_still_be_demoted(database, owners, project):
    quote = "A bachelor's degree in Computer Science is preferred."
    result, _ = analysed(database, owners, project, [quote], hard=[("Bachelor's degree in Computer Science", quote)])
    assert by_quote(result)[quote].group is RequirementGroup.UNCERTAIN


# --- a soft word only on the whole of what a clause asks --------------------------------------------------------------


@pytest.mark.parametrize(
    "quote",
    [
        # Codex's two, and the same with other marks between: a soft clause beside a stated one.
        "Bachelor's degree in Computer Science, Master's preferred.",
        "Valid work authorization for your preferred office location.",
        "A degree is a prerequisite, a master's preferred.",
        "Bachelor's degree - Master's preferred.",
        "Bachelor's degree—Master's preferred.",
        "Bachelor's degree in Computer Science: Master's preferred.",
        "Bachelor's degree in Computer Science (Master's preferred).",
        "本科学历，硕士优先",
        "工作许可，最好是绿卡",
        # A denial beside a stated clause: the stated one keeps its group.
        "Bachelor's degree in Computer Science, no Master's needed.",
        # A soft word inside a clause, on one of its nouns, or a verb with a subject of its own.
        "Valid work authorization in the location you preferred.",
        "Valid work authorization for the location the candidate preferred.",
        # A soft predicate, or a bare soft word, on two things joined: on both or on the last, unread.
        "Bachelor's degree in Computer Science and Master's preferred.",
        "Bachelor's degree in Computer Science and Master's (preferred).",
        # A bare soft word at the end is on the one clause before it only.
        "Bachelor's degree in Computer Science, Master's (preferred).",
        # A "should", or a soft predicate, past "that" or "where": in a clause of its own.
        "A degree that should be in Computer Science.",
        "Work authorization for the office where you should work.",
        "Work authorization for the office where SQL is preferred.",
        "Valid work authorization for the team that Go is a plus for.",
        # A soft word that begins a noun or a name is no soft way of asking.
        "建议书撰写经验",
        "最好的沟通能力",
        "Preferred Provider Organization coverage is included.",
        # A soft word on one part, or in another sense (R11's, now by the same reading).
        "Bachelor's degree, preferably in Computer Science.",
        "Bachelor's degree (Computer Science preferred).",
        "Python (preferably 3.10 or later).",
        "Eligible for Optional Practical Training (OPT).",
        "This full-time role comes with an annual bonus.",
    ],
)
def test_a_soft_word_on_another_part_leaves_the_sentence_as_stated(quote):
    assert strength(quote) == STATED


@pytest.mark.parametrize(
    "quote",
    [
        # Codex's: a soft predicate on the whole.
        "A bachelor's degree in Computer Science is preferred.",
        "Knowledge of SQL is strongly preferred.",
        "An MBA is preferred.",
        "Experience in Go is considered a plus.",
        "Experience with Go is a plus for this role.",
        "GPA 3.0 or above preferred.",
        "Bachelor's degree in Computer Science or related field preferred.",
        # Led by a soft way of asking, or "should" after the subject.
        "It would be great if you know Go.",
        "The ideal candidate has shipped a web service.",
        "We prefer candidates with Go experience.",
        "Students on F-1 visas are encouraged to confirm their work authorization.",
        "You should, ideally, have shipped a web service.",
        "你应该熟悉 Go",
        # A bare soft word on the one thing beside it, a label or a sentence adverb on all.
        "Bachelor's degree in Computer Science (preferred).",
        "Familiarity with Docker: nice to have.",
        "Preferred: Python, SQL and Tableau.",
        "Ideally, Python, SQL and Tableau.",
        "Docker（可选）",
        # A label that is a soft way of asking, or a denial, before a colon: on all that follows.
        "Nice to have for this role: Docker.",
        "Not required: Go.",
        # Soft clauses and denials together; a condition asks nothing of its own.
        "Experience with Python is a plus but not required.",
        "Docker experience is preferred, not required.",
        "Although not required, Go is a plus.",
        "If you have a degree in CS, that's a plus.",
        "If you have a degree in CS, that would be great.",
        "That should be your final internship.",
        "如果你在纽约，最好到办公室工作",
        "熟悉 Python、SQL 者优先",
        "Docker 是加分项",
        "希望你熟悉 Go",
        "建议具备 Docker 经验",
        "欢迎有 Go 经验的同学",
        "Preferred qualifications: SQL.",
    ],
)
def test_a_soft_word_on_the_whole_of_what_is_asked_still_reads_as_soft(quote):
    assert strength(quote) == EXPECTED


@pytest.mark.parametrize(
    "quote, summary",
    [
        ("Bachelor's degree in Computer Science and Master's preferred.", "Bachelor's degree in Computer Science"),
        ("Bachelor's degree—Master's preferred.", "Bachelor's degree"),
        ("Bachelor's degree in Computer Science (Master's preferred).", "Bachelor's degree in Computer Science"),
        ("Bachelor's degree in Computer Science, no Master's needed.", "Bachelor's degree in Computer Science"),
        ("Valid work authorization in the location you preferred.", "Valid work authorization"),
        ("Valid work authorization for the location the candidate preferred.", "Valid work authorization"),
        ("本科学历，硕士优先", "本科学历"),
        ("建议书撰写经验", "建议书撰写经验"),
        ("Work authorization for the office where SQL is preferred.", "Work authorization"),
    ],
)
def test_a_hard_condition_beside_or_under_a_soft_part_stays_one_with_its_words(database, owners, project, quote, summary):
    result, _ = analysed(database, owners, project, [quote], hard=[(summary, quote)])
    found = by_quote(result)[quote]

    assert (found.group, found.text) == (RequirementGroup.HARD_CONSTRAINT, summary)


def test_a_line_plainly_soft_on_the_whole_split_into_several_keeps_each_its_own_words(database, owners, project):
    # R10's protection under a line soft on all it lists: each requirement keeps its own words less the "must", read
    # as one item; the whole line is never the list of each of them.
    line = "Preferred: Python, SQL and Tableau."
    result, _ = analysed(
        database, owners, project, [line],
        required=[("Must know Python", line), ("Must know SQL", line), ("Must know Tableau", line)],
    )

    assert [(finding.group, finding.text, finding.listed) for finding in result.findings] == [
        (RequirementGroup.UNCERTAIN, "Python", ()),
        (RequirementGroup.UNCERTAIN, "SQL", ()),
        (RequirementGroup.UNCERTAIN, "Tableau", ()),
    ]
