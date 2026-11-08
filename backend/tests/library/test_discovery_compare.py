"""U3-B0: the comparison of the agent and the fixed flow on the frozen synthetic tasks, offline.

Two kinds of run, both inside this process. Offline, the agent's decisions come from the simulated decider or a
script and nothing goes through a gate. The rehearsal of a real run puts a made-up model behind a real gate, under
the budget policy fixed_reserve_v1 with made-up prices: it answers each decision from a list the test gives (the
decisions of an offline run, replayed) and sends nothing anywhere. What is shown here is that the tools, the records,
the judgement and the stops work; nothing here says how a real model would decide.
"""
import json
from dataclasses import replace

import pytest

from app.ai_provider import (
    AIProvider,
    AIProviderConfigError,
    AIProviderResponseError,
    AIProviderTimeoutError,
    ProviderIdentity,
    SendState,
    request_bytes,
    responses_request,
)
from app.discovery import agent, compare, compare_tasks
from app.discovery.agent import ScriptedDecider, make_action, request_problem
from app.discovery.compare import BatchStop, EvalSending, FixedClock, Settings, hard_checks, run_batch
from app.discovery.compare_tasks import FROZEN_DIGEST, TASKS, derive_truth, seed_material, taskset_digest
from app.discovery.contracts import EndReason, Tool
from app.discovery.runner import ParentStopped
from app.discovery.synthetic import ADDRESSES, CAREERS, LEADS
from app.job_analysis.simulation import SimulatedProvider
from app.live_gate import FIXED_RESERVE_V1, LiveRefused, LiveSetup
from discovery_helpers import ref
from test_live_gate import USED, answer, budget_terms

T1, T2, T3, T4 = TASKS
NEVER_SENT = (compare_tasks.PROJECT["actions"], compare_tasks.PROJECT["outcome"], compare_tasks.PROJECT["private_note"], compare_tasks.PROJECT["reflection"])


