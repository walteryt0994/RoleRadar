"""Entry point of the RoleRadar experience library web app (v0.1 U1).

This is a separate app from app.main: it mounts only the library pages,
stores data in its own SQLite file and attachment folder, and never imports
the legacy database, models or routers. Importing this module has no side
effects; create_app() does the work. Run it with `python -m app.web_main`,
which only binds to 127.0.0.1 (see main()).
"""
import argparse
import asyncio
import contextlib
import logging
from datetime import timedelta

import uvicorn
from fastapi import FastAPI

from app.library.clock import Clock, SystemClock
from app.library.config import LibraryConfig, check_files_path, check_storage_path, load_config_from_env
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.files import FileStore
from app.library.identity import ensure_dev_users
from app.library.models import create_schema, read_library_id
from app.library.web import LibraryRuntime, build_templates, install_web

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = logging.getLogger("roleradar.library")


def create_app(config: LibraryConfig | None = None, *, clock: Clock | None = None) -> FastAPI:
    """Build the app; checks the data paths before anything is created or opened.

    The attachment folder belongs to one database. A folder that already
    belongs to a database can only be used with that database, so it is
    refused before a new database file would be created next to it.
    """
    config = config or load_config_from_env()
    db_path = check_storage_path(config.db_path, test_root=config.test_root)
    files = FileStore(check_files_path(config.files_dir, db_path=db_path, test_root=config.test_root))

    if files.claimed_by() is not None and not db_path.exists():
        raise ConfigError("这个附件目录已经属于另一个经历库数据库；请为新的数据库指定一个新的空目录。")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = make_engine(db_path)

    try:
        create_schema(engine)
        files.claim(read_library_id(engine))
    except ConfigError:
        engine.dispose()
        raise

    database = Database(engine)
    clock = clock or SystemClock(config.dev_clock_offset)

    if config.dev_login:
        with database.transaction() as db:
            ensure_dev_users(db, clock.now())

    app = FastAPI(
        title="RoleRadar 经历库（本地开发）",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
    )
    app.state.library = LibraryRuntime(
        config=config,
        database=database,
        clock=clock,
        templates=build_templates(config),
        files=files,
    )
    install_web(app, config)

    return app


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    runtime = app.state.library

    if runtime.config.purge_on_startup:
        # Catch up on anything that expired while the app was not running.
        await asyncio.to_thread(runtime.run_cleanup)

    task = None

    if runtime.config.purge_interval_seconds:
        task = asyncio.create_task(_cleanup_loop(runtime))

    try:
        yield
    finally:
        if task is not None:
            task.cancel()

            with contextlib.suppress(asyncio.CancelledError):
                await task

        runtime.database.engine.dispose()


async def _cleanup_loop(runtime: LibraryRuntime) -> None:
    """Run clean-up at a fixed interval for as long as the app runs."""
    while True:
        await asyncio.sleep(runtime.config.purge_interval_seconds)

        try:
            await asyncio.to_thread(runtime.run_cleanup)
        except Exception:  # keep the loop alive; the next pass retries
            logger.exception("unexpected error in the clean-up loop")


def check_bind_host(host: str) -> None:
    if host != LOCAL_HOST:
        raise ConfigError(
            f"本地开发版只能监听 {LOCAL_HOST}；真实登录建成之前，不能让其他设备访问。"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.web_main",
        description="启动本地经历库网页：只监听 127.0.0.1，只用于合成资料。",
    )
    parser.add_argument(
        "--db",
        help="经历库 SQLite 文件的绝对路径（必须在仓库外，不能是旧数据库）；"
        "也可以用环境变量 ROLERADAR_LIBRARY_DB_PATH 指定",
    )
    parser.add_argument(
        "--files",
        help="私有附件目录的绝对路径（一个新的空目录，必须在仓库外；它会和这个数据库绑定）；"
        "也可以用环境变量 ROLERADAR_LIBRARY_FILES_DIR 指定",
    )
    parser.add_argument("--host", default=LOCAL_HOST, help="只允许 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--dev-login",
        action="store_true",
        help="启用合成用户 A/B 的开发登录（不是真实身份验证）",
    )
    parser.add_argument(
        "--dev-clock-offset-days",
        type=float,
        default=0.0,
        help="仅用于本地验证：把时钟往后拨若干天，查看 7 天到期后的效果",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        check_bind_host(args.host)
        config = load_config_from_env(
            db_path=args.db,
            files_dir=args.files,
            dev_login=True if args.dev_login else None,
            dev_clock_offset=timedelta(days=args.dev_clock_offset_days),
        )
        app = create_app(config)
    except ConfigError as error:
        parser.exit(2, f"无法启动：{error}\n")

    # proxy_headers=False: the client address must come from the socket, so a
    # forged X-Forwarded-For header cannot pass the local-only check.
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
