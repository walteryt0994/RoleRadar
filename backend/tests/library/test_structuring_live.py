"""U2-C3: structuring an import through a real service's control path, offline.

Everything of a real service but the provider: a synthetic approval, input
bound and prices open the gate on the test's own library, and a synthetic
provider answers under the approved recipient's name by the local rules,
applied to what it was sent. Nothing leaves the process, and the launcher
blocks the network.
"""
import json
import threading
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from app.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    AIProviderConnectionError,
    AIProviderResponseError,
    AIProviderTimeoutError,
    GenerationResult,
    ProviderIdentity,
    SendState,
    Usage,
)
from app.library.candidates import MAX_CANDIDATES, TOO_MANY
from app.library.identity import token_hash
from app.library.imports import IMPORT_WINDOW, STALE_STRUCTURING_AFTER, delete_draft, discard
from app.live_structuring.service import (
    INSTRUCTIONS,
    SCHEMA_FORMAT,
    SEGMENTS_END,
    SEGMENTS_START,
    cancel,
    checked_answer,
    request_for,
)
from app.library.models import CandidateState, LiveRequestState, ResumeImport
from app.library.resume_rules import SIMULATION_NAME, SimulatedStructurer, structure
from app.library.review import Asked, candidates_of, structure_import, structuring_of
from app.library.web import SESSION_COOKIE
from app.live_gate import LiveSetup, Purpose
from app.web_main import create_app
from job_helpers import InlineExecutor, text_of, wait_until
from library_helpers import LOCAL_BASE_URL, Browser, form_for
from structuring_cases import CASES
from test_library_adoption import adopt_new, adoptions
from test_library_candidates import owner_id, paste, rt
from test_live_gate import BOUND, requests_of, terms

RECIPIENT = ("synthetic", "synthetic-model", "https://synthetic.invalid/v1")
OUTPUT_LIMIT = 800
USED = Usage(
    input_tokens=1000,
    output_tokens=200,
    total_tokens=1200,
    cache_read_tokens=0,
    cache_write_tokens=0,
    reasoning_tokens=0,
    service_tier="default",
)
# 1000 input tokens at 2 and 200 output tokens at 8 (test_live_gate.PRICES), in millionths of a dollar.
COST = 1000 * 2 + 200 * 8
SECRETS = ("lin.synthetic@example.com", "138 0000 0000", "https://example.com/portfolio", "林合成")


def approval(**changes):
    values = {
        "purposes": frozenset({"analysis", "structuring"}),
        "max_requests": 6,
        "max_cost_micro_usd": 1_000_000,
        "structuring_max_output_tokens": OUTPUT_LIMIT,
    }
    values.update(changes)

    return terms(**values)


def sent_data(prompt: str) -> list[dict]:
    return json.loads(prompt.split(f"{SEGMENTS_START}\n", 1)[1].rsplit(f"\n{SEGMENTS_END}", 1)[0])


def by_rules(prompt: str) -> str:
    """What the local rules find in the segments of a prompt, in the service's format, without the basic profile."""
    data = sent_data(prompt)
    texts = {item["segment"]: item["text"] for item in data}
    count = max(texts, default=0)
    segments = [{"kind": "text", "number": number, "text": texts.get(number, "")} for number in range(1, count + 1)]
    candidates = [
        {
            "kind": item["kind"],
            "fields": [{"field": name, "pieces": pieces} for name, pieces in item["fields"].items()],
            "notes": item.get("notes", []),
        }
        for item in structure(segments)
        if item["kind"] != "profile"
    ]

    return json.dumps({"candidates": candidates}, ensure_ascii=False)


class Answer:
    """A script step: answer with give(prompt) instead of by the local rules."""

    def __init__(self, give):
        self.give = give


class Structuring(AIProvider):
    """Made up for these tests: answers by the local rules under the approved recipient's name.

    script holds what each call does first, in order: an exception to raise,
    a callable to run (it may wait, or stop the run), an Answer, or None.
    """

    SYNTHETIC = True

    def __init__(self, *script, usage=USED):
        self.script = list(script)
        self.calls = []
        self.usage = usage

    def identity(self):
        return ProviderIdentity(*RECIPIENT)

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.calls.append(
            {
                "prompt": prompt,
                "max_output_tokens": max_output_tokens,
                "reasoning_effort": reasoning_effort,
                "format": json_schema_format,
            }
        )
        step = self.script.pop(0) if self.script else None
        give = by_rules

        if isinstance(step, BaseException):
            raise step

        if isinstance(step, Answer):
            give = step.give
        elif callable(step):
            step()

        usage = self.usage

        return GenerationResult(
            text=give(prompt),
            provider="synthetic",
            requested_model="synthetic-model",
            model="synthetic-model",
            latency_seconds=0.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            service_tier=usage.service_tier,
        )


