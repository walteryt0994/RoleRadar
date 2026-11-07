"""Shared pieces of the U3-A2 search agent tests: a synthetic library, scopes, confirmations and stand-in services.

Everything is synthetic and stays in this process. The sources are the synthetic ones (app.discovery.synthetic),
decisions come from scripts or the simulated decider, analyses from the app's simulation, and the one service
behind a gate here is made up, with synthetic prices, and answers inside this process.
"""
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

from app.ai_provider import AIProvider, GenerationResult, ProviderIdentity
from app.analysis_contracts import Recipient, SelectedPreference, SelectedSource
from app.discovery import synthetic
from app.discovery.agent import ACTION_SCHEMA_NAME
from app.discovery.contracts import (
    DimensionScope,
    Limits,
    Others,
    ParentConfirmation,
    PostingRef,
    SearchApproval,
    SearchScope,
    search_fingerprint,
)
from app.discovery.synthetic import CAREERS
from app.discovery.tools import start
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider
from app.live_gate import ApprovalTerms, InputBound, PriceBasis
from analysis_helpers import confirmed, saved_preferences
from job_helpers import RecordingProvider

SENTINEL = "没有选择发送的栏目-u3a2-5d1f"
PROJECT_ACTIONS = "用 Python 清洗订单数据，并用 SQL 汇总每周销量。"
PROJECT_TITLE = "校园订单看板（合成）"
NOTICE = "discovery-test-a2"
SALES = SelectedPreference("work_content_no", "销售")
# The scope of the A1 tests, over app.discovery.synthetic.POSTINGS.
SCOPE = SearchScope(
    city=DimensionScope(("北京", "上海"), Others.EXCLUDED, ("北京",), ("深圳",)),
    job_type=DimensionScope(("实习",), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope(("数据分析",), Others.INCLUDED, ("数据分析",)),
    targets=("数据分析实习生",),
)
WITHIN = dict(job_type="实习", direction="数据分析")
# The scope of the senior-first case (A2 pre-check, 1): Beijing and Shanghai, internships and graduate roles,
# data analysis only.
SENIOR_SCOPE = SearchScope(
    city=DimensionScope(("北京", "上海"), Others.EXCLUDED, ("北京",)),
    job_type=DimensionScope(("实习", "校招"), Others.EXCLUDED, ("实习",)),
    direction=DimensionScope(("数据分析",), Others.EXCLUDED, ("数据分析",)),
    targets=("数据分析实习生",),
)
SYNTHETIC_RECIPIENT = Recipient("synthetic", "synthetic-model", "https://synthetic.invalid/v1")
PRICES = PriceBasis(Decimal("2"), None, None, Decimal("8"), Decimal("2"), "synthetic", "2026-11-07", True)
BOUND = InputBound("synthetic-bytes", lambda prompt, output_format: len(prompt.encode("utf-8")) + 50, "synthetic", "2026-11-07", True)


def ref(posting_id: str, source: str = CAREERS) -> PostingRef:
    return PostingRef(source, posting_id)


def make_library(database, owners, clock) -> SimpleNamespace:
    """Two confirmed synthetic experiences and saved preferences of the first user."""
    owner, other = owners
    project = confirmed(
        database, owner, clock, "project", private_note=f"私有备注{SENTINEL}",
        title=PROJECT_TITLE, actions=PROJECT_ACTIONS, outcome="看板在课程展示中使用。", reflection=f"反思{SENTINEL}",
    )
    education = confirmed(database, owner, clock, "education", school="合成大学", major="数据科学", graduation="2027 年 6 月（预计）")
    preferences = saved_preferences(
        database, owner, clock, cities_ideal="北京", cities_ok="上海", cities_no="深圳", job_types_ideal="实习",
        directions_ideal="数据分析", work_content_no="销售", must_haves="最早 2026 年 7 月入职",
    )

    return SimpleNamespace(owner=owner, other=other, project=project, education=education, preferences=preferences)


def confirmation_for(
    library, *, scope=SCOPE, sources=synthetic.GRANTS, recipient=RECIPIENT, selection=None, preferences=(SALES,), limits=Limits()
) -> ParentConfirmation:
    """What the user confirmed for a search: two experiences and some of their fields unless said otherwise."""
    if selection is None:
        selection = (
            SelectedSource(*library.project, ("title", "actions", "outcome")),
            SelectedSource(*library.education, ("school", "major", "graduation")),
        )

    return ParentConfirmation(
        owner_id=library.owner,
        scope=scope,
        selection=selection,
        preferences=preferences,
        recipient=recipient,
        sources=sources,
        notice_version=NOTICE,
        limits=limits,
    )


def begin(database, clock, confirmation, provider=None, *, register=None, cancel=None, monotonic=lambda: 0.0):
    """A search of exactly this confirmation, approved as a test approves it (not a user's permission)."""
    return start(
        database,
        confirmation,
        SearchApproval(search_fingerprint(confirmation)),
        provider or RecordingProvider(),
        register=register or synthetic.register(),
        clock=clock,
        cancel=cancel,
        monotonic=monotonic,
    )


def gate_terms(*, purposes=frozenset({"discovery"}), max_requests=30) -> ApprovalTerms:
    """Made-up terms of a synthetic service; nothing real."""
    return ApprovalTerms(
        approval_id="synthetic-discovery-a2",
        purposes=purposes,
        provider="synthetic",
        model="synthetic-model",
        endpoint="https://synthetic.invalid/v1",
        service_tier="default",
        reasoning_effort="low",
        max_output_tokens=2_000,
        structuring_max_output_tokens=2_000,
        timeout_seconds=30.0,
        structuring_max_bytes=8_000,
        max_requests=max_requests,
        max_cost_micro_usd=50_000_000,
        prices=PRICES,
        input_bound=BOUND.name,
        rules_checked_on="2026-11-07",
        request_unit="generation",
        max_input_tokens=32_000,
    )


class SyntheticService(AIProvider):
    """A made-up service behind a gate: decisions from a script, analyses from the app's simulation; made-up usage.

    decisions holds, for each decision asked for in order, the raw answer, an action as JSON data, or a function of
    nothing run before answering (it may stop the task, then answer). sent keeps (kind, prompt, output limit) of
    every request that reached it.
    """

    SYNTHETIC = True

    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.inner = SimulatedProvider()
        self.sent = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        name = json_schema_format.name if json_schema_format is not None else ""
        self.sent.append((name, prompt, max_output_tokens))

        if name == ACTION_SCHEMA_NAME:
            item = self.decisions.pop(0)

            while callable(item):
                item = item()

            text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            answer = GenerationResult(text, "synthetic", "synthetic-model", "synthetic-model", 0.0)
        else:
            answer = self.inner.generate_text(prompt, json_schema_format=json_schema_format)

        return replace(
            answer,
            provider="synthetic",
            requested_model="synthetic-model",
            model="synthetic-model",
            input_tokens=120,
            output_tokens=30,
            total_tokens=150,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            service_tier="default",
        )

    def kinds(self) -> list[str]:
        return [name for name, _, _ in self.sent]
