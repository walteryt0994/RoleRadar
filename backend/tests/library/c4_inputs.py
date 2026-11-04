"""Check the stored inputs of a frozen C4 job case before it is sent (U2-C4 R1).

The job cases of the C4 batch are the frozen cases of the U2-A2 R11 archive
(its CASES), read here as data: the archive is parsed, never run. Before the
Owner sends a case, this compares the synthetic user's library with it,
opened read-only so that nothing in it changes:

- each experience of the case confirmed, in its category, with every field
  as frozen and no other field filled;
- for a case with job preferences, the saved preferences with exactly its
  fields, each under its dimension and stance with its value, and no other.

Any difference means the case must not be sent (exit status 1). What is
chosen on the page itself, the posting, the experiences ticked and whether
the preferences are used, never reaches the library: this prints what they
must be, to be checked on the review page before sending.

Usage, from backend/:
    ../.venv/bin/python tests/library/c4_inputs.py --db LIBRARY_DB --archive R11_SCRIPT --user A --case case1
"""
import argparse
import ast
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.library.content import (  # noqa: E402
    CATEGORY_FIELDS,
    CATEGORY_LABELS,
    PREFERENCE_FIELDS,
    PREFERENCE_GROUPS,
    PREFERENCE_SINGLES,
    TITLE_FIELDS,
)
from app.library.identity import DEV_USER_LABELS  # noqa: E402
from app.library.models import User  # noqa: E402
from app.library.projection import current_preferences, project_confirmed_library  # noqa: E402

# A preference as the pages name it: its dimension and stance, as 工作方式（不能接受）.
PREFERENCE_LABELS = {
    field.name: f"{label}（{field.label}）" for _, label, fields in PREFERENCE_GROUPS for field in fields
} | {field.name: field.label for field in PREFERENCE_SINGLES}
PREFERENCE_ORDER = [field.name for field in PREFERENCE_FIELDS]


@dataclass(frozen=True)
class JobCase:
    """The inputs of one frozen job case: what is stored in the library, and what is chosen on the page."""

    name: str
    title: str
    posting: str
    # (category, the fields filled), in the order the case lists them.
    experiences: tuple[tuple[str, dict[str, str]], ...]
    preferences: dict[str, str]


def load_cases(archive: Path) -> dict[str, JobCase]:
    """The archive's CASES by name, read as data: its source is parsed and the literals taken, nothing of it is run."""
    tree = ast.parse(archive.read_text(encoding="utf-8"))
    assigned = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and any(getattr(target, "id", None) == "CASES" for target in node.targets)
    ]

    if len(assigned) != 1:
        raise ValueError("the archive has no one CASES")

    cases = {}

    for call in assigned[0].elts:
        given = {keyword.arg: keyword.value for keyword in call.keywords}
        case = JobCase(*(ast.literal_eval(given[name]) for name in ("name", "title", "posting", "experiences", "preferences")))
        cases[case.name] = case

    return cases


def _read_only(db_path: Path) -> sqlite3.Connection:
    # A plain path, so that the isolation audit sees the file itself; SQLite then refuses every write on it.
    connection = sqlite3.connect(str(db_path))
    connection.execute("PRAGMA query_only = ON")

    return connection


def check(db_path: Path, user: str, case: JobCase) -> list[str]:
    """What differs between what the user has stored and the case; nothing when it is as frozen."""
    if not db_path.is_file():
        # Never a new, empty library made by asking about one that is not there.
        raise FileNotFoundError(db_path)

    engine = create_engine("sqlite://", creator=lambda: _read_only(db_path.resolve()))

    try:
        with Session(engine) as db:
            owner = db.scalar(select(User).where(User.display_label == user))

            if owner is None:
                return [f"库里没有用户“{user}”。"]

            experiences = project_confirmed_library(db, owner.id, datetime.now(timezone.utc)).experiences
            preferences = current_preferences(db, owner.id)
    finally:
        engine.dispose()

    problems = [
        _missing(category, fields, experiences)
        for category, fields in case.experiences
        if not any(entry.category == category and entry.content == fields for entry in experiences)
    ]

    if case.preferences:
        problems += _preference_problems(case.preferences, preferences)

    return problems