@pytest.fixture
def live(make_config, clock):
    """build(provider, ...) -> an app with a real service on the test's library, and a signed-in browser."""
    stack = ExitStack()

    def build(provider=None, *, approval_terms=None, inline=True):
        provider = provider or Structuring()
        setup = LiveSetup(approval_terms or approval(), BOUND, lambda: provider)
        application = create_app(make_config(), clock=clock, live=setup)
        # The client keeps the app's lifespan running; leaving it closes the gate.
        stack.enter_context(TestClient(application, base_url=LOCAL_BASE_URL, client=("127.0.0.1", 50000)))

        if inline:
            application.state.structuring.executor = InlineExecutor()

        return application, Browser.signed_in(application, "合成用户 A")

    yield build
    stack.close()


def confirm_url(import_id: str) -> str:
    return f"/imports/{import_id}/structure/confirm"


def confirmation(browser, import_id: str) -> dict:
    page = browser.get(confirm_url(import_id))
    assert page.status_code == 200, page.text

    return form_for(page.text, confirm_url(import_id)).fields


def send(browser, import_id: str):
    return browser.post(confirm_url(import_id), confirmation(browser, import_id))


def record(app, import_id: str):
    with rt(app).database.transaction() as db:
        current = structuring_of(db, import_id)

        return None if current is None else (current.state, current.outcome, current.structurer)


def kept(app, import_id: str) -> list:
    with rt(app).database.transaction() as db:
        return [(row.kind, row.extracted) for row in candidates_of(db, owner_id(app), import_id)]


def states_of(app):
    return sorted((row.state, row.charged_micro_usd) for row in requests_of(rt(app).database))


def paused(app):
    return app.state.structuring.live.gate.status().paused_reason


def review_text(browser, import_id: str) -> str:
    return text_of(browser.get(f"/imports/{import_id}/review").text)


# --- where a real structuring is offered --------------------------------------------------------------


def test_without_a_real_service_only_the_local_rules_are_offered(app, browser_a):
    import_id = paste(browser_a)

    assert "发给外部服务" not in browser_a.get(f"/imports/{import_id}").text
    assert browser_a.get(confirm_url(import_id)).status_code == 404


def test_an_approval_that_does_not_cover_structuring_offers_only_the_local_rules(live):
    app, browser = live(approval_terms=approval(purposes=frozenset({"analysis"})))
    import_id = paste(browser)

    assert "发给外部服务" not in browser.get(f"/imports/{import_id}").text
    assert browser.get(confirm_url(import_id)).status_code == 404


def test_the_confirmation_shows_all_that_would_go_out_and_none_of_what_was_masked(live):
    app, browser = live()
    import_id = paste(browser)
    page = text_of(browser.get(f"/imports/{import_id}").text)

    assert "发给外部服务整理…" in page and "用本机规则整理（不发送）" in page

    html = browser.get(confirm_url(import_id)).text
    shown = text_of(html)

    assert "synthetic 的 synthetic-model，经 https://synthetic.invalid/v1 发送" in shown
    assert "邮箱 1 处、电话号码 1 处、链接 1 处" in shown and "写着姓名的 1 行" in shown
    assert "［已隐去邮箱］" in shown and "教育经历" in shown and "确认并发给外部服务" in shown

    for secret in SECRETS:
        assert secret not in html

    # Looking reserves nothing.
    assert requests_of(rt(app).database) == []


# --- a confirmed run ---------------------------------------------------------------------------------


