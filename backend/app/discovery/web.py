"""Pages of 岗位机会 → 找岗位 (U3-A3): a preview of the search agent on synthetic postings.

    /discover                           what to look for: job titles, where, and which experiences
    /discover/review              POST  check it and keep it as a search to confirm
    /discover/tasks/<id>                the review, the running search, or what it found
    /discover/tasks/<id>/confirm  POST  update what each analysis sends, or confirm and start
    /discover/tasks/<id>/cancel   POST
    /discover/tasks/<id>/status         the running search's steps, for the page's script

Only a preview runs here (U3-A3 pre-check, 6.2). The postings come from the frozen synthetic sources
(app.discovery.synthetic), each decision from SimulatedDecider (fixed local rules, not AI) and each analysis from the
app's simulation (app.job_analysis.simulation). DiscoveryRuntime binds the three itself: it never asks the job
analysis pages which service they use, so a real service the app may have for those is never reached from here, and
nothing a page sends (a form, a URL parameter) nor the environment can switch a search to one. The sources, the
recipient and the notice are the server's, and every confirmation's fingerprint covers them.

The sources may be swapped for another set held in this process (SourceSet, U3-D2-3): tests pass one in, and a
local preview may name the Lever-shaped replay (app.discovery.lever_demo) by DEMO_VARIABLE. Every set there is is
synthetic: none reaches a network, none is a real source, and a value the app does not know stops it from
starting rather than run anything else. The pages say which set a search used, as an offline demo. The real source
(app.discovery.lever_live, D2-E1) is read under its approvals only, its decisions and analyses still simulated.

Validation B (U3-D2-E3, app.discovery.model_live) is the one exception to the simulation, and only when chosen
explicitly with the real source: then each decision is a model's (ModelDecider, its own limits) and each analysis a
real service's, both through the app's one service and gate, entered once for the whole search; the review names the
three recipients, the notice (model_live.NOTICE_VERSION) and both approvals, and the search binds both approvals to
itself before anything is sent. A real service the app has for other pages is never taken otherwise, and the app
does not start for validation B without all of it.

Every page and post is the signed-in user's, and every post carries the session's CSRF token and comes from this site
(app.job_analysis.web.checked_fields, app.library.web). A GET starts nothing, and checking what was entered
(tools.scope_check) starts nothing either: no task, no service, no write. A search is the user's and the session's: an
id alone opens nothing. Only confirming a reviewed search starts it, once, with the fingerprint of the very
confirmation reviewed (DiscoveryTaskStore.begin), and start() checks it all again before the first step. Nothing a
search finds is kept: tasks, their steps and results live in memory for TASK_TTL_SECONDS and are gone with a restart,
nothing is saved to the library on its own and nothing is applied for. The user may keep a posting or a search's
summary themselves (U3-D3a, app.discovery.records_web), and only from sources the server lets keep (SourceSet.keep).

Kept directions (U3-C3b) come in by decision id and revision only (app.directions.handoff), from the records page or a
draft: their names are the library's, read again on the input page, on the review and right before a start, and each
goes into the scope whole. A change in between voids the confirmation before anything else, so an older form can
never start a search, and nothing here writes a decision or the preferences.
"""
import logging
import os
import threading
import time
import unicodedata
from functools import partial
from types import SimpleNamespace
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import FormData

from app.analysis_contracts import RequirementGroup, SelectedPreference, SelectedSource
from app.directions.handoff import check_kept, kept_for_search
from app.discovery import batch_live, lever_demo, lever_live, model_live, presentation, records, rules, supplied, synthetic, updates, watches
from app.discovery.agent import MAX_DECISIONS, ModelDecider, SimulatedDecider
from app.discovery.contracts import (
    CATEGORY_LABELS as RESULT_CATEGORIES,
    DIMENSION_LABELS,
    NOT_PERSONALISED,
    Category,
    Dimension,
    DimensionScope,
    Limits,
    Others,
    ParentConfirmation,
    PostingRef,
    SearchScope,
    SourceGrant,
)
from app.discovery.contracts import Access
from app.discovery.batch_live import BatchRefused, OperationKind
from app.discovery.runner import _Stops, live_provider_for, run_search, tracing_variables
from app.discovery.sources import Register
from app.discovery.tasks import TASK_TTL_SECONDS, DiscoveryTaskStore, Stage, Start, TooManyTasks
from app.discovery.tools import scope_check
from app.experience_analysis import PREFERENCE_TOPICS
from app.job_analysis import presentation as analysis_text
from app.job_analysis.scope import (
    BLOCKED,
    LEFT_OUT,
    MAX_EXPERIENCES,
    MAX_OUTBOUND_CHARS,
    MAX_POSTING_CHARS,
    PREFERENCES_KEY,
    SEND,
    InputProblem,
    PreferencePlan,
    SourcePlan,
    preference_plans,
    source_plan,
)
from app.job_analysis.service import LiveService, local_provider
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.job_analysis.tasks import SWEEP_SECONDS
from app.job_analysis.web import checked_fields
from app.library.content import CATEGORY_LABELS
from app.library import job_watches
from app.library.errors import ConfigError, NotFound
from app.library.identity import CurrentUser
from app.library.job_choices import decision_label
from app.library.job_recommendations import KEEP_DAYS
from app.library.projection import current_preferences, project_confirmed_library, read_selected_confirmations
from app.library.references import read_record_refs
from app.library.service import list_experiences
from app.library.presentation import format_short
from app.library.web import current_user, message_page, render, runtime, safe_next, see_other

BASE = "/discover"
# What every search here is confirmed for, decided by the server: its runtime's sources (SourceSet), the simulation as
# the recipient of each analysis, this notice and the set's limits; validation B has its own recipient and notice
# (model_live). Changing a word of the review changes the version (2: the offline demo, its sources named, U3-D2-3;
# 3: the review says what each set reads, D2-E1; 4: what is not done is each set's own, the real source's never
# "offline", U3-D2-E3; 5: a set that lets keep says nothing is kept by itself and what the user may keep, and for how
# long, U3-D3b; 6: how many postings the user skipped are not listed again, U3-D3c; 7: what is held for tracking when
# the user ticks it, for how long, and that it is not 我的岗位, U3-D4b-2). A review made under another version than the
# pages run now is never confirmed (confirm).
NOTICE_VERSION = "discovery-preview-7"
LIMITS = Limits()
# Free text a page takes: the titles looked for and the values added to a dimension; each value is at most what one
# search's words may be.
TEXT_CHARS = 200
VALUE_CHARS = LIMITS.keyword_chars
NOTICES = {
    "already": "这次确认已经开始过，没有重复执行。",
    "scope_updated": "已按你的选择更新核对内容，请再看一遍后确认。",
    "scope_changed": "你改动了发送范围，所以还没有开始。下面已按新的范围更新，请再看一遍后确认。",
    "kept_changed": "你选择的保留方向在核对之后有变化，原来的确认已经作废，没有开始。下面已按现在的情况重新核对，请再看一遍后确认。",
    # The postings the user skipped changed after the review (U3-D3c).
    "skips_changed": "你跳过的岗位在核对之后有变化（新跳过、撤销、改了决定、删除或恢复），原来的确认已经作废，没有开始。下面已按现在的情况重新核对，请再看一遍后确认。",
    # The review was made under other sources, another recipient, notice or limits than the pages run now (U3-D3b).
    "service_changed": "来源、接收方或处理说明在核对之后有了变化，原来的确认已经作废，没有开始。下面已按现在的设置重新生成核对内容，请再看一遍后确认。",
    "batch_changed": "这一批的剩余次数或预算在核对之后有了变化，原来的确认已经作废，没有开始。下面已按现在的剩余重新生成核对内容，请再看一遍后确认。",
    # What the user kept from this search (U3-D3a, app.discovery.records_web).
    "kept": "已加入我的岗位。",
    "decided": "已记下你的决定，这个岗位已加入我的岗位。",
    "feedback": "已记下你指出的不准，这个岗位已加入我的岗位。",
    "summary": "这次查找的摘要已保存到岗位记录。",
    "analysis_kept": "这个岗位的分析已保存到岗位记录。",
}

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass(frozen=True)
class SourceSet:
    """Where the postings of these pages' searches come from (U3-D2-3, D2-E1), and how the pages say so.

    key is how the app is started for it; grants are what each search is confirmed for, with these limits;
    make_register gives the sources of one confirmed search, given that search (its id and cancel signal). mode is
    synthetic, replay or real; the texts are what the pages show of it, a synthetic set's postings called synthetic
    samples, the real source's never. Every set keeps the decisions and analyses of these pages simulated: none of
    them reaches a model, and the requests line says so (requests_note).
    """

    key: str
    label: str
    grants: tuple[SourceGrant, ...]
    make_register: Callable[[object], Register]
    mode: str = "synthetic"
    limits: Limits = LIMITS
    banner: str = presentation.PREVIEW
    source_note: str = "都是本机的合成数据（离线演示），不联网，不打开任何链接。"
    source_tag: str = "本机合成数据，不联网"
    link_note: str = "（合成示例地址，不能访问）"
    checked_note: str = "合成来源在本机回答，不是真实核验"
    evidence_note: str = "的合成样例"
    requests_note: str = presentation.SIMULATED_REQUESTS
    # What the review says of the decisions and the analyses, and of what is not done (U3-D2-E3).
    decision_note: str = "本机模拟决策：RoleRadar 内置的固定规则，不是 AI。"
    analysis_note: str = "本机模拟服务：固定规则，不是 AI。在这台电脑上运行，不联网，不保存请求。"
    # What is not done, as the review lists it; the review adds whether results are kept (U3-D3b): never by themselves,
    # and only by the user where the set lets keep.
    kept_note: str = "不联网，不打开岗位里的链接，不投递"
    # The real source's terms, and the day as its checks read it; None for the sets of this process.
    terms: "lever_live.SourceTerms | None" = None
    today: Callable[[], object] = lever_live.today_here
    # Where a search notes what came of each analysis it tried, given the search (U3-D2-E3): the run directory's
    # record for the real source, nowhere for the others.
    diagnose_for: "Callable[[object], Callable[[dict], None]] | None" = None
    # Validation B (U3-D2-E3): the model approval bound with the read approval; None for every other set.
    model: "model_live.ModelTerms | None" = None
    # The sources of this set whose results the user may keep (U3-D3a), by id; none unless a set says so. The server's
    # registration, never a form's: app.discovery.records also keeps synthetic sources only, whatever a set lists.
    keep: frozenset[str] = frozenset()
    # Those whose analyses the user may keep too (U3-D3b), never wider than keep; the same holds for them.
    keep_analyses: frozenset[str] = frozenset()
    # Those whose 404, with a complete unfiltered list without the posting read in the same check, may show a kept
    # posting closed (U3-D4a, app.discovery.updates). None in any set the app runs on: Lever's 404 is not confirmed.
    delisting: frozenset[str] = frozenset()
    # The D6 batch (app.discovery.batch_live): the admitted board, read and modelled under approvals shared by every
    # operation of one run day; None for every other set.
    batch: "batch_live.Batch | None" = None


