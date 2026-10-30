"""Configuration and data-path guard for the experience library.

The library keeps its own SQLite file and has no default location for it:
the path must be given explicitly. Before the file is created or opened,
check_storage_path() rejects any location that could reach the legacy
RoleRadar database, the parse-record directory, or this repository's working
tree (where a data file could be committed by accident). The checks look at
path names and file metadata only; they never open the files they compare.

Attachment files are kept in a folder of their own, also named explicitly.
check_files_path() keeps it out of the repository and the records directory,
and apart from the database file and any legacy database.
"""
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from app.library.errors import ConfigError, UnsafeStoragePath

BACKEND_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_DIR.parent
RECORDS_DIR = BACKEND_DIR / "records"
LEGACY_DB_NAME = "roleradar.db"

DB_PATH_ENV = "ROLERADAR_LIBRARY_DB_PATH"
FILES_DIR_ENV = "ROLERADAR_LIBRARY_FILES_DIR"
DEV_LOGIN_ENV = "ROLERADAR_LIBRARY_DEV_LOGIN"
TEST_ROOT_ENV = "ROLERADAR_TEST_ROOT"
LEGACY_DB_URL_ENV = "DATABASE_URL"


@dataclass(frozen=True)
class LibraryConfig:
    db_path: Path
    dev_login: bool = False
    test_root: Path | None = None
    session_lifetime: timedelta = timedelta(hours=12)
    purge_interval_seconds: float | None = 600.0
    purge_on_startup: bool = True
    dev_clock_offset: timedelta = timedelta(0)
    max_form_bytes: int = 256 * 1024
    # The private attachment folder; create_app() refuses to start without one.
    files_dir: Path | None = None

    def __post_init__(self):
        if self.dev_clock_offset and not self.dev_login:
            raise ConfigError("时钟偏移只用于本地开发验证，必须同时启用开发登录。")


def load_config_from_env(
    environ=None,
    *,
    db_path: str | Path | None = None,
    files_dir: str | Path | None = None,
    dev_login: bool | None = None,
    dev_clock_offset: timedelta = timedelta(0),
) -> LibraryConfig:
    environ = os.environ if environ is None else environ
    raw_path = db_path or environ.get(DB_PATH_ENV)
    raw_files = files_dir or environ.get(FILES_DIR_ENV)

    if not raw_path:
        raise ConfigError(
            f"请用 --db 或环境变量 {DB_PATH_ENV} 明确指定经历库的数据库文件；"
            "经历库不使用任何默认路径。"
        )

    if dev_login is None:
        dev_login = environ.get(DEV_LOGIN_ENV) == "1"

    test_root = environ.get(TEST_ROOT_ENV)

    return LibraryConfig(
        db_path=Path(raw_path),
        dev_login=dev_login,
        test_root=Path(test_root) if test_root else None,
        dev_clock_offset=dev_clock_offset,
        files_dir=Path(raw_files) if raw_files else None,
    )


def legacy_database_paths(environ=None, cwd=None) -> list[Path]:
    """Every file the legacy app could be using as its SQLite database."""
    environ = os.environ if environ is None else environ
    cwd = Path.cwd() if cwd is None else Path(cwd)
    candidates = [
        BACKEND_DIR / LEGACY_DB_NAME,
        REPO_ROOT / LEGACY_DB_NAME,
        cwd / LEGACY_DB_NAME,
    ]
    url = environ.get(LEGACY_DB_URL_ENV)

    if url:
        try:
            parsed = make_url(url)
        except ArgumentError as error:
            raise ConfigError(
                f"无法解析 {LEGACY_DB_URL_ENV}，不能确认新旧数据库是否分开。"
            ) from error

        if parsed.drivername.startswith("sqlite") and parsed.database not in (
            None,
            "",
            ":memory:",
        ):
            candidates.append(cwd / parsed.database)

    return candidates


def check_storage_path(
    db_path: str | Path,
    *,
    test_root: Path | None = None,
    repo_root: Path = REPO_ROOT,
    records_dir: Path = RECORDS_DIR,
    legacy_paths: list[Path] | None = None,
) -> Path:
    """Return the resolved database path, or raise UnsafeStoragePath."""
    path = Path(db_path).expanduser()

    if not path.is_absolute():
        raise UnsafeStoragePath("经历库数据库路径必须是绝对路径。")

    resolved = path.resolve()

    if resolved.is_dir():
        raise UnsafeStoragePath("经历库数据库路径指向了一个目录，需要指向一个文件。")

    if _inside(resolved, repo_root.resolve()):
        raise UnsafeStoragePath("经历库数据库不能放在代码仓库里，以免被误提交。")

    if _inside(resolved, records_dir.resolve()):
        raise UnsafeStoragePath("经历库数据库不能放在 records 目录里。")

    if legacy_paths is None:
        legacy_paths = legacy_database_paths()

    for legacy in legacy_paths:
        if _same_file(resolved, Path(legacy)):
            raise UnsafeStoragePath("经历库数据库与旧版 RoleRadar 数据库是同一个文件。")

    if test_root is not None and not _inside(resolved, Path(test_root).resolve()):
        raise UnsafeStoragePath("测试模式下，经历库数据库必须位于测试临时目录内。")

    return resolved


def check_files_path(
    files_dir: str | Path | None,
    *,
    db_path: Path,
    test_root: Path | None = None,
    repo_root: Path = REPO_ROOT,
    records_dir: Path = RECORDS_DIR,
    legacy_paths: list[Path] | None = None,
) -> Path:
    """Return the resolved attachment folder, or raise.

    The folder may not lie in the repository or the records directory, nor
    contain either of them, the library database or a legacy database.
    Whether it is empty or already this library's is checked when the app
    claims it (files.FileStore.claim).
    """
    if not files_dir:
        raise ConfigError(
            f"请用 --files 或环境变量 {FILES_DIR_ENV} 明确指定附件目录（一个新的空目录）；"
            "附件不使用任何默认位置。"
        )

    path = Path(files_dir).expanduser()

    if not path.is_absolute():
        raise UnsafeStoragePath("附件目录必须是绝对路径。")

    resolved = path.resolve()

    if resolved.exists() and not resolved.is_dir():
        raise UnsafeStoragePath("附件目录路径指向了一个文件，需要指向一个目录。")

    for protected, message in (
        (repo_root.resolve(), "附件目录不能放在代码仓库里，也不能包含代码仓库。"),
        (records_dir.resolve(), "附件目录不能放在 records 目录里，也不能包含它。"),
    ):
        if _inside(resolved, protected) or _inside(protected, resolved):
            raise UnsafeStoragePath(message)

    if _inside(Path(db_path).resolve(), resolved):
        raise UnsafeStoragePath("经历库数据库不能放在附件目录里；请把两者分开。")

    if legacy_paths is None:
        legacy_paths = legacy_database_paths()

    for legacy in legacy_paths:
        if _inside(Path(legacy).resolve(), resolved):
            raise UnsafeStoragePath("附件目录不能包含旧版 RoleRadar 数据库。")

    if test_root is not None and not _inside(resolved, Path(test_root).resolve()):
        raise UnsafeStoragePath("测试模式下，附件目录必须位于测试临时目录内。")

    return resolved


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _same_file(path: Path, other: Path) -> bool:
    other = other.resolve()

    if path == other:
        return True

    # A hard link has a different name but is the same file on disk.
    if path.exists() and other.exists():
        return os.path.samefile(path, other)

    return False
