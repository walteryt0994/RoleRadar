"""U3-D2-E3, offline: a posting's type, with the hours or the contract told apart from whether it is an internship.

What a Lever board writes as a posting's commitment (lever._job_type) is read as an internship or a campus hire only
when it says so. Full-time, Part-time, Contract, Permanent and Temporary only say the hours or the contract: never
taken as "not an internship" and left out, never as one either; the type stays pending as written (rules.HOURS_ONLY),
and a title stating an internship beside them is no conflict (a full-time internship is one). Only another kind of
hire in the type beside such a title is a conflict, and then neither is taken. A type not given, or not read, stays
pending as before. This is no type system: nothing else is read, and nothing is ranked here (D5).
"""
import pytest

from app.discovery import lever, lever_live, presentation, rules
from app.discovery.contracts import DimensionScope, Others, Screen, SearchScope
from lever_helpers import posting

# Internships only, nothing else listed: what validation A's search asked for.
INTERNSHIPS_ONLY = SearchScope(
    city=DimensionScope(("New York",), Others.INCLUDED, ()),
    job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope((), Others.INCLUDED, ()),
    targets=("Software Engineer",),
)
HOURS_NOTE = "职位名写的是实习，来源的类型字段只写了工时或合同，两者不冲突；求职类型仍待确认，不排除"


def read_from_lever(title: str, commitment: str | None):
    """A posting as the Lever adapter reads it from the board's own fields."""
    facts = lever.LeverSource(lever_live.BOARD)._facts(posting(1, title, board=lever_live.BOARD, commitment=commitment))

    return facts.posting


@pytest.mark.parametrize("commitment", ["Full-time", "Full Time", "Part-time", "Contract", "Permanent", "Temporary", "Full-time / Permanent"])
def test_hours_or_a_contract_alone_never_say_whether_it_is_an_internship(commitment):
    item = read_from_lever("Software Engineer", commitment)
    screening = rules.screen(INTERNSHIPS_ONLY, item)

    # Kept, pending, as written: never left out as "not an internship".
    assert (item.job_type, item.type_stated) == (None, commitment)
    assert screening.status is Screen.PENDING and screening.reasons == ("求职类型只写了工时或合同，待确认",)
    assert presentation.type_text(item) == f"类型只写了工时或合同：{commitment}（没有说明是不是实习）"


def test_a_full_time_internship_is_kept_pending_and_never_called_a_conflict():
    item = read_from_lever("Software Engineer Intern", "Full-time")
    screening = rules.screen(INTERNSHIPS_ONLY, item)

    assert (item.job_type, rules.type_conflict(item)) == (None, False)
    assert screening.status is Screen.PENDING and screening.reasons == ("求职类型只写了工时或合同，待确认",)
    assert screening.notes == (HOURS_NOTE,)
    # The type field saying both: read as an internship, in the scope.
    both = read_from_lever("Software Engineer", "Full-time Internship")
    assert both.job_type == "实习" and rules.screen(INTERNSHIPS_ONLY, both).status is Screen.IN


def test_a_title_and_a_type_of_another_kind_of_hire_are_at_odds_and_neither_is_taken():
    item = read_from_lever("Software Engineer Intern", "校招")
    screening = rules.screen(INTERNSHIPS_ONLY, item)

    # The board says a campus hire, the title an internship: pending, not left out as a campus hire.
    assert (item.job_type, rules.type_conflict(item)) == ("校招", True)
    assert screening.status is Screen.PENDING and screening.reasons == ("求职类型和职位名不一致，待确认",)
    assert presentation.type_text(item) == "类型和职位名不一致：校招"
    # With the type open in the scope, the conflict is noted instead.
    open_type = rules.screen(
        SearchScope(INTERNSHIPS_ONLY.city, DimensionScope((), Others.INCLUDED, ()), INTERNSHIPS_ONLY.direction, ("x",)), item
    )
    assert open_type.status is Screen.IN and open_type.notes == ("求职类型待确认：职位名写的是实习，来源的类型字段写的是另一种招聘（如校招），两者不一致",)


@pytest.mark.parametrize(
    "commitment, reason, shown",
    [
        (None, "求职类型来源没有提供，待确认", "类型来源没有提供"),
        ("Regular", "求职类型写了但没有认出，待确认", "类型写了但没有认出：Regular"),
        ("Contractor", "求职类型写了但没有认出，待确认", "类型写了但没有认出：Contractor"),
    ],
)
def test_a_type_not_given_or_not_read_stays_pending_as_before(commitment, reason, shown):
    item = read_from_lever("Software Engineer", commitment)
    screening = rules.screen(INTERNSHIPS_ONLY, item)

    assert screening.status is Screen.PENDING and screening.reasons == (reason,)
    assert presentation.type_text(item) == shown


def test_a_title_stating_an_internship_beside_a_type_not_read_is_noted_as_unknown_not_as_a_conflict():
    item = read_from_lever("Data Intern", "Regular")
    screening = rules.screen(INTERNSHIPS_ONLY, item)

    assert not rules.type_conflict(item)
    assert screening.notes == ("职位名写的是实习，来源的类型字段没有认出，两者是否一致待确认",)


def test_the_rules_have_a_version_of_their_own_for_this():
    assert rules.RULES_VERSION == "8"
    assert rules.hours_or_contract("Full-time") and not rules.hours_or_contract("Full-time Internship")
    assert not rules.hours_or_contract("Contractor") and not rules.hours_or_contract("")