SYNTHETIC_SOURCES = SourceSet(
    "synthetic",
    "本机冻结的合成数据",
    synthetic.GRANTS,
    lambda task: synthetic.register(),
    keep=frozenset(grant.source_id for grant in synthetic.GRANTS),
    keep_analyses=frozenset(grant.source_id for grant in synthetic.GRANTS),
)
LEVER_REPLAY = SourceSet(
    lever_demo.KEY,
    lever_demo.LABEL,
    lever_demo.GRANTS,
    lambda task: lever_demo.register(),
    mode="replay",
    keep=frozenset(grant.source_id for grant in lever_demo.GRANTS),
    keep_analyses=frozenset(grant.source_id for grant in lever_demo.GRANTS),
)
# How the app is started for another set than the default: unset or empty is the default.
DEMO_VARIABLE = "ROLERADAR_DISCOVERY_DEMO"


def live_source_set(terms: lever_live.SourceTerms, **inject) -> SourceSet:
    """Palantir's Lever board as these pages' source (D2-E1), under the admission and read approval given: read only
    once a search is confirmed, within its approval, with decisions and analyses still simulated. inject is for tests:
    the transport, the day and the guard's clock (lever_live.register)."""
    return SourceSet(
        lever_live.KEY,
        lever_live.NAME,
        (lever_live.entry(terms).grant,),
        lambda task: lever_live.register(terms, task, **inject),
        mode="real",
        limits=lever_live.LIMITS,
        banner=(
            "真实来源受控只读：岗位来自 Palantir 的 Lever 公开职位接口；决策和分析仍由本机固定规则模拟，不是 AI，"
            "也不发给任何模型；核对时间是实际请求的时间，“核对时仍公开发布”不代表企业还有名额。"
        ),
        source_note=(
            f"只读 Palantir 的 Lever 公开职位接口：读取批准 {terms.approval_id}，只在 {terms.run_date}（美东）有效，"
            f"最多 {terms.max_requests} 次请求，确认后开始计时 {int(terms.window_seconds)} 秒；"
            f"来源准入 {terms.admission_id} 到 {terms.admission_until} 为止。决策和分析由本机模拟，什么都不发给模型；"
            "页面不提供可点击的链接。确认之前什么都不读取。"
        ),
        source_tag="真实来源，受控只读",
        link_note="（真实岗位地址，页面不提供链接）",
        checked_note="来源在这个时间回答：核对时仍公开发布，不代表企业还有名额",
        evidence_note="",
        requests_note="（决策和分析都是本机模拟，没有发给任何模型，不产生模型费用；岗位来自真实来源的受控读取）",
        kept_note="除了上面写明的来源读取，不访问别的网站；不打开岗位里的链接，不投递，不保存结果",
        terms=terms,
        today=inject.get("today", lever_live.today_here),
        diagnose_for=lambda task: lever_live.analysis_record(terms, task.id),
    )


def live_model_source_set(terms: lever_live.SourceTerms, model: "model_live.ModelTerms", approval, **inject) -> SourceSet:
    """Validation B (U3-D2-E3): the real source as live_source_set reads it, each decision and analysis a real
    service's under the model approval given (approval: its gate's terms, which name the service). inject as there."""
    service = analysis_text.PROVIDER_NAMES.get(approval.provider, approval.provider)
    named = f"{service} 的 {approval.model}"

    return replace(
        live_source_set(terms, **inject),
        model=model,
        banner=(
            "验证 B：岗位来自 Palantir 的 Lever 公开职位接口的受控只读；每一步的决策和每个岗位的分析由真实模型"
            f"（{named}）给出，确认之后才发送；核对时间是实际请求的时间，“核对时仍公开发布”不代表企业还有名额。"
        ),
        source_note=(
            f"只读 Palantir 的 Lever 公开职位接口：读取批准 {terms.approval_id}，只在 {terms.run_date}（美东）有效，"
            f"最多 {terms.max_requests} 次请求，确认后开始计时 {int(terms.window_seconds)} 秒；"
            f"来源准入 {terms.admission_id} 到 {terms.admission_until} 为止。页面不提供可点击的链接。确认之前什么都不读取。"
        ),
        decision_note=(
            f"{named}（模型批准 {model.approval_id}，只在 {model.run_date}（美东）有效）：每一步发送检索条件、来源名称、"
            "剩余次数、岗位摘要行和截短、遮盖过的原文摘录，由它选下一个动作；"
            f"最多 {MAX_DECISIONS} 次。不发送经历、偏好原文、私有备注和岗位链接。"
        ),
        analysis_note=(
            f"{named}（同一份模型批准）：每分析一个岗位，发送下面勾选的栏目、勾选的偏好和这一个岗位处理过的英文原文"
            f"（不超过 {MAX_POSTING_CHARS:,} 字；邮箱、电话、证件号和英文地址行遮盖，有保密字样、中文地址或跨行地址的整份不发），"
            "通常两个请求：先解析岗位，再对照经历。"
        ),
        requests_note="（决策和分析都交给真实模型；岗位来自真实来源的受控读取）",
        kept_note="除了上面写明的来源读取和模型请求，不访问别的网站；不打开岗位里的链接，不投递，不保存结果",
    )


def batch_source_set(batch: "batch_live.Batch", approval) -> SourceSet:
    """The D6 batch's board as these pages' source (D6 plan, 1.1): read only within an operation the user began,
    under the batch's approvals; each decision and analysis a real service's, under the batch's model approval
    (approval: its gate's terms). What may be kept is the admission's save permission alone (batch_live.keep_of)."""
    terms = batch.source
    keep, keep_analyses = batch_live.keep_of(terms)
    service = analysis_text.PROVIDER_NAMES.get(approval.provider, approval.provider)
    named = f"{service} 的 {approval.model}"
    saved = batch_live.SAVE_TEXT[terms.save]

    return SourceSet(
        batch_live.KEY,
        terms.board.name,
        (batch_live.entry(terms).grant,),
        lambda task: batch.register(),
        mode="real",
        limits=batch_live.LIMITS,
        banner=(
            f"D6 真实验收：岗位来自 {terms.board.company} 的 Lever 公开职位接口的受控只读；每一步的决策和每个岗位的分析由真实模型"
            f"（{named}）给出，确认之后才发送；核对时间是实际请求的时间，“核对时仍公开发布”不代表企业还有名额。"
        ),
        source_note=(
            f"只读 {terms.board.company} 的 Lever 公开职位接口：读取批准 {terms.approval_id}，只在 {terms.run_date}（美东）有效，"
            f"这一批最多 {terms.max_requests} 次请求，由这一天的各项操作共用；来源准入 {terms.admission_id} 到 {terms.admission_until} 为止，"
            f"保存许可：{saved}。页面不提供可点击的链接。确认之前什么都不读取。"
        ),
        source_tag="真实来源，受控只读",
        link_note="（真实岗位地址，页面不提供链接）",
        checked_note="来源在这个时间回答：核对时仍公开发布，不代表企业还有名额",
        evidence_note="",
        decision_note=(
            f"{named}（模型批准 {batch.model.approval_id}，只在 {batch.model.run_date}（美东）有效）：每一步发送检索条件、来源名称、"
            "剩余次数、岗位摘要行和截短、遮盖过的原文摘录，由它选下一个动作；"
            f"最多 {MAX_DECISIONS} 次。不发送经历、偏好原文、私有备注和岗位链接。"
        ),
        analysis_note=(
            f"{named}（同一份模型批准）：每分析一个岗位，发送下面勾选的栏目、勾选的偏好和这一个岗位处理过的英文原文"
            f"（不超过 {MAX_POSTING_CHARS:,} 字；邮箱、电话、证件号和英文地址行遮盖，有保密字样、中文地址或跨行地址的整份不发），"
            f"通常两个请求：先解析岗位，再对照经历。这一批每次查找最多完整分析 {batch_live.LIMITS.analyses} 个岗位。"
        ),
        requests_note="（决策和分析都交给真实模型；岗位来自真实来源的受控读取）",
        kept_note=(
            "除了上面写明的来源读取和模型请求，不访问别的网站；不打开岗位里的链接，不投递；"
            + ("不保存结果" if not keep else f"结果只在你点保存以后保存（{saved}），不存岗位全文")
        ),
        today=batch.today,
        diagnose_for=lambda task: batch_live.analysis_record(batch, task.id),
        keep=keep,
        keep_analyses=keep_analyses,
        batch=batch,
    )


