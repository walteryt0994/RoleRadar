"""Entry point of the RoleRadar experience library web app (v0.1 U1, U2-A2, U2-B1, U2-B2, U2-B3).

This is a separate app from app.main: it mounts the library pages, the
resume import pages (app.library.imports_web), the review of their
candidates (app.library.review_web) and their adoption at the user's choice
(app.library.adoption_web), finishing an import and keeping its original
as an attachment (app.library.finish_web), the job analysis pages
(app.job_analysis), the pages that find postings (app.discovery.web), with
simulated decisions and analyses on synthetic sources or the real source read
under its approvals, and the real service below only for validation B, chosen
explicitly with both of its approvals (app.discovery.model_live, U3-D2-E3),
the direction exploration pages (app.directions.web,
U3-C3a), which take the real service below only when its approval covers
direction exploration and run the local simulation otherwise, stores data in its own SQLite file and attachment
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
from app.discovery import batch_live, model_live, updates, watches
from app.discovery.web import DEMO_VARIABLE, DiscoveryRuntime, SourceSet, batch_source_set, install_discovery, sources_from
from app.discovery import records_web
from app.directions.web import DirectionsRuntime, check_live_terms, install_directions
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
    discovery_sources: SourceSet | None = None,
    job_update_seconds: float | None = None,
    batch: "batch_live.Batch | None" = None,
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

    batch = batch or (discovery_sources.batch if discovery_sources is not None else None)

    try:
        return _create_app(config, clock, simulation_delay, job_sweep_seconds, environ, live, discovery_sources, job_update_seconds, batch)
    except BaseException:
        if batch is not None:
            batch.close()

        raise


def _create_app(config, clock, simulation_delay, job_sweep_seconds, environ, live, discovery_sources, job_update_seconds, batch) -> FastAPI:
    environ = os.environ if environ is None else environ

    try:
        launch = batch_live.launch_of(environ)
    except ValueError as problem:
        raise ConfigError(str(problem)) from None

    if launch is not None or batch is not None:
        # The D6 batch's two launches (D6 plan, 1.2): never with another set or validation B, never without its approvals.
        if (environ.get(DEMO_VARIABLE) or "").strip() or model_live.chosen(environ):
            raise ConfigError(f"D6 的启动（{batch_live.LAUNCH_VARIABLE}）不能和 {DEMO_VARIABLE} 或验证 B 一起用")

        if batch is None:
            try:
                batch = batch_live.load(environ, launch, live.terms if live is not None else None)
            except batch_live.BatchTermsError as problem:
                raise ConfigError(str(problem)) from None

        if batch.launch is batch_live.Launch.DISCOVERY and discovery_sources is None:
            discovery_sources = batch_source_set(batch, live.terms)

    if discovery_sources is None:
        # The search pages' sources (U3-D2-3, D2-E1): the default, the Lever-shaped replay a local preview names, or
        # the real source under its approvals; validation B (U3-D2-E3) also takes the real service's approval. Any
        # other value, and anything missing or not valid for them, stops the app here, before anything is created.
        try:
            discovery_sources = sources_from(environ, approval=live.terms if live is not None else None)
        except ValueError as problem:
            raise ConfigError(str(problem)) from None

    if discovery_sources.model is not None:
        # Validation B needs the very service its model approval was read for, or the app does not start.
        try:
            if live is None:
                raise ValueError("验证 B 要用真实模型，但应用没有真实服务")

            model_live.check_service(discovery_sources.model, live.terms)
        except ValueError as problem:
            raise ConfigError(str(problem)) from None

    if live is not None:
        # Before anything is created or opened: an approval for direction exploration names a service its processing
        # notice covers, or the app does not start (never quietly simulating).
        check_live_terms(live.terms)

    db_path = check_storage_path(config.db_path, test_root=config.test_root)

    if batch is not None and db_path.resolve().parent != batch.run_dir:
        # The gate's ledger and stop file are beside the library, the batch's records in the run directory: one place,
        # so one LIVE_STOP stops the model's requests and the source's reads alike, and both records go together.
        raise ConfigError("D6 的经历库要放在运行目录里：这样一个 LIVE_STOP 能同时停住来源读取和模型请求，两边的记录也在一起")

    if discovery_sources.model is not None and db_path.resolve().parent != discovery_sources.model.source.run_dir:
        # The gate's stop file is beside the library (app.live_gate), the source's in the run directory: one place, so
        # one LIVE_STOP stops the model's requests and the source's reads alike.
        raise ConfigError("验证 B 的经历库要放在运行目录里：这样运行目录里的一个 LIVE_STOP 能同时停住来源读取和模型请求")

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

    if batch is not None:
        # The model's approval as its gate holds it: paused (a request left out by a crash, its usage unknown), the
        # batch stops for good and the app does not start. So it does when this library is not the one the batch's
        # claims name, or its ledger shows less than the batch noted in HOME (D6-0 R1, F1: a library swapped or put
        # back from a copy made before).
        try:
            if service is None:
                raise batch_live.BatchTermsError(["D6 的启动要用真实模型，但应用没有真实服务"])

            batch.attach_gate(service.gate, read_library_id(engine))
        except batch_live.BatchTermsError as problem:
            if service is not None:
                service.close()

            engine.dispose()
            raise ConfigError(str(problem)) from None

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
    # One real service, one gate: structuring (U2-C3) and the analysis share it. Validation B's approval is for
    # discovery alone (U3-D2-E3): then the analysis pages keep the simulation, and the gate is closed here.
    app.state.live_service = service
    app.state.batch = batch
    # The D6 batch's approvals are its own operations' alone: the other pages keep the simulation (D6 plan, 1.7).
    borrowed = discovery_sources.model is not None or batch is not None
    analyses = None if borrowed else service
    install_structuring(app, live=None if batch is not None else service)
    install_job_analysis(app, simulation_delay=simulation_delay, sweep_seconds=job_sweep_seconds, live=analyses)
    # The search pages take the service for validation B alone, which was chosen with both of its approvals; every
    # other set runs the simulation (app.discovery.web).
    install_discovery(
        app,
        simulation_delay=simulation_delay,
        sweep_seconds=job_sweep_seconds,
        sources=discovery_sources,
        live=service if discovery_sources.model is not None or discovery_sources.batch is not None else None,
        # The automatic check of the postings tracked (U3-D4b-1): off unless asked for, synthetic sources only.
        update_seconds=job_update_seconds,
    )
    # What the user keeps from a finished search, and the records kept (U3-D3a): only what they ask for, only from
    # sources the server lets keep (SourceSet.keep).
    app.include_router(records_web.router)
    # Direction exploration shares the same service, gate and ledger, and takes it only when the approval names the
    # direction purpose; otherwise it runs the local simulation.
    install_directions(
        app,
        simulation_delay=simulation_delay,
        sweep_seconds=job_sweep_seconds,
        live=service,
        batch=batch if batch is not None and batch.launch is batch_live.Launch.DIRECTION else None,
    )

    return app


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    runtime = app.state.library

    if runtime.config.purge_on_startup:
        # Catch up on anything that expired while the app was not running.
        await asyncio.to_thread(runtime.run_cleanup)

    # A run of checks the app stopped or failed in is said to be interrupted, never done (U3-D4b-1).
    await asyncio.to_thread(updates.close_unfinished, runtime.database)
    # So is a run of 每日查新 left open, and it wrote nothing (U3-D4c).
    await asyncio.to_thread(watches.close_unfinished, runtime.database)
    # Current recommendations whose time came while the app was not running go at once (U3-D4b-2); the sweep removes
    # the others as their time comes, and nothing shows or checks one from that moment on.
    await asyncio.to_thread(app.state.discovery.remove_lapsed)
    # New postings of 每日查新 whose time came while the app was not running go too; their ids stay seen (U3-D4c).
    await asyncio.to_thread(app.state.discovery.remove_expired_new)

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
    # The automatic check, when it is on: at once (catching up), then every update_seconds while the app runs.
    checks = asyncio.create_task(_update_loop(searches, runtime)) if searches.update_seconds else None

    try:
        yield
    finally:
        # A batch running stops before its next call, and says it was interrupted.
        searches.stopping.set()

        for running in (task, jobs.sweeper, searches.sweeper, explorations.sweeper, checks):
            if running is not None:
                running.cancel()

                with contextlib.suppress(asyncio.CancelledError):
                    await running

        # Real structuring runs, running explorations, searches (which use the gate for validation B) and running
        # analyses stop before their next request; then the gate closes once, after any request still out is
        # settled: by the analysis pages when they have it, else here.
        app.state.structuring.shutdown()
        explorations.shutdown()
        searches.shutdown()
        jobs.shutdown()

        if app.state.live_service is not None and jobs.live is None:
            app.state.live_service.close()

        if getattr(app.state, "batch", None) is not None:
            # The run directory's lock goes last, once nothing of the batch can send.
            app.state.batch.close()

        runtime.database.engine.dispose()


async def _cleanup_loop(runtime: LibraryRuntime) -> None:
    """Run clean-up at a fixed interval for as long as the app runs."""
    while True:
        await asyncio.sleep(runtime.config.purge_interval_seconds)

        try:
            await asyncio.to_thread(runtime.run_cleanup)
        except Exception:  # keep the loop alive; the next pass retries
            logger.exception("unexpected error in the clean-up loop")


async def _update_loop(searches: DiscoveryRuntime, runtime) -> None:
    """The automatic check of the postings tracked (U3-D4b-1): due ones now, then every update_seconds. Nothing is
    checked while the app is not running."""
    while True:
        try:
            await asyncio.to_thread(searches.run_due_updates, runtime.database, runtime.now)
        except Exception as error:
            logger.error("unexpected error in the automatic check (%s)", type(error).__name__)

        await asyncio.sleep(searches.update_seconds)


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
    parser.add_argument(
        "--job-updates-every",
        type=float,
        default=0.0,
        help="仅用于本地验证：每隔多少分钟自动看一次已保存的岗位是否到期（只用合成来源；默认不开启）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        check_bind_host(args.host)

        if args.simulation_delay and not args.dev_login:
            raise ConfigError("模拟等待只用于本地开发验证，必须同时启用开发登录。")

        if not 0 <= args.simulation_delay <= 60:
            raise ConfigError("模拟等待要在 0 到 60 秒之间。")

        if args.job_updates_every and (args.job_updates_every < 0 or not args.dev_login):
            raise ConfigError("自动检查只用于本地开发验证：要启用开发登录，间隔要大于 0。")

        config = load_config_from_env(
            db_path=args.db,
            files_dir=args.files,
            dev_login=True if args.dev_login else None,
            dev_clock_offset=timedelta(days=args.dev_clock_offset_days),
        )
        app = create_app(
            config,
            simulation_delay=args.simulation_delay,
            job_update_seconds=args.job_updates_every * 60 if args.job_updates_every else None,
        )
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