class ReplayService(AIProvider):
    """A made-up model behind the gate: answers each decision with the next of its answers, or fails as told.

    faults maps a call's number (from 1) to the error it raises; usage is what every answer reports. sizes keeps,
    for every request that reached it, the size of the body it would send with those very arguments.
    """

    SYNTHETIC = True

    def __init__(self, answers, *, faults=None, usage=USED):
        self.answers = list(answers)
        self.faults = dict(faults or {})
        self.usage = usage
        self.calls = []
        self.sizes = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        return responses_request(
            "synthetic-model",
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            service_tier="default",
            json_schema_format=json_schema_format,
        )

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.calls.append({"prompt": prompt, "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        self.sizes.append(request_bytes(self.request_body(prompt, max_output_tokens, reasoning_effort, json_schema_format)))
        fault = self.faults.get(len(self.calls))

        if fault is not None:
            raise fault

        return replace(answer(self.usage), text=self.answers.pop(0))


@pytest.fixture
def new_folder(tmp_path):
    count = iter(range(1, 100))

    return lambda: tmp_path / f"batch-{next(count)}"


@pytest.fixture
def offline(new_folder):
    """One offline batch of the four tasks: its folder and summary."""
    folder = new_folder()

    return folder, run_batch(folder)


def record(folder, task, flow) -> dict:
    return json.loads((folder / "runs" / f"{task}-{flow}.json").read_text(encoding="utf-8"))


def replayed(folder) -> list[str]:
    """The decisions of every agent run of an offline batch, in order, as a model would answer them."""
    answers = []

    for task in TASKS:
        answers += [json.dumps(entry["action"], ensure_ascii=False) for entry in record(folder, task.id, "agent")["actions"]]

    return answers


def live(service, **terms) -> LiveSetup:
    """A made-up approval for the comparison alone, under the real budget policy, with made-up prices."""
    values = {"purposes": frozenset({"discovery"}), "max_output_tokens": 2_000, "max_requests": 40, "max_cost_micro_usd": 50_000_000}

    return LiveSetup(budget_terms(**{**values, **terms}), FIXED_RESERVE_V1, lambda: service)


# --- what is frozen -----------------------------------------------------------------------------------------------


def test_the_frozen_truth_is_the_synthetic_datas_own_and_the_digest_holds(database):
    """Each task's lists come from the data alone: worked out again here, posting by posting, they are the same."""
    material = seed_material(database, FixedClock())

    for task in TASKS:
        legal, others = derive_truth(task, database, material, FixedClock(), SimulatedProvider())

        assert legal == {posting_id: category for group, category in task.legal for posting_id in group}, task.id
        assert others == dict(task.not_results), task.id

    assert taskset_digest() == FROZEN_DIGEST


def test_the_digest_moves_with_any_frozen_part(monkeypatch):
    monkeypatch.setattr(compare_tasks, "TASKS", (replace(T1, title="改过的任务"), T2, T3, T4))
    changed_task = taskset_digest()
    monkeypatch.setattr(compare_tasks, "TASKS", TASKS)
    monkeypatch.setattr(compare_tasks, "DECISION_REQUEST_BYTES", 16_000)
    changed_setting = taskset_digest()
    monkeypatch.setattr(compare_tasks, "DECISION_REQUEST_BYTES", compare_tasks.DECISION_REQUEST_BYTES)
    # What a result's provenance is held to is frozen too.
    monkeypatch.setattr(compare_tasks.synthetic, "ADDRESSES", {**ADDRESSES, CAREERS: "https://elsewhere.invalid/postings"})
    changed_address = taskset_digest()

    assert FROZEN_DIGEST not in (changed_task, changed_setting, changed_address)
    assert len({changed_task, changed_setting, changed_address}) == 3


# --- the decider's own settings -----------------------------------------------------------------------------------


class Answering(AIProvider):
    def __init__(self, body=None):
        self.body = body
        self.asked = []

    def identity(self):
        return ProviderIdentity("synthetic", "synthetic-model", "https://synthetic.invalid/v1")

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        if self.body is None:
            return super().request_body(prompt, max_output_tokens, reasoning_effort, json_schema_format)

        return {**responses_request("synthetic-model", prompt, max_output_tokens=max_output_tokens, json_schema_format=json_schema_format), **self.body}

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.asked.append(max_output_tokens)

        return replace(answer(USED), text=json.dumps(make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束")))


def test_the_decider_keeps_the_product_defaults_and_takes_checked_settings_of_its_own():
    default = agent.ModelDecider(Answering())
    evaluation = agent.ModelDecider(Answering(), output_tokens=2_000, max_request_bytes=15_000)

    assert (default.output_tokens, default.max_request_bytes) == (agent.DECISION_OUTPUT_TOKENS, agent.MAX_DECISION_BYTES) == (600, 32_000)
    assert (evaluation.output_tokens, evaluation.max_request_bytes) == (2_000, 15_000)

    for bad in ({"output_tokens": 0}, {"output_tokens": True}, {"output_tokens": 2.5}, {"max_request_bytes": 0}, {"max_request_bytes": 32_001}):
        with pytest.raises(ValueError):
            agent.ModelDecider(Answering(), **bad)


def test_a_decisions_size_is_that_of_the_body_its_provider_would_send():
    prompt = "决策说明"
    plain = request_bytes(responses_request("synthetic-model", prompt, max_output_tokens=600, json_schema_format=agent.ACTION_FORMAT))
    padded = Answering(body={"service_tier": "default", "reasoning": {"effort": "low"}, "padding": "x" * 2_000})

    # A provider that cannot tell its body is measured as before: the product's own path is unchanged.
    assert request_problem(prompt, Answering(), max_bytes=plain) is None
    assert request_problem(prompt, Answering(), max_bytes=plain - 1).startswith("决策请求有")
    # One that can is measured on every field it would send.
    assert request_problem(prompt, padded, max_bytes=plain + 100).startswith("决策请求有")


# --- the preview and the command ------------------------------------------------------------------------------------


def test_the_preview_says_what_would_go_where_and_sends_nothing(monkeypatch, capsys):
    monkeypatch.setattr(compare, "analysis_setup", lambda environ: pytest.fail("the preview read an approval"))
    monkeypatch.setattr(compare, "run_batch", lambda *args, **kwargs: pytest.fail("the preview ran something"))

    assert compare.main([]) == 0
    text = capsys.readouterr().out

    assert FROZEN_DIGEST in text and "什么都不发送" in text
    assert "检索条件（城市、求职类型、岗位方向，以及不能接受的项）" in text and "不含：经历正文、偏好记录原文、私有备注、附件" in text
    assert "本机模拟服务" in text and "字节不是 token" in text and "不承诺费用不超过预留" in text
    assert "整批共用一个批准和一本账" in text
    assert "退出状态：0 整批跑完；2 没有运行；3 按停止条件提前停止" in text


@pytest.mark.parametrize("case", ["tracing", "exists", "inside", "digest", "no-approval"])
def test_a_run_is_refused_before_anything_is_made_or_read(monkeypatch, tmp_path, capsys, case):
    folder, environ = tmp_path / "new", {}
    argv = ["--send", str(folder), "--digest", FROZEN_DIGEST]

    if case == "tracing":
        environ = {"LANGSMITH_TRACING": "true"}
    elif case == "exists":
        folder.mkdir()
    elif case == "inside":
        folder = compare.REPOSITORY / "backend" / "a-new-folder"
        argv[1] = str(folder)
    elif case == "digest":
        argv[3] = "0" * 64

    if case != "no-approval":
        monkeypatch.setattr(compare, "analysis_setup", lambda environ: pytest.fail("an approval was read"))

    assert compare.main(argv, environ=environ) == 2
    assert "没有运行" in capsys.readouterr().err
    assert folder.exists() is (case == "exists")


def test_an_approval_for_other_purposes_is_refused_before_anything_is_made(new_folder):
    folder = new_folder()

    with pytest.raises(LiveRefused):
        run_batch(folder, live=live(ReplayService([]), purposes=frozenset({"discovery", "analysis"})))

    assert not folder.exists()


class Failing:
    """A decider that fails in a way no tool or service error describes: a fault of its own."""

    kind = "scripted"

    def decide(self, observation):
        raise RuntimeError("合成的内部错误")


class Rambling:
    """A decider that never says an action: each run ends on refused actions, and two in a row stop the batch."""

    kind = "scripted"

    def decide(self, observation):
        return "这不是一个动作"


@pytest.mark.parametrize(
    "decider, status, said", [(None, 0, "已写入"), (Rambling, 3, "批次提前停止"), (Failing, 4, "有运行失败")], ids=["done", "stopped", "failed"]
)
def test_the_command_exits_by_how_the_batch_ended_and_always_leaves_the_report(monkeypatch, tmp_path, capsys, decider, status, said):
    if decider is not None:
        monkeypatch.setattr(compare, "SimulatedDecider", decider)

    folder = tmp_path / "batch"

    assert compare.main(["--offline", str(folder)], environ={}) == status
    printed = capsys.readouterr()
    assert said in (printed.out if status == 0 else printed.err)
    assert (folder / "report.md").is_file() and (folder / "batch.json").is_file()


# --- an offline batch -----------------------------------------------------------------------------------------------


def test_an_offline_batch_runs_both_flows_of_each_task_into_records_and_a_report(offline):
    folder, summary = offline

    assert sorted(path.name for path in (folder / "runs").iterdir()) == sorted(f"{task.id}-{flow}.json" for task in TASKS for flow in ("agent", "baseline"))
    assert (summary["mode"], summary["stopped"], summary["failed"], summary["not_run"], summary["taskset_digest"]) == ("offline", None, [], [], FROZEN_DIGEST)
    assert "这是离线运行" in summary["conclusion"] and "不代表真实模型的表现" in summary["conclusion"]
    assert "# U3-B 合成集对照记录" in (folder / "report.md").read_text(encoding="utf-8")

    for task in TASKS:
        base, agent_run = record(folder, task.id, "baseline"), record(folder, task.id, "agent")

        for run in (base, agent_run):
            assert run["hard_problems"] == [] and run["error"] is None and run["ledger"] == [] and run["decisions"] == []
            assert run["status"] == "finished" and [posting["posting_id"] for posting in run["postings"]] == run["found"]
            assert run["analysis_recipient"] == {"provider": "simulation", "model": "roleradar-rules-1", "endpoint": None}
            assert {result["category"] for result in run["results"]} <= {"partial", "conflict"}

        assert base["decider"] is None and base["actions"] == [] and agent_run["decider"] == "simulated"
        # Both flows of a task start from the same confirmation, each from a run of its own.
        assert base["confirmation_fingerprint"] == agent_run["confirmation_fingerprint"]

    # The fixed flow searches once and adjusts nothing; on the senior-first page it ends with nothing analysed.
    (t3,) = [item for item in summary["comparisons"] if item["task"] == "T3"]
    assert (t3["baseline"]["valid_results"], t3["agent"]["valid_results"], t3["agent_better"]) == (0, 3, True)
    assert [evidence["kind"] for evidence in t3["adjustments"]] == ["检索调整"]


def test_hard_checks_catch_what_a_run_must_never_do(offline):
    folder, _ = offline
    good = record(folder, "T1", "agent")
    first = good["results"][0]

    def broken(**changes):
        return hard_checks(T1, {**good, **changes})

    assert hard_checks(T1, good) == []
    assert any("不应作为结果的岗位成了结果：c-closed（核对时已关闭）" in problem for problem in broken(results=[{**first, "posting_id": "c-closed"}]))
    assert any("按规则应为" in problem for problem in broken(results=[{**first, "category": "strong"}]))
    assert first["posting_id"] == "c-bj-analyst"
    assert any("同一个岗位有两条结果" in problem for problem in broken(results=[first, {**first, "posting_id": "c-bj-analyst-repost"}]))
    assert any("不能追溯" in problem for problem in broken(results=[{**first, "checked_at": None}]))
    assert any("没有合适结果" in problem for problem in broken(end="no_results", results=[], unsearched=["上海 · 实习 · 数据分析"]))
    assert any("越界的检索：城市 深圳" in problem for problem in broken(searches=[{**good["searches"][0], "filters": {"city": "深圳", "job_type": "实习", "direction": None}}]))


# What a forged result would show instead of its posting's own (U3-B0 R1, F1).
FORGED = {
    "source_id": "unregistered-source",
    "source": "别的来源",
    "evidence": "lead",
    "title": "改过的岗位名称",
    "company": "改过的公司",
    "url": "https://not-the-source.invalid/invented",
    "apply_url": "https://not-the-source.invalid/invented/apply",
    "content_sha256": "0" * 64,
}


def test_a_result_shows_its_postings_frozen_provenance_and_the_runs_own_read_and_check(offline):
    folder, _ = offline
    good = record(folder, "T1", "agent")
    first = good["results"][0]
    posting_id = first["posting_id"]
    (seen,) = [posting for posting in good["postings"] if posting["posting_id"] == posting_id]
    frozen = T1.provenance(posting_id)

    # A result shows what the frozen data has for its posting, and what the run itself read and checked of it.
    assert {name: first[name] for name in compare.PROVENANCE_LABELS} == {name: getattr(frozen, name) for name in compare.PROVENANCE_LABELS}
    assert (seen["read"]["content_sha256"], seen["check"]["status"]) == (frozen.content_sha256, "open")
    assert hard_checks(T1, good) == []

    def with_result(**changes):
        return hard_checks(T1, {**good, "results": [{**first, **changes}, *good["results"][1:]]})

    for name, label in compare.PROVENANCE_LABELS.items():
        assert f"{posting_id} 的{label}和冻结数据对不上" in with_result(**{name: FORGED[name]}), name

    # The run's own read and check of that very posting: another read, no answer that it is open, or none at all.
    others = [posting for posting in good["postings"] if posting["posting_id"] != posting_id]
    unread = hard_checks(T1, {**good, "postings": [{**seen, "read": {**seen["read"], "content_sha256": "1" * 64}}, *others]})
    unknown = hard_checks(T1, {**good, "postings": [{**seen, "check": {**seen["check"], "status": "unknown"}}, *others]})
    unchecked = hard_checks(T1, {**good, "postings": [{**seen, "check": None}, *others]})

    assert f"{posting_id} 的链接、读取时间或内容指纹和这次运行的读取记录对不上" in unread
    assert f"{posting_id} 没有在这次运行里核对为仍在招聘，或核对时间对不上" in unknown
    assert f"{posting_id} 没有在这次运行里核对为仍在招聘，或核对时间对不上" in unchecked
    assert f"{posting_id} 不是这次运行在这个来源找到的岗位" in hard_checks(T1, {**good, "postings": others})


def test_nothing_suitable_holds_only_with_every_posting_found_concluded_and_nothing_left_untaken(offline):
    folder, _ = offline

    # The positive cases: nothing suitable in Guangzhou, said by both flows; and nothing suitable in what the fixed
    # flow searched of T3, the rest unchecked. Every posting they found was outside the scope.
    for task, flow, end in ((T4, "baseline", "no_results"), (T4, "agent", "no_results"), (T3, "baseline", "partly_no_results")):
        run = record(folder, task.id, flow)

        assert (run["end"], run["results"], run["hard_problems"]) == (end, [], [])
        assert run["found"] and set(run["found"]) == set(run["unlisted"]["out_of_scope"])

    empty = record(folder, "T4", "baseline")
    claim = "写了“没有合适结果”，但"

    def problems(**changes):
        return hard_checks(T4, {**empty, **changes})

    # A posting found and left with no conclusion: a lead, or stopped by the outbound rules.
    lead = problems(found=[*empty["found"], "l-sh-analyst"], leads=[{"posting_id": "l-sh-analyst", "why": "没有分析"}])
    blocked = problems(found=[*empty["found"], "c-address"], unlisted={**empty["unlisted"], "blocked": ["c-address"]})

    assert f"{claim}有 1 个找到的岗位没有结论（l-sh-analyst）" in lead
    assert f"{claim}有 1 个找到的岗位没有结论（c-address）" in blocked
    # What came back and could not be taken.
    assert f"{claim}来源返回的岗位里有 1 个无法核验" in problems(unverifiable=1)
    assert f"{claim}来源返回的记录里有 2 条不属于该来源（来源异常）" in problems(foreign=2)

    # No search that answered: nothing covered, the sources failed. The combinations left are worked out again from
    # the searches, never taken from the record alone.
    failed = [{**search, "outcome": "failed"} for search in empty["searches"]]
    left = list(compare._unsearched(T4.scope, failed))

    assert {f"{claim}没有一次检索成功（来源失败）", f"{claim}还有没查的组合"} <= set(problems(searches=failed, unsearched=left))
    assert "记录的没查组合和检索记录对不上" in problems(searches=failed)

    # Nothing suitable in what was searched needs the same, and combinations left.
    partly = record(folder, "T3", "baseline")
    every = [
        {**partly["searches"][0], "filters": {"city": city, "job_type": job_type, "direction": "数据分析"}}
        for city in ("北京", "上海")
        for job_type in ("实习", "校招")
    ]

    assert "写了“已查查询无结果，仍有范围未核验”，但有 1 个找到的岗位没有结论（p-bj-intern-1）" in hard_checks(
        T3, {**partly, "found": [*partly["found"], "p-bj-intern-1"]}
    )
    assert "写了“已查查询无结果，仍有范围未核验”，但没有没查的组合" in hard_checks(T3, {**partly, "searches": every, "unsearched": []})


def test_an_adjustment_counts_only_with_feedback_a_changed_choice_and_the_action_done(offline):
    folder, _ = offline
    senior_first = record(folder, "T3", "agent")
    (evidence,) = compare.adjustments(senior_first)

    assert evidence["feedback"] == "第 1 步检索新找到 5 个岗位，都是资深或超出条件"
    assert evidence["choice"] == "第 2 步改了检索词" and evidence["done"] == "第 2 步检索已执行"

    # The same next search after results it could use is going on, not adjusting.
    usable = [{**senior_first["steps"][0], "new": [{**item, "screen": "in", "senior": False} for item in senior_first["steps"][0]["new"]]}, *senior_first["steps"][1:]]
    assert compare.adjustments({**senior_first, "steps": usable}) == []
    # Nor is a second search unchanged, or a run that searched once and read what it found.
    same = [{**entry, "action": {**entry["action"], "keywords": "数据分析"}} if entry["number"] == 2 else entry for entry in senior_first["actions"]]
    assert compare.adjustments({**senior_first, "actions": same}) == []
    assert compare.adjustments(record(folder, "T1", "agent")) == []


def search(source, city="上海", note="检索"):
    return make_action(Tool.SEARCH, source_id=source, keywords="数据分析", city=city, job_type="实习", direction="数据分析", note=note)


def test_reading_the_official_posting_a_lead_points_to_counts_only_when_the_lead_showed_it(new_folder):
    lead_first = [search(LEADS), make_action(Tool.READ, postings=[ref("c-sh-analyst")], note="读线索指向的官方岗位"), make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束")]
    folder = new_folder()
    run_batch(folder, decider_for=lambda: ScriptedDecider(lead_first), tasks=(T2,))
    (evidence,) = compare.adjustments(record(folder, "T2", "agent"))

    assert evidence == {
        "kind": "线索到官方岗位",
        "feedback": "第 1 步找到线索 l-sh-analyst，指向官方岗位 c-sh-analyst",
        "choice": "第 2 步读取了 c-sh-analyst",
        "done": "第 2 步读取已执行",
    }

    # Found by a search of its own first, the official posting's reading owes nothing to the lead.
    official_first = [search(CAREERS), search(LEADS), make_action(Tool.READ, postings=[ref("c-sh-analyst")], note="读"), make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束")]
    folder = new_folder()
    run_batch(folder, decider_for=lambda: ScriptedDecider(official_first), tasks=(T2,))

    assert compare.adjustments(record(folder, "T2", "agent")) == []


def test_an_attempt_outside_the_scope_is_counted_as_the_models_and_as_blocked_never_as_done(new_folder):
    """A scripted probe: the decider asks for Shenzhen, which the user cannot accept (its premise is set by the test)."""
    probe = [search(CAREERS, city="深圳", note="脚本探针：查深圳"), make_action(Tool.FINISH, claim=EndReason.ENOUGH, note="结束")]
    folder = new_folder()
    summary = run_batch(folder, decider_for=lambda: ScriptedDecider(probe), tasks=(T1,))
    probed = record(folder, "T1", "agent")

    assert [step["outcome"] for step in probed["steps"]] == ["refused", "done"]
    assert probed["searches"] == [] and probed["hard_problems"] == []
    assert {key: probed["metrics"][key] for key in ("out_of_scope_attempts", "out_of_scope_blocked", "out_of_scope_executed")} == {
        "out_of_scope_attempts": 1,
        "out_of_scope_blocked": 1,
        "out_of_scope_executed": 0,
    }
    assert summary["stopped"] is None


# --- a rehearsal of a real run: a made-up model behind a real gate ----------------------------------------------------


def test_a_rehearsal_through_the_gate_replays_the_offline_decisions_and_keeps_analyses_local(offline, new_folder):
    offline_folder, offline_summary = offline
    answers = replayed(offline_folder)
    service = ReplayService(answers)
    folder = new_folder()
    summary = run_batch(folder, live=live(service))

    assert (summary["mode"], summary["stopped"], summary["not_run"]) == ("rehearsal", None, [])
    assert "这是合成服务演练" in summary["conclusion"] and "不代表真实模型的表现" in summary["conclusion"]
    assert summary["decision_recipient"] == {"provider": "synthetic", "model": "synthetic-model", "endpoint": "https://synthetic.invalid/v1"}
    assert len(service.calls) == len(answers) == summary["gate"]["requests_used"]

    for task in TASKS:
        rehearsed, alone = record(folder, task.id, "agent"), record(offline_folder, task.id, "agent")
        same = lambda run: [(step["tool"], step["outcome"], step["summary"]) for step in run["trace"]]

        # The same decisions through the gate take the very same steps, to the same results.
        assert same(rehearsed) == same(alone) and rehearsed["results"] == alone["results"]
        # Only decisions went through the gate, one ledger row each, all for the search's purpose; analyses stayed here.
        assert len(rehearsed["ledger"]) == len(rehearsed["decisions"]) == rehearsed["decision_count"]
        assert {row["purpose"] for row in rehearsed["ledger"]} <= {"discovery"}
        assert rehearsed["local_analysis_requests"] == alone["local_analysis_requests"]
        assert record(folder, task.id, "baseline")["ledger"] == []

        for entry in rehearsed["decisions"]:
            assert entry["status"] == "answered" and entry["answer"] and not entry["prompt_truncated"]
            assert entry["max_output_tokens"] == 2_000 and entry["request_bytes"] <= 15_000
            assert not any(text in entry["prompt"] for text in NEVER_SENT)

    # What was checked before sending is what was sent: the very body, every field, request by request.
    checked = [entry["request_bytes"] for task in TASKS for entry in record(folder, task.id, "agent")["decisions"]]
    assert checked == service.sizes
    assert {call["max_output_tokens"] for call in service.calls} == {2_000}
    assert {call["reasoning_effort"] for call in service.calls} == {"low"}
    # The comparisons come out the same as offline, the gate's own counts aside.
    assert [(item["agent"]["valid_results"], item["baseline"]["valid_results"]) for item in summary["comparisons"]] == [
        (item["agent"]["valid_results"], item["baseline"]["valid_results"]) for item in offline_summary["comparisons"]
    ]


def stopped_batch(new_folder, service, **terms):
    folder = new_folder()

    return folder, run_batch(folder, live=live(service, **terms))


def test_the_four_tasks_share_one_allowance_a_task_never_gets_a_fresh_one(offline, new_folder):
    service = ReplayService(replayed(offline[0]))
    folder, summary = stopped_batch(new_folder, service, max_requests=10)
    first, second = record(folder, "T1", "agent"), record(folder, "T2", "agent")

    # T1 used 8 of the 10; T2 had what was left, 2, and its third decision was kept out.
    assert (len(first["ledger"]), len(second["ledger"]), len(service.calls)) == (8, 2, 10)
    assert summary["gate"]["requests_used"] == 10 and second["end"] == EndReason.GATE.value
    assert summary["stopped"] == "闸门拦下了决策请求（requests）"
    assert summary["not_run"] == ["T3", "T4"]
    assert "批次没有跑完" in summary["conclusion"]


def test_the_budget_is_one_for_the_whole_batch(offline, new_folder):
    # Room for ten decisions: each reserved at 16,000 x 10 + 2,000 x 20 millionths, and accounted at USED's 946.
    service = ReplayService(replayed(offline[0]))
    folder, summary = stopped_batch(new_folder, service, max_cost_micro_usd=200_000 + 946 * 9)

    assert len(service.calls) == 10 and summary["stopped"] == "闸门拦下了决策请求（cost）"
    assert (len(record(folder, "T1", "agent")["ledger"]), len(record(folder, "T2", "agent")["ledger"])) == (8, 2)
    assert summary["not_run"] == ["T3", "T4"]


@pytest.mark.parametrize(
    "fault, status",
    [
        (AIProviderResponseError("The provider returned an unfinished response.", sent=SendState.ANSWERED), "error_AIProviderResponseError"),
        (AIProviderTimeoutError("The provider request timed out."), "error_AIProviderTimeoutError"),
    ],
    ids=["unfinished-answer", "timeout"],
)
def test_a_decision_without_a_usable_answer_stops_the_batch_and_nothing_more_goes_out(offline, new_folder, fault, status):
    service = ReplayService(replayed(offline[0]), faults={3: fault})
    folder, summary = stopped_batch(new_folder, service)
    first = record(folder, "T1", "agent")

    assert len(service.calls) == 3
    assert [entry["status"] for entry in first["decisions"]] == ["answered", "answered", status]
    assert first["end"] == EndReason.CANCELLED.value
    assert summary["stopped"] == f"服务没有给出可用的回答（{type(fault).__name__}）"
    assert summary["not_run"] == ["T2", "T3", "T4"]
    # The task's fixed flow ran before; no flow of a later task started.
    assert sorted(path.name for path in (folder / "runs").iterdir()) == ["T1-agent.json", "T1-baseline.json"]


@pytest.mark.parametrize(
    "usage, paused",
    [
        # Priced, and above the 200,000 reserved: 30,000 input (40 cached, 10 written) and 5,000 output come to 219,866.
        (replace(USED, input_tokens=30_000, output_tokens=5_000, total_tokens=35_000), "over_reservation"),
        # Beyond the input the approval's prices were checked for (32,000): not priced, so not known.
        (replace(USED, input_tokens=60_000, total_tokens=60_030), "unknown_usage"),
    ],
    ids=["above-the-reservation", "beyond-the-priced-range"],
)
def test_usage_the_reservation_does_not_cover_pauses_the_approval_and_stops_the_batch_at_once(offline, new_folder, usage, paused):
    service = ReplayService(replayed(offline[0]), usage=usage)
    folder, summary = stopped_batch(new_folder, service)
    (entry,) = record(folder, "T1", "agent")["decisions"]

    assert len(service.calls) == 1 and entry["paused"] == summary["gate"]["paused"] == paused
    assert summary["stopped"] == f"批准已暂停（{paused}）" and summary["not_run"] == ["T2", "T3", "T4"]


def test_a_decision_over_the_evaluations_limit_is_never_sent_nor_reserved(offline, new_folder):
    service = ReplayService(replayed(offline[0]))
    folder = new_folder()
    summary = run_batch(folder, live=live(service), settings=Settings(request_bytes=1_000))
    first = record(folder, "T1", "agent")

    assert service.calls == [] and first["ledger"] == [] and first["end"] == EndReason.GATE.value
    assert first["actions"][0]["status"] == "not_sent" and first["actions"][0]["reason"].endswith("超过 1000 字节的上限")
    assert summary["stopped"].startswith("决策请求没有发出") and summary["not_run"] == ["T2", "T3", "T4"]


def test_nothing_is_handed_on_once_the_batch_has_stopped():
    class Run:
        def __init__(self, stopping):
            self._stopping = stopping

        def stopping(self):
            return self._stopping

    batch = BatchStop()
    EvalSending(batch, Run(False)).hand_on(None)

    with pytest.raises(ParentStopped):
        EvalSending(batch, Run(True)).hand_on(None)

    batch.stop("测试", cancel=False)

    with pytest.raises(ParentStopped):
        EvalSending(batch, Run(False)).hand_on(None)


# --- a run that fails, and making ready that fails (U3-B0 R1, F2) -------------------------------------------------


def test_a_failed_run_is_never_not_worse_even_with_as_many_valid_results(offline):
    folder, _ = offline
    base, agent_run = record(folder, "T4", "baseline"), record(folder, "T4", "agent")

    assert (base["metrics"]["valid_results"], agent_run["metrics"]["valid_results"]) == (0, 0)
    assert compare.compare(T4, base, agent_run)["agent_not_worse"] is True

    # Either flow failing leaves nothing to compare, however many valid results there are.
    agent_failed = compare.compare(T4, base, {**agent_run, "status": "failed", "end": None})
    base_failed = compare.compare(T4, {**base, "status": "failed", "end": None}, agent_run)

    assert (agent_failed["comparable"], agent_failed["agent_not_worse"], agent_failed["agent_better"]) == (False, False, False)
    assert (base_failed["comparable"], base_failed["agent_not_worse"]) == (False, False)


def test_a_run_that_fails_keeps_what_it_had_done_is_never_compared_and_stops_the_batch(new_folder):
    failing = [search(CAREERS), make_action(Tool.READ, postings=[ref("c-sh-analyst")], note="读取"), RuntimeError("合成的内部错误")]
    folder = new_folder()
    summary = run_batch(folder, decider_for=lambda: ScriptedDecider(failing))
    failed = record(folder, "T1", "agent")

    # It never ended and says so: no end, and no conclusion made up; what it had done stays.
    assert (failed["status"], failed["end"], failed["error"]) == ("failed", None, "运行出错（RuntimeError）")
    assert (failed["results"], failed["leads"], failed["unlisted"]) == ([], [], {})
    assert [step["tool"] for step in failed["steps"]] == [step["tool"] for step in failed["trace"]] == ["search_postings", "get_posting"]
    assert [entry["status"] for entry in failed["actions"]] == ["decided", "decided", "failed_RuntimeError"]
    assert failed["decision_count"] == 3 and len(failed["searches"]) == 1
    (read,) = [posting for posting in failed["postings"] if posting["posting_id"] == "c-sh-analyst"]
    assert read["read"]["content_sha256"] == T1.provenance("c-sh-analyst").content_sha256 and read["check"] is None
    assert failed["hard_problems"] == [] and failed["metrics"]["status"] == "failed"

    # The batch stopped there: no later task started, and a failed run is never "not worse".
    assert summary["stopped"] == "T1 的Agent没有正常运行：运行出错（RuntimeError）"
    assert summary["failed"] == ["T1 的Agent：运行出错（RuntimeError）"] and summary["not_run"] == ["T2", "T3", "T4"]
    assert sorted(path.name for path in (folder / "runs").iterdir()) == ["T1-agent.json", "T1-baseline.json"]
    (item,) = summary["comparisons"]
    assert (item["comparable"], item["agent_not_worse"], item["agent_better"]) == (False, False, False)
    assert summary["conclusion"].startswith("运行失败")

    text = (folder / "report.md").read_text(encoding="utf-8")
    assert "- Agent：运行失败（运行出错（RuntimeError）），没有结束原因，不参与对照。失败前已记录：2 步，检索 1 次" in text
    assert "- 对照：无法对照（有运行失败）" in text and "- 运行失败：T1 的Agent：运行出错（RuntimeError）" in text


def test_a_run_failing_through_the_gate_keeps_its_ledger_and_nothing_more_goes_out(offline, new_folder):
    service = ReplayService(replayed(offline[0]), faults={3: RuntimeError("合成的内部错误")})
    folder, summary = stopped_batch(new_folder, service)
    failed = record(folder, "T1", "agent")

    assert len(service.calls) == 3
    assert [entry["status"] for entry in failed["decisions"]] == ["answered", "answered", "failed_RuntimeError"]
    assert (failed["status"], failed["end"], failed["error"], len(failed["steps"])) == ("failed", None, "运行出错（RuntimeError）", 2)
    # Its ledger stays: two settled from their usage, the third, which may have gone out, kept apart as not known.
    assert [row["state"] for row in failed["ledger"]] == ["settled", "settled", "unknown"]
    used = failed["metrics"]
    assert (used["gate_requests"], used["sent_requests"], used["charged_micro_usd"], used["unknown_requests"]) == (3, 3, 946 * 2, 1)
    assert used["unknown_micro_usd"] == failed["ledger"][2]["charged_micro_usd"] > 0
    # Nothing is left reserved, and nothing more went out: the batch stopped at the failure.
    assert (summary["gate"]["requests_used"], summary["gate"]["reserved_micro_usd"]) == (3, 0)
    assert summary["stopped"] == "决策请求失败（RuntimeError）" and summary["not_run"] == ["T2", "T3", "T4"]
    assert summary["failed"] == ["T1 的Agent：运行出错（RuntimeError）"]
    assert f"闸门请求 3（已发出或可能已发出 3），核算 1892，费用待核实 {used['unknown_micro_usd']}（1 次）" in (folder / "report.md").read_text(encoding="utf-8")


class Elsewhere(ReplayService):
    """A made-up model the approval does not name: the gate lets no run of it start."""

    def identity(self):
        return ProviderIdentity("synthetic", "another-model", "https://synthetic.invalid/v1")


def test_a_run_the_gate_does_not_let_start_fails_sends_nothing_and_stops_the_batch(new_folder):
    service = Elsewhere([])
    folder, summary = stopped_batch(new_folder, service)
    refused = record(folder, "T1", "agent")

    assert service.calls == [] and refused["ledger"] == [] and refused["decisions"] == [] and refused["steps"] == []
    assert (refused["status"], refused["end"]) == ("failed", None)
    assert refused["error"] == "闸门没有放行这次运行（这个服务商不是批准里写明的接收方。）"
    assert summary["failed"] == [f"T1 的Agent：{refused['error']}"] and summary["not_run"] == ["T2", "T3", "T4"]
    assert summary["gate"]["requests_used"] == 0 and summary["conclusion"].startswith("运行失败")
    assert "- Agent：运行失败（闸门没有放行这次运行" in (folder / "report.md").read_text(encoding="utf-8")


def test_a_service_not_made_stops_the_batch_before_any_run_and_still_leaves_a_report(new_folder):
    def unmade():
        raise AIProviderConfigError("合成的服务没能建好")

    folder = new_folder()
    summary = run_batch(folder, live=LiveSetup(live(ReplayService([])).terms, FIXED_RESERVE_V1, unmade))

    assert summary["failed"] == [summary["stopped"]] == ["准备评估时出错（AIProviderConfigError）"]
    assert summary["not_run"] == ["T1", "T2", "T3", "T4"] and summary["comparisons"] == []
    assert list((folder / "runs").iterdir()) == []
    # The gate opened on the folder's own ledger and closed with nothing reserved or sent.
    assert (summary["gate"]["requests_used"], summary["gate"]["reserved_micro_usd"]) == (0, 0)
    assert summary["conclusion"].startswith("运行失败")
    assert "- 运行失败：准备评估时出错（AIProviderConfigError）" in (folder / "report.md").read_text(encoding="utf-8")


def test_a_folder_that_exists_is_never_run_into(new_folder):
    folder = new_folder()
    folder.mkdir()

    with pytest.raises(FileExistsError):
        run_batch(folder)

    assert list(folder.iterdir()) == []


NETWORK_CLIENTS = {"socket", "ssl", "http", "urllib", "requests", "httpx", "httpx2", "aiohttp", "openai"}
# Pages, the app, and the stores and runners of tasks: the comparison runs its own batch, outside all of them.
NOT_FOR_THE_COMPARISON = {"app.web_main", "app.discovery.web", "app.job_analysis.web", "app.discovery.tasks", "app.job_analysis.runner", "app.job_analysis.tasks"}


def test_the_comparison_has_no_network_client_of_its_own_and_leaves_the_pages_alone():
    """Only the service an approval names may send, through the gate; the A3 pages stay the simulation's alone."""
    for name in ("compare", "compare_tasks"):
        imported = _imports(name)

        assert not {module.split(".")[0] for module in imported} & NETWORK_CLIENTS, name
        assert not imported & NOT_FOR_THE_COMPARISON, name


def _imports(name: str) -> set[str]:
    import ast
    from pathlib import Path

    tree = ast.parse((Path(compare.__file__).parent / f"{name}.py").read_text(encoding="utf-8"))
    found = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}

    return found | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
