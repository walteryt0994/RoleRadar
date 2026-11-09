"""U3-C3b B: kept directions handed to 找岗位 by decision id and revision, and the entry for an unsure direction.

The records page offers the kept directions a search may take, by C1's rule of each decision's own basis
(usable_for_search), and says why not of the other kept ones. Handing them over is a GET naming "<id>.<revision>"
only: nothing is written or started, and the search page reads each again, offers every usable one and ticks those
handed over as they were chosen. The review reads them again, and so does the confirmation, right before the start
and under the library's write lock; a change voids the confirmation before anything else, so an older form never
starts a search, even when the review made again fails (C precheck, 19.3). Names come from the library and join the
scope whole, never split and never ideal; the preferences are never written. Everything runs on the synthetic
sources and the local simulation, and nothing leaves the process.
"""
import re
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.discovery.web as discovery_web
from app.directions import records
from app.directions.actions import changeable
from app.discovery.tasks import Stage
from app.library.models import DirectionDecision, ExperienceItem, ExperienceVersion
from app.library.references import usable_for_search
from job_helpers import confirmed, text_of
from library_helpers import form_for, forms_in
from test_direction_records import direction, quote, trusted
from test_discovery_pages import CHOSEN, PROJECT, checked, inline, search_of, searches, stored

BASE = "/discover"
RECORDS = "/directions/records"
PREFERENCES = dict(
    cities_ideal="北京",
    job_types_ideal="实习",
    directions_ideal="数据分析",
    directions_no="销售",
    work_content_ideal="希望多分析数据，少做重复事务",
)


@pytest.fixture
def world(app, browser_a):
    """User A's preferences and one confirmed project, entered through the pages."""
    assert browser_a.submit("/job-preferences/edit", "/job-preferences", **PREFERENCES).status_code == 303
    project = confirmed(browser_a, **PROJECT)

    with app.state.library.database.transaction() as db:
        item = db.get(ExperienceItem, project)
        prefs = db.scalar(
            select(ExperienceItem).where(ExperienceItem.owner_id == item.owner_id, ExperienceItem.category == "preferences")
        )
        prefs_version = db.scalar(
            select(ExperienceVersion.id)
            .where(ExperienceVersion.item_id == prefs.id, ExperienceVersion.kind == "saved")
            .order_by(ExperienceVersion.number.desc())
        )

        return SimpleNamespace(
            owner=item.owner_id, project=project, version=item.confirmed_version_id, prefs=prefs.id, prefs_version=prefs_version
        )


# --- helpers ---------------------------------------------------------------------------------------------------------


def database_of(app):
    return app.state.library.database


def ref(decision) -> str:
    return f"{decision.id}.{decision.revision}"


def personal(app, clock, owner, name, state="keep", seen=None):
    with database_of(app).transaction() as db:
        return records.decide_personal(db, owner, name, state, seen, clock.now())


def on_saved(app, clock, world, name, *, also_preferences=False):
    """A decision kept on a saved exploration whose direction quotes the project; its other direction quotes the
    preferences when asked (also_preferences)."""
    project = ("experience", world.project, world.version, ("actions",))
    prefs = ("preferences", world.prefs, world.prefs_version, ("work_content_ideal",))
    others = [
        direction(
            "用户研究",
            ideal=[{"condition": quote(prefs, "work_content_ideal", "希望多分析数据"), "relation": "fits", "basis": "常常整理数据"}]
            if also_preferences
            else (),
        ),
        direction("产品运营"),
    ]
    result = trusted(world.owner, clock, (project, prefs) if also_preferences else (project,), [direction(name, [quote(project, "actions", "清洗订单数据")]), *others])

    with database_of(app).transaction() as db:
        exploration = records.save_exploration(db, world.owner, result, clock.now()).id

        return records.decide_on_saved(db, world.owner, exploration, name, "keep", None, clock.now())


def current(app, decision_id):
    with database_of(app).transaction() as db:
        return db.get(DirectionDecision, decision_id)


def edit_project(browser, world, actions):
    assert browser.submit(f"/experiences/{world.project}/edit", f"/experiences/{world.project}/draft", actions=actions).status_code == 303
    assert browser.submit(f"/experiences/{world.project}", f"/experiences/{world.project}/confirm").status_code == 303


