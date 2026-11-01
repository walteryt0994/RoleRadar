"""Upgrading an experience library database from structure version 3 to 4 (U2-B1).

Version 4 adds one table, resume_imports; nothing already in the file
changes. The upgrade is an explicit command, never a part of starting the
app. From backend/, with the app stopped and the file backed up:

    python -m app.library.migrate --db /path/to/library.db           # check only
    python -m app.library.migrate --db /path/to/library.db --apply   # upgrade

Before anything changes, the file must exist, pass the same path checks as
the app's database (never the legacy RoleRadar database, never inside the
repository), be a library database of version 3 with its library id and
without the new table, and pass SQLite's quick check. The upgrade is one
transaction that takes the write lock first: it creates the table with its
indexes and then sets the version, so any failure leaves the file as it
was. A file that is already version 4 is left alone. Afterwards every table
that was there must hold the same number of rows, and the library id must
be the same. Only table names and row counts are printed, never content.
"""
import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.library.config import TEST_ROOT_ENV, check_storage_path
from app.library.db import make_engine
from app.library.errors import ConfigError
from app.library.models import PREVIOUS_SCHEMA_VERSION, SCHEMA_VERSION, LibraryMeta, ResumeImport

NEW_TABLE = ResumeImport.__tablename__
REFUSED, FAILED = 2, 1


class MigrationRefused(Exception):
    """The file is not one this command upgrades; nothing was changed."""


@dataclass
class Survey:
    """What a library file holds: its version, id and the rows of each table (no content)."""

    version: str | None
    library_id: str | None
    counts: dict[str, int] = field(default_factory=dict)


def survey(engine: Engine) -> Survey:
    """Read the file's version, id and row counts; raise MigrationRefused if it is not a library file."""
    try:
        with engine.connect() as connection:
            tables = sorted(inspect(connection).get_table_names())

            if LibraryMeta.__tablename__ not in tables:
                raise MigrationRefused("这个文件不是经历库数据库（没有 library_meta 表）。")

            meta = dict(connection.execute(text("SELECT key, value FROM library_meta")).all())
            # Table names come from the file itself, so they are quoted.
            counts = {
                name: connection.execute(text(f'SELECT count(*) FROM "{name}"')).scalar_one()
                for name in tables
                if not name.startswith("sqlite_")
            }
            check = connection.execute(text("PRAGMA quick_check")).scalar_one()
    except SQLAlchemyError as error:
        raise MigrationRefused("这个文件不是可用的 SQLite 数据库。") from error

    if check != "ok":
        raise MigrationRefused("SQLite 检查发现这个文件有损坏，没有升级。请先从备份恢复。")

    return Survey(version=meta.get("schema_version"), library_id=meta.get("library_id"), counts=counts)


def check_upgradable(found: Survey) -> bool:
    """True if the file needs the upgrade, False if it is already version 4; raise if it is neither."""
    if found.version == SCHEMA_VERSION:
        return False

    if found.version != PREVIOUS_SCHEMA_VERSION:
        raise MigrationRefused(
            f"这个经历库的结构版本是 {found.version or '未知'}；本命令只把 {PREVIOUS_SCHEMA_VERSION} 升级到 {SCHEMA_VERSION}。"
        )

    if not found.library_id:
        raise MigrationRefused("这个经历库缺少编号（library_id），无法确认它和附件目录的关系，没有升级。")

    if NEW_TABLE in found.counts:
        raise MigrationRefused(f"版本 {PREVIOUS_SCHEMA_VERSION} 的文件里已经有 {NEW_TABLE} 表，状态不明，没有升级。")

    return True