def test_a_confirmed_run_sends_only_what_was_shown_and_its_candidates_wait_for_the_user(live):
    provider = Structuring()
    app, browser = live(provider)
    import_id = paste(browser)
    shown = browser.get(confirm_url(import_id)).text
    response = send(browser, import_id)

    assert response.status_code == 303
    assert response.headers["location"] == f"/imports/{import_id}/review?notice=structuring_started"

    [call] = provider.calls

    # The structuring limit and the approved effort, with the strict format; every word sent was shown.
    assert (call["max_output_tokens"], call["reasoning_effort"], call["format"]) == (OUTPUT_LIMIT, "low", SCHEMA_FORMAT)
    assert all(line in shown for item in sent_data(call["prompt"]) for line in item["text"].split("\n"))
    assert not any(secret in call["prompt"] for secret in SECRETS)
    assert states_of(app) == [(LiveRequestState.SETTLED, COST)]
    assert record(app, import_id) == ("done", None, "live:synthetic/synthetic-model/1")

    kinds = [kind for kind, _ in kept(app, import_id)]

    # The basic profile comes from the local rules alone, which read the import here.
    assert kinds[0] == "profile" and {"education", "work", "project", "award", "skills", "intention"} <= set(kinds)

    text = review_text(browser, import_id)

    assert "外部 AI 服务（synthetic synthetic-model）整理，基本资料由本机规则整理" in text
    assert "什么都没有发送出去" not in text

    # Nothing is written anywhere until the user adopts a candidate, as with the local rules.
    with rt(app).database.transaction() as db:
        education = next(row for row in candidates_of(db, owner_id(app), import_id) if row.kind == "education")

    assert adoptions(app) == []
    assert adopt_new(browser, import_id, education.id).status_code == 303

    with rt(app).database.transaction() as db:
        assert db.get(type(education), education.id).state == CandidateState.ADOPTED

    assert len(adoptions(app)) == 1


def test_a_confirmation_does_not_carry_over_to_changed_text_or_a_made_up_one(live):
    provider = Structuring()
    app, browser = live(provider)
    import_id = paste(browser)
    fields = confirmation(browser, import_id)
    made_up = browser.post(confirm_url(import_id), {**fields, "fingerprint": "0" * 64})

    assert made_up.status_code == 409 and "有了变化" in text_of(made_up.text)

    # Saved as a draft since: the import's revision moved on, so what was confirmed no longer holds.
    assert browser.submit(f"/imports/{import_id}", f"/imports/{import_id}/save").status_code == 303

    changed = browser.post(confirm_url(import_id), fields)

    assert changed.status_code == 409 and "有了变化" in text_of(changed.text)
    assert provider.calls == [] and requests_of(rt(app).database) == [] and record(app, import_id) is None

    # Confirmed again as it is now, it runs.
    assert send(browser, import_id).status_code == 303 and len(provider.calls) == 1


def test_a_second_confirmation_of_the_same_run_starts_nothing(live):
    provider = Structuring()
    app, browser = live(provider)
    import_id = paste(browser)
    fields = confirmation(browser, import_id)

    assert browser.post(confirm_url(import_id), fields).headers["location"].endswith("notice=structuring_started")

    again = browser.post(confirm_url(import_id), fields)

    assert again.status_code == 303 and again.headers["location"].endswith("notice=already_structured")
    assert len(provider.calls) == 1


def test_text_over_the_limit_is_never_sent_nor_cut_short_and_the_local_rules_still_work(live):
    provider = Structuring()
    app, browser = live(provider, approval_terms=approval(structuring_max_bytes=100))
    import_id = paste(browser)
    page = text_of(browser.get(f"/imports/{import_id}").text)

    assert "超过了单次 100 字节的上限" in page and "发给外部服务整理…" not in page

    html = browser.get(confirm_url(import_id)).text

    assert "确认并发给外部服务" not in html

    posted = browser.post(confirm_url(import_id), form_for(html, confirm_url(import_id)).fields)

    assert posted.status_code == 409
    assert provider.calls == [] and requests_of(rt(app).database) == [] and record(app, import_id) is None
    assert browser.submit(f"/imports/{import_id}", f"/imports/{import_id}/structure").status_code == 303
    assert record(app, import_id)[0] == "done"


# --- the boundary of the request ----------------------------------------------------------------------


