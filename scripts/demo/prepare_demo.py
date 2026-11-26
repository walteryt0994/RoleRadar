#!/usr/bin/env python3
"""Prepare a fresh synthetic RoleRadar demo library in a new folder (local only).

It builds the app in this process (app.web_main.create_app) on a new SQLite file and attachment folder, and fills it
the way a person would, through the app's own pages and forms (an in-process test client, no server, no port): it
signs in as the development user 合成用户 A and saves the demo story of docs/demo/fixtures/demo_story.json.

What it leaves in the folder:
- the story's basic profile, two confirmed experiences and the job preferences;
- fallback records for a live demo, each made by the normal flow and run on the local simulation: one saved direction
  exploration (no direction kept, so the live demo can keep one), one saved search summary, and the supplied job
  description of docs/demo/fixtures/supplied_jd.txt added to 我的岗位 (marked 收藏, with a note saying it was prepared)
  with one saved analysis;
- demo-manifest.json, saying what was made and when (scripts/demo/run_demo.sh only starts folders that have it).

Safety:
- the folder must not exist yet: an existing folder is never reused or overwritten; to reset, prepare a new folder;
- the app is built with an empty environment (create_app(environ={})), so no real model, source or approval is ever
  read from the shell: everything runs on the local simulation and the synthetic sources;
- any network connection or name lookup outside this machine raises at once (an audit hook, before the app loads);
- nothing outside the new folder is written (Python's bytecode cache is turned off for this run).

Usage, from the repository root:
    .venv/bin/python scripts/demo/prepare_demo.py [--dir NEW_FOLDER]
With no --dir, the folder is ~/roleradar-demo/demo-<date>-<time>.
"""
import argparse
import json
import re
import shlex
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
FIXTURES = ROOT / "docs" / "demo" / "fixtures"
DEV_USER = "合成用户 A"
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}
WAIT_SECONDS = 60
MANIFEST = "demo-manifest.json"


def _no_network(event: str, args: tuple) -> None:
    """Refuse every connection or lookup that would leave this machine (the app never needs one in a demo)."""
    if event == "socket.connect":
        address = args[1]
        host = address[0] if isinstance(address, tuple) else None

        if host is not None and host not in LOCAL_HOSTS:
            raise RuntimeError("演示准备不允许联网：只用本机的模拟服务和合成数据")
    elif event == "socket.getaddrinfo":
        host = args[0]

        if host not in (None, *LOCAL_HOSTS):
            raise RuntimeError("演示准备不允许联网：只用本机的模拟服务和合成数据")


class _Forms(HTMLParser):
    """The forms of a page as a browser submits them: checked boxes only, repeated names kept, plus every value a
    named checkbox offers (checked or not)."""

    def __init__(self):
        super().__init__()
        self.forms, self.offered = [], {}
        self._form = self._select = self._text = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")

        if tag == "form":
            self._form = {"action": a.get("action", ""), "method": (a.get("method") or "get").lower(), "pairs": []}
            self.forms.append(self._form)
        elif tag == "input" and name:
            kind = (a.get("type") or "text").lower()

            if kind in ("checkbox", "radio"):
                self.offered.setdefault(name, []).append(a.get("value", "on"))

            if self._form is None or kind in ("submit", "button", "file", "reset"):
                return

            if kind not in ("checkbox", "radio") or "checked" in a:
                self._form["pairs"].append((name, a.get("value", "on" if kind in ("checkbox", "radio") else "")))
        elif tag == "textarea" and name and self._form is not None:
            self._text = [name, ""]
        elif tag == "select" and name and self._form is not None:
            self._select = [name, None, None]
        elif tag == "option" and self._select is not None:
            value = a.get("value", "")
            self._select[2] = value if self._select[2] is None else self._select[2]
            self._select[1] = value if "selected" in a else self._select[1]

    def handle_data(self, data):
        if self._text is not None:
            self._text[1] += data

    def handle_endtag(self, tag):
        if tag == "textarea" and self._text is not None:
            self._form["pairs"].append(tuple(self._text))
            self._text = None
        elif tag == "select" and self._select is not None:
            name, chosen, first = self._select
            self._form["pairs"].append((name, chosen if chosen is not None else (first or "")))
            self._select = None
        elif tag == "form":
            self._form = None


def _read(html: str) -> _Forms:
    reader = _Forms()
    reader.feed(html)

    return reader