def entered(browser, path=BASE, **changes) -> dict:
    """The input page's form as a browser posts it from this address: its ticks, the scope chosen, and changes."""
    page = browser.get(path).text
    data = {"csrf_token": browser.csrf_token(), **CHOSEN}

    for name in ("city", "job_type", "direction", "kept"):
        data[name] = checked(page, name)

    data.update(changes)

    return data


def offered_kept(page: str) -> list[str]:
    return re.findall(r'name="kept" value="([^"]+)"', page)


def handoff_section(page: str) -> str:
    start = page.index('id="handoff-title"')

    return page[start: page.index("</section>", start)]


def confirm_form(browser, url) -> dict:
    """The review's start form as the page gives it, kept to be posted later."""
    page = browser.get(url).text
    form = form_for(page, f"{url}/confirm")

    return {
        "csrf_token": form.fields["csrf_token"],
        "fingerprint": form.fields["fingerprint"],
        "action": "start",
        "send": re.findall(r'name="send" value="([^"]+)" checked', page),
    }


def tasks(searches):
    with searches.store._lock:
        return {task.id: task.stage for task in searches.store._tasks.values()}


# --- the records page --------------------------------------------------------------------------------------------------


def test_the_records_page_hands_over_only_kept_directions_a_search_may_take(app, browser_a, clock, world):
    kept = personal(app, clock, world.owner, "数据分析/商业分析")
    personal(app, clock, world.owner, "运营", "defer")
    personal(app, clock, world.owner, "销售", "exclude")
    moved = on_saved(app, clock, world, "产品经理")
    edit_project(browser_a, world, "后来改成了别的做法。")

    section = handoff_section(browser_a.get(RECORDS).text)

    # Only the kept one a search may take, by id and revision, ticked; a GET that starts nothing.
    assert '<form method="get" action="/discover">' in section
    assert re.findall(r'name="kept" value="([^"]+)" checked', section) == [ref(kept)]
    assert "数据分析/商业分析" in section and "运营" not in section and "销售" not in section
    assert "“产品经理”：依据的资料之后改过。在上面把它“重新定为我的选择”之后，才能用于找岗。" in text_of(section)
    assert ref(current(app, moved.id)) not in section


def test_a_kept_direction_whose_own_quotes_stand_is_handed_over_though_it_cannot_be_changed_as_it_is(app, browser_a, clock, world):
    """C precheck, 19.3: may a search take it (C1, its own basis) is not may its state change (C3a, its exploration)."""
    decision = on_saved(app, clock, world, "商业分析", also_preferences=True)
    page = browser_a.get("/job-preferences/edit").text
    [form] = [form for form in forms_in(page) if form.method == "post" and form.action.startswith("/job-preferences")]
    assert browser_a.post(form.action, {**form.fields, "work_content_ideal": "希望多和用户交流"}).status_code == 303

    with database_of(app).transaction() as db:
        row = db.get(DirectionDecision, decision.id)
        assert usable_for_search(db, world.owner, row, clock.now()) and not changeable(db, world.owner, row, clock.now())

    records_page = browser_a.get(RECORDS).text
    section = handoff_section(records_page)
    # The decision's own row says its exploration changed and offers to make it the user's own choice...
    assert f"/directions/decisions/{decision.id}/own" in records_page
    # ...and the hand-off still offers it, ticked, with no reason against it.
    assert re.findall(r'name="kept" value="([^"]+)" checked', section) == [ref(decision)] and "不能用于找岗" not in section

    handed = browser_a.get(f"{BASE}?kept={ref(decision)}").text
    assert checked(handed, "kept") == [ref(decision)]
    url = search_of(browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}")))
    assert "商业分析" in stored(app, browser_a, url).confirmation.scope.direction.values


# --- handing over and the search page --------------------------------------------------------------------------------