def stop(how: str, app, browser, import_id: str, clock) -> None:
    """Stop a run the way a page, a session, an import's end or time would."""
    library = rt(app)
    owner = owner_id(app)

    if how == "expired":
        clock.advance(days=IMPORT_WINDOW.days)
    elif how == "run_timeout":
        clock.advance(seconds=STALE_STRUCTURING_AFTER.total_seconds())
    elif how == "cancel":
        with library.database.transaction() as db:
            attempt = structuring_of(db, import_id).attempt

        cancel(library.database, app.state.structuring, owner, import_id, attempt, clock.now)
    elif how == "logout":
        library.session_ended(token_hash(browser.client.cookies[SESSION_COOKIE]))
    elif how == "discard":
        discard(library.database, library.files, library.readings, owner, import_id, clock.now(), runs=library.runs)
    elif how == "delete_draft":
        with library.database.transaction() as db:
            revision = db.get(ResumeImport, import_id).revision

        delete_draft(library.database, owner, import_id, revision, clock.now, runs=library.runs)
    elif how == "shutdown":
        app.state.structuring.shutdown()


STOPS = ["expired", "run_timeout", "cancel", "logout", "discard", "delete_draft", "shutdown"]
# What a stop leaves in the import's record: its outcome, or no record (the import ended).
LEFT = {
    "expired": ("failed", "expired"),
    "run_timeout": ("failed", "run_timeout"),
    "cancel": ("failed", "cancelled"),
    "logout": ("failed", "logged_out"),
    "discard": None,
    "delete_draft": ("failed", None),
    "shutdown": ("failed", "shutdown"),
}


@pytest.mark.parametrize("how", STOPS)
def test_a_run_stopped_while_its_request_is_reserved_hands_nothing_on(live, clock, monkeypatch, how):
    provider = Structuring()
    app, browser = live(provider)
    import_id = paste(browser)

    if how == "delete_draft":
        assert browser.submit(f"/imports/{import_id}", f"/imports/{import_id}/save").status_code == 303

    gate = app.state.structuring.live.gate
    reserve = gate._reserve

    def reserve_then_stop(*args):
        reserved = reserve(*args)
        # Checked before the reservation, the import was in use and the run in time; the stop comes now.
        stop(how, app, browser, import_id, clock)

        return reserved

    monkeypatch.setattr(gate, "_reserve", reserve_then_stop)

    assert send(browser, import_id).status_code == 303
    assert provider.calls == []
    assert states_of(app) == [(LiveRequestState.RELEASED, 0)]
    assert kept(app, import_id) == []

    left = record(app, import_id)

    assert (left[:2] if left else None) == LEFT[how]


@pytest.mark.parametrize("how", ["expired", "cancel", "discard"])
def test_a_request_out_when_its_run_stops_is_settled_and_its_answer_dropped(live, clock, how):
    held = {}
    provider = Structuring(lambda: stop(how, held["app"], held["browser"], held["import"], clock))
    app, browser = live(provider)
    held.update(app=app, browser=browser)
    held["import"] = paste(browser)

    assert send(browser, held["import"]).status_code == 303
    assert len(provider.calls) == 1
    assert states_of(app) == [(LiveRequestState.SETTLED, COST)]
    assert kept(app, held["import"]) == []

    if how == "discard":
        assert record(app, held["import"]) is None
    else:
        outcome = "cancelled_sent" if how == "cancel" else f"{how}_sent"
        assert record(app, held["import"])[:2] == ("failed", outcome)

    if how == "cancel":
        assert "这次请求已经交给外部服务，可能已经产生费用" in review_text(browser, held["import"])


def test_cancelling_or_ending_an_import_answers_at_once_while_its_request_is_out(live, clock):
    out, release = threading.Event(), threading.Event()

    def hold():
        out.set()
        assert release.wait(10), "the test never released the answer"

    provider = Structuring(hold, hold)
    app, browser = live(provider, inline=False)
    first = paste(browser)

    assert send(browser, first).status_code == 303
    assert out.wait(10)

    review = browser.get(f"/imports/{first}/review").text

    assert "data-task-running" in review and "已交给服务的请求：1 次" in text_of(review)
    assert browser.get(f"/imports/{first}/structure/status").json() == {
        "stage": "running",
        "step": "由外部服务整理",
        "requests_handed": 1,
    }

    # Nothing waits for the request: the cancel is taken while it is still out.
    cancelled = browser.post(f"/imports/{first}/structure/cancel", form_for(review, f"/imports/{first}/structure/cancel").fields)

    assert cancelled.status_code == 303 and cancelled.headers["location"].endswith("notice=structuring_cancelled")

    attempt = form_for(review, f"/imports/{first}/structure/cancel").fields["attempt"]
    release.set()
    wait_until(lambda: rt(app).runs.handed(attempt) is None)

    assert browser.get(f"/imports/{first}/structure/status").json() == {"stage": "done"}
    assert kept(app, first) == [] and record(app, first)[:2] == ("failed", "cancelled_sent")

    out.clear()
    release.clear()
    second = paste(browser)

    assert send(browser, second).status_code == 303
    assert out.wait(10)

    library = rt(app)
    # Ending the import does not wait for the request either.
    discard(library.database, library.files, library.readings, owner_id(app), second, clock.now(), runs=library.runs)
    release.set()
    wait_until(lambda: len(states_of(app)) == 2 and all(state == LiveRequestState.SETTLED for state, _ in states_of(app)))

    assert kept(app, second) == [] and record(app, second) is None