class Visitor:
    """One browser signed in as the development user, talking to the app in this process."""

    def __init__(self, client):
        self.client = client

    def page(self, path: str) -> str:
        response = self.client.get(path)

        if response.status_code != 200:
            raise RuntimeError(f"打开 {path} 失败：{response.status_code}")

        return response.text

    def post(self, path: str, action_end: str, expect: int = 303, **changes) -> str:
        """Open path, fill its post form whose action ends with action_end, send it; the redirect's target."""
        html = self.page(path)
        forms = [form for form in _read(html).forms if form["method"] == "post" and form["action"].endswith(action_end)]

        if not forms:
            raise RuntimeError(f"{path} 上没有提交到 …{action_end} 的表单")

        data: dict[str, list[str]] = {}

        for name, value in forms[0]["pairs"]:
            data.setdefault(name, []).append(value)

        for name, value in changes.items():
            data[name] = value if isinstance(value, list) else [value]

        response = self.client.post(forms[0]["action"], data=data)

        if response.status_code != expect:
            raise RuntimeError(f"提交 {forms[0]['action']} 得到 {response.status_code}（应为 {expect}）")

        return response.headers.get("location", "")

    def offered(self, path: str, name: str) -> list[str]:
        return _read(self.page(path)).offered.get(name, [])

    def wait_for(self, path: str, marker: str) -> str:
        """Open a task page until it carries marker (its result form): the local simulation answers in moments."""
        deadline = time.monotonic() + WAIT_SECONDS

        while time.monotonic() < deadline:
            html = self.page(path)

            if marker in html:
                return html

            time.sleep(0.2)

        raise RuntimeError(f"{path} 在 {WAIT_SECONDS} 秒内没有出结果")

    def sign_in(self, label: str) -> None:
        html = self.page("/login")
        chosen = [
            form for form in _read(html).forms if form["action"] == "/login/dev"
        ]
        # The page has one form per development user; pick the one whose button names the user.
        blocks = re.findall(r'<form[^>]*action="/login/dev".*?</form>', html, re.S)

        for form, block in zip(chosen, blocks):
            if label in block:
                response = self.client.post("/login/dev", data=dict(form["pairs"]))

                if response.status_code != 303:
                    raise RuntimeError(f"开发登录失败：{response.status_code}")

                return

        raise RuntimeError(f"登录页上没有 {label} 的开发登录按钮")


def _path_only(location: str) -> str:
    return location.split("?")[0]


def _first_link(html: str, prefix: str) -> str:
    """The first link on a page whose path starts with prefix (to note a saved record's own page)."""
    found = re.search(rf'href="({re.escape(prefix)}[^"?#]+)', html)

    if found is None:
        raise RuntimeError(f"页面上没有 {prefix}… 的链接")

    return found.group(1)


def _last(location: str) -> str:
    return _path_only(location).rstrip("/").rsplit("/", 1)[1]