def sources_from(environ: Mapping[str, str], *, today=None, approval=None) -> SourceSet:
    """The set the app is started for, or the default; any other value, and the real source without its admission and
    read approval as they must be (lever_live.load_terms), stops the app from starting (ValueError): it never falls
    back to another set. Validation B (model_live.VARIABLE) takes the real source and approval, the gate's terms of the
    app's real service, as model_live.load_terms checks them; without all of it the app does not start either."""
    value = (environ.get(DEMO_VARIABLE) or "").strip()

    if model_live.chosen(environ):
        if value != lever_live.KEY:
            raise ValueError(f"验证 B（{model_live.VARIABLE}={model_live.LIVE}）只能和真实来源一起用：{DEMO_VARIABLE} 要设为 {lever_live.KEY}")

        terms = lever_live.load_terms(environ, today=today)

        return live_model_source_set(terms, model_live.load_terms(approval, terms, today=today), approval)

    if not value:
        return SYNTHETIC_SOURCES

    if value == LEVER_REPLAY.key:
        return LEVER_REPLAY

    if value == lever_live.KEY:
        return live_source_set(lever_live.load_terms(environ, today=today))

    raise ValueError(
        f"{DEMO_VARIABLE} 只能不设，或者设为 {LEVER_REPLAY.key}（离线合成回放）或 {lever_live.KEY}（真实来源，需要批准）；"
        "其他值不启用任何来源，网页没有启动。"
    )