# --- what came back -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "usage, state, pauses",
    [
        (Usage(1000, OUTPUT_LIMIT, 0, 0, 0, 1000 + OUTPUT_LIMIT, "default"), LiveRequestState.SETTLED, False),
        (None, LiveRequestState.UNKNOWN, True),
        (Usage(malformed=True), LiveRequestState.UNKNOWN, True),
        (Usage(1000, OUTPUT_LIMIT, 0, 0, 0, 1000 + OUTPUT_LIMIT, "flex"), LiveRequestState.UNKNOWN, True),
        (Usage(1_000_000, OUTPUT_LIMIT, 0, 0, 0, 1_000_000 + OUTPUT_LIMIT, "default"), LiveRequestState.OVER, True),
    ],
    ids=["usage", "no-usage", "malformed", "other-tier", "over-reservation"],
)
def test_an_unfinished_answer_keeps_nothing_and_the_gate_alone_says_what_it_cost(live, usage, state, pauses):
    error = AIProviderResponseError("The provider returned an unfinished response.", sent=SendState.ANSWERED, usage=usage)
    app, browser = live(Structuring(error))
    import_id = paste(browser)

    assert send(browser, import_id).status_code == 303
    assert kept(app, import_id) == []
    assert record(app, import_id)[:2] == ("failed", "provider_failed_sent")
    assert [row.state for row in requests_of(rt(app).database)] == [state]
    assert (paused(app) is not None) is pauses

    text = review_text(browser, import_id)

    # Never read as a cut-off answer: the same error comes of an error response, a refusal or no text.
    assert "外部服务没有返回可用的整理结果。这次请求已经交给外部服务" in text and "输出上限" not in text


@pytest.mark.parametrize(
    "error, outcome, state, says",
    [
        (AIProviderTimeoutError("synthetic"), "provider_timeout_sent", LiveRequestState.UNKNOWN, "没有在时限内回答"),
        (AIProviderConnectionError("synthetic"), "provider_failed_sent", LiveRequestState.UNKNOWN, "没有返回可用的整理结果"),
        (
            AIProviderResponseError("The provider returned an error response (HTTP 500).", sent=SendState.ANSWERED),
            "provider_failed_sent",
            LiveRequestState.UNKNOWN,
            "没有返回可用的整理结果",
        ),
        (AIProviderConfigError("synthetic"), "provider_config", LiveRequestState.RELEASED, "在发出之前就停止了"),
    ],
    ids=["timeout", "connection", "error-response", "config"],
)
def test_a_failed_request_is_counted_by_whether_it_went_out(live, error, outcome, state, says):
    app, browser = live(Structuring(error))
    import_id = paste(browser)

    assert send(browser, import_id).status_code == 303
    assert record(app, import_id)[:2] == ("failed", outcome)
    assert [row.state for row in requests_of(rt(app).database)] == [state]

    text = review_text(browser, import_id)

    assert says in text
    assert ("这次请求已经交给外部服务" in text) is outcome.endswith("_sent")
    assert ("没有发出请求" in text) is not outcome.endswith("_sent")


def test_while_the_gate_refuses_nothing_is_offered_and_a_confirmation_posted_anyway_sends_nothing(live):
    provider = Structuring(AIProviderTimeoutError("synthetic"))
    app, browser = live(provider)
    import_id = paste(browser)
    fields = confirmation(browser, import_id)

    assert browser.post(confirm_url(import_id), fields).status_code == 303
    assert paused(app) == "unknown_usage"

    html = browser.get(confirm_url(import_id)).text

    assert "这批批准已暂停" in text_of(html) and "确认并发给外部服务" not in html

    posted = browser.post(confirm_url(import_id), form_for(html, confirm_url(import_id)).fields)

    assert posted.status_code == 409 and len(provider.calls) == 1
    assert "发给外部服务的整理现在不可用：这批批准已暂停" in review_text(browser, import_id)