def seed(visitor: Visitor, story: dict, posting_text: str) -> dict:
    made: dict[str, str] = {}
    person = story["persona"]
    visitor.post("/basic-profile/edit", "/basic-profile", name=person["name"], city=person["city"], skills=person["skills"])

    experiences = []

    for item in story["experiences"]:
        fields = {
            "category": item["category"],
            "context": item["context"],
            "body": "\n".join(item["body"]),
            "period_mode": "months",
            "period_start_year": str(item["period"]["start"][0]),
            "period_start_month": str(item["period"]["start"][1]),
            "period_end_year": str(item["period"]["end"][0]),
            "period_end_month": str(item["period"]["end"][1]),
        }
        fields.update({key: item[key] for key in ("title", "role", "name") if key in item})
        item_id = _last(visitor.post(f"/experiences/new?kind={item['category']}", "/experiences", **fields))
        visitor.post(f"/experiences/{item_id}", f"/experiences/{item_id}/confirm")
        experiences.append(item_id)

    made["experiences"] = experiences

    preferences = story["preferences"]
    rows: dict[str, str] = {}

    for dimension in ("directions", "job_types", "cities", "work_content", "work_style"):
        for index, (text, stance) in enumerate(preferences[dimension]):
            rows[f"{dimension}_r{index}"] = text
            rows[f"{dimension}_r{index}_stance"] = stance

    rows.update(
        less_content=preferences["less_content"],
        must_start_year=str(preferences["must_start"][0]),
        must_start_month=str(preferences["must_start"][1]),
        must_other=preferences["must_other"],
    )
    visitor.post("/job-preferences/edit", "/job-preferences", **rows)

    # Fallback 1: a saved direction exploration of both experiences and the preferences, no direction kept yet.
    sources = visitor.offered("/directions", "source")
    task = _path_only(visitor.post("/directions", "/directions/review", source=sources))
    visitor.post(task, "/confirm", action="start")
    visitor.wait_for(task, f'action="{task}/save"')
    saved_page = visitor.page(visitor.post(task, f"{task}/save"))
    made["saved_exploration"] = _first_link(saved_page, "/directions/explorations/")

    # Fallback 2: a saved search summary of the synthetic sources (the results themselves stay 30 minutes only).
    sources = visitor.offered("/discover", "source")
    search = _path_only(visitor.post("/discover", "/discover/start", targets="数据分析实习生", source=sources, use_preferences="yes"))
    visitor.wait_for(search, f'action="{search}/summary"')
    visitor.post(search, f"{search}/summary")
    made["saved_search_summary"] = _first_link(visitor.page("/job-records/history?tab=searches"), "/job-records/searches/")

    # Fallback 3: the supplied job description, added to 我的岗位, marked 收藏, with one saved analysis.
    saved = story["saved_job"]
    review = _path_only(
        visitor.post(
            "/job-records/supply",
            "/job-records/supply/text",
            title=posting_text.splitlines()[0],
            company=posting_text.splitlines()[1].split(" · ")[0],
            text=posting_text,
        )
    )
    record = _path_only(visitor.post(review, "/keep"))
    record_id = _last(record)
    # The decision and its note go together, as the record page's "原因和备注" form sends them.
    visitor.post(record, f"{record}/decide", decision=saved["decision"], reasons_shown="1", note=saved["note"])

    sources = visitor.offered(f"/job-analysis?record={record_id}", "source")
    analysis = _path_only(
        visitor.post(f"/job-analysis?record={record_id}", "/job-analysis/review", source=sources, use_preferences="yes")
    )
    visitor.post(analysis, "/confirm", action="start")
    visitor.wait_for(analysis, f'action="{analysis}/save"')
    visitor.post(analysis, f"{analysis}/save")
    made["saved_job"] = record
    made["saved_job_analysis"] = _first_link(visitor.page(record), "/job-records/analyses/")

    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/demo/prepare_demo.py",
        description="在一个新目录里准备 RoleRadar 合成演示库（只用本机模拟服务和合成数据，不联网）。",
    )
    parser.add_argument("--dir", help="新的演示目录（必须还不存在）；默认 ~/roleradar-demo/demo-<日期>-<时间>")
    args = parser.parse_args(argv)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(args.dir).expanduser() if args.dir else Path.home() / "roleradar-demo" / f"demo-{stamp}"
    target = target.resolve()

    if target.exists():
        print(f"无法准备：{target} 已经存在。演示目录不会被复用或覆盖；要重置演示，请换一个新目录。", file=sys.stderr)

        return 2

    if target == ROOT or ROOT in target.parents:
        print(f"无法准备：演示目录要放在仓库外面（{ROOT} 以外）。", file=sys.stderr)

        return 2

    sys.addaudithook(_no_network)
    sys.path.insert(0, str(BACKEND))

    from fastapi.testclient import TestClient

    from app.library.config import LibraryConfig
    from app.web_main import create_app

    story = json.loads((FIXTURES / "demo_story.json").read_text(encoding="utf-8"))
    posting_text = (FIXTURES / story["saved_job"]["fixture"]).read_text(encoding="utf-8")

    target.mkdir(parents=True)
    config = LibraryConfig(db_path=target / "library.db", files_dir=target / "files", dev_login=True, purge_interval_seconds=None)
    # An empty environment: no real model, source or approval can be picked up from the shell.
    app = create_app(config, environ={})

    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50001), follow_redirects=False) as client:
        visitor = Visitor(client)
        visitor.sign_in(DEV_USER)
        made = seed(visitor, story, posting_text)

    manifest = {
        "roleradar_demo": 1,
        "prepared_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "story": "docs/demo/fixtures/demo_story.json",
        "user": DEV_USER,
        "made_by_normal_flows": made,
        "notes": [
            "合成示例：人物、经历、公司和岗位都是虚构的。",
            "所有分析和探索都由本机模拟服务按固定规则生成，不是 AI 的判断。",
            "查找、探索和分析的结果页只保留 30 分钟，服务重启后要重做；保存过的记录一直在。",
        ],
    }
    (target / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"已准备好演示目录：{target}")
    print("内容：林夏（合成示例）的基本资料、两段已确认经历、求职偏好；预先保存的一次方向探索、一份查找摘要、")
    print("      一条补充的合成岗位（已收藏）和它的一份分析。")
    print("下一步（在仓库根目录运行）：")
    # Quoted for the shell (spaces, Chinese, quotes in the folder name), so the line can be copied as it is.
    print(f"  scripts/demo/run_demo.sh {shlex.quote(str(target))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
