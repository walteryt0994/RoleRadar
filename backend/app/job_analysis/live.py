"""The switch for a real analysis service: off unless every condition holds (U2-A2).

The pages use the simulated service only. A real service may be used after
its recipient and processing rules have been checked against the provider's
own documents and the Owner has approved fixed synthetic samples, a number
of requests and a cost limit. Until then nothing here is called by the
pages, and nothing here sends anything by itself.

LivePolicy is what a real run needs, all of it explicit: the model, the
provider's official endpoint, a request budget, an output limit, a timeout
and the date its processing rules were checked. load_live_policy refuses
whatever is missing or doubtful, including proxy settings or an
OPENAI_BASE_URL in the environment that could send requests elsewhere.
make_live_provider fixes the endpoint, ignores proxies from the environment
and turns the SDK's retries off, so one logical request is at most one paid
request. RequestBudget hands out requests before they are sent, never after.
The key is read from the environment when the provider is built, and is
never shown, logged or written anywhere.
"""
import os
import re
import threading
from dataclasses import dataclass

from app.ai_provider import OpenAIProvider, OpenAIProviderConfig
from app.analysis_contracts import Recipient

MODE_VARIABLE = "ROLERADAR_ANALYSIS_MODE"
LIVE = "live"
SETTINGS = {
    "provider": "ROLERADAR_LIVE_PROVIDER",
    "model": "ROLERADAR_LIVE_MODEL",
    "endpoint": "ROLERADAR_LIVE_ENDPOINT",
    "max_requests": "ROLERADAR_LIVE_MAX_REQUESTS",
    "max_output_tokens": "ROLERADAR_LIVE_MAX_OUTPUT_TOKENS",
    "timeout_seconds": "ROLERADAR_LIVE_TIMEOUT_SECONDS",
    "rules_checked_on": "ROLERADAR_LIVE_RULES_CHECKED_ON",
}
# The only addresses a request may go to, by provider.
OFFICIAL_ENDPOINTS = {"openai": "https://api.openai.com/v1"}
# Settings that could send requests through someone else or to another address.
REROUTING_VARIABLES = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "https_proxy",
    "http_proxy",
    "all_proxy",
    "OPENAI_BASE_URL",
)
KEY_VARIABLE = "OPENAI_API_KEY"
# Upper bounds for any approval of this batch: a few synthetic samples.
MAX_REQUESTS = 20
MAX_OUTPUT_TOKENS = 4000
MAX_TIMEOUT_SECONDS = 120
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class LiveRefused(Exception):
    """A real service may not be used; the message says why, in words for the Owner."""


@dataclass(frozen=True)
class LivePolicy:
    provider: str
    model: str
    endpoint: str
    max_requests: int
    max_output_tokens: int
    timeout_seconds: float
    rules_checked_on: str

    @property
    def recipient(self) -> Recipient:
        return Recipient(self.provider, self.model, self.endpoint)


def load_live_policy(environ=None) -> LivePolicy:
    """The policy the environment asks for, or LiveRefused saying what is missing or doubtful."""
    environ = os.environ if environ is None else environ

    if environ.get(MODE_VARIABLE) != LIVE:
        raise LiveRefused("没有要求使用真实服务；默认只用模拟服务。")

    missing = [variable for variable in SETTINGS.values() if not environ.get(variable, "").strip()]

    if missing:
        raise LiveRefused("真实服务的设置不完整，缺少：" + "、".join(missing))

    values = {name: environ[variable].strip() for name, variable in SETTINGS.items()}
    official = OFFICIAL_ENDPOINTS.get(values["provider"])

    if official is None:
        raise LiveRefused(f"不认识的服务商：{values['provider']}；只能使用已核对官方地址的服务商。")

    if values["endpoint"] != official:
        raise LiveRefused("请求地址不是这家服务商的官方地址，可能会把资料交给其他接收方。")

    rerouting = [variable for variable in REROUTING_VARIABLES if environ.get(variable)]

    if rerouting:
        raise LiveRefused("环境里设置了可能改变请求去向的变量：" + "、".join(rerouting) + "；请先移除。")

    if not environ.get(KEY_VARIABLE, "").strip():
        raise LiveRefused("没有提供服务凭据。")

    if not DATE.fullmatch(values["rules_checked_on"]):
        raise LiveRefused("处理规则的核对日期要写成 YYYY-MM-DD。")

    max_requests = _whole(values["max_requests"], 1, MAX_REQUESTS, "请求次数上限")
    max_output_tokens = _whole(values["max_output_tokens"], 1, MAX_OUTPUT_TOKENS, "输出上限")
    timeout = _whole(values["timeout_seconds"], 1, MAX_TIMEOUT_SECONDS, "超时秒数")

    return LivePolicy(
        provider=values["provider"],
        model=values["model"],
        endpoint=values["endpoint"],
        max_requests=max_requests,
        max_output_tokens=max_output_tokens,
        timeout_seconds=float(timeout),
        rules_checked_on=values["rules_checked_on"],
    )


def _whole(raw: str, low: int, high: int, name: str) -> int:
    if not raw.isascii() or not raw.isdigit() or not low <= int(raw) <= high:
        raise LiveRefused(f"{name}要写成 {low} 到 {high} 之间的整数。")

    return int(raw)


def make_live_provider(policy: LivePolicy, environ=None) -> OpenAIProvider:
    """The provider of a checked policy: fixed endpoint, no proxies from the environment, no retries."""
    environ = os.environ if environ is None else environ

    return OpenAIProvider(
        OpenAIProviderConfig(
            api_key=environ.get(KEY_VARIABLE, ""),
            model=policy.model,
            timeout_seconds=policy.timeout_seconds,
            max_retries=0,
            base_url=policy.endpoint,
            use_environment_proxies=False,
        )
    )


class RequestBudget:
    """Requests a policy allows, handed out before they are sent."""

    def __init__(self, limit: int):
        self._lock = threading.Lock()
        self.limit = limit
        self.used = 0

    def take(self, count: int) -> bool:
        """Reserve count requests at once, or none when they would exceed the limit."""
        with self._lock:
            if self.used + count > self.limit:
                return False

            self.used += count

            return True