def test_another_real_task_running_keeps_a_run_from_sending(live):
    provider = Structuring()
    app, browser = live(provider)
    import_id = paste(browser)
    gate = app.state.structuring.live.gate

    with gate.task(Purpose.ANALYSIS, "another-owner", Structuring()):
        assert send(browser, import_id).status_code == 303

    assert provider.calls == [] and requests_of(rt(app).database) == []
    assert record(app, import_id)[:2] == ("failed", "gate_busy")
    assert "另一个真实任务正在进行，请等它结束后再整理。没有发出请求" in review_text(browser, import_id)


# --- frozen synthetic resumes -------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
def test_synthetic_resumes_go_through_masking_both_checks_and_review(live, case):
    provider = Structuring(*([Answer(case.answer)] if case.answer else []))
    app, browser = live(provider)
    import_id = paste(browser, case.text)
    shown = browser.get(confirm_url(import_id)).text

    assert not any(words in shown for words in case.never_sent)
    assert send(browser, import_id).status_code == 303

    [call] = provider.calls

    assert not any(words in call["prompt"] for words in case.never_sent)
    # The resume is data after the instructions; nothing of it comes before them.
    assert call["prompt"].startswith(f"{INSTRUCTIONS}\n\n{SEGMENTS_START}\n")

    state, outcome, _ = record(app, import_id)

    if case.failure:
        assert (state, outcome) == ("failed", f"{case.failure}_sent") and kept(app, import_id) == []
        return

    with rt(app).database.transaction() as db:
        rows = candidates_of(db, owner_id(app), import_id)
        problems = structuring_of(db, import_id).problems or []

    assert state == "done"
    assert set(case.kinds) <= {row.kind for row in rows}
    assert set(case.cautions) <= {caution["code"] for row in rows for caution in row.cautions}
    assert set(case.dropped) <= {problem["reason"] for problem in problems}
    # Every word kept is a quote of the import itself.
    assert all(piece["quote"] in case.text for row in rows for pieces in row.extracted.values() for piece in pieces)


# --- one confirmation, one run (C3 acceptance F1) ---------------------------------------------------


@pytest.mark.parametrize("how", ["failed", "cancelled"])
def test_a_confirmation_begins_one_run_and_another_run_takes_a_new_one(live, clock, how):
    held = {}
    script = {
        "failed": Answer(lambda prompt: "{"),
        "cancelled": lambda: stop("cancel", held["app"], held["browser"], held["import"], clock),
    }[how]
    provider = Structuring(script)
    app, browser = live(provider)
    held.update(app=app, browser=browser)
    import_id = held["import"] = paste(browser)
    fields = confirmation(browser, import_id)

    assert browser.post(confirm_url(import_id), fields).status_code == 303
    assert record(app, import_id)[:2] == ("failed", {"failed": "invalid_answer_sent", "cancelled": "cancelled_sent"}[how])

    # The same form again, by a second click or a retried request: the run it named is over, and so is it.
    replayed = browser.post(confirm_url(import_id), fields)

    assert replayed.status_code == 409 and "这份确认已经用过" in text_of(replayed.text)
    assert len(provider.calls) == 1 and len(requests_of(rt(app).database)) == 1

    # Confirmed again, as the page shows it now, it begins a new run.
    assert send(browser, import_id).status_code == 303
    assert len(provider.calls) == 2 and record(app, import_id)[0] == "done"


def test_a_confirmation_posted_again_while_its_run_goes_on_begins_nothing_more(live):
    out, release = threading.Event(), threading.Event()

    def hold():
        out.set()
        assert release.wait(10), "the test never released the answer"

    provider = Structuring(hold)
    app, browser = live(provider, inline=False)
    import_id = paste(browser)
    fields = confirmation(browser, import_id)

    assert browser.post(confirm_url(import_id), fields).status_code == 303
    assert out.wait(10)

    again = browser.post(confirm_url(import_id), fields)

    assert again.status_code == 303 and again.headers["location"].endswith("notice=structuring_running")

    release.set()
    wait_until(lambda: record(app, import_id)[0] == "done")

    assert len(provider.calls) == 1 and len(requests_of(rt(app).database)) == 1