@dataclass
class DiscoveryRuntime:
    """The searches of this app, and the one way they run: the set of sources the app was started for, simulated
    decisions and analyses; for validation B alone (sources.model), the app's real service (live), whose gate a search
    enters once for its decisions and analyses (U3-D2-E3)."""

    store: DiscoveryTaskStore
    executor: ThreadPoolExecutor
    # Only for trying the pages locally (web_main --simulation-delay): each simulated analysis request waits this long.
    simulation_delay: float = 0.0
    sweep_seconds: float = SWEEP_SECONDS
    # The lifespan's sweeping task, while the app runs.
    sweeper: object = None
    # Where the tracing variables are looked for; None is the process environment.
    environ: Mapping[str, str] | None = None
    # Where its postings come from (U3-D2-3, D2-E1): synthetic data in this process unless started for another set.
    sources: "SourceSet" = None
    # The app's real service, for validation B only (install_discovery); never taken by any other set.
    live: LiveService | None = None
    # The texts 申请前核对 read, in memory only, and the records being checked now (U3-D4a, app.discovery.updates).
    apply_checks: updates.ApplyChecks = field(default_factory=updates.ApplyChecks)
    checking: updates.Checking = field(default_factory=updates.Checking)
    # What users read from links and pasted (U3-D4d), in memory only, until they add it to 我的岗位 or it goes.
    supply: supplied.Supplied = field(default_factory=supplied.Supplied)
    # Batches of checks of the postings tracked (U3-D4b-1): one batch of a user at a time, held from when it is queued
    # until it ends (F1), the app's stop signal, how often the automatic check looks (None: off, the default) and the
    # clock a batch's seconds are counted on.
    batches: updates.Slots = field(default_factory=updates.Slots)
    stopping: threading.Event = field(default_factory=threading.Event)
    update_seconds: float | None = None
    monotonic: Callable[[], float] = time.monotonic
    # The library's database and clock (install_discovery), for current recommendations held for tracking (U3-D4b-2):
    # held when a ticked search ends (retain), removed when their time has come (sweep); the texts their 申请前核对 read
    # go by the same clock (apply_checks, F1).
    database: object = None
    now: Callable[[], object] | None = None

    def __post_init__(self):
        if self.sources is None:
            self.sources = SYNTHETIC_SOURCES

        if self.sources.batch is not None:
            if self.live is None:
                raise ValueError("D6 的找岗启动要用应用的真实服务，但应用没有真实服务")
        elif self.sources.model is None:
            self.live = None
        elif self.live is None:
            raise ValueError("验证 B 要用应用的真实服务，但应用没有真实服务")
        else:
            model_live.check_service(self.sources.model, self.live.gate.terms)

        if self.now is not None:
            # A current recommendation's text goes at its time on the app's own clock, read where the text is
            # published and read (U3-D4b-2 F1).
            self.apply_checks.now = self.now

    @property
    def model(self) -> "model_live.ModelTerms | None":
        """Validation B's model approval, or None: every other set runs the simulation (or is the D6 batch's)."""
        return self.sources.model

    @property
    def batch(self) -> "batch_live.Batch | None":
        """The D6 batch, or None (D6 plan, 3)."""
        return self.sources.batch

    @property
    def uses_model(self) -> bool:
        """Whether the searches' decisions and analyses are a real service's: validation B, or the D6 batch."""
        return self.model is not None or self.batch is not None

    @property
    def recipient(self):
        """Who receives each analysis, as every confirmation names it: the real service for validation B or D6 alone."""
        return self.live.recipient if self.uses_model else RECIPIENT

    @property
    def notice_version(self) -> str:
        if self.batch is not None:
            return batch_live.NOTICE_VERSION

        return model_live.NOTICE_VERSION if self.model is not None else NOTICE_VERSION

    @property
    def notice(self) -> tuple[str, ...]:
        if self.batch is not None:
            return batch_live.NOTICE

        return model_live.NOTICE if self.model is not None else ()

    def batch_state(self) -> str:
        """The D6 batch as it stands now, for a confirmation; empty for every other set."""
        return "" if self.batch is None else batch_live.state_of(self.batch)

    def make_provider(self, task) -> SimulatedProvider:
        # The simulation waits on the task's cancel signal, so stopping the search ends its wait.
        return SimulatedProvider(self.simulation_delay, stop=task.cancel)

    def provider_for(self, task, sending):
        """The provider of the whole search (runner.run_search): the simulation's, or for validation B the real
        service's behind its gate, entered once for the search (runner.live_provider_for)."""
        if self.uses_model:
            return live_provider_for(self.live)(task, sending)

        return local_provider(self.make_provider)(task, sending)

    def register(self, task):
        """Its set's sources for one confirmed search, new for each."""
        return self.sources.make_register(task)

    def decider_for(self, provider):
        """The simulated decider, or for validation B a model's through the search's provider, with its own limits."""
        if self.batch is not None:
            return ModelDecider(
                provider,
                output_tokens=batch_live.DECISION_OUTPUT_TOKENS,
                max_request_bytes=batch_live.DECISION_REQUEST_BYTES,
            )

        if self.model is not None:
            return ModelDecider(
                provider,
                output_tokens=model_live.DECISION_OUTPUT_TOKENS,
                max_request_bytes=model_live.DECISION_REQUEST_BYTES,
            )

        return SimulatedDecider()

    def binding_for(self, task):
        """Validation B's approvals, held for one confirmed search (model_live.Binding); the D6 batch's, for the
        operation begun when the search was confirmed (batch_live.Binding); None for every other set."""
        if self.batch is not None:
            return batch_live.Binding(self.batch, self.batch.operation(task.id))

        if self.model is None:
            return None

        return model_live.Binding(self.model, task.id, getattr(task, "cancel", None), today=self.sources.today)

    def trajectory_for(self, task):
        """Where one search of the D6 batch keeps its compact trajectory (batch_live.Trajectory); None for every other set."""
        return None if self.batch is None else batch_live.Trajectory(self.batch, task.id)

    def diagnose_for(self, task):
        """Where one search notes what came of its analyses, metadata only; None for the sets of this process."""
        return None if self.sources.diagnose_for is None else self.sources.diagnose_for(task)

    def refusal(self) -> str | None:
        """Why a search of validation B may not start now, in words, if it may not; None for every other set. The
        gate's own (its stop file, a pause, no request left), then the approvals' (used, the day, LIVE_STOP)."""
        if not self.uses_model:
            return None

        blocked = self.live.refusal()

        if blocked is not None:
            return f"模型服务现在不能开始：{presentation.gate_text(blocked.value)}"

        if self.batch is not None:
            problem = self.batch.problem()

            return None if problem is None else problem[1]

        return model_live.start_problem(self.model, self.sources.today())

    def start_problem(self, owner_id: str) -> str | None:
        """Why the user's search may not start now, in words, if it may not: refusal()'s, then for the D6 batch what
        one search would reserve of what is left (batch_live.Batch.availability), said without writing, claiming or
        sending anything (D6-1 R3, L3). The review shows it in place of its start button; a confirmation asks it again,
        and the batch's begin() once more, under its lock, before anything is taken."""
        refusal = self.refusal()

        if refusal is not None or self.batch is None:
            return refusal

        short = self.batch.availability(OperationKind.SEARCH, owner_id)

        return None if short is None else short[1]

    def refresh_limits(self) -> "updates.BatchLimits":
        """The limits a 现在核对 or an automatic check runs under, as it runs: the D6 batch checks at most three
        postings, under its own requests and time for the operation (D6 plan, 8.2); every other set the defaults. The
        page says these, never others (D6-1 R3, L2)."""
        if self.batch is None:
            return updates.BatchLimits()

        return updates.BatchLimits(
            postings=batch_live.RECHECK_POSTINGS,
            calls=batch_live.OPERATION_REQUESTS[OperationKind.RECHECK],
            seconds=batch_live.OPERATION_SECONDS[OperationKind.RECHECK],
        )

    def service_lines(self) -> list[tuple[str, str]]:
        """What the review says of validation B's or the D6 batch's model approval as it stands, and of the batch's read
        approval; nothing for the other sets."""
        if not self.uses_model:
            return []

        status = self.live.gate.status()
        terms = self.live.gate.terms

        def usd(micro: int) -> str:
            return f"{micro / 1_000_000:.2f}"

        lines = [
            ("模型批准", f"{terms.approval_id}：最多 {terms.max_requests} 次请求，还剩 {status.requests_left} 次；"
             f"应用内预算 ${usd(terms.max_cost_micro_usd)}，还剩 ${usd(status.micro_usd_left)}（不是服务商账单的上限）"),
        ]

        if status.paused_reason:
            lines.append(("已暂停", analysis_text.PAUSE_REASONS.get(status.paused_reason, status.paused_reason)))

        if self.batch is not None and self.batch.source is not None:
            batch, need = self.batch, self.batch.model_need(OperationKind.SEARCH)
            lines.append(
                (
                    "读取批准",
                    f"{batch.source.approval_id}：这一批最多 {batch.source.max_requests} 次请求，已用 {batch.sent} 次；"
                    f"一次查找最多用 {batch_live.OPERATION_REQUESTS[OperationKind.SEARCH]} 次"
                    + ("" if batch.robots_read else "，这次启动的第一项读取操作另加 1 次 robots"),
                )
            )
            lines.append(("这次查找的上限", f"模型最多 {need[0]} 次请求，最坏预留 ${usd(need[1])}；剩余不够就不开始"))

        return lines

    def tracing(self) -> tuple[str, ...]:
        """The names of the variables that would keep a search from starting (runner.tracing_variables)."""
        return tracing_variables(os.environ if self.environ is None else self.environ)

    def end_session(self, session: str) -> None:
        self.store.end_session(session)
        self.apply_checks.end_session(session)
        self.supply.end_session(session)

    def sweep(self) -> None:
        self.store.sweep()
        self.apply_checks.sweep()
        self.supply.sweep()
        self.remove_lapsed()
        self.remove_expired_new()

    def remove_expired_new(self) -> int:
        """Remove the new postings of 每日查新 whose time has come (U3-D4c); their ids stay seen. Every page, keep and
        export judges the time itself, so nothing waits on this."""
        if self.database is None:
            return 0

        with self.database.transaction() as db:
            return job_watches.remove_expired(db, self.now())

    def watch_register(self):
        """The set's sources as a watch is checked and run on (U3-D4c), new for each."""
        return self.register(SimpleNamespace(id="watch", cancel=None))

    def supply_register(self):
        """The set's sources as a link the user gives is read on (U3-D4d), new for each read."""
        return self.register(SimpleNamespace(id="supply", cancel=None))

    def begin_operation(self, kind: "OperationKind", owner_id: str, op_id: str, cancel=None):
        """Begin one of the D6 batch's operations for the user (batch_live.Batch.begin), or None for every other set.
        BatchRefused when it may not start: nothing is sent."""
        if self.batch is None:
            return None

        return self.batch.begin(kind, owner_id, op_id, cancel)

    def end_operation(self, operation, outcome: str = "done") -> None:
        if operation is not None and self.batch is not None:
            self.batch.end(operation, outcome)

    def start_watch(self, database, owner_id: str, watch_id: str, trigger: "watches.Trigger", now) -> str:
        """Queue one run of a watch (its baseline when started, or 现在查新) on the user's slot, as a batch of checks is
        (start_batch): started; busy, when a batch or a run of theirs is queued or runs already; or failed, when the
        queue took nothing. Held from now until it ends, and given back with its own ticket if it never runs."""
        ticket = self.batches.claim(owner_id)

        if ticket is None:
            return "busy"

        try:
            operation = self.begin_operation(OperationKind.WATCH, owner_id, f"watch-{watch_id}-{ticket}", self.stopping)
        except BatchRefused as refused:
            self.batches.release(owner_id, ticket)

            return f"batch:{refused}"

        try:
            queued = self.executor.submit(self._logged_watch, database, owner_id, watch_id, trigger, now, ticket)
        except Exception as error:
            self.batches.release(owner_id, ticket)
            self.end_operation(operation, "not_queued")
            logger.error("a run of 每日查新 could not be queued (%s)", type(error).__name__)

            return "failed"

        if queued is not None:
            queued.add_done_callback(lambda done: done.cancelled() and self.batches.release(owner_id, ticket))
            # The batch's operation ends whatever came of the run: run, failed or never run.
            queued.add_done_callback(lambda done: self.end_operation(operation))
        else:
            # Run at once (an inline executor): it has ended.
            self.end_operation(operation)

        return "started"

    def run_watch(self, database, owner_id: str, watch_id: str, trigger: "watches.Trigger", now) -> "watches.RunResult | None":
        """One run of a watch, run now on the user's slot, or None when a batch or a run of theirs holds it."""
        ticket = self.batches.claim(owner_id)

        if ticket is None:
            return None

        return self._watch(database, owner_id, watch_id, trigger, now, ticket)

    def _logged_watch(self, database, owner_id: str, watch_id: str, trigger, now, ticket: str):
        try:
            return self._watch(database, owner_id, watch_id, trigger, now, ticket)
        except Exception as error:
            logger.error("unexpected error in a run of 每日查新 (%s)", type(error).__name__)

    def _watch(self, database, owner_id: str, watch_id: str, trigger, now, ticket: str):
        try:
            # Its slot is still its own: the one it was queued with, never another's.
            if not self.batches.holds(owner_id, ticket):
                return None

            return watches.run(
                database,
                owner_id,
                watch_id,
                trigger,
                keep=self.sources.keep,
                register=self.watch_register,
                now=now,
                monotonic=self.monotonic,
                stop=self.stopping,
            )
        finally:
            self.batches.release(owner_id, ticket)

    def retain(self, task) -> None:
        """Hold a finished search's current recommendations (U3-D4b-2), once, as records.keep_recommendations judges; what
        memory holds of one removed meanwhile is dropped before its removal commits. What came of it is said on its page."""
        if self.database is None:
            return

        try:
            with self.database.transaction() as db:
                retention = records.keep_recommendations(db, task, self.sources.keep, self.now(), forget=self.apply_checks.forget_record)
        except Exception as error:  # one transaction: nothing of it was written
            logger.error("current recommendations were not held (%s)", type(error).__name__)
            retention = records.Retention(reason="write_failed")

        self.store.note_retention(task.id, retention)

    def remove_lapsed(self) -> int:
        """Remove the current recommendations whose time has come (U3-D4b-2), with what memory holds of each, before the
        removal commits; they were shown nowhere and checked never from the moment it came."""
        if self.database is None:
            return 0

        with self.database.transaction() as db:
            return records.remove_lapsed(db, self.now(), forget=self.apply_checks.forget_record)

    def start_batch(self, database, owner_id: str, trigger: "updates.Trigger", now) -> str:
        """Queue one batch of the user's tracked postings (现在核对): started; busy, when one of theirs is queued or runs
        already; or failed, when the queue took nothing. Its slot is held from now until it ends, and given back if it
        is never run: refused by the queue, or cancelled there (the app closing)."""
        ticket = self.batches.claim(owner_id)

        if ticket is None:
            return "busy"

        try:
            operation = self.begin_operation(OperationKind.RECHECK, owner_id, f"recheck-{ticket}", self.stopping)
        except BatchRefused as refused:
            self.batches.release(owner_id, ticket)

            return f"batch:{refused}"

        try:
            queued = self.executor.submit(self._logged_batch, database, owner_id, trigger, now, ticket)
        except Exception as error:
            self.batches.release(owner_id, ticket)
            self.end_operation(operation, "not_queued")
            logger.error("a batch of checks could not be queued (%s)", type(error).__name__)

            return "failed"

        if queued is not None:
            queued.add_done_callback(lambda done: done.cancelled() and self.batches.release(owner_id, ticket))
            queued.add_done_callback(lambda done: self.end_operation(operation))
        else:
            self.end_operation(operation)

        return "started"

    def run_batch(self, database, owner_id: str, trigger: "updates.Trigger", now) -> "updates.BatchResult | None":
        """One batch of the user's tracked postings, run now, or None when one of theirs is queued or runs already
        (nothing starts twice): the automatic check's way, and 现在核对's once queued."""
        ticket = self.batches.claim(owner_id)

        if ticket is None:
            return None

        return self._batch(database, owner_id, trigger, now, ticket)

    def _logged_batch(self, database, owner_id: str, trigger: "updates.Trigger", now, ticket: str):
        try:
            return self._batch(database, owner_id, trigger, now, ticket)
        except Exception as error:
            logger.error("unexpected error in a batch of checks (%s)", type(error).__name__)

    def _batch(self, database, owner_id: str, trigger: "updates.Trigger", now, ticket: str) -> "updates.BatchResult | None":
        try:
            # Its slot is still its own: the one it was queued with, never another batch's.
            if not self.batches.holds(owner_id, ticket):
                return None

            return updates.run_batch(
                database,
                owner_id,
                trigger,
                keep=self.sources.keep,
                delisting=self.sources.delisting,
                register=lambda: self.register(SimpleNamespace(id=f"batch-{owner_id}", cancel=None)),
                now=now,
                monotonic=self.monotonic,
                checking=self.checking,
                stop=self.stopping,
                # The D6 batch checks at most three postings at a time, the rest waiting their turn (D6 plan, 8.2).
                limits=self.refresh_limits(),
            )
        finally:
            self.batches.release(owner_id, ticket)

    def run_due_updates(self, database, now) -> None:
        """The automatic check, when it is on: a batch for every user with a tracked posting due, then every 每日查新
        due (U3-D4c), each on its user's slot."""
        if self.update_seconds is None:
            return

        with database.transaction() as db:
            owners = updates.owners_tracking(db)

        for owner_id in owners:
            if self.stopping.is_set():
                return

            try:
                self.run_batch(database, owner_id, updates.Trigger.SCHEDULED, now)
            except Exception as error:
                logger.error("unexpected error in a batch of checks (%s)", type(error).__name__)

        # 每日查新 (U3-D4c): each watch due, one run at a time on its user's slot, after the batches.
        with database.transaction() as db:
            due = watches.due_watches(db, now(), keep=self.sources.keep, register=self.watch_register())

        for owner_id, watch_id in due:
            if self.stopping.is_set():
                return

            try:
                self.run_watch(database, owner_id, watch_id, watches.Trigger.SCHEDULED, now)
            except Exception as error:
                logger.error("unexpected error in a run of 每日查新 (%s)", type(error).__name__)

    def shutdown(self) -> None:
        # A batch running stops before its next call, and says it was interrupted.
        self.stopping.set()
        self.store.shutdown()
        self.executor.shutdown(wait=False, cancel_futures=True)