def test_handing_over_ticks_what_was_chosen_offers_the_rest_and_writes_or_starts_nothing(app, browser_a, clock, world, searches):
    chosen = personal(app, clock, world.owner, "商业分析")
    other = personal(app, clock, world.owner, "用户研究")

    with database_of(app).transaction() as db:
        before = (db.get(ExperienceItem, world.prefs).revision, dict(db.get(ExperienceItem, world.prefs).content))

    direct = browser_a.get(BASE).text
    handed = browser_a.get(f"{BASE}?kept={ref(chosen)}").text

    # Opened directly, every usable kept direction is offered and none is ticked for the user.
    assert sorted(offered_kept(direct)) == sorted([ref(chosen), ref(other)]) and checked(direct, "kept") == []
    assert sorted(offered_kept(handed)) == sorted([ref(chosen), ref(other)]) and checked(handed, "kept") == [ref(chosen)]
    assert "保留的方向（来自探索记录，只用于这次查找，不改求职偏好）" in text_of(handed)
    assert '<a href="/directions">方向还不确定？先去探索方向</a>' in handed

    with database_of(app).transaction() as db:
        assert (db.get(ExperienceItem, world.prefs).revision, dict(db.get(ExperienceItem, world.prefs).content)) == before

    assert tasks(searches) == {}


@pytest.mark.parametrize("change", ["state", "undone", "made_again", "basis", "another_users", "unknown"])
def test_a_direction_not_as_it_was_chosen_is_neither_handed_over_nor_reviewed(app, browser_a, browser_b, clock, world, searches, change):
    if change == "basis":
        decision = on_saved(app, clock, world, "产品经理")
    else:
        decision = personal(app, clock, world.owner, "商业分析")

    old = ref(decision)

    if change == "state":
        with database_of(app).transaction() as db:
            seen = records.SeenDecision(decision.id, decision.revision)
            records.decide_personal(db, world.owner, "商业分析", "defer", seen, clock.now())
            records.decide_personal(db, world.owner, "商业分析", "keep", records.SeenDecision(decision.id, decision.revision + 1), clock.now())
    elif change in ("undone", "made_again"):
        with database_of(app).transaction() as db:
            records.delete_decision(db, world.owner, decision.id, decision.revision, clock.now())

        if change == "made_again":
            personal(app, clock, world.owner, "商业分析")
    elif change == "basis":
        edit_project(browser_a, world, "后来改成了别的做法。")
    elif change == "another_users":
        theirs_project = confirmed(browser_b, **PROJECT)

        with database_of(app).transaction() as db:
            their_owner = db.get(ExperienceItem, theirs_project).owner_id

        old = ref(personal(app, clock, their_owner, "别人的方向"))
    else:
        old = f"{uuid.uuid4()}.1"

    page = browser_a.get(f"{BASE}?kept={old}").text
    shown = text_of(page)

    assert old not in checked(page, "kept") and old not in offered_kept(page)

    if change in ("state", "basis"):
        name = "商业分析" if change == "state" else "产品经理"
        assert f"“{name}”在你选择之后有变化，或者现在不能用于找岗，没有带入。" in shown
    else:
        assert "另有 1 个方向已经撤销或找不到，没有带入。" in shown and "别人的方向" not in page

    # The review reads them again too: a form naming it makes no search.
    refused = browser_a.post(f"{BASE}/review", {**entered(browser_a), "kept": [old]})
    assert refused.status_code == 422 and "下面已按现在的情况列出保留的方向，请重新选择后再核对。" in text_of(refused.text)
    assert "别人的方向" not in refused.text and tasks(searches) == {}


@pytest.mark.parametrize("bad", ["not-a-reference", "{id}", "{id}.01", "{id}.1&kept={id}.2", "{upper}.1"])
def test_a_reference_that_cannot_be_read_is_a_bad_request(app, browser_a, clock, world, searches, bad):
    decision = personal(app, clock, world.owner, "商业分析")
    query = bad.format(id=decision.id, upper=decision.id.upper())

    assert browser_a.get(f"{BASE}?kept={query}").status_code == 400
    assert browser_a.post(f"{BASE}/review", {**entered(browser_a), "kept": query.split("&kept=")}).status_code == 400
    assert tasks(searches) == {}


def test_a_kept_name_holding_separators_stays_one_value_and_never_becomes_ideal(app, browser_a, clock, world, searches):
    decision = personal(app, clock, world.owner, "数据分析/商业分析")

    with database_of(app).transaction() as db:
        before = db.get(ExperienceItem, world.prefs).revision

    url = search_of(browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}")))
    scope = stored(app, browser_a, url).confirmation.scope.direction

    assert scope.values == ("数据分析", "数据分析/商业分析") and scope.ideal == ("数据分析",)
    assert "“数据分析/商业分析”" in text_of(browser_a.get(url).text)

    with database_of(app).transaction() as db:
        assert db.get(ExperienceItem, world.prefs).revision == before