# --- a cancel says what its request finally showed (C3 acceptance F2) -------------------------------------


@pytest.mark.parametrize(
    "error, outcome, state",
    [
        (AIProviderConfigError("synthetic: stopped before sending"), "cancelled", LiveRequestState.RELEASED),
        (AIProviderTimeoutError("synthetic"), "cancelled_sent", LiveRequestState.UNKNOWN),
    ],
    ids=["explicitly-not-sent", "perhaps-sent"],
)
def test_a_run_cancelled_while_its_request_is_out_says_what_the_request_showed(live, clock, error, outcome, state):
    held = {}

    def cancel_then_fail():
        stop("cancel", held["app"], held["browser"], held["import"], clock)
        # Written at once, while the request is out: it may have gone out.
        held["meanwhile"] = record(held["app"], held["import"])
        raise error

    app, browser = live(Structuring(cancel_then_fail))
    held.update(app=app, browser=browser)
    held["import"] = paste(browser)

    assert send(browser, held["import"]).status_code == 303
    assert held["meanwhile"][:2] == ("failed", "cancelled_sent")
    assert [row.state for row in requests_of(rt(app).database)] == [state]
    # Still cancelled: only whether it went out follows the evidence (explicitly not sent, nothing reported).
    assert record(app, held["import"])[:2] == ("failed", outcome)
    assert kept(app, held["import"]) == []

    text = review_text(browser, held["import"])

    assert "这次整理已取消。" in text
    assert ("没有发出请求" in text) is (outcome == "cancelled")
    assert ("这次请求已经交给外部服务" in text) is (outcome == "cancelled_sent")


def test_a_cancelled_runs_late_request_never_touches_the_run_after_it(live, clock):
    held = {}

    def cancel_go_on_then_fail():
        stop("cancel", held["app"], held["browser"], held["import"], clock)
        # The user goes on with the local rules while the cancelled request is still out.
        held["local"] = structure_import(rt(held["app"]).database, SimulatedStructurer(), owner_id(held["app"]), held["import"], clock.now)
        raise AIProviderConfigError("synthetic: stopped before sending")

    app, browser = live(Structuring(cancel_go_on_then_fail))
    held.update(app=app, browser=browser)
    held["import"] = paste(browser)

    assert send(browser, held["import"]).status_code == 303
    assert held["local"] == Asked.STORED
    assert states_of(app) == [(LiveRequestState.RELEASED, 0)]
    # The newer run's record and candidates stand as they were.
    assert record(app, held["import"]) == ("done", None, SIMULATION_NAME)
    assert kept(app, held["import"])


# --- the contract counts the local profile too (C3 acceptance F3) ---------------------------------------

PROJECT = {"kind": "project", "fields": [{"field": "title", "pieces": [{"segment": 1, "quote": "合成项目"}]}], "notes": []}


def answering(count: int) -> str:
    return json.dumps({"candidates": [PROJECT] * count}, ensure_ascii=False)


@pytest.mark.parametrize(
    "text, answered, kept_count",
    [
        ("姓名：林合成\n项目经历\n合成项目", MAX_CANDIDATES - 1, MAX_CANDIDATES),
        ("姓名：林合成\n项目经历\n合成项目", MAX_CANDIDATES, 0),
        ("项目经历\n合成项目", MAX_CANDIDATES, MAX_CANDIDATES),
    ],
    ids=["59-and-a-profile", "60-and-a-profile", "60-without-a-profile"],
)
def test_what_is_kept_with_the_local_profile_stays_within_the_contract(text, answered, kept_count):
    segments = [{"kind": "text", "number": 1, "text": text}]
    checked = checked_answer(answering(answered), request_for(segments), segments)

    assert len(checked.candidates) == kept_count
    # Over the contract, refused whole: never cut down to fit.
    assert checked.failure == (TOO_MANY if kept_count == 0 else None)


def test_sixty_candidates_and_the_local_profile_are_refused_whole(live):
    app, browser = live(Structuring(Answer(lambda prompt: answering(MAX_CANDIDATES))))
    import_id = paste(browser, "姓名：林合成\n项目经历\n合成项目\n")

    assert send(browser, import_id).status_code == 303
    assert kept(app, import_id) == []
    assert record(app, import_id)[:2] == ("failed", "too_many_sent")
    assert "连同本机规则整理的基本资料" in review_text(browser, import_id)
