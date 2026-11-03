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
request; it also caps the output of every request at the policy's limit,
whatever the caller asks for. RequestBudget hands out requests before they
are sent, never after.
The key is read from the environment when the provider is built, and is
never shown, logged or written anywhere.

load_live_approval reads the terms of the approval a real run is under
(U2-C1): its id, the purposes, the service tier, the reasoning effort, the
limits (a structuring request has an output limit of its own and a limit on
the bytes of resume text it may send, U2-C3), the price of every item a
request may be billed for, and, since the U2-C4 prerequisites, the price a
reservation takes every input token at, the price of one count of a
request's input and the most input tokens a request may have. Every one is
written out: a count's price is never taken to be 0. Even complete terms
are refused while no counted input bound has been checked for the model
and the tier (app.live_gate.CERTIFIED_INPUT_BOUNDS, empty), so no real
service can be used yet. analysis_setup turns what the environment asks
for into the app's service (U2-C2): the simulated one, or a real one from
approved terms; asked for and refused, it stops the app from starting rather
than quietly simulating.
"""
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.ai_provider import OpenAIProvider, OpenAIProviderConfig
from app.analysis_contracts import Recipient
from app.library.errors import ConfigError
from app.live_gate import (
    CERTIFIED_INPUT_BOUNDS,
    ApprovalTerms,
    LiveRefused,
    LiveSetup,
    PriceBasis,
    Purpose,
    RequestUnit,
)

MODE_VARIABLE = "ROLERADAR_ANALYSIS_MODE"
LIVE = "live"
SIMULATION = "simulation"
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
# What an approval names besides the policy above (U2-C1). Prices are US
# dollars per million tokens; a cache item the service does not bill on its
# own is written NOT_BILLED, never left out.
APPROVAL_SETTINGS = {
    "approval_id": "ROLERADAR_LIVE_APPROVAL_ID",
    "purposes": "ROLERADAR_LIVE_PURPOSES",
    "service_tier": "ROLERADAR_LIVE_SERVICE_TIER",
    "reasoning_effort": "ROLERADAR_LIVE_REASONING_EFFORT",
    "structuring_max_bytes": "ROLERADAR_LIVE_STRUCTURING_MAX_BYTES",
    "structuring_max_output_tokens": "ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS",
    "max_input_tokens": "ROLERADAR_LIVE_MAX_INPUT_TOKENS",
    "max_cost_usd": "ROLERADAR_LIVE_MAX_COST_USD",
    "price_input": "ROLERADAR_LIVE_PRICE_INPUT",
    "price_cache_read": "ROLERADAR_LIVE_PRICE_CACHE_READ",
    "price_cache_write": "ROLERADAR_LIVE_PRICE_CACHE_WRITE",
    "price_output": "ROLERADAR_LIVE_PRICE_OUTPUT",
    # Reservations only: the dearest price any input token may be billed at.
    "price_reserve_input": "ROLERADAR_LIVE_PRICE_RESERVE_INPUT",
    # Dollars per count of a request's input.
    "price_count": "ROLERADAR_LIVE_PRICE_COUNT",
    "price_source": "ROLERADAR_LIVE_PRICE_SOURCE",
    "price_checked_on": "ROLERADAR_LIVE_PRICE_CHECKED_ON",
}
NOT_BILLED = "none"
# The reasoning effort of an approval whose requests send none.
OMIT = "omit"
APPROVAL_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
SERVICE_TIER = re.compile(r"[a-z_]{1,20}")
REASONING_EFFORT = re.compile(r"[a-z]{1,20}")
PRICE = re.compile(r"\d{1,6}(\.\d{1,6})?")
# Upper bounds for any approval of this batch, as above.
MAX_COST_USD = Decimal("5")
# A structuring request's ceilings (U2-C3): suggested starting values for the
# offline wiring, not shown to be enough and not an approval of any cost.
MAX_STRUCTURING_BYTES = 8_000
MAX_STRUCTURING_OUTPUT_TOKENS = 8_000
# The most input tokens an approval may let one request have (U2-C4 prerequisites): far below
# the short-context band of the model in view (272,000 input tokens for gpt-5.6-sol, pricing page
# checked 2026-11-03), so that an approval's short-context prices hold for any request it lets out.
MAX_INPUT_TOKENS = 32_000


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


def load_live_approval(policy: LivePolicy, environ=None) -> ApprovalTerms:
    """The terms of the approval the environment names for this policy, or LiveRefused saying what is wrong.

    Every term must be written out. Prices read here are never synthetic.
    Complete terms are refused all the same while no input bound has been
    checked for this model and service tier.
    """
    environ = os.environ if environ is None else environ
    missing = [variable for variable in APPROVAL_SETTINGS.values() if not environ.get(variable, "").strip()]

    if missing:
        raise LiveRefused("批准条款不完整，缺少：" + "、".join(missing))

    values = {name: environ[variable].strip() for name, variable in APPROVAL_SETTINGS.items()}

    if not APPROVAL_ID.fullmatch(values["approval_id"]):
        raise LiveRefused("批准编号只能由字母、数字和 . _ - 组成，最多 64 个字符。")

    purposes = [purpose.strip() for purpose in values["purposes"].split(",")]

    if len(set(purposes)) != len(purposes) or not set(purposes) <= set(Purpose):
        raise LiveRefused("批准的用途只能是 analysis、structuring，用逗号分开，各写一次。")

    if not SERVICE_TIER.fullmatch(values["service_tier"]):
        raise LiveRefused("服务档要写成一个小写名称，例如服务商文档里的那个档位名。")

    effort = values["reasoning_effort"]

    if effort != OMIT and not REASONING_EFFORT.fullmatch(effort):
        raise LiveRefused(f"推理强度要写成一个小写名称；不发送推理强度时写 {OMIT}。")

    if not values["price_source"].startswith("https://"):
        raise LiveRefused("价格来源要写服务商官方价格页的 https 地址。")

    if not DATE.fullmatch(values["price_checked_on"]):
        raise LiveRefused("价格的核对日期要写成 YYYY-MM-DD。")

    try:
        prices = PriceBasis(
            input=_price(values["price_input"], "普通输入单价"),
            cache_read=_price(values["price_cache_read"], "缓存读取单价", may_be_none=True),
            cache_write=_price(values["price_cache_write"], "缓存写入单价", may_be_none=True),
            output=_price(values["price_output"], "输出单价"),
            reserve_input=_price(values["price_reserve_input"], "预留输入单价"),
            source=values["price_source"],
            checked_on=values["price_checked_on"],
            # Written out like the others, 0 included: an unknown price is never taken to be 0.
            count=_price(values["price_count"], "计数单价"),
        )
    except ValueError:
        raise LiveRefused("预留输入单价不能低于普通输入、缓存读取或缓存写入的单价。") from None

    max_cost = _price(values["max_cost_usd"], "费用上限")

    if not 0 < max_cost <= MAX_COST_USD:
        raise LiveRefused(f"费用上限要大于 0、不超过 {MAX_COST_USD} 美元。")

    structuring_max_bytes = _whole(values["structuring_max_bytes"], 1, MAX_STRUCTURING_BYTES, "简历整理单次外发字节上限")
    structuring_max_output_tokens = _whole(
        values["structuring_max_output_tokens"], 1, MAX_STRUCTURING_OUTPUT_TOKENS, "简历整理输出上限"
    )
    max_input_tokens = _whole(values["max_input_tokens"], 1, MAX_INPUT_TOKENS, "单次输入上限")
    bound = CERTIFIED_INPUT_BOUNDS.get((policy.provider, policy.model, values["service_tier"]))

    if bound is None or bound.synthetic or not bound.counted:
        raise LiveRefused(
            f"还没有为模型 {policy.model}（服务档 {values['service_tier']}）核验过输入上界，真实服务不能启用。"
        )

    return ApprovalTerms(
        approval_id=values["approval_id"],
        purposes=frozenset(purposes),
        provider=policy.provider,
        model=policy.model,
        endpoint=policy.endpoint,
        service_tier=values["service_tier"],
        reasoning_effort=None if effort == OMIT else effort,
        max_output_tokens=policy.max_output_tokens,
        structuring_max_output_tokens=structuring_max_output_tokens,
        timeout_seconds=policy.timeout_seconds,
        structuring_max_bytes=structuring_max_bytes,
        max_requests=policy.max_requests,
        max_cost_micro_usd=int(max_cost * 1_000_000),
        prices=prices,
        input_bound=bound.name,
        rules_checked_on=policy.rules_checked_on,
        # A request of a counted bound is a count and at most one generation.
        request_unit=RequestUnit.COUNTED_GENERATION,
        max_input_tokens=max_input_tokens,
    )


def _price(raw: str, name: str, *, may_be_none: bool = False) -> Decimal | None:
    if may_be_none and raw == NOT_BILLED:
        return None

    if not PRICE.fullmatch(raw):
        also = f"；不单独计费时写 {NOT_BILLED}" if may_be_none else ""
        raise LiveRefused(f"{name}要写成美元金额，最多 6 位小数{also}。")

    return Decimal(raw)


def analysis_setup(environ: Mapping[str, str]) -> LiveSetup | None:
    """The real service the environment asks for, None for the simulated one, or ConfigError saying why not.

    An unset mode means the simulated service. A real one comes only from
    complete approved terms whose model and tier have a checked input bound
    (load_live_approval); none has one yet, so asking for a real service
    stops the app from starting instead.
    """
    mode = environ.get(MODE_VARIABLE, "").strip()

    if mode in ("", SIMULATION):
        return None

    if mode != LIVE:
        raise ConfigError(f"不认识的分析模式“{mode}”；不设置时只用模拟服务。")

    try:
        policy = load_live_policy(environ)
        terms = load_live_approval(policy, environ)
    except LiveRefused as refused:
        raise ConfigError(f"要求使用真实服务，但不能启用：{refused}") from None

    bound = CERTIFIED_INPUT_BOUNDS[(terms.provider, terms.model, terms.service_tier)]

    # The provider's own cap covers every approved purpose; the gate caps each request by its purpose.
    limit = max(terms.output_limit(purpose) for purpose in terms.purposes)

    return LiveSetup(
        terms,
        bound,
        lambda: make_live_provider(policy, environ, service_tier=terms.service_tier, max_output_tokens=limit),
    )


def make_live_provider(
    policy: LivePolicy, environ=None, *, service_tier: str | None = None, max_output_tokens: int | None = None
) -> OpenAIProvider:
    """The provider of a checked policy: fixed endpoint, no proxies from the environment, no retries, capped output.

    service_tier, when given, is asked for by every request (an approval's tier).
    max_output_tokens, when given, replaces the policy's output cap (an approval's largest limit).
    """
    environ = os.environ if environ is None else environ

    return OpenAIProvider(
        OpenAIProviderConfig(
            api_key=environ.get(KEY_VARIABLE, ""),
            model=policy.model,
            timeout_seconds=policy.timeout_seconds,
            max_retries=0,
            base_url=policy.endpoint,
            use_environment_proxies=False,
            max_output_tokens=max_output_tokens or policy.max_output_tokens,
            service_tier=service_tier,
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