def _missing(category: str, fields: dict[str, str], experiences) -> str:
    """Why an experience of the case is not there as frozen: a draft, or the fields of the one by its title."""
    labels = {field.name: field.label for field in CATEGORY_FIELDS[category]}
    title = fields.get(TITLE_FIELDS[category], "")
    named = [
        entry for entry in experiences if entry.category == category and entry.content.get(TITLE_FIELDS[category]) == title
    ]
    said = f"{CATEGORY_LABELS[category]}「{title}」"

    if not named:
        return f"没有已确认的{said}（草稿要先确认）。"

    differences = [
        f"{labels[name]}应为“{fields.get(name, '')}”，库里是“{named[0].content.get(name, '')}”"
        for name in labels
        if fields.get(name, "") != named[0].content.get(name, "")
    ]

    return f"{said}和冻结样例不同：" + "；".join(differences) + "。"


def _preference_problems(frozen: dict[str, str], stored: dict[str, str]) -> list[str]:
    """Each preference that differs, by dimension, stance and value."""
    problems = []

    for name in sorted(set(frozen) | set(stored), key=PREFERENCE_ORDER.index):
        wanted, found = frozen.get(name), stored.get(name)

        if wanted == found:
            continue

        if wanted is None:
            problems.append(f"偏好多填了{PREFERENCE_LABELS[name]}：“{found}”，冻结样例里没有这一项。")
        elif found is None:
            elsewhere = [other for other, value in stored.items() if value == wanted and other not in frozen]
            moved = f"（库里填在了{PREFERENCE_LABELS[elsewhere[0]]}）" if elsewhere else ""
            problems.append(f"偏好缺少{PREFERENCE_LABELS[name]}：应为“{wanted}”{moved}。")
        else:
            problems.append(f"偏好{PREFERENCE_LABELS[name]}应为“{wanted}”，库里是“{found}”。")

    return problems


def page_checks(case: JobCase) -> list[str]:
    """What the review page must show for the case: chosen there, so never in the library."""
    ticked = "、".join(f"{CATEGORY_LABELS[category]}「{fields[TITLE_FIELDS[category]]}」" for category, fields in case.experiences)
    lines = [f"只勾选这些经历：{ticked}。"]

    if case.preferences:
        listed = "；".join(f"{PREFERENCE_LABELS[name]}：{value}" for name, value in case.preferences.items())
        lines.append(f"勾选「参考我的求职偏好」，核对页列出的偏好正好是：{listed}。")
    else:
        lines.append("不要勾选「参考我的求职偏好」。")

    lines.append("岗位描述逐字如下（核对页上联系方式会显示为已隐去）：")
    lines.append(case.posting)

    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="c4_inputs.py", description="发送前核对 C4 岗位样例存进合成库的输入：只读，不改库，不运行存档。"
    )
    parser.add_argument("--db", required=True, help="合成库的 SQLite 文件")
    parser.add_argument("--archive", required=True, help="冻结样例所在的 R11 存档脚本（只解析，不运行）")
    parser.add_argument("--user", required=True, choices=("A", "B"), help="合成用户")
    parser.add_argument("--case", required=True, help="样例名，如 case1")
    options = parser.parse_args(argv)
    db_path = Path(options.db)

    if not db_path.is_file():
        parser.error(f"没有这个数据库文件：{db_path}")

    cases = load_cases(Path(options.archive))

    if options.case not in cases:
        parser.error(f"存档里没有样例 {options.case}；有：{'、'.join(cases)}")

    case = cases[options.case]
    user = DEV_USER_LABELS["AB".index(options.user)]
    problems = check(db_path, user, case)
    print(f"{case.title}（{case.name}），{user}")

    if problems:
        print("库里的输入和冻结样例不一致，不要发送：")

        for problem in problems:
            print(f"- {problem}")

        return 1

    print("库里的经历和偏好与冻结样例一致。发送前在核对页再对照：")

    for line in page_checks(case):
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