def install_discovery(
    app: FastAPI,
    *,
    simulation_delay: float = 0.0,
    sweep_seconds: float = SWEEP_SECONDS,
    clock=None,
    executor=None,
    sources: "SourceSet | None" = None,
    live: LiveService | None = None,
    update_seconds: float | None = None,
) -> DiscoveryRuntime:
    """Mount the pages. live, the app's real service, is taken for validation B alone (sources.model), which needs
    it; every other set runs the simulation whatever live is (DiscoveryRuntime). update_seconds turns the automatic
    check of the postings tracked on (U3-D4b-1), for a set of synthetic sources only: off by default."""
    chosen = sources or SYNTHETIC_SOURCES

    if update_seconds is not None and (
        update_seconds <= 0 or not chosen.grants or any(grant.access is not Access.SYNTHETIC for grant in chosen.grants)
    ):
        raise ConfigError("每 24 小时的自动检查只能在合成来源上开启。")

    library = app.state.library
    searches = DiscoveryRuntime(
        store=DiscoveryTaskStore(clock or time.monotonic),
        executor=executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="discovery"),
        simulation_delay=simulation_delay,
        sweep_seconds=sweep_seconds,
        sources=chosen,
        live=live,
        update_seconds=update_seconds,
        database=library.database,
        now=library.now,
    )
    app.state.discovery = searches
    # The 岗位分析 page fills in a supplied posting's text from here (U3-D4d), without importing these pages.
    app.state.supplied = searches.supply
    # Logging out, or signing in as someone else, ends the session's searches.
    app.state.library.session_end_listeners.append(searches.end_session)
    app.include_router(router)

    return searches


def searches_of(request: Request) -> DiscoveryRuntime:
    return request.app.state.discovery


# --- what was entered ------------------------------------------------------------------


@dataclass(frozen=True)
class Wanted:
    """What the user entered about one dimension: the offered values ticked, values added, and whether others count."""

    ticked: tuple[str, ...] = ()
    added: str = ""
    # Others.INCLUDED or Others.EXCLUDED as chosen, or "" when nothing was chosen.
    others: str = ""


@dataclass(frozen=True)
class SearchInputs:
    targets: str = ""
    city: Wanted = Wanted()
    job_type: Wanted = Wanted()
    direction: Wanted = Wanted()
    # (experience id, confirmed version id), as the page offered them.
    chosen: tuple[tuple[str, str], ...] = ()
    use_preferences: bool = False
    # "<experience id>:<field>" or "preferences:<field>" the user chose to leave out on the review.
    left_out: frozenset[str] = field(default_factory=frozenset)
    # (decision id, revision) of the kept directions chosen (U3-C3b); their names are read from the library each time.
    kept: tuple[tuple[str, int], ...] = ()
    # 临时留存并跟踪这次的推荐, unticked unless the user ticks it (U3-D4b-2); the review holds it only where offered.
    track: bool = False

    def of(self, dimension: Dimension) -> Wanted:
        return getattr(self, dimension.value)


@dataclass(frozen=True)
class Material:
    """What each analysis of the search sends besides the posting, field by field, as the review shows it."""

    sources: tuple[SourcePlan, ...]
    use_preferences: bool
    preferences: tuple[PreferencePlan, ...]
    chars: int

    @property
    def sent_preferences(self) -> tuple[PreferencePlan, ...]:
        return tuple(plan for plan in self.preferences if plan.status == SEND)


@dataclass(frozen=True)
class Draft:
    """What a page entered and reviewed for one search (DiscoveryTask.draft)."""

    inputs: SearchInputs
    material: Material


def fresh_inputs(saved: Mapping[str, str]) -> SearchInputs:
    """A new search: the ideal and acceptable values of the preferences ticked, nothing else chosen."""
    offered = rules.prefill(saved)

    return SearchInputs(**{dimension.value: Wanted(ticked=offered[dimension].values) for dimension in Dimension})


def _chosen(values: list[str]) -> tuple[tuple[str, str], ...]:
    chosen = []

    for value in values:
        item_id, _, version_id = value.partition(":")

        if not item_id or not version_id:
            raise HTTPException(status_code=400)

        chosen.append((item_id, version_id))

    return tuple(chosen)


def kept_refs(values: list[str]) -> tuple[tuple[str, int], ...]:
    """The kept directions a page names, as "<decision id>.<revision>"; anything else is a bad request."""
    if not values:
        return ()

    refs = read_record_refs(values)

    if refs is None:
        raise HTTPException(status_code=400)

    return refs


def inputs_from(form: FormData) -> SearchInputs:
    return SearchInputs(
        targets=form.get("targets", ""),
        **{
            dimension.value: Wanted(
                ticked=tuple(form.getlist(dimension.value)),
                added=form.get(f"{dimension.value}_added", ""),
                others=form.get(f"{dimension.value}_others", ""),
            )
            for dimension in Dimension
        },
        chosen=_chosen(form.getlist("source")),
        use_preferences=form.get("use_preferences") == "yes",
        kept=kept_refs(form.getlist("kept")),
        track=form.get("track_recommendations") == "yes",
    )


def build_scope(inputs: SearchInputs, saved: Mapping[str, str], kept: tuple[str, ...] = ()) -> SearchScope:
    """The scope as entered: offered values ticked and values added; what the user cannot accept comes from the
    saved preferences only, never from the page, and nothing the user did not choose is decided for them.

    kept are the names of the kept directions chosen, as the library stores them (U3-C3b): each joins the directions
    whole, never split as values typed in are, and none becomes ideal (kept is not ideal).
    """
    offered = rules.prefill(saved)
    parts = {}

    for dimension in Dimension:
        given, draft = inputs.of(dimension), offered[dimension]
        ticked = tuple(value for value in draft.values if value in given.ticked)
        seen = {rules.normalized(dimension, value) for value in ticked}
        handed = []

        for value in kept if dimension is Dimension.DIRECTION else ():
            if rules.normalized(dimension, value) not in seen:
                seen.add(rules.normalized(dimension, value))
                handed.append(value)

        added = []

        for value in rules.values_of(given.added):
            if rules.normalized(dimension, value) not in seen:
                seen.add(rules.normalized(dimension, value))
                added.append(value)

        others = Others(given.others) if given.others in (Others.INCLUDED.value, Others.EXCLUDED.value) else Others.UNDECIDED
        parts[dimension] = DimensionScope(
            values=(*ticked, *handed, *added),
            others=others,
            ideal=tuple(value for value in draft.ideal if value in ticked),
            excluded=draft.excluded,
        )

    return SearchScope(
        city=parts[Dimension.CITY],
        job_type=parts[Dimension.JOB_TYPE],
        direction=parts[Dimension.DIRECTION],
        targets=rules.values_of(inputs.targets),
    )


