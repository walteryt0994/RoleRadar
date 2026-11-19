"""UI bilingual batch: the workspace (plan 5.1), a read-only page over what already exists.

Continue lists the saved resume-import drafts and what this session still runs or holds; Recently saved at most five
records kept for good, newest first, with their kind, never one in the bin, a temporary recommendation or anyone
else's. Rendering it writes nothing and sends nothing. Synthetic data only.
"""
import re

from job_helpers import confirmed, text_of
from library_helpers import forms_in


def recent_titles(page: str) -> list[str]:
    section = page[page.index('id="recent-title"'):]

    return re.findall(r'<a class="home-row-title" href="[^"]+">([^<]+)</a>', section)


def test_an_empty_workspace_says_so_and_offers_the_journeys(browser_a):
    page = browser_a.get("/workspace").text

    # The four tasks at the top (owner redesign, plan 8.1): 记录经历, 探索方向, 发现岗位, 分析岗位.
    entries = page[page.index('<nav class="task-entries"'): page.index("</nav>", page.index('<nav class="task-entries"'))]
    assert re.findall(r'href="([^"]+)"', entries) == ["/experiences/new", "/directions", "/discover", "/job-analysis"]
    assert "还没有保存记录" in page


def test_recently_saved_shows_five_newest_first_and_nothing_binned(app, browser_a, clock):
    items = []

    for number in range(7):
        clock.advance(seconds=60)
        items.append(confirmed(browser_a, title=f"项目 {number}（合成）", body="合成内容。"))

    titles = recent_titles(browser_a.get("/workspace").text)
    assert titles == [f"项目 {number}（合成）" for number in (6, 5, 4, 3, 2)]

    form = next(form for form in forms_in(browser_a.get(f"/experiences/{items[6]}/delete").text) if form.action.endswith("/delete"))
    assert browser_a.post(form.action, form.fields).status_code == 303
    assert "项目 6（合成）" not in recent_titles(browser_a.get("/workspace").text)


def test_another_users_records_never_show(browser_a, browser_b):
    confirmed(browser_a, title="只属于 A（合成）", body="合成内容。")

    assert "只属于 A（合成）" not in text_of(browser_b.get("/workspace").text)


def test_viewing_the_workspace_writes_nothing(app, browser_a):
    confirmed(browser_a, title="看看而已（合成）", body="合成内容。")
    before = browser_a.get("/workspace").text

    assert browser_a.get("/workspace").text == before
