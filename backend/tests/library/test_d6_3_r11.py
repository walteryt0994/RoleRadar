"""U3-D6-3 R11: Codex's review of R10 found two firm sentences read as soft (F1), fixed offline (Claude, 2026-11-16).

"Valid work authorization is not optional." counted "optional" as an expectation, its "not" unread; "SQL is not
preferred but required." took "not preferred but required" for one denial, so its "required" was denied. Both moved a
hard constraint the parse had right to the uncertain group. Now "not optional" requires, an expectation said not to
hold expects nothing, a denial reaches only its plain forms, and a sentence is read as soft only when it plainly is:
what is unclear keeps the group the parse put it in (app.requirement_reading, part 3, R11).

Everything is synthetic: scripted services, no network, no model. The first seven tests are Codex's
(/private/tmp/roleradar-d63-review-w4rifR/test_r10_boundaries.py, 1fa2bfbc…), their bodies as written, renamed
test_r10_boundaries_*. What the module says it still misreads ("Bachelor's degree in Computer Science, Master's
preferred.") and the soft sentences it leaves in their group ("Candidates who have not yet graduated should submit a
transcript.") are not pinned by a test here: a test must never fix a misreading in place.
"""
import pytest

from app.analysis_contracts import RequirementGroup
from app.requirement_reading import EXPECTED, OBLIGED, STATED, obliges, strength, without_obligation
from test_d6_3_fixes import analysed, by_quote, project  # noqa: F401 (project is a fixture)


# --- Codex's independent tests, as written ------------------------------------------------------------------------


@pytest.mark.parametrize('quote', [
    'Valid work authorization is not optional.',
    'SQL is not preferred but required.',
    'No exceptions: a degree is required.',
    'Valid work authorization is required.',
])
def test_r10_boundaries_genuine_mandatory_requirement_is_not_demoted(database, owners, project, quote):
    result, _ = analysed(database, owners, project, [quote], hard=[(quote, quote)])
    found = by_quote(result)[quote]
    assert found.group is RequirementGroup.HARD_CONSTRAINT, (quote, strength(quote), found.group, found.text)


@pytest.mark.parametrize('quote', [
    'A degree is not required.',
    'SQL is preferred.',
    'This should be your final internship before graduating.',
])
def test_r10_boundaries_actual_soft_requirements_are_still_demoted(database, owners, project, quote):
    result, _ = analysed(database, owners, project, [quote], hard=[(quote, quote)])
    found = by_quote(result)[quote]
    assert found.group is RequirementGroup.UNCERTAIN, (quote, strength(quote), found.group)


# --- how far a denial reaches -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote, expected",
    [
        # Codex's two: an "optional" denied requires; a denial ends at a turn.
        ("Valid work authorization is not optional.", OBLIGED),
        ("SQL is not preferred but required.", OBLIGED),
        ("Work authorization isn't optional.", OBLIGED),
        ("Work authorization is non-optional.", OBLIGED),
        ("Attendance is by no means optional.", OBLIGED),
        ("Attendance is anything but optional.", OBLIGED),
        ("Attendance is far from optional.", OBLIGED),
        ("Attendance is no longer optional.", OBLIGED),
        ("Attendance will not be optional.", OBLIGED),
        ("This is not only required but expected.", OBLIGED),
        # A denial does not cross a mark, nor a word that turns the sentence; a dash is a mark, no word.
        ("No exceptions: a degree is required.", OBLIGED),
        ("A degree is not, however, required.", OBLIGED),
        ("No degree but SQL required.", OBLIGED),
        ("Is it preferred? No - it is required.", OBLIGED),
        # A clause between denies nothing, nor does a "no" inside a clause.
        ("Applicants who have not yet graduated are required to submit a transcript.", OBLIGED),
        ("Candidates with no degree are required to complete a test.", OBLIGED),
        ("Students who have no SQL are required to take a course.", OBLIGED),
        ("Applicants holding no degree are required to take a test.", OBLIGED),
        ("With no degree, you are required to take a test.", OBLIGED),
        ("Candidates without SQL experience are required to take a course.", OBLIGED),
        # Plain denials still deny: "no" where it begins its clause, "not" through "be".
        ("A degree is not required.", EXPECTED),
        ("A degree isn't strictly required.", EXPECTED),
        ("No degree required.", EXPECTED),
        ("No prior experience is required.", EXPECTED),
        ("Python preferred, no degree required.", EXPECTED),
        ("Python preferred - no degree required.", EXPECTED),
        ("A degree is no longer required.", EXPECTED),
        ("You will not be required to relocate.", EXPECTED),
        ("You do not need a degree, but you should know Python.", EXPECTED),
        ("Not required but preferred: Go.", EXPECTED),
        ("You have no need for a degree.", EXPECTED),
        # Where a denial's span was taken out, the words on either side never join ("have" + "to").
        ("Candidates have not really needed to code before.", EXPECTED),
        # An expectation said not to hold leaves the sentence as stated: its group stays.
        ("Python is not preferred.", STATED),
        ("Experience with Go is not a plus.", STATED),
        ("Experience with Go is hardly a plus.", STATED),
        ("Docker 不是加分项", STATED),
        # Its span is taken out, so the rest of the sentence is still read: a soft word or a denial beside it is soft.
        ("Python is not preferred, but SQL is preferred.", EXPECTED),
        ("A degree is not required, and Go is not a plus.", EXPECTED),
        # Chinese denials and requirements.
        ("工作许可不是可选项", OBLIGED),
        ("Python 不是加分项，而是必须", OBLIGED),
        ("不要求工作经验", EXPECTED),
        # And what R10 read rightly, still.
        ("This should be your final internship before graduating.", EXPECTED),
        ("You must not have graduated before 2027.", OBLIGED),
        ("Must have a degree; experience with Go is a plus.", OBLIGED),
        ("Bachelor's degree in Computer Science. You should have Go experience.", STATED),
    ],
)
def test_a_denial_reaches_only_its_plain_forms(quote, expected):
    assert strength(quote) == expected