def test_a_kept_name_no_typed_value_could_hold_is_refused_at_the_review(app, browser_a, clock, world, searches):
    """A name is the library's, yet a search takes it only as it would take a value typed in."""
    decision = personal(app, clock, world.owner, "数据\x07分析")
    refused = browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}"))

    assert refused.status_code == 422 and "含有无法处理的控制字符，不能用于找岗" in text_of(refused.text) and tasks(searches) == {}


def test_a_kept_direction_the_preferences_cannot_accept_is_refused_at_the_review(app, browser_a, clock, world, searches):
    decision = personal(app, clock, world.owner, "销售")
    refused = browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}"))

    assert refused.status_code == 422 and "“销售”是不能接受的，不能选" in text_of(refused.text) and tasks(searches) == {}


# --- the confirmation (C precheck, 19.3) ----------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["state", "undone", "made_again", "basis_then_back"])
def test_a_change_after_the_review_voids_the_confirmation_for_good(app, inline, browser_a, clock, world, searches, change):
    if change == "basis_then_back":
        decision = on_saved(app, clock, world, "产品经理")
    else:
        decision = personal(app, clock, world.owner, "商业分析")

    url = search_of(browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}")))
    old = confirm_form(browser_a, url)

    if change == "state":
        personal(app, clock, world.owner, "商业分析", "defer", records.SeenDecision(decision.id, decision.revision))
    elif change in ("undone", "made_again"):
        with database_of(app).transaction() as db:
            records.delete_decision(db, world.owner, decision.id, decision.revision, clock.now())

        if change == "made_again":
            personal(app, clock, world.owner, "商业分析")
    else:
        edit_project(browser_a, world, "后来改成了别的做法。")

    voided = browser_a.post(f"{url}/confirm", old)

    # Refused, and the review made again from it fails as well: the old confirmation is gone all the same.
    assert voided.status_code == 422 and "保留的方向" in text_of(voided.text)
    assert tasks(searches) == {} and browser_a.get(url).status_code == 404

    if change == "basis_then_back":
        edit_project(browser_a, world, PROJECT["actions"])

        with database_of(app).transaction() as db:
            assert usable_for_search(db, world.owner, db.get(DirectionDecision, decision.id), clock.now())

    # The old form, posted again once things are as they were or not, starts nothing.
    replayed = browser_a.post(f"{url}/confirm", old)
    assert replayed.status_code == 404 and tasks(searches) == {}

    # Only a new review, confirmed by the user, starts a search, once.
    fresh = [entry for entry in offered_kept(browser_a.get(BASE).text)]
    again = search_of(browser_a.post(f"{BASE}/review", {**entered(browser_a), "kept": fresh}))
    form = confirm_form(browser_a, again)
    started = browser_a.post(f"{again}/confirm", form)
    assert started.status_code == 303 and started.headers["location"] == again
    assert tasks(searches)[again.rsplit("/", 1)[1]] is not Stage.REVIEW
    assert browser_a.post(f"{again}/confirm", form).headers["location"] == f"{again}?notice=already"


def test_a_change_found_by_the_last_check_before_the_start_voids_it_too(app, inline, browser_a, clock, world, searches, monkeypatch):
    """The last check and the start happen under the library's write lock: as if a decision changed just before it."""
    decision = personal(app, clock, world.owner, "商业分析")
    url = search_of(browser_a.post(f"{BASE}/review", entered(browser_a, f"{BASE}?kept={ref(decision)}")))
    old = confirm_form(browser_a, url)
    real, calls = discovery_web.check_kept, []

    def changed_on_the_second_look(db, owner_id, chosen, now):
        calls.append(chosen)
        found = real(db, owner_id, chosen, now)

        return found if len(calls) == 1 else replace(found, kept=(), changed=("商业分析",))

    monkeypatch.setattr(discovery_web, "check_kept", changed_on_the_second_look)
    voided = browser_a.post(f"{url}/confirm", old)

    assert len(calls) >= 2 and voided.status_code == 422 and tasks(searches) == {}
    assert browser_a.post(f"{url}/confirm", old).status_code == 404
