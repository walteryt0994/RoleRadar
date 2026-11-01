"""U2-A2 parts below the pages: the outbound review, the scope of a task, the
task store, the simulated service and the switch for a real one. Synthetic
material only; the real provider is never built with a real client."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.analysis_contracts import AnalysisStatus, task_fingerprint
from app.experience_analysis import analyze_selected_experiences
from app.job_analysis import live
from app.job_analysis.runner import run_task
from app.job_analysis.scope import (
    BLOCKED,
    LEFT_OUT,
    MAX_OUTBOUND_CHARS,
    SEND,
    InputProblem,
    TaskInputs,
    build_plan,
    build_request,
)
from app.job_analysis.simulation import RECIPIENT, SimulatedProvider, parse_posting
from app.job_analysis.tasks import MAX_TASKS_PER_SESSION, Stage, Start, TaskStore
from app.outbound_review import kinds, masked_posting, review
from analysis_helpers import approved, confirmed, saved_preferences
from job_helpers import POSTING


# --- the outbound review ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, found",
    [
        ("联系我：zhang.san@example.com", ("email",)),
        ("手机 138 0013 8000，座机 021-12345678", ("phone",)),
        ("身份证 11010119900307123X", ("id_number",)),
        ("家住合成路 88 号 3栋502", ("address",)),
        ("签了保密协议，内部资料不能展示", ("confidential",)),
        ("I signed an NDA last year", ("confidential",)),
        # Harmless numbers, words that only contain a marker, and plain work stay unflagged.
        ("用 Python 清洗了 1,000 条订单数据，在实验室 302 房间开会", ()),
        ("The agenda was shared with the class; version 1.2.3", ()),
    ],
)
def test_the_outbound_review_finds_what_it_is_meant_to_find(text, found):
    assert kinds(review(text)) == found


@pytest.mark.parametrize(
    "text",
    [
        "home address: 123 Synthetic Lane, Exampleville",
        "家庭地址: 北京市合成区示例小区东园七栋五层",
        "工作地点：合成市示例路 88 号",
        "面试在合成大厦 3栋502 进行",
    ],
)
def test_an_address_stops_a_posting_and_is_never_half_masked(text):
    masked, hidden, stopping = masked_posting(text)

    # Left exactly as it was: a placeholder over a label would only hide part of it.
    assert (masked, hidden, stopping) == (text, (), ("address",))


def test_a_posting_keeps_its_text_with_contact_details_masked():
    masked, hidden, stopping = masked_posting("薪资保密。简历请发 hr@example.com，电话 13800138000。")

    assert masked == "薪资保密。简历请发 ［已隐去邮箱］，电话 ［已隐去电话号码］。"
    # "薪资保密" is how postings talk about themselves, not confidential material.
    assert (hidden, stopping) == (("email", "phone"), ())
    assert masked_posting("本岗位为内部资料，不得外传。")[2] == ("confidential",)
    # What goes out passes the review the service runs again.
    assert review(masked, posting=True) == ()
    # An identity number is masked whole, too.
    assert masked_posting("证件 11010119900307123X 已登记") == ("证件 ［已隐去证件号码］ 已登记", ("id_number",), ())


# --- the scope of a task ------------------------------------------------------------------------


def inputs(chosen, posting=POSTING, **more) -> TaskInputs:
    return TaskInputs(chosen=tuple(chosen), posting=posting, use_preferences=more.pop("use_preferences", False), **more)


def test_a_plan_decides_field_by_field_and_the_request_sends_exactly_that(database, owners, clock):
    owner = owners[0]
    item = confirmed(database, owner, clock, "project", title="合成项目", actions="电话 13800138000", outcome="合成结果", role="合成角色")
    saved_preferences(database, owner, clock, cities_ideal="上海", work_style_no="频繁出差")

    with database.transaction() as db:
        plan = build_plan(db, owner, inputs([item], use_preferences=True, left_out=frozenset({f"{item[0]}:role", "preferences:work_style_no"})))

    statuses = {field.name: field.status for field in plan.sources[0].fields}
    request = build_request(owner, plan, RECIPIENT, "1")

    assert statuses == {"title": SEND, "role": LEFT_OUT, "actions": BLOCKED, "outcome": SEND}
    assert plan.sources[0].fields[2].blocked == ("phone",)
    assert {item.field: item.status for item in plan.preferences} == {"cities_ideal": SEND, "work_style_no": LEFT_OUT}
    assert request.selection[0].fields == ("title", "outcome")
    assert [(preference.field, preference.text) for preference in request.preferences] == [("cities_ideal", "上海")]
    assert request.job_posting == plan.posting


@pytest.mark.parametrize("problem", ["nothing_left", "too_much", "repeated"])
def test_a_plan_is_refused_rather_than_cut(database, owners, clock, problem):
    owner = owners[0]
    item = confirmed(database, owner, clock, "project", title="合成项目", actions="合成内容")
    chosen = {
        "nothing_left": lambda: inputs([item], left_out=frozenset({f"{item[0]}:title", f"{item[0]}:actions"})),
        # Five full fields of 5000 characters each are more than one task may send.
        "too_much": lambda: inputs([confirmed(database, owner, clock, "project", actions="合" * 5000) for _ in range(5)]),
        "repeated": lambda: inputs([item, item]),
    }[problem]()

    with database.transaction() as db, pytest.raises(InputProblem) as refused:
        build_plan(db, owner, chosen)

    assert refused.value.where == "sources"

    if problem == "too_much":
        assert str(MAX_OUTBOUND_CHARS) in refused.value.message


# --- the task store ---------------------------------------------------------------------------------


def stored(store: TaskStore, session="s1", owner="o1"):
    return store.create(owner, session, None, None, None, "f" * 64)


def test_concurrent_starts_of_one_task_start_it_once():
    store = TaskStore()
    task = stored(store)
    outcomes, barrier = [], threading.Barrier(8)

    def begin():
        barrier.wait()
        outcomes.append(store.begin(task.id, "o1", "s1", task.fingerprint)[0])

    threads = [threading.Thread(target=begin) for _ in range(8)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert sorted(outcomes) == sorted([Start.STARTED] + [Start.ALREADY] * 7)


def test_a_late_answer_stays_out_of_a_cancelled_task():
    store = TaskStore()
    task = stored(store)
    store.begin(task.id, "o1", "s1", task.fingerprint)
    store.progress(task.id, "parsing")
    store.cancel(task.id, "o1", "s1")

    class Late:
        status, requests_sent = AnalysisStatus.COMPLETE, 2

    store.finish(task.id, Late())
    after = store.get(task.id, "o1", "s1")

    assert (after.stage, after.result, after.requests_sent, after.cancel.is_set()) == (Stage.CANCELLED, None, 2, True)


def test_a_session_keeps_a_limited_number_of_tasks_and_never_drops_a_running_one():
    ticks = iter(range(1000))
    store = TaskStore(lambda: next(ticks))
    running = stored(store)
    store.begin(running.id, "o1", "s1", running.fingerprint)
    later = [stored(store) for _ in range(MAX_TASKS_PER_SESSION + 3)]

    assert store.get(running.id, "o1", "s1").stage is Stage.RUNNING
    assert sum(store.get(task.id, "o1", "s1") is not None for task in later) == MAX_TASKS_PER_SESSION - 1
    assert store.get(later[-1].id, "o1", "s1") is not None
    # Another session of the same user sees none of them.
    assert store.get(later[-1].id, "o1", "s2") is None


# --- the simulated service ------------------------------------------------------------------------------


def test_the_simulation_answers_from_the_words_it_gets_and_its_answers_pass_the_checks(database, owners, clock):
    owner = owners[0]
    did = confirmed(database, owner, clock, "project", title="合成项目", actions="用 Python 清洗了订单数据。我没有使用 SQL。")
    planned = confirmed(database, owner, clock, "project", title="计划", actions="计划下学期学习 Python。")
    results = {}

    for name, chosen, posting in (
        ("did", did, POSTING),
        ("planned", planned, POSTING),
        ("english", did, "Required: Python and SQL.\nMust have a work authorization.\nPreferred:\n- Docker"),
    ):
        with database.transaction() as db:
            plan = build_plan(db, owner, inputs([chosen], posting))

        request = build_request(owner, plan, RECIPIENT, "1")
        result = analyze_selected_experiences(database, request, approved(request), SimulatedProvider())
        results[name] = {finding.text: finding.conclusion for finding in result.findings}

        assert result.status is AnalysisStatus.COMPLETE, (name, result.problem)

    assert results["did"]["Python"] == "supported" and results["did"]["SQL"] == "no_evidence"
    assert results["planned"]["Python"] == "no_evidence"
    # It never judges a hard condition.
    assert results["did"]["2027 年及以后毕业"] == "unknown"
    assert results["english"]["Must have a work authorization."] == "unknown" and "Docker" in results["english"]


def test_the_simulation_never_says_a_must_have_is_met():
    prompt_answer = json.loads(
        SimulatedProvider().generate_text(
            "<<<REQUIREMENTS_START>>>\n[]\n<<<REQUIREMENTS_END>>>\n<<<EXPERIENCES_START>>>\n[]\n<<<EXPERIENCES_END>>>\n"
            '<<<PREFERENCES_START>>>\n[{"preference_id": "F1", "stance": "must_have", "text": "上海"}, '
            '{"preference_id": "F2", "stance": "ideal", "text": "上海"}]\n<<<PREFERENCES_END>>>\n'
            "<<<JOB_POSTING_START>>>\n工作地点：上海\n<<<JOB_POSTING_END>>>",
            json_schema_format=type("Format", (), {"name": "experience_evidence_analysis"})(),
        ).text
    )

    assert [item["relation"] for item in prompt_answer["preferences"]] == ["unclear", "consistent"]


def test_the_simulated_parse_quotes_the_posting_word_for_word():
    parsed = json.loads(parse_posting(POSTING))
    quotes = [item["evidence_text"] for group in ("required_skills", "preferred_skills", "hard_constraints") for item in parsed[group]]

    assert [item["text"] for item in parsed["required_skills"]] == ["Python", "SQL"]
    assert [item["text"] for item in parsed["preferred_skills"]] == ["Docker"]
    assert all(quote in POSTING for quote in quotes)


# --- the switch for a real service ----------------------------------------------------------------------

READY = {
    "ROLERADAR_ANALYSIS_MODE": "live",
    "ROLERADAR_LIVE_PROVIDER": "openai",
    "ROLERADAR_LIVE_MODEL": "synthetic-model",
    "ROLERADAR_LIVE_ENDPOINT": "https://api.openai.com/v1",
    "ROLERADAR_LIVE_MAX_REQUESTS": "6",
    "ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "1500",
    "ROLERADAR_LIVE_TIMEOUT_SECONDS": "30",
    "ROLERADAR_LIVE_RULES_CHECKED_ON": "2026-10-31",
    "OPENAI_API_KEY": "synthetic-not-a-key",
}


@pytest.mark.parametrize(
    "change, says",
    [
        ({"ROLERADAR_ANALYSIS_MODE": None}, "默认只用模拟服务"),
        ({"ROLERADAR_LIVE_MODEL": None}, "ROLERADAR_LIVE_MODEL"),
        ({"ROLERADAR_LIVE_ENDPOINT": "https://gateway.example.invalid/v1"}, "官方地址"),
        ({"ROLERADAR_LIVE_PROVIDER": "unknown"}, "不认识的服务商"),
        ({"HTTPS_PROXY": "http://127.0.0.1:9"}, "HTTPS_PROXY"),
        ({"OPENAI_BASE_URL": "https://gateway.example.invalid/v1"}, "OPENAI_BASE_URL"),
        ({"OPENAI_API_KEY": None}, "凭据"),
        ({"ROLERADAR_LIVE_MAX_REQUESTS": "500"}, "请求次数上限"),
        ({"ROLERADAR_LIVE_RULES_CHECKED_ON": "soon"}, "核对日期"),
    ],
)
def test_a_real_service_is_refused_unless_everything_is_explicit_and_checked(change, says):
    environ = {**READY, **change}
    environ = {name: value for name, value in environ.items() if value is not None}

    with pytest.raises(live.LiveRefused, match=says):
        live.load_live_policy(environ)


def test_the_test_run_has_no_real_service_settings():
    with pytest.raises(live.LiveRefused):
        live.load_live_policy()


def test_a_real_provider_fixes_its_endpoint_ignores_proxies_and_never_retries():
    policy = live.load_live_policy(READY)

    with patch("app.ai_provider.OpenAI") as client, patch("app.ai_provider.DefaultHttpxClient") as http:
        provider = live.make_live_provider(policy, READY)

    options = client.call_args.kwargs

    assert (options["base_url"], options["max_retries"], options["timeout"]) == ("https://api.openai.com/v1", 0, 30.0)
    assert http.call_args.kwargs["trust_env"] is False
    assert provider.identity().endpoint == policy.recipient.endpoint == "https://api.openai.com/v1"
    assert policy.recipient.model == "synthetic-model"


def answered_by_the_rules(**request):
    """The SDK's create() with a well-formed reply, worked out by the simulation's rules; nothing leaves the process."""
    schema = SimpleNamespace(name=request["text"]["format"]["name"])

    return SimpleNamespace(
        status="completed",
        output=[],
        output_text=SimulatedProvider().generate_text(request["input"], json_schema_format=schema).text,
        model="synthetic-model-2026-10-01",
        usage=SimpleNamespace(input_tokens=100, output_tokens=50, total_tokens=150),
    )


def test_both_requests_of_a_real_task_are_capped_at_the_policy_output_limit(database, owners, clock):
    owner = owners[0]
    chosen = confirmed(database, owner, clock, "project", title="合成项目", actions="用 Python 清洗了订单数据。我没有使用 SQL。")
    policy = live.load_live_policy(READY)

    with database.transaction() as db:
        plan = build_plan(db, owner, inputs([chosen]))

    request = build_request(owner, plan, policy.recipient, "1")
    store = TaskStore()
    task = store.create(owner, "s1", None, None, request, task_fingerprint(request))

    assert store.begin(task.id, owner, "s1", task.fingerprint)[0] is Start.STARTED

    with patch("app.ai_provider.OpenAI") as client, patch("app.ai_provider.DefaultHttpxClient"):
        provider = live.make_live_provider(policy, READY)

    service = client.return_value.responses.create
    service.side_effect = answered_by_the_rules
    # The way a real run goes: the runner, the analysis service, then this provider.
    run_task(store, database, task.id, lambda _task: provider)
    result = store.get(task.id, owner, "s1").result
    sent = [call.kwargs for call in service.call_args_list]

    assert result.status is AnalysisStatus.COMPLETE, result.problem
    # Neither the parse nor the analysis asks for a limit; the provider adds the policy's to both.
    assert [each["max_output_tokens"] for each in sent] == [policy.max_output_tokens] * 2 == [1500, 1500]
    assert all(each["model"] == "synthetic-model" and each["store"] is False for each in sent)
    assert client.call_args.kwargs["base_url"] == "https://api.openai.com/v1"


def test_a_request_budget_is_taken_before_sending_and_never_overdrawn():
    budget = live.RequestBudget(3)

    assert budget.take(2) is True
    assert budget.take(2) is False and budget.used == 2
    assert budget.take(1) is True and budget.take(1) is False