def upgrade(engine: Engine) -> bool:
    """Upgrade a version 3 file in one transaction; return False if it was version 4 already."""
    with engine.begin() as connection:
        # Read again under the write lock: another run may have upgraded it meanwhile.
        version = connection.execute(
            text("SELECT value FROM library_meta WHERE key = 'schema_version'")
        ).scalar_one_or_none()

        if version == SCHEMA_VERSION:
            return False

        if version != PREVIOUS_SCHEMA_VERSION:
            raise MigrationRefused(f"这个经历库的结构版本是 {version or '未知'}，没有升级。")

        ResumeImport.__table__.create(connection)
        connection.execute(
            text("UPDATE library_meta SET value = :version WHERE key = 'schema_version'"),
            {"version": SCHEMA_VERSION},
        )

    return True


def verify(before: Survey, after: Survey) -> list[str]:
    """What differs, apart from the new table and the version; empty if nothing does."""
    problems = []

    if after.version != SCHEMA_VERSION:
        problems.append(f"升级后的结构版本是 {after.version}，不是 {SCHEMA_VERSION}。")

    if after.library_id != before.library_id:
        problems.append("升级后经历库编号变了。")

    for name, count in before.counts.items():
        if after.counts.get(name) != count:
            problems.append(f"{name} 表的行数从 {count} 变成了 {after.counts.get(name)}。")

    if after.counts.get(NEW_TABLE) != 0:
        problems.append(f"新表 {NEW_TABLE} 不是空的。")

    return problems


def _print_counts(found: Survey) -> None:
    for name, count in sorted(found.counts.items()):
        print(f"  {name}: {count} 行")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.library.migrate",
        description=f"把经历库数据库从结构版本 {PREVIOUS_SCHEMA_VERSION} 升级到 {SCHEMA_VERSION}（新增简历导入暂存表）。"
        "不加 --apply 时只检查，不修改文件。",
    )
    parser.add_argument("--db", required=True, help="要升级的经历库 SQLite 文件的绝对路径")
    parser.add_argument("--apply", action="store_true", help="确认执行升级（请先停止网页应用并备份文件）")
    args = parser.parse_args(argv)
    path = Path(args.db).expanduser()
    test_root = os.environ.get(TEST_ROOT_ENV)

    try:
        resolved = check_storage_path(path, test_root=Path(test_root) if test_root else None)
    except ConfigError as error:
        print(f"没有升级：{error}", file=sys.stderr)
        return REFUSED

    if not resolved.is_file():
        # Opening a missing file would create an empty database.
        print("没有升级：找不到这个文件。本命令只升级已有的经历库，不会新建文件。", file=sys.stderr)
        return REFUSED

    engine = make_engine(resolved)

    try:
        try:
            before = survey(engine)
            needed = check_upgradable(before)
        except MigrationRefused as error:
            print(f"没有升级：{error}", file=sys.stderr)
            return REFUSED

        print(f"文件：{resolved}")
        print(f"当前结构版本：{before.version}")
        _print_counts(before)

        if not needed:
            print(f"已经是结构版本 {SCHEMA_VERSION}，不需要升级，文件没有修改。")
            return 0

        if not args.apply:
            print(
                f"检查通过：可以升级到 {SCHEMA_VERSION}，只会新增空的 {NEW_TABLE} 表并更新版本号。"
                "这次只检查，没有修改文件；确认已备份后加 --apply 执行。"
            )
            return 0

        try:
            changed = upgrade(engine)
        except MigrationRefused as error:
            print(f"没有升级：{error}", file=sys.stderr)
            return REFUSED
        except SQLAlchemyError as error:
            print(f"升级失败，已回滚，文件保持原样（{type(error.__cause__ or error).__name__}）。", file=sys.stderr)
            return FAILED

        after = survey(engine)
        problems = verify(before, after)

        if problems:
            print("升级后的核对没有通过：", file=sys.stderr)

            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)

            return FAILED

        print("已升级。" if changed else f"已经是结构版本 {SCHEMA_VERSION}，文件没有修改。")
        print(f"升级后结构版本：{after.version}；原有各表行数不变，经历库编号不变。")
        _print_counts(after)

        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
