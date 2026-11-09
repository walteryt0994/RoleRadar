"""Entry point of the RoleRadar experience library web app (v0.1 U1, U2-A2, U2-B1, U2-B2, U2-B3).

This is a separate app from app.main: it mounts the library pages, the
resume import pages (app.library.imports_web), the review of their
candidates (app.library.review_web) and their adoption at the user's choice
(app.library.adoption_web), finishing an import and keeping its original
as an attachment (app.library.finish_web), the job analysis pages
(app.job_analysis), the preview of finding postings on synthetic sources
with simulated decisions and analyses only (app.discovery.web, U3-A3: it never
takes the real service below), the direction exploration pages on the local
simulation only (app.directions.web, U3-C3a: neither), stores data in its own SQLite file and attachment
folder, and never imports the legacy database, models or routers. A resume
import is read on this machine in a child process and sent nowhere; its
candidates come from local simulated rules, or, with a real service whose
approval covers structuring, from that service, sent only the masked text
the user confirmed (app.live_structuring, U2-C3). The job
analysis uses the shared job description contracts of
app.schemas, which hold no database. Importing this module has no side
effects; create_app() does the work. The analysis uses the simulated
service unless it is built with a real one (U2-C2): from approved terms in
the environment (app.job_analysis.live, analysis_setup, which refuses while
no input bound is checked), or a synthetic LiveSetup a test passes. The real
service's gate (app.live_gate) is opened on the library's ledger before the
app serves anything, and closed when it stops. An app asked to use a real
service it cannot have refuses to start rather than quietly simulating. Run
it with `python -m app.web_main`, which only binds to 127.0.0.1 (see main()).
"""
import argparse
import asyncio
import contextlib
import logging
import os
from collections.abc import Mapping
from datetime import timedelta

import uvicorn
from fastapi import FastAPI

from app.library.clock import Clock, SystemClock
from app.library.config import LibraryConfig, check_files_path, check_storage_path, load_config_from_env
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.files import FileStore
from app.library.identity import ensure_dev_users
from app.library.adoption_web import install_adoption
from app.library.finish_web import install_finish
from app.library.imports_web import install_imports
from app.library.models import create_schema, read_library_id
from app.library.review_web import install_review
from app.discovery.web import DiscoveryRuntime, install_discovery
from app.directions.web import DirectionsRuntime, install_directions
from app.job_analysis.live import analysis_setup
from app.job_analysis.service import LiveService
from app.job_analysis.tasks import SWEEP_SECONDS
from app.job_analysis.web import JobAnalysisRuntime, install_job_analysis
from app.live_structuring.web import install_structuring
from app.library.web import LibraryRuntime, build_templates, install_web
from app.live_gate import LiveRefused, LiveSetup, open_gate

LOCAL_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

logger = logging.getLogger("roleradar.library")


def create_app(
    config: LibraryConfig | None = None,
    *,
    clock: Clock | None = None,
    simulation_delay: float = 0.0,
    job_sweep_seconds: float = SWEEP_SECONDS,
    environ: Mapping[str, str] | None = None,
    live: LiveSetup | None = None,
) -> FastAPI:
    """Build the app; checks the data paths before anything is created or opened.

    The attachment folder belongs to one database. A folder that already
    belongs to a database can only be used with that database, so it is
    refused before a new database file would be created next to it. The
    analysis service comes from live, else from environ (the process
    environment when None); a real one asked for and refused stops the app
    first of all. A real service's gate is opened once the database is ready.
    """
    config = config or load_config_from_env()

    if live is None:
        live = analysis_setup(os.environ if environ is None else environ)
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
    service = None

    if live is not None:
        try:
            service = LiveService(open_gate(database, db_path, live.terms, live.basis, clock), live.make_provider)
        except LiveRefused as refused:
            engine.dispose()
            raise ConfigError(f"要求使用真实服务，但不能启用：{refused}") from None

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
    install_imports(app)
    install_review(app)
    install_adoption(app)
    install_finish(app)
    # One real service, one gate: structuring (U2-C3) and the analysis share it.
    install_structuring(app, live=service)
    install_job_analysis(app, simulation_delay=simulation_delay, sweep_seconds=job_sweep_seconds, live=service)
    # The search preview gets no service: it runs the simulation only (app.discovery.web).
    install_discovery(app, simulation_delay=simulation_delay, sweep_seconds=job_sweep_seconds)
    # Direction exploration (U3-C3a) runs the local simulation only: it gets no service either.
    install_directions(app, simulation_delay=simulation_delay, sweep_seconds=job_sweep_seconds)

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

    jobs = app.state.job_analysis
    searches = app.state.discovery
    explorations = app.state.directions
    # Analysis tasks, searches and explorations expire, and overdue ones stop, even when no page asks about them.
    jobs.sweeper = asyncio.create_task(_sweep_loop(jobs))
    searches.sweeper = asyncio.create_task(_sweep_loop(searches))
    explorations.sweeper = asyncio.create_task(_sweep_loop(explorations))

    try:
        yield
    finally:
        for running in (task, jobs.sweeper, searches.sweeper, explorations.sweeper):
            if running is not None:
                running.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await running

        # Real structuring runs and running analyses stop before their next request; the gate
        # closes once (jobs.shutdown), after any request still out is settled.
        app.state.structuring.shutdown()
        jobs.shutdown()
        searches.shutdown()
        explorations.shutdown()
        runtime.database.engine.dispose()


async def _cleanup_loop(runtime: LibraryRuntime) -> None:
    """Run clean-up at a fixed interval for as long as the app runs."""
    while True:
        await asyncio.sleep(runtime.config.purge_interval_seconds)

        try:
            await asyncio.to_thread(runtime.run_cleanup)
        except Exception:  # keep the loop alive; the next pass retries
            logger.exception("unexpected error in the clean-up loop")


async def _sweep_loop(jobs: JobAnalysisRuntime | DiscoveryRuntime | DirectionsRuntime) -> None:
    """Sweep the analysis tasks, or the searches, every jobs.sweep_seconds for as long as the app runs."""
    while True:
        await asyncio.sleep(jobs.sweep_seconds)

        try:
            await asyncio.to_thread(jobs.sweep)
        except Exception as error:  # keep the loop alive; the next pass retries
            logger.error("unexpected error while sweeping tasks (%s)", type(error).__name__)


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
        "--simulation-delay",
        type=float,
        default=0.0,
        help="仅用于本地验证：模拟服务每一步故意等待若干秒，用来查看等待、取消和迟到的回答",
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

        if args.simulation_delay and not args.dev_login:
            raise ConfigError("模拟等待只用于本地开发验证，必须同时启用开发登录。")

        if not 0 <= args.simulation_delay <= 60:
            raise ConfigError("模拟等待要在 0 到 60 秒之间。")

        config = load_config_from_env(
            db_path=args.db,
            files_dir=args.files,
            dev_login=True if args.dev_login else None,
            dev_clock_offset=timedelta(days=args.dev_clock_offset_days),
        )
        app = create_app(config, simulation_delay=args.simulation_delay)
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