@pytest.mark.parametrize(
    "quote, expected",
    [
        # A negation the rules do not read: unclear, never soft.
        ("Python is not, however, preferred.", STATED),
        ("Neither SQL nor Go is preferred.", STATED),
        ("Docker 并不算加分项", STATED),
        ("A degree is not required, but you cannot lack SQL.", STATED),
        # One right after "should" denies what is asked, not how firmly: still soft.
        ("You should not have graduated before 2027.", EXPECTED),
        ("You shouldn't need sponsorship.", EXPECTED),
    ],
)
def test_a_negation_left_unread_keeps_the_sentence_from_reading_as_soft(quote, expected):
    assert strength(quote) == expected


@pytest.mark.parametrize(
    "quote, expected",
    [
        # A soft word on one part of what is asked: the rest stands as it is.
        ("Bachelor's degree, preferably in Computer Science.", STATED),
        ("Bachelor's degree (Computer Science preferred).", STATED),
        ("Python (preferably 3.10 or later).", STATED),
        # A soft word in another sense, and "need" on its own.
        ("Eligible for Optional Practical Training (OPT).", STATED),
        ("This full-time role comes with an annual bonus.", STATED),
        ("You need a bachelor's degree, ideally in Computer Science.", OBLIGED),
        # A soft word on the whole stays soft.
        ("Python (preferred).", EXPECTED),
        ("Python (highly preferred).", EXPECTED),
        ("SQL, preferably.", EXPECTED),
        ("Preferred: Bachelor's degree, ideally in Computer Science.", EXPECTED),
        ("Bonus: experience with Go.", EXPECTED),
        ("Experience with Go (bonus).", EXPECTED),
        ("Go experience is a bonus.", EXPECTED),
        ("Bonus points for Go.", EXPECTED),
        ("Optional: a cover letter.", EXPECTED),
    ],
)
def test_a_soft_word_on_one_part_or_in_another_sense_leaves_the_rest_as_it_stands(quote, expected):
    assert strength(quote) == expected


@pytest.mark.parametrize(
    "quote",
    [
        "Valid work authorization is not optional.",
        "SQL is not preferred but required.",
        "Applicants who have not yet graduated are required to submit a transcript.",
        "Candidates with no degree are required to complete a test.",
        "Students who have no SQL are required to take a course.",
        "Attendance is anything but optional.",
        "Applicants holding no degree are required to take a test.",
        "Bachelor's degree, preferably in Computer Science.",
        "Eligible for Optional Practical Training (OPT).",
        "You need a bachelor's degree, ideally in Computer Science.",
    ],
)
def test_a_firm_hard_condition_stays_one_with_its_words(database, owners, project, quote):
    result, _ = analysed(database, owners, project, [quote], hard=[("Work authorization or a course", quote)])
    found = by_quote(result)[quote]

    assert (found.group, found.text) == (RequirementGroup.HARD_CONSTRAINT, "Work authorization or a course")


@pytest.mark.parametrize(
    "quote",
    ["Python preferred, no degree required.", "You should not have graduated before 2027.", "A degree is no longer required."],
)
def test_a_plainly_soft_hard_condition_is_still_assessed_with_the_uncertain_ones(database, owners, project, quote):
    result, _ = analysed(database, owners, project, [quote], hard=[("Must hold a degree", quote)])

    assert by_quote(result)[quote].group is RequirementGroup.UNCERTAIN


def test_a_required_skill_whose_expectation_is_denied_stays_required(database, owners, project):
    quote = "Python is not preferred but required."
    result, _ = analysed(database, owners, project, [quote], required=[("Python", quote)])

    assert by_quote(result)[quote].group is RequirementGroup.REQUIRED


def test_not_optional_words_require_and_are_taken_out_in_their_plain_form():
    assert obliges("Work authorization is not optional") is True
    assert without_obligation("Work authorization is not optional") == "Work authorization"
    # Over a soft quote, the parse's "not optional" is not shown: its words less it.
    assert obliges("SQL is not preferred but required") is True
    assert obliges("A degree is not required") is False
    # "need" on its own requires too; "no need" and "need not" deny it.
    assert obliges("Candidates need a degree") is True
    assert obliges("There is no need for a degree") is False
    assert obliges("Candidates need not hold a degree") is False


def test_a_soft_quote_under_not_optional_words_is_shown_without_them(database, owners, project):
    quote = "Work authorization is preferred."
    result, _ = analysed(database, owners, project, [quote], hard=[("Work authorization is not optional", quote)])
    found = by_quote(result)[quote]

    assert (found.group, found.text) == (RequirementGroup.UNCERTAIN, "Work authorization")