def text_problems(inputs: SearchInputs) -> list[str]:
    """Free text the page cannot take as it is: too long, or holding control characters."""
    texts = [("要找的岗位名称", inputs.targets)] + [
        (f"{DIMENSION_LABELS[dimension]}（也查）", inputs.of(dimension).added) for dimension in Dimension
    ]
    problems = []

    for label, text in texts:
        if len(text) > TEXT_CHARS:
            problems.append(f"{label}最多 {TEXT_CHARS} 字")
        elif any(unicodedata.category(char) == "Cc" for char in text):
            problems.append(f"{label}里有无法处理的控制字符，请重新输入")
        elif any(len(value) > VALUE_CHARS for value in rules.values_of(text)):
            problems.append(f"{label}：每一项最多 {VALUE_CHARS} 字")

    return problems


def kept_name_problems(names: tuple[str, ...]) -> list[str]:
    """A kept direction's name is the library's, yet held to what a value typed in may be."""
    return [
        f"保留的方向“{name}”超过 {VALUE_CHARS} 字或含有无法处理的控制字符，不能用于找岗"
        for name in names
        if len(name) > VALUE_CHARS or any(unicodedata.category(char) == "Cc" for char in name)
    ]


def build_material(db, owner_id: str, inputs: SearchInputs) -> Material:
    """Read the chosen experiences and preferences again, and decide field by field what each analysis may send.

    No experience is fine: the search then only finds, reads and checks postings, and sends no preference either.
    The preferences the scope is made of (city, job type, direction) are held against postings by the rules and never
    sent (rules.SCOPE_FIELDS).
    """
    chosen = inputs.chosen

    if len(chosen) > MAX_EXPERIENCES:
        raise InputProblem(f"一次最多选择 {MAX_EXPERIENCES} 段经历，现在选了 {len(chosen)} 段。", "sources")

    if len({item_id for item_id, _ in chosen}) != len(chosen):
        raise InputProblem("同一段经历只能选择一次，请重新选择。", "sources")

    entries, problems = read_selected_confirmations(db, owner_id, dict(chosen))

    if problems:
        raise InputProblem(
            f"你选择的经历里有 {len(problems)} 段已经修改、重新确认或删除，不能按原来的版本使用。下面已按当前内容刷新，请重新选择。",
            "sources",
        )

    sources = tuple(source_plan(entry, inputs.left_out) for entry in entries)

    for source in sources:
        if not source.sent:
            raise InputProblem(
                f"“{source.title}”没有可以发送的栏目（都被排除或含有疑似敏感信息）。请取消选择它，或修改这段经历后重新确认。",
                "sources",
            )

    use_preferences = inputs.use_preferences and bool(sources)
    preferences = (
        tuple(plan for plan in preference_plans(db, owner_id, inputs.left_out) if plan.field not in rules.SCOPE_FIELDS)
        if use_preferences
        else ()
    )
    chars = sum(len(plan.text) for source in sources for plan in source.fields if plan.status == SEND)
    chars += sum(len(plan.text) for plan in preferences if plan.status == SEND)

    # As start() counts it: not even the shortest posting would fit in one analysis with them.
    if chars >= MAX_OUTBOUND_CHARS:
        raise InputProblem(
            f"所选经历和偏好共 {chars} 字，加上岗位原文会超过一次分析 {MAX_OUTBOUND_CHARS} 字的上限。请少选几段经历，或排除一些栏目。",
            "sources",
        )

    return Material(sources=sources, use_preferences=use_preferences, preferences=preferences, chars=chars)


def confirmation_of(
    owner_id: str,
    scope: SearchScope,
    material: Material,
    grants: tuple[SourceGrant, ...] = synthetic.GRANTS,
    limits: Limits = LIMITS,
    recipient=RECIPIENT,
    notice_version: str = NOTICE_VERSION,
    skipped: tuple[PostingRef, ...] = (),
    track: bool = False,
    batch_state: str = "",
) -> ParentConfirmation:
    """The confirmation of exactly what the review shows, with the server's sources, recipient, notice and limits, and
    the postings the user skipped on those sources as the server read them (U3-D3c)."""
    return ParentConfirmation(
        owner_id=owner_id,
        scope=scope,
        selection=tuple(
            SelectedSource(source.entry.item_id, source.entry.version_id, source.sent) for source in material.sources
        ),
        preferences=tuple(SelectedPreference(plan.field, plan.text) for plan in material.sent_preferences),
        recipient=recipient,
        sources=grants,
        notice_version=notice_version,
        limits=limits,
        skipped=skipped,
        track_recommendations=track,
        batch_state=batch_state,
    )


# --- the input page --------------------------------------------------------------------


@router.get(BASE)
def input_page(request: Request, user: CurrentUser = Depends(current_user)):
    rt = runtime(request)
    origin = request.query_params.get("from", "")
    # Kept directions handed over from the records page (U3-C3b): ids and revisions only, read again below.
    handed = kept_refs(request.query_params.getlist("kept"))
    inputs = None

    if origin:
        task = searches_of(request).store.get(origin, user.id, user.token_hash)

        if task is not None and task.draft is not None:
            inputs = task.draft.inputs

    if inputs is None:
        with rt.database.transaction() as db:
            inputs = replace(fresh_inputs(current_preferences(db, user.id)), kept=handed)

    return input_form(request, user, inputs)


def input_form(request: Request, user: CurrentUser, inputs: SearchInputs, problems=(), *, where: str = "") -> Response:
    """The input page with what the user entered; an experience whose version is gone is not ticked again."""
    rt = runtime(request)

    with rt.database.transaction() as db:
        snapshot = project_confirmed_library(db, user.id, rt.now())
        unconfirmed = len(list_experiences(db, user.id)) - len(snapshot.experiences)
        saved = current_preferences(db, user.id)
        # Every kept direction a search may take now is offered; only those chosen, as they were chosen, are ticked.
        candidates = kept_for_search(db, user.id, rt.now())
        kept = check_kept(db, user.id, inputs.kept, rt.now())

    offered = {(entry.item_id, entry.version_id) for entry in snapshot.experiences}
    ticked = {item_id for item_id, version_id in inputs.chosen if (item_id, version_id) in offered}
    prefill = rules.prefill(saved)

    return render(
        request,
        "discover_input.html",
        {
            "area": "jobs",
            "section": "discover",
            "inputs": inputs,
            "dimensions": [
                (dimension, DIMENSION_LABELS[dimension], prefill[dimension], inputs.of(dimension)) for dimension in Dimension
            ],
            "experiences": snapshot.experiences,
            "unconfirmed": unconfirmed,
            "ticked": ticked,
            "dropped": len(inputs.chosen) - len(ticked) if not problems else 0,
            "kept_candidates": [(entry, entry in kept.kept) for entry in candidates],
            # What a review said already is not said twice.
            "kept_lines": presentation.kept_lines(kept) if where != "kept" else [],
            "analysed_preferences": [name for name in saved if name not in rules.SCOPE_FIELDS and saved.get(name)],
            "problems": problems,
            "where": where,
            "tracing": searches_of(request).tracing(),
            "unsaved_input": bool(problems),
            "PREVIEW": searches_of(request).sources.banner,
            "SOURCE_SET": searches_of(request).sources,
            "KEEP_DAYS": KEEP_DAYS,
            "MAX_EXPERIENCES": MAX_EXPERIENCES,
            "VALUE_CHARS": VALUE_CHARS,
            "Others": Others,
            "CATEGORY_LABELS": CATEGORY_LABELS,
        },
        user=user,
        status_code=422 if problems else 200,
    )


