"""Which analysis service a task uses, and how it gets its provider (U2-C2).

The simulated service is the default: the rules of app.job_analysis.simulation,
inside this process. A real one exists only when the app was built with a
LiveSetup (web_main): an approval, its input bound and a way to build the
provider, behind an open gate (app.live_gate). Each service has a recipient
and a notice version; the review page shows them and every task's
fingerprint binds them, so a task reviewed under one service or notice is
never started under another.

A task's provider tells the task's sending (app.live_gate.Sending) of each
request right before it is handed on and again when it comes back: for the
simulation at once, for a real service only after the gate's last check, so
a request the gate stopped never reaches it. Under a budget policy a real
service's request is one generation, never counted (U2-C cost control); a
counted bound would count it first. Either way the sending has the last word before
a request is handed on, and a request counts as sent when it was answered,
as maybe sent when no answer came (app.live_gate.delivery).
"""
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from app.ai_provider import AIProvider, SendState, how_far
from app.analysis_contracts import Recipient
from app.library.sends import Call
from app.live_gate import Blocked, LiveGate, Purpose, Sending, delivery

# What the review page says about a real service. Changing a word changes the version,
# and with it every fingerprint: a confirmation of the old text does not carry over.
LIVE_NOTICE_VERSION = "live-3"
LIVE_NOTICE = (
    "确认后，上面勾选的内容会发给这个外部 AI 服务，只用于生成这一次分析：一次分析通常发出两个请求，先解析岗位，再分析经历。"
    "按服务商的政策，API 数据默认不用于训练模型。",
    "请求设置为不保存回答，但这不等于不留存：按服务商的默认政策，滥用监控日志最多保留 30 天，"
    "法律要求或为防止伤害时可能更长；为了提速，服务商还可能缓存由提示内容算出的加密中间数据（不是原文），最长 24 小时。",
    "每次发送前按固定的保守额度预留费用，返回后按服务商报告的用量核算。这是应用内的预算检查，不是服务商账单的硬上限："
    "正在进行的请求可能超出预留。已经发出的请求无法撤回，取消只能停止之后的发送。",
)


class ProviderUnavailable(Exception):
    """The provider of a real service could not be built; nothing was sent."""


class _Announcing(AIProvider):
    """A provider of this process, telling the task's sending of each request it hands on and gets back."""

    def __init__(self, provider: AIProvider, sending: Sending):
        self._provider = provider
        self._sending = sending

    def identity(self):
        return self._provider.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self._sending.hand_on(Call.GENERATION)

        try:
            result = self._provider.generate_text(
                prompt,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                json_schema_format=json_schema_format,
            )
        except BaseException as error:
            self._sending.came_back(Call.GENERATION, delivery(*how_far(error)))
            raise

        # Answered: it went out.
        self._sending.came_back(Call.GENERATION, delivery(SendState.ANSWERED, result.usage))

        return result


def local_provider(make_provider: Callable[[object], AIProvider]):
    """How a task gets a provider of this process (the simulation, or one a test gives): nothing is gated."""

    @contextmanager
    def provider_for(task, sending: Sending) -> Iterator[AIProvider]:
        yield _Announcing(make_provider(task), sending)

    return provider_for


class LiveService:
    """A real analysis service behind its gate; made by web_main from a LiveSetup."""

    def __init__(self, gate: LiveGate, make_provider: Callable[[], AIProvider]):
        self.gate = gate
        self._make_provider = make_provider
        terms = gate.terms
        self.recipient = Recipient(terms.provider, terms.model, terms.endpoint)
        self.notice_version = LIVE_NOTICE_VERSION

    def provider_for(self, task, sending: Sending):
        """The analysis task's provider behind the gate, as a context (runner.run_task)."""
        return self.provider_of(task.owner_id, sending, Purpose.ANALYSIS)

    @contextmanager
    def provider_of(self, owner_id: str, sending: Sending, purpose: Purpose) -> Iterator[AIProvider]:
        """A provider behind the gate for one task of this purpose (U2-C3: structuring too).

        LiveBlocked or LiveRefused when it may not start; ProviderUnavailable when it cannot be built.
        """
        try:
            provider = self._make_provider()
        except Exception as error:
            raise ProviderUnavailable() from error

        with self.gate.task(purpose, owner_id, provider, sending=sending) as gated:
            yield gated

    def refusal(self) -> Blocked | None:
        """Why no task may start now, if none may: the stop file, a paused approval, no request left."""
        if self.gate.stop_file.exists():
            return Blocked.STOPPED

        status = self.gate.status()

        if status.paused_reason is not None:
            return Blocked.PAUSED

        if status.requests_left < 1:
            return Blocked.REQUESTS

        return None

    def close(self) -> None:
        # Requests already out are settled before the ledger's lock is given up (LiveGate.close).
        self.gate.close()