@router.post(f"{BASE}/review")
def review(request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    return new_review(request, user, inputs_from(form))


def new_review(request: Request, user: CurrentUser, inputs: SearchInputs, *, replaces: str | None = None, notice: str = "") -> Response:
    """Check what was entered and keep it as a search to confirm; nothing starts here.

    The scope is checked as start() checks it (tools.scope_check), read-only: what keeps it from starting is shown on
    the input page, with what was entered, and no search is kept.
    """
    rt = runtime(request)
    searches = searches_of(request)
    material, problems, where = None, (), ""

    with rt.database.transaction() as db:
        kept = check_kept(db, user.id, inputs.kept, rt.now())
        names = tuple(entry.name for entry in kept.kept)
        scope = build_scope(inputs, current_preferences(db, user.id), names)
        problems, where = presentation.kept_problems(kept), "kept"

        if not problems:
            problems, where = text_problems(inputs) or kept_name_problems(names) or list(scope_check(scope)), "scope"

        if not problems:
            try:
                material = build_material(db, user.id, inputs)
            except InputProblem as problem:
                problems, where = (problem.message,), problem.where

        # The postings this user skipped on the sources the search would ask (U3-D3c), read by the server, never a form.
        skipped = records.skipped_refs(db, user.id, source_ids(searches.sources.grants))

    # The input page reads the library again, in a transaction of its own.
    if material is None:
        return input_form(request, user, inputs, problems, where=where)

    # Held for tracking only where the server offers it (U3-D4b-2): a form's tick alone never widens what is kept.
    track = inputs.track and records.recommendations_offered(searches.sources.grants, searches.sources.keep, bool(material.sources))

    try:
        confirmation = confirmation_of(
            user.id,
            scope,
            material,
            searches.sources.grants,
            searches.sources.limits,
            searches.recipient,
            searches.notice_version,
            skipped,
            track,
            searches.batch_state(),
        )
        task = searches.store.create(user.id, user.token_hash, confirmation, draft=Draft(inputs, material))
    except TooManyTasks:
        return message_page(
            request,
            title="暂时不能开始新的查找",
            message="服务器上正在保留的临时查找太多了。请稍后再试，或先取消你不需要的查找。",
            status_code=503,
            user=user,
            links=[(BASE, "返回找岗位")],
        )

    if replaces:
        searches.store.discard(replaces, user.id, user.token_hash)

    return see_other(f"{BASE}/tasks/{task.id}" + (f"?notice={notice}" if notice else ""))


# --- one search -------------------------------------------------------------------------


def _task(request: Request, user: CurrentUser, task_id: str):
    task = searches_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        raise NotFound()

    return task


def gone_page(request: Request, user: CurrentUser) -> Response:
    return message_page(
        request,
        title="找不到这次查找",
        message=(
            f"它可能已经过期（临时结果保留 {TASK_TTL_SECONDS // 60} 分钟）、服务已重启、你已退出登录，或者它属于其他会话。"
            "需要的话，请重新开始一次查找。"
        ),
        status_code=404,
        user=user,
        links=[(BASE, "返回找岗位")],
    )


@router.get(f"{BASE}/tasks/{{task_id}}")
def task_page(task_id: str, request: Request, notice: str = "", user: CurrentUser = Depends(current_user)):
    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is Stage.REVIEW:
        return review_page(request, user, task, notice=NOTICES.get(notice))

    context = {
        "area": "jobs",
        "section": "discover",
        "task": task,
        "notice": NOTICES.get(notice),
        "PREVIEW": searches_of(request).sources.banner,
        "SOURCE_SET": searches_of(request).sources,
        "status_line": presentation.running_status(task, searches_of(request).sources.requests_note, live=searches_of(request).uses_model),
        "steps": [presentation.step_view(step) for step in task.steps],
        "requests": presentation.requests_text(task, searches_of(request).sources.requests_note, live=searches_of(request).uses_model),
        "scope_lines": presentation.scope_lines(task.confirmation.scope),
        "material": task.draft.material,
        "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
    }

    if task.stage is Stage.CANCELLED:
        context["cancelled_by"] = presentation.CANCELLED_BY.get(task.cancelled_by, "这次查找已停止")

    # Why it did not run, when it did not: nothing was sent then.
    context["problem"] = presentation.problem_text(task.problem) if task.problem is not None else None

    if task.finished is not None:
        context.update(outcome_context(request, user, task))

    return render(request, "discover_task.html", context, user=user)


def review_page(request: Request, user: CurrentUser, task, *, notice=None, error=None, status_code=200) -> Response:
    material = task.draft.material
    searches = searches_of(request)

    return render(
        request,
        "discover_review.html",
        {
            "area": "jobs",
            "section": "discover",
            "task": task,
            "material": material,
            "scope_lines": presentation.scope_lines(task.confirmation.scope),
            "sources": [
                (grant.name, presentation.evidence_label(grant.evidence, searches_of(request).sources.evidence_note))
                for grant in task.confirmation.sources
            ],
            "limits": task.confirmation.limits,
            "notice": notice,
            "error": error,
            "tracing": searches_of(request).tracing(),
            "PREVIEW": searches_of(request).sources.banner,
            "SOURCE_SET": searches_of(request).sources,
            "MAX_DECISIONS": MAX_DECISIONS,
            # Validation B (U3-D2-E3): its notice, its approval as it stands, and why it may not start now, if so; under
            # the D6 batch also when what is left cannot cover one search (D6-1 R3, L3: no start button then).
            "LIVE_NOTICE": searches.notice,
            "service_lines": searches.service_lines(),
            "refusal": searches.start_problem(user.id),
            "SEND": SEND,
            "LEFT_OUT": LEFT_OUT,
            "BLOCKED": BLOCKED,
            "PREFERENCES_KEY": PREFERENCES_KEY,
            "CATEGORY_LABELS": CATEGORY_LABELS,
            "found_labels": analysis_text.found_labels,
            "TASK_TTL_MINUTES": TASK_TTL_SECONDS // 60,
            # 临时留存并跟踪这次的推荐 (U3-D4b-2): held as confirmed, or why not although ticked.
            "tracking": {
                "on": task.confirmation.track_recommendations,
                "ticked": task.draft.inputs.track,
                "terms": presentation.TRACKING_TERMS,
                "not_offered": presentation.NOT_OFFERED,
            },
        },
        user=user,
        status_code=status_code,
    )


def _sendable(material: Material) -> set[str]:
    keys = {
        f"{source.entry.item_id}:{plan.name}" for source in material.sources for plan in source.fields if plan.status != BLOCKED
    }

    return keys | {f"{PREFERENCES_KEY}:{plan.field}" for plan in material.preferences if plan.status != BLOCKED}


def _sent(material: Material) -> set[str]:
    keys = {f"{source.entry.item_id}:{name}" for source in material.sources for name in source.sent}

    return keys | {f"{PREFERENCES_KEY}:{plan.field}" for plan in material.sent_preferences}


@router.post(f"{BASE}/tasks/{{task_id}}/confirm")
def confirm(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    """Update what each analysis sends, or confirm and start: the only post that starts a search.

    The form carries no source, recipient or address: those are the server's. Fields unticked since the review are
    never sent: a start whose ticks differ from the reviewed ones becomes a new review instead. A kept direction chosen
    that changed since the review voids this confirmation first (_voided), and the last check of them and the start
    happen under one write lock (_start).
    """
    searches = searches_of(request)

    try:
        task = _task(request, user, task_id)
    except NotFound:
        return gone_page(request, user)

    if task.stage is not Stage.REVIEW:
        notice = "" if task.stage is Stage.CANCELLED else "?notice=already"

        return see_other(f"{BASE}/tasks/{task.id}{notice}")

    if terms_changed(task.confirmation, searches):
        # Reviewed under other sources, another recipient, notice or limits than the pages run now (U3-D3b): that
        # review is not confirmed, whatever its own fingerprint says; it is redone from what was entered.
        return new_review(request, user, task.draft.inputs, replaces=task.id, notice="service_changed")

    if task.confirmation.batch_state != searches.batch_state():
        # The D6 batch used or lost something since the review (another operation, a pause, a stop): that review is
        # not confirmed; it is redone with what is left now (D6 plan, 3.2).
        return new_review(request, user, task.draft.inputs, replaces=task.id, notice="batch_changed")

    material = task.draft.material
    sendable = _sendable(material)
    ticked = set(form.getlist("send")) & sendable
    inputs = task.draft.inputs

    if ticked != _sent(material):
        inputs = replace(inputs, left_out=frozenset(sendable - ticked))

    if not _kept_stand(request, user, inputs):
        return _voided(request, user, task, inputs)

    if ticked != _sent(material):
        notice = "scope_changed" if form.get("action") == "start" else "scope_updated"

        return new_review(request, user, inputs, replaces=task.id, notice=notice)

    if form.get("action") != "start":
        return see_other(f"{BASE}/tasks/{task.id}")

    if searches.tracing():
        return review_page(
            request,
            user,
            task,
            error="环境里设置了可能打开 LangSmith 追踪的变量，所以没有开始，什么都没有发送。请去掉这些变量后重新启动本地网页。",
            status_code=409,
        )

    refusal = searches.start_problem(user.id)

    if refusal is not None:
        # Validation B and the D6 batch only: asked again right before the start; the batch's begin() asks once more,
        # under its lock, before it takes anything, and the search's binding once more inside the gate.
        return review_page(request, user, task, error=f"{refusal}，所以没有开始，什么都没有发送。", status_code=409)

    try:
        # The D6 batch's operation for this search: begun before it starts, or nothing starts (D6 plan, 3.2).
        operation = searches.begin_operation(OperationKind.SEARCH, user.id, task.id, _Stops(searches.store, task.id))
    except BatchRefused as refused:
        return review_page(request, user, task, error=f"{refused}，所以没有开始，什么都没有发送。", status_code=409)

    begun = _start(request, user, task, inputs, form.get("fingerprint", ""))

    if isinstance(begun, str):
        searches.end_operation(operation, "not_started")

        return _voided(request, user, task, inputs, notice=CHANGED_SINCE_REVIEW[begun])

    outcome, started = begun

    if outcome is not Start.STARTED:
        searches.end_operation(operation, "not_started")

    if outcome is Start.STARTED:
        try:
            queued = searches.executor.submit(
                partial(
                run_search,
                searches.store,
                runtime(request).database,
                started.id,
                searches.provider_for,
                register=searches.register(started),
                clock=runtime(request).clock,
                decider_for=searches.decider_for,
                environ=searches.environ,
                binding=searches.binding_for(started),
                diagnose=searches.diagnose_for(started),
                retain=searches.retain,
                trajectory=searches.trajectory_for(started),
            )
            )
        except Exception:
            searches.end_operation(operation, "not_queued")
            raise

        if queued is not None:
            # The batch's operation ends whatever came of the search: run, failed, cancelled or never run.
            queued.add_done_callback(lambda done: searches.end_operation(operation))
        else:
            # Run at once (an inline executor): it has ended.
            searches.end_operation(operation)

        return see_other(f"{BASE}/tasks/{started.id}")

    if outcome is Start.ALREADY:
        return see_other(f"{BASE}/tasks/{task.id}?notice=already")

    if outcome is Start.BUSY:
        return review_page(
            request, user, task, error="你已经有一个查找正在进行。请等它结束，或者取消它之后再开始。", status_code=409
        )

    if outcome is Start.STALE_FORM:
        return review_page(
            request,
            user,
            task,
            error="这个确认来自旧的核对页面，所以没有开始。请看一遍下面最新的核对内容后再确认。",
            status_code=409,
        )

    return gone_page(request, user)


def terms_changed(confirmation, searches) -> bool:
    """Whether a confirmation was made under other terms than the pages run now: its sources, the recipient of its
    analyses, its notice or its limits."""
    return (
        confirmation.notice_version != searches.notice_version
        or confirmation.recipient != searches.recipient
        or tuple(confirmation.sources) != tuple(searches.sources.grants)
        or confirmation.limits != searches.sources.limits
    )


def _kept_stand(request: Request, user: CurrentUser, inputs: SearchInputs) -> bool:
    """Whether every kept direction the search was reviewed with is still the user's, as chosen, and usable."""
    if not inputs.kept:
        return True

    rt = runtime(request)

    with rt.database.transaction() as db:
        return check_kept(db, user.id, inputs.kept, rt.now()).stands


def _voided(request: Request, user: CurrentUser, task, inputs: SearchInputs, *, notice: str = "kept_changed") -> Response:
    """A kept direction (U3-C3b), or the postings the user skipped (U3-D3c), changed since the review: the confirmation
    goes before anything else is tried, so no form of it can start a search again, whatever the review made again from
    what was entered says."""
    searches_of(request).store.discard(task.id, user.id, user.token_hash)

    return new_review(request, user, inputs, notice=notice)


# What _start says changed since the review, beginning nothing, and how the review made again says it.
CHANGED_SINCE_REVIEW = {"kept": "kept_changed", "skips": "skips_changed"}


def source_ids(grants) -> tuple[str, ...]:
    return tuple(sorted({grant.source_id for grant in grants}))


def _start(request: Request, user: CurrentUser, task, inputs: SearchInputs, fingerprint: str):
    """Begin the search once; or say what changed since the review ("kept" or "skips") and begin nothing.

    The kept directions chosen and the postings the user skipped are read again, and the search begun, under the
    library's write lock, so neither changes between the last check and the start; the skips are read again for every
    search, one reviewed with none and no kept direction too (U3-D3c). Once begun, the search keeps the skips it was
    confirmed with: what the user decides after counts from the next search on."""
    searches, rt = searches_of(request), runtime(request)

    with rt.database.transaction() as db:
        if inputs.kept and not check_kept(db, user.id, inputs.kept, rt.now()).stands:
            return "kept"

        if records.skipped_refs(db, user.id, source_ids(task.confirmation.sources)) != task.confirmation.skipped:
            return "skips"

        # When it starts, by the app's clock: which of two searches is the later, for current recommendations (U3-D4b-2).
        return searches.store.begin(task.id, user.id, user.token_hash, fingerprint, started_at=rt.now())


@router.post(f"{BASE}/tasks/{{task_id}}/cancel")
def cancel(task_id: str, request: Request, user: CurrentUser = Depends(current_user), form: FormData = Depends(checked_fields)):
    task = searches_of(request).store.cancel(task_id, user.id, user.token_hash)

    if task is None:
        return gone_page(request, user)

    # The search's own page says what cancelling did and did not stop.
    return see_other(safe_next(form.get("next", "")) or f"{BASE}/tasks/{task.id}")


@router.get(f"{BASE}/tasks/{{task_id}}/status")
def task_status(task_id: str, request: Request, user: CurrentUser = Depends(current_user)):
    """What the running page's script needs, and nothing more: no token, session, confirmation or material."""
    task = searches_of(request).store.get(task_id, user.id, user.token_hash)

    if task is None:
        return JSONResponse({"stage": "gone"}, status_code=404)

    return JSONResponse(
        {
            "stage": task.stage.value,
            "status": presentation.running_status(
                task, searches_of(request).sources.requests_note, live=searches_of(request).uses_model
            ),
            "steps": [presentation.step_view(step) for step in task.steps],
        }
    )


# --- what a search found ---------------------------------------------------------------


def outcome_context(request: Request, user: CurrentUser, task) -> dict:
    """What the result shows. The chosen material is read again on every view: analyses made with material that has
    changed since are said to be so, never shown as current."""
    finished = task.finished
    outcome = finished.outcome
    confirmation = task.confirmation
    material = task.draft.material
    changed = False

    if outcome.results and (confirmation.selection or confirmation.preferences):
        rt = runtime(request)

        with rt.database.transaction() as db:
            chosen = {source.item_id: source.version_id for source in confirmation.selection}
            _, problems = read_selected_confirmations(db, user.id, chosen)
            saved = current_preferences(db, user.id) if confirmation.preferences else {}

        changed = bool(problems) or any(saved.get(item.field) != item.text for item in confirmation.preferences)

    names = {grant.source_id: grant.name for grant in confirmation.sources}
    # What the user kept of it (U3-D3a), read from the library on every view; nothing here writes.
    sources = searches_of(request).sources
    keepable = records.search_keepable(task, sources.keep)
    rt = runtime(request)

    with rt.database.transaction() as db:
        now = rt.now()
        kept = records.kept_states(db, user.id, [item.ref for item in (*outcome.results, *outcome.leads)], now)
        # 临时留存跟踪 apart from 我的岗位 (U3-D4b-2): CURRENT or CLOSED, and until when.
        held = records.held_states(db, kept.values(), now)
        kept_summary = records.kept_search(db, user.id, task.id)
        kept_analyses = records.kept_analyses(db, user.id, task.id, now)

    # How many of its results are held as current recommendations now (R10, U3-D6-3 F3): read with the rest, never sent.
    holding = sum(
        1 for result in outcome.results if (row := kept.get((result.ref.source_id, result.ref.posting_id))) and row.id in held
    )

    return {
        "finished": finished,
        "outcome": outcome,
        "keepable": keepable,
        "kept": kept,
        "held": held,
        "retention_text": presentation.retention_text(task, format_short, holding),
        "kept_summary": kept_summary,
        # The analyses kept from this search (U3-D3b), in use or in the bin, and whether any may be kept.
        "analyses_keepable": records.analyses_keepable(task, sources.keep, sources.keep_analyses),
        "kept_analyses": kept_analyses,
        "decision_label": decision_label,
        "changed_since": changed,
        "results_by_category": [
            (category, RESULT_CATEGORIES[category], [result for result in outcome.results if result.category is category])
            for category in presentation.SHOWN_CATEGORIES
        ],
        "trace_steps": [presentation.step_view(step) for step in finished.trace.steps],
        "searches": presentation.search_lines(outcome.searches, names),
        "source_names": "、".join(grant.name for grant in confirmation.sources),
        "end_label": presentation.end_label(outcome.end),
        "NOT_PERSONALISED": NOT_PERSONALISED,
        "UNLISTED_LABELS": presentation.UNLISTED_LABELS,
        "blocked_lines": presentation.blocked_lines(outcome.blocked),
        "end_note": presentation.end_note(outcome),
        "titles": {source.entry.item_id: source for source in material.sources},
        "versions": {source.entry.item_id: source.entry.version_number for source in material.sources},
        "stated": presentation.stated,
        "checked_text": partial(presentation.checked_text, note=searches_of(request).sources.checked_note),
        "SOURCE_SET": searches_of(request).sources,
        "place_text": presentation.place_text,
        "type_text": presentation.type_text,
        "partial_lines": [presentation.partial_text(read) for read in outcome.partial],
        "deadlines_text": presentation.deadlines_text,
        "evidence_label": partial(presentation.evidence_label, note=searches_of(request).sources.evidence_note),
        "shown_ref": synthetic_ref,
        "conclusion_label": analysis_text.conclusion_label,
        "preference_label": analysis_text.preference_label,
        "reason_labels": analysis_text.reason_labels,
        "BASIS_LABELS": analysis_text.BASIS_LABELS,
        "GROUP_HEADINGS": analysis_text.GROUP_HEADINGS,
        "PREFERENCE_TOPICS": PREFERENCE_TOPICS,
        "Group": RequirementGroup,
        "Category": Category,
    }


def synthetic_ref(ref) -> str:
    """A posting's id in its source, as a page shows it: the source's own id, never an address."""
    return ref.posting_id
