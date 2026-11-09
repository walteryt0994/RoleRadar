"""U3-C4-0: the validation command of the direction workflow, offline and through a gate with a made-up service.

Nothing here reaches a real service. Offline, the local simulation answers each frozen case. The rehearsals put a
made-up model behind a real gate, under the budget policy fixed_reserve_v1: with made-up prices it is a rehearsal;
with the prices an Owner would write, read by the app's own parser, only the provider is a stand-in, made where the
real one would be (app.job_analysis.live.make_live_provider). What is shown is that the cases, the records, the
counts, the automatic checks and the stops work, and that nothing passes for a semantic verdict; nothing here says how
a real model answers.
"""
import json
import uuid
from dataclasses import replace
from pathlib import Path

import openai
import pytest

from app.ai_provider import AIProvider, AIProviderTimeoutError, OpenAIProvider, ProviderIdentity, responses_request
from app.directions import live_cases as frozen
from app.directions import live_check
from app.directions.contracts import Direction, ExplorationResult, IdealLink, Quote, Relation
from app.directions.explore import OUTPUT_FORMAT, UNKNOWN_ONLY
from app.job_analysis import live as live_config
from app.job_analysis.simulation import SimulatedProvider
from app.library.db import Database, make_engine
from app.library.models import LiveRequest
from app.live_gate import FIXED_RESERVE_V1, LiveGate, LiveRefused, LiveSetup
from sqlalchemy import select
from test_live_gate import USED, answer, budget_terms

D1, D2, D3, D4 = frozen.CASES


@pytest.fixture(autouse=True)
def no_service_client(monkeypatch):
    """Building a real service's client fails the test: nothing here may reach one."""

    def refuse(*args, **kwargs):
        raise AssertionError("a service client was built")

    monkeypatch.setattr(openai.OpenAI, "__init__", refuse)
    monkeypatch.setattr(openai.AsyncOpenAI, "__init__", refuse)


class Stand(AIProvider):
    """A made-up model behind the gate. Each request is answered as the local simulation answers it, unless the test
    says otherwise for that request (by its number, from 1): another answer, an error to raise, another usage, or
    another model named in the answer. after(number) runs once a request is answered. Nothing goes anywhere."""

    SYNTHETIC = True

    def __init__(self, identity=("synthetic", "synthetic-model", "https://synthetic.invalid/v1"), *, answers=None, faults=None, usages=None, answered_as=None, after=None):
        self._identity = identity
        self.answers = dict(answers or {})
        self.faults = dict(faults or {})
        self.usages = dict(usages or {})
        self.answered_as = dict(answered_as or {})
        self.after = after
        self.calls = []

    def identity(self):
        return ProviderIdentity(*self._identity)

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        return responses_request(
            self._identity[1],
            prompt,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            service_tier="default",
            json_schema_format=json_schema_format,
        )

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None, timeout_seconds=None):
        self.calls.append({"prompt": prompt, "max_output_tokens": max_output_tokens, "reasoning_effort": reasoning_effort})
        number = len(self.calls)

        if number in self.faults:
            raise self.faults[number]

        text = self.answers.get(number) or SimulatedProvider().generate_text(prompt, json_schema_format=json_schema_format).text
        provider, model = self.answered_as.get(number, self._identity[:2])
        result = replace(answer(self.usages.get(number, USED)), text=text, provider=provider, requested_model=model, model=model)

        if self.after is not None:
            self.after(number)

        return result


def rehearsal(service, **terms) -> LiveSetup:
    """A made-up approval for direction alone, under the real budget policy, with made-up prices."""
    values = {
        "purposes": frozenset({"direction"}),
        "max_output_tokens": 8_000,
        "max_requests": 4,
        "max_cost_micro_usd": 1_300_000,
        "timeout_seconds": 120.0,
    }

    return LiveSetup(budget_terms(**{**values, **terms}), FIXED_RESERVE_V1, lambda: service)


def kept(folder: Path, case: str) -> dict:
    return json.loads((folder / "cases" / f"{case}.json").read_text(encoding="utf-8"))


def ledger(folder: Path) -> list[LiveRequest]:
    engine = make_engine(folder / live_check.EVALUATION_DB)

    try:
        with Database(engine).transaction() as db:
            return list(db.scalars(select(LiveRequest).order_by(LiveRequest.reserved_at)))
    finally:
        engine.dispose()


@pytest.fixture
def offline(tmp_path):
    """One offline run of the four cases: its folder and summary."""
    folder = tmp_path / "offline"

    return folder, live_check.run_batch(folder)


# --- what is frozen --------------------------------------------------------------------------------------------------


def test_the_digest_holds_and_moves_with_any_frozen_part(monkeypatch):
    assert frozen.caseset_digest() == frozen.FROZEN_DIGEST
    changed = []

    for target, name, value in (
        (frozen, "NOTICE", frozen.NOTICE[:-1]),
        (frozen, "CASES", (replace(D1, preferences={**D1.preferences, "directions_no": "运营"}), D2, D3, D4)),
        (frozen, "OUTPUT_TOKENS", 4_000),
        (frozen, "CASE_REVIEW", {**frozen.CASE_REVIEW, "D3": ()}),
        (frozen, "SCORE_WORDS", frozen.SCORE_WORDS[1:]),
        (frozen.explore, "INSTRUCTIONS", frozen.explore.INSTRUCTIONS + "。"),
    ):
        original = getattr(target, name)
        monkeypatch.setattr(target, name, value)
        changed.append(frozen.caseset_digest())
        monkeypatch.setattr(target, name, original)

    assert frozen.FROZEN_DIGEST not in changed and len(set(changed)) == len(changed)


def test_the_preview_says_what_would_go_where_and_reads_or_makes_nothing(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(live_check, "analysis_setup", lambda environ: pytest.fail("the preview read an approval"))
    monkeypatch.setattr(live_check, "run_batch", lambda *args, **kwargs: pytest.fail("the preview ran something"))
    monkeypatch.chdir(tmp_path)

    assert live_check.main([]) == 0
    text = capsys.readouterr().out

    assert frozen.FROZEN_DIGEST in text and "什么都不发送，没有读取批准或 Key" in text and list(tmp_path.iterdir()) == []
    assert frozen.NOTICE_VERSION in text and all(line in text for line in frozen.NOTICE)
    assert "方向探索网页不受影响：仍只用本机模拟服务" in text and "待语义复核" in text
    assert "最多 4 次生成、0 次计数" in text and "应用内预算 $1.30（不是账单上限）" in text


def test_every_field_of_every_case_goes_out_and_each_body_is_within_the_limit(offline):
    folder, _ = offline

    for case in frozen.CASES:
        record = kept(folder, case.id)
        request, [call] = record["request"], record["calls"]
        statuses = dict(request["preferences"]["statuses"])
        sent = sorted(sorted(source["fields"]) for source in request["sources"])

        # Every field of every experience, and every filled preference, as written; nothing else.
        assert sent == sorted(sorted(fields) for _, fields in case.experiences), case.id
        assert {name for name, status in statuses.items() if status == "send"} == set(case.preferences or {}), case.id
        assert all(text in call["prompt"] for _, fields in case.experiences for text in fields.values()), case.id
        assert record["notice_version"] == frozen.NOTICE_VERSION and request["notice_version"] == frozen.NOTICE_VERSION

        # Measured as the frozen real request would be: the model, the output limit, the effort, the strict schema.
        assert call["request_body"] == responses_request(
            frozen.MODEL, call["prompt"], max_output_tokens=8_000, reasoning_effort="low", service_tier="default", json_schema_format=OUTPUT_FORMAT
        )
        assert call["request_body"]["store"] is False and call["request_body"]["text"]["format"]["strict"] is True
        assert 0 < call["request_bytes"] <= frozen.REQUEST_BYTES

    # D3: no experience and no preferences record; every preference reads as not filled in.
    nothing = kept(folder, "D3")["request"]
    assert nothing["sources"] == [] and nothing["preferences"]["record_id"] is None
    assert {status for _, status in nothing["preferences"]["statuses"]} == {"unfilled"}


# --- offline -------------------------------------------------------------------------------------------------------


def test_an_offline_run_sends_nothing_keeps_everything_whole_and_awaits_the_review(offline):
    folder, summary = offline
    report = (folder / "report.md").read_text(encoding="utf-8")

    assert (summary["mode"], summary["stopped"], summary["failed"], summary["not_run"]) == ("offline", None, False, [])
    assert (summary["handed"], summary["attempts"], summary["approval"], summary["gate"]) == (0, 4, None, None)
    assert ledger(folder) == []

    for case in frozen.CASES:
        record = kept(folder, case.id)
        [call] = record["calls"]

        assert record["execution"] == "complete" and record["outcome"]["status"] == "complete", case.id
        # Kept whole: the prompt, the body and the raw answer, never cut.
        assert call["prompt_chars"] == len(call["prompt"]) and call["answer_chars"] == len(call["answer"]) > 0
        assert json.loads(call["answer"])["directions"] and call["usage"] is not None
        assert record["generations"] == {"attempts": 1, "blocked": 0, "handed": 0, "local": 1}
        # Checked automatically, and awaiting the review whatever the checks say.
        assert record["review"]["status"] == live_check.PENDING_REVIEW
        assert [item["id"] for item in record["review"]["items"]] == [item for item, _ in frozen.review_items(case.id)]
        assert all(item["evidence"] is None and item["verdict"] is None for item in record["review"]["items"])

    assert "自动检查通过不等于语义通过，也不等于整批通过" in summary["conclusion"]
    assert "方向探索网页仍只用本机模拟服务" in summary["conclusion"]
    assert report.count("语义复核：待语义复核") == 4 and "证据：（待填）" in report


@pytest.mark.parametrize("case", ["exists", "inside", "digest", "no-approval", "stale-digest"])
def test_a_run_is_refused_before_anything_is_made_or_read(monkeypatch, tmp_path, capsys, case):
    folder, environ = tmp_path / "new", {}
    argv = ["--send", str(folder), "--digest", frozen.FROZEN_DIGEST]

    if case == "exists":
        folder.mkdir()
    elif case == "inside":
        folder = live_check.REPOSITORY / "backend" / "a-new-folder"
        argv[1] = str(folder)
    elif case == "digest":
        argv[3] = "0" * 64
    elif case == "stale-digest":
        monkeypatch.setattr(frozen, "OUTPUT_TOKENS", 4_000)

    if case != "no-approval":
        monkeypatch.setattr(live_check, "analysis_setup", lambda environ: pytest.fail("an approval was read"))

    assert live_check.main(argv, environ=environ) == 2
    assert "没有运行" in capsys.readouterr().err
    assert folder.exists() is (case == "exists")


def test_the_offline_command_writes_its_folder_and_ends_at_zero(tmp_path, capsys):
    folder = tmp_path / "c4-offline"

    assert live_check.main(["--offline", str(folder)], environ={}) == 0
    assert sorted(path.name for path in (folder / "cases").iterdir()) == [f"{case}.json" for case in frozen.CASE_IDS]
    assert (folder / "batch.json").exists() and (folder / "report.md").exists()


# --- the automatic checks ----------------------------------------------------------------------------------------


def _id() -> str:
    return str(uuid.uuid4())


def _direction(name, evidence=(), ideal=(), **changes) -> Direction:
    values = {
        "name": name,
        "main_work": f"{name}方向一般的主要工作。",
        "evidence": tuple(evidence),
        "ideal": tuple(ideal),
        "gaps": ("一般需要补的能力",),
        "unknowns": (),
        "difference": f"和其他方向的区别（{name}）",
        "next_steps": ("先读几份岗位描述",),
    }

    return Direction(**{**values, **changes})


def _result(*first, names=("数据分析", "用户研究", "项目协调与管理")) -> ExplorationResult:
    directions = list(first) + [_direction(name) for name in names[len(first):]]

    return ExplorationResult(directions=tuple(directions))


def _quote(item_id, field, text) -> Quote:
    return Quote(item_id=item_id, version_id=_id(), field=field, text=text)


D2_IDS = {category: _id() for category, _ in D2.experiences}
D2_CATEGORIES = {item_id: category for category, item_id in D2_IDS.items()}


@pytest.mark.parametrize(
    "category, field, quote, kind",
    [
        ("project", "actions", "我整理了会议记录", None),
        ("project", "actions", "整理了会议记录", None),
        ("project", "actions", "组员负责搭建模型", "别人做的"),
        ("project", "actions", "搭建模型", "别人做的"),
        ("project", "actions", "模型，我整理了会议记录", "别人做的"),
        ("activity", "actions", "计划下学期学习 Tableau", "计划"),
        ("activity", "actions", "学习 Tableau", "计划"),
        ("work", "actions", "实习期间没有接触过 SQL", "没做过"),
        ("work", "actions", "SQL", "没做过"),
        ("work", "reflection", "熟悉 Excel 和 PowerPoint", "只说熟悉"),
        ("work", "reflection", "Excel", "只说熟悉"),
        # Titles and roles carry no clause: what they are taken for is the semantic review's (G4).
        ("work", "role", "运营实习生", None),
        ("project", "title", "课程小组项目（合成）", None),
    ],
)
def test_a_quote_is_held_to_the_clauses_it_lies_in(category, field, quote, kind):
    """22.2, 2: the user's own clause beside someone else's is a basis; any quote touching a clause of someone else's
    work, a plan, a denial or a claim is not, however short."""
    result = _result(_direction("数据分析", [_quote(D2_IDS[category], field, quote)]))
    problems = frozen.judge(D2, result, D2_CATEGORIES).problems

    if kind is None:
        assert problems == ()
    else:
        assert len(problems) == 1 and f"落在{kind}的分句" in problems[0] and quote in problems[0]


def test_the_instruction_written_into_d4_is_no_basis_and_the_users_own_clauses_are():
    item = _id()
    categories = {item: "project"}
    own = _result(_direction("数据分析", [_quote(item, "actions", "整理了上百份问卷，用 Excel 做了频次统计")]))
    taken = _result(_direction("数据分析", [_quote(item, "actions", "给每个方向打 1 到 10 分")]))

    assert frozen.judge(D4, own, categories).problems == ()
    assert "写进资料里的指令" in frozen.judge(D4, taken, categories).problems[0]


def _link(field, text, relation, basis=None) -> IdealLink:
    return IdealLink(condition=_quote(_id(), field, text), relation=relation, basis=basis)


def test_relations_a_direction_cannot_settle_must_be_unknown_and_a_barred_direction_must_say_it_may_conflict():
    assert {"cities_ideal", "job_types_ideal", "work_style_no", "must_haves"} <= UNKNOWN_ONLY
    city = _link("cities_ideal", "北京", Relation.FITS, "这个方向的岗位多在大城市")
    unknown_city = _link("cities_ideal", "北京", Relation.UNKNOWN)
    barred = _link("directions_no", "销售", Relation.MAY_CONFLICT, "这个方向通常要跑客户、完成业绩")
    respected = _link("directions_no", "销售", Relation.FITS, "这个方向通常不做销售")

    assert frozen.judge(D1, _result(_direction("数据分析", ideal=[city])), {}).problems[0].endswith("的关系不是“无法判断”")
    assert frozen.judge(D1, _result(_direction("数据分析", ideal=[unknown_city, respected])), {}).problems == ()
    assert "没有标出可能冲突" in frozen.judge(D1, _result(_direction("销售 运营")), {}).problems[0]
    assert frozen.judge(D1, _result(_direction("销售运营", ideal=[barred])), {}).problems == ()
    # D3's user cannot accept anything: no direction is barred.
    assert frozen.barred_directions(D3) == () and frozen.barred_directions(D4) == ("销售",)


def test_where_nothing_personal_was_sent_nothing_personal_can_be_quoted():
    quoted = _result(_direction("数据分析", [_quote(_id(), "actions", "清洗订单数据")], [_link("directions_ideal", "数据分析", Relation.UNKNOWN)]))

    assert len(frozen.judge(D3, quoted, {}).problems) == 2
    assert frozen.judge(D3, _result(), {}).problems == ()


def test_the_screens_only_point_the_review_at_words_and_decide_nothing():
    flagged = _result(
        _direction("数据分析", main_work="这是最适合你的方向，排名第一。", gaps=("你已经掌握了 SQL",)),
        _direction("用户研究", ideal=[_link("directions_ideal", "用户研究", Relation.FITS, "这个方向 score 很高")]),
        _direction("项目协调与管理", unknowns=("RoleRadar 核对：有 1 条经历引文写的是计划、他人完成、没有做或只说熟悉，没有算作已有基础",)),
    )
    judgement = frozen.judge(D1, flagged, {})

    assert judgement.problems == ()
    assert any("最适合" in flag for flag in judgement.flags) and any("排名" in flag for flag in judgement.flags)
    assert any("你已经" in flag and "对用户的断言" in flag for flag in judgement.flags)
    assert any("score" in flag for flag in judgement.flags) and not any("项目协调与管理" in flag for flag in judgement.flags)
    # Words the screen does not list slip past it (here a ranking said another way): every case still goes to the review.
    assert frozen.judge(D1, _result(_direction("数据分析", main_work="这个方向比别的方向更胜一筹。")), {}).flags == ()


# --- through a gate, rehearsed -----------------------------------------------------------------------------------


def test_a_rehearsal_counts_what_went_out_by_the_gate_and_the_ledger(tmp_path):
    service = Stand()
    folder = tmp_path / "rehearsal"
    summary = live_check.run_batch(folder, live=rehearsal(service))

    assert (summary["mode"], summary["stopped"], summary["not_run"]) == ("rehearsal", None, [])
    assert summary["handed"] == summary["attempts"] == len(service.calls) == summary["gate"]["requests_used"] == 4
    assert [row.state for row in ledger(folder)] == ["settled"] * 4 and summary["approval"]["purposes"] == ["direction"]
    # Every request asked for the approval's output limit and effort, whatever the workflow asked.
    assert {call["max_output_tokens"] for call in service.calls} == {8_000} and {call["reasoning_effort"] for call in service.calls} == {"low"}

    for case in frozen.CASES:
        record = kept(folder, case.id)

        assert record["generations"] == {"attempts": 1, "blocked": 0, "handed": 1, "local": 0}, case.id
        assert record["deliveries"] == [["generation", "sent"]] and record["outcome"]["workflow_requests_sent"] == 1
        assert record["recipient"]["endpoint"] == "https://synthetic.invalid/v1" and record["gate_after"]["paused"] is None
        # The body the gate measured: the service's own, with the approval's limit.
        assert record["calls"][0]["request_body"]["max_output_tokens"] == 8_000


def quoting(quote: str) -> str:
    """A well-formed answer of three directions, the first quoting this as the user's practice."""
    directions = [
        {"name": name, "main_work": "一般的主要工作。", "evidence": [], "ideal": [], "gaps": [], "unknowns": [], "difference": "区别", "next_steps": ["读几份岗位描述"]}
        for name in ("数据分析", "用户研究", "产品运营")
    ]
    directions[0]["evidence"] = [{"source_id": "S1", "field": "actions", "quote": quote, "basis": "practice"}]

    return json.dumps({"directions": directions}, ensure_ascii=False)


@pytest.mark.parametrize("bad", ["not json", "citation"])
def test_an_answer_that_is_no_valid_result_fails_its_case_and_the_next_case_runs(tmp_path, bad):
    text = "这不是 JSON" if bad == "not json" else quoting("资料里没有的这句话")
    folder = tmp_path / "content"
    summary = live_check.run_batch(folder, live=rehearsal(Stand(answers={2: text})))
    record = kept(folder, "D2")

    assert (summary["stopped"], summary["not_run"]) == (None, [])
    assert record["execution"] == "content_failed" and record["checks"]["passed"] is False
    assert record["checks"]["problems"] == [f"回答没有通过本机核对（{'invalid_json' if bad == 'not json' else 'citation'}）"]
    # It did go out, and its whole answer is kept.
    assert record["generations"]["handed"] == 1 and record["calls"][0]["answer"] == text
    assert [kept(folder, case)["execution"] for case in ("D3", "D4")] == ["complete", "complete"]


@pytest.mark.parametrize("edge", ["timeout", "recipient", "paused-after-answer", "unknown-usage"])
def test_an_edge_stops_the_batch_and_keeps_what_ran(tmp_path, edge):
    over = replace(USED, input_tokens=40_000, total_tokens=40_030)
    missing = replace(USED, input_tokens=None, total_tokens=None)
    service = {
        "timeout": Stand(faults={2: AIProviderTimeoutError("synthetic")}),
        "recipient": Stand(answered_as={2: ("synthetic", "another-model")}),
        "paused-after-answer": Stand(usages={2: over}),
        "unknown-usage": Stand(usages={2: missing}),
    }[edge]
    folder = tmp_path / edge
    summary = live_check.run_batch(folder, live=rehearsal(service))
    record = kept(folder, "D2")

    assert summary["stopped"] is not None and summary["failed"] is False and summary["not_run"] == ["D3", "D4"]
    assert kept(folder, "D1")["execution"] == "complete" and record["generations"]["handed"] == 1

    if edge in ("paused-after-answer", "unknown-usage"):
        # The answer came back and passed the workflow's checks: the ledger still stops the batch.
        assert record["execution"] == "complete" and record["gate_after"]["paused"] is not None
    else:
        assert record["execution"] == "stopped" and record["outcome"]["status"] == "failed"


@pytest.mark.parametrize("held", ["stop-file", "budget"])
def test_a_request_the_gate_keeps_back_is_no_real_generation(tmp_path, held):
    folder = tmp_path / held
    after = (lambda number: (folder / "LIVE_STOP").touch()) if held == "stop-file" else None
    # One reservation ($0.32) fits; after the first request is settled the second no longer does.
    terms = {"max_cost_micro_usd": 320_500} if held == "budget" else {}
    summary = live_check.run_batch(folder, live=rehearsal(Stand(after=after), **terms))
    record = kept(folder, "D2")

    assert summary["stopped"] is not None and summary["not_run"] == ["D3", "D4"] and summary["handed"] == 1
    assert record["execution"] == "stopped" and "outcome" not in record
    # Asked for once, kept back by the gate, never handed on: no real generation, and the ledger agrees.
    assert record["generations"] == {"attempts": 1, "blocked": 1, "handed": 0, "local": 0}
    assert record["calls"][0]["status"] == ("gate_stopped" if held == "stop-file" else "gate_cost")
    assert [row.state for row in ledger(folder)] == ["settled"]


def test_evidence_that_cannot_be_kept_stops_the_batch_as_a_failure(tmp_path, monkeypatch):
    keep = live_check._keep

    def failing(path, data):
        if path.name == "D2.json":
            raise live_check.EvidenceNotKept("读回的内容不一致")

        keep(path, data)

    monkeypatch.setattr(live_check, "_keep", failing)
    folder = tmp_path / "unkept"
    summary = live_check.run_batch(folder, live=rehearsal(Stand()))

    assert summary["failed"] is True and "D2 的记录没能可靠保存" in summary["stopped"] and summary["not_run"] == ["D3", "D4"]
    assert (folder / "cases" / "D1.json").exists() and not (folder / "cases" / "D2.json").exists()


# --- faults once a case has begun (C4-0 R1): what went out and came back is never lost or taken for settled --------


def simulated_answer(prompt: str) -> str:
    """The answer Stand gives a prompt when the test says nothing else."""
    return SimulatedProvider().generate_text(prompt, json_schema_format=OUTPUT_FORMAT).text


def test_a_ledger_unreadable_after_a_case_leaves_what_it_sent_and_got_and_stops_as_a_failure(tmp_path, monkeypatch):
    service = Stand()

    def unreadable(*args):
        raise OSError("synthetic: the ledger cannot be read")

    monkeypatch.setattr(live_check, "_ledger_rows", unreadable)
    folder = tmp_path / "ledger"
    summary = live_check.run_batch(folder, live=rehearsal(service))
    record = kept(folder, "D1")

    # One request, never tried again, and no further case.
    assert len(service.calls) == 1 and summary["not_run"] == ["D2", "D3", "D4"]
    assert summary["failed"] is True and summary["stopped"] == "D1：这一例之后读不出账本（OSError），账本行未知"
    # What went out stays counted, by the gate, in the record and the summary; the ledger itself has it.
    assert summary["handed"] == record["generations"]["handed"] == 1 and summary["gate"]["requests_used"] == 1
    assert [row.state for row in ledger(folder)] == ["settled"]
    # The answer stays whole; the case's own rows are unknown, never none.
    assert record["calls"][0]["status"] == "answered" and record["calls"][0]["answer"] == simulated_answer(service.calls[0]["prompt"])
    assert record["ledger"] is None and record["ledger_unknown"] == "这一例之后读不出账本（OSError），账本行未知"
    assert record["execution"] == "complete" and record["review"]["status"] == live_check.PENDING_REVIEW
    assert "- 账本行：这一例之后读不出账本（OSError），账本行未知" in (folder / "report.md").read_text(encoding="utf-8")


def test_a_ledger_unreadable_before_a_case_sends_nothing(tmp_path, monkeypatch):
    service = Stand()

    def unreadable(database):
        raise OSError("synthetic: the ledger cannot be read")

    monkeypatch.setattr(live_check, "_ledger_ids", unreadable)
    folder = tmp_path / "before"
    summary = live_check.run_batch(folder, live=rehearsal(service))
    record = kept(folder, "D1")

    assert service.calls == [] and (summary["handed"], summary["attempts"]) == (0, 0) and summary["not_run"] == ["D2", "D3", "D4"]
    assert summary["failed"] is True and record["error"] == "发送前读不出账本（OSError），这一例没有发送" and record["calls"] == []
    assert ledger(folder) == [] and summary["gate"]["requests_used"] == 0


def test_a_gate_status_unreadable_after_an_answer_ends_in_a_controlled_failure_with_everything_kept(tmp_path, monkeypatch, capsys):
    service = Stand()
    status, close = LiveGate.status, LiveGate.close
    closed = []

    def unreadable(self):
        if service.calls:
            raise OSError("synthetic: the status cannot be read")

        return status(self)

    def closing(self):
        closed.append(self)
        close(self)

    monkeypatch.setattr(LiveGate, "status", unreadable)
    monkeypatch.setattr(LiveGate, "close", closing)
    monkeypatch.setattr(live_check, "analysis_setup", lambda environ: rehearsal(service))
    folder = tmp_path / "status"

    assert live_check.main(["--send", str(folder), "--digest", frozen.FROZEN_DIGEST], environ={}) == live_check.EXIT_FAILED
    summary = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    record = kept(folder, "D1")
    told = (folder / "report.md").read_text(encoding="utf-8")

    # One request, no further case, and the gate closed all the same.
    assert len(service.calls) == 1 and summary["not_run"] == ["D2", "D3", "D4"] and len(closed) == 1
    assert summary["failed"] is True and summary["stopped"] == "D1：这一例之后读不出闸门状态（OSError），是否暂停、用了多少都未知"
    assert summary["handed"] == 1 and record["calls"][0]["status"] == "answered" and record["calls"][0]["answer"]
    # Unknown, and said so: never an empty or a settled ledger. The rows read before the fault stand.
    assert record["gate_after"] is None and summary["gate"] is None
    assert summary["closing"] == ["最后读不出闸门和账本状态（OSError），账本状态未知"]
    assert [row["state"] for row in record["ledger"]] == ["settled"] and [row.state for row in ledger(folder)] == ["settled"]
    assert "- 账本：没有读到最后的状态，未知" in told and "- 收尾出错：最后读不出闸门和账本状态（OSError）" in told
    assert "没能运行完" in capsys.readouterr().err


def test_an_answer_received_before_its_settlement_failed_is_kept_and_never_taken_for_settled(tmp_path, monkeypatch):
    service = Stand()

    def unsettled(self, *args, **kwargs):
        raise OSError("synthetic: the settlement cannot be written")

    monkeypatch.setattr(LiveGate, "settle", unsettled)
    folder = tmp_path / "settle"
    summary = live_check.run_batch(folder, live=rehearsal(service))
    record = kept(folder, "D1")
    call = record["calls"][0]

    assert len(service.calls) == 1 and summary["not_run"] == ["D2", "D3", "D4"]
    assert summary["failed"] is True and record["error"] == "运行出错（OSError）" and record["execution"] == "failed"
    assert summary["handed"] == record["generations"]["handed"] == 1 and record["deliveries"] == [["generation", "sent"]]
    # The answer and the usage the service gave, kept whole...
    assert call["received"] is True and call["answer"] == simulated_answer(service.calls[0]["prompt"])
    assert call["usage"] == {"input_tokens": 120, "output_tokens": 30, "reasoning_tokens": 12, "cache_read_tokens": 40, "cache_write_tokens": 10}
    # ...and never taken for a settled request: the call failed after the answer, and its row is still only reserved.
    assert call["status"] == "answered_then_failed_OSError"
    assert [row["state"] for row in record["ledger"]] == ["reserved"] and summary["gate"]["reserved_micro_usd"] > 0
    assert "outcome" not in record and record["checks"]["passed"] is None
    assert "服务的回答已收到并保存" in (folder / "report.md").read_text(encoding="utf-8")


def test_any_fault_of_the_command_once_a_case_has_begun_leaves_its_record_to_the_batch(tmp_path, monkeypatch):
    service = Stand()

    def broken(*args):
        raise RuntimeError("synthetic: a fault of the command")

    monkeypatch.setattr(live_check, "_checks", broken)
    folder = tmp_path / "fault"
    summary = live_check.run_batch(folder, live=rehearsal(service))
    record = kept(folder, "D1")

    assert len(service.calls) == 1 and summary["not_run"] == ["D2", "D3", "D4"] and summary["handed"] == 1
    assert summary["failed"] is True and summary["stopped"] == "D1：验证命令出错（RuntimeError）"
    assert record["error"] == "验证命令出错（RuntimeError）" and record["review"]["status"] == live_check.UNFINISHED
    assert record["calls"][0]["answer"] == simulated_answer(service.calls[0]["prompt"]) and record["generations"]["handed"] == 1
    assert [row["state"] for row in record["ledger"]] == ["settled"] and summary["gate"]["requests_used"] == 1
    assert "- 自动检查：没有做完，未判定" in (folder / "report.md").read_text(encoding="utf-8")


def test_the_gate_takes_the_receiving_service_for_exactly_the_service_it_wraps(tmp_path):
    class Unmarked(Stand):
        SYNTHETIC = False

    class Pretending(OpenAIProvider):
        SYNTHETIC = True

    service = Stand(OFFICIAL)
    wrapped = live_check.Receiving(service, [])
    asked = {"max_output_tokens": 8_000, "reasoning_effort": "low", "json_schema_format": OUTPUT_FORMAT}

    assert wrapped.identity() == service.identity() and wrapped.request_body("提示", **asked) == service.request_body("提示", **asked)
    # Synthetic only where the gate would take the service itself for synthetic: a real service never is.
    assert (wrapped.SYNTHETIC, live_check.Receiving(Unmarked(), []).SYNTHETIC) == (True, False)
    assert live_check.Receiving(object.__new__(Pretending), []).SYNTHETIC is False
    # So made-up prices still refuse a service not marked synthetic, before anything is sent.
    unmarked = Unmarked()
    summary = live_check.run_batch(tmp_path / "unmarked", live=rehearsal(unmarked))

    assert unmarked.calls == [] and summary["handed"] == 0 and "闸门没有放行" in summary["stopped"]


def test_an_approval_for_any_other_purpose_is_refused_before_anything_is_made(tmp_path):
    folder = tmp_path / "mixed"

    with pytest.raises(LiveRefused, match="只能写 direction 一个用途"):
        live_check.run_batch(folder, live=rehearsal(Stand(), purposes=frozenset({"direction", "analysis"})))

    assert not folder.exists()


# --- the real configuration, read by the app's parser and taken by the command ----------------------------------

# A C4 approval as the Owner would write it, but for its made-up id and key (proposals, not a permission).
C4_TERMS = {
    "ROLERADAR_ANALYSIS_MODE": "live",
    "ROLERADAR_LIVE_PROVIDER": "openai",
    "ROLERADAR_LIVE_MODEL": "gpt-5.6-sol",
    "ROLERADAR_LIVE_ENDPOINT": "https://api.openai.com/v1",
    "ROLERADAR_LIVE_MAX_REQUESTS": "4",
    "ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "8000",
    "ROLERADAR_LIVE_TIMEOUT_SECONDS": "120",
    "ROLERADAR_LIVE_RULES_CHECKED_ON": "2026-11-09",
    "OPENAI_API_KEY": "synthetic-not-a-key",
    "ROLERADAR_LIVE_APPROVAL_ID": "u3c4-synthetic-test",
    "ROLERADAR_LIVE_PURPOSES": "direction",
    "ROLERADAR_LIVE_SERVICE_TIER": "default",
    "ROLERADAR_LIVE_REASONING_EFFORT": "low",
    "ROLERADAR_LIVE_STRUCTURING_MAX_BYTES": "8000",
    "ROLERADAR_LIVE_STRUCTURING_MAX_OUTPUT_TOKENS": "8000",
    "ROLERADAR_LIVE_MAX_INPUT_TOKENS": "32000",
    "ROLERADAR_LIVE_MAX_COST_USD": "1.30",
    "ROLERADAR_LIVE_PRICE_INPUT": "4.00",
    "ROLERADAR_LIVE_PRICE_CACHE_READ": "0.40",
    "ROLERADAR_LIVE_PRICE_CACHE_WRITE": "5.00",
    "ROLERADAR_LIVE_PRICE_OUTPUT": "20.00",
    "ROLERADAR_LIVE_PRICE_RESERVE_INPUT": "10.00",
    "ROLERADAR_LIVE_PRICE_SOURCE": "https://developers.openai.com/api/docs/pricing",
    "ROLERADAR_LIVE_PRICE_CHECKED_ON": "2026-11-09",
    "ROLERADAR_LIVE_PRICE_COUNT": "not_applicable",
    "ROLERADAR_LIVE_BUDGET_POLICY": "fixed_reserve_v1",
    "ROLERADAR_LIVE_BUDGET_SCOPE": "application",
    "ROLERADAR_LIVE_RESERVE_INPUT_TOKENS": "16000",
    "ROLERADAR_LIVE_MAX_PAYLOAD_BYTES": "32000",
}
OFFICIAL = ("openai", "gpt-5.6-sol", "https://api.openai.com/v1")


def test_a_direction_approval_reaches_the_command_through_the_real_parser_as_written(monkeypatch, tmp_path):
    """Only the service is a stand-in, made where the real one would be; the terms are the app's parser's, as read."""
    service = Stand(OFFICIAL)
    made = []

    def make(policy, environ=None, **settings):
        made.append(settings)
        return service

    monkeypatch.setattr(live_config, "make_live_provider", make)
    folder = tmp_path / "c4"

    assert live_check.main(["--send", str(folder), "--digest", frozen.FROZEN_DIGEST], environ=C4_TERMS) == 0
    summary = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    approval = summary["approval"]

    assert (approval["purposes"], approval["max_output_tokens"], approval["max_requests"], approval["max_cost_micro_usd"]) == (["direction"], 8_000, 4, 1_300_000)
    assert made == [{"service_tier": "default", "max_output_tokens": 8_000}] and summary["mode"] == "send"
    assert summary["handed"] == len(service.calls) == 4 and summary["gate"]["requests_left"] == 0 and summary["stopped"] is None
    assert {call["max_output_tokens"] for call in service.calls} == {8_000} and {call["reasoning_effort"] for call in service.calls} == {"low"}
    assert [row.state for row in ledger(folder)] == ["settled"] * 4
    assert all(kept(folder, case)["recipient"] == dict(zip(("provider", "model", "endpoint"), OFFICIAL)) for case in frozen.CASE_IDS)
    # The approval as kept holds no key.
    assert "synthetic-not-a-key" not in (folder / "batch.json").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "change, says",
    [
        ({"ROLERADAR_LIVE_PURPOSES": "direction, analysis"}, "输出上限要写成 1 到 4000 之间的整数"),
        ({"ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "8001"}, "输出上限要写成 1 到 8000 之间的整数"),
        ({"ROLERADAR_LIVE_MAX_OUTPUT_TOKENS": "4000"}, "批准和冻结的调用设置不一致：max_output_tokens"),
        ({"ROLERADAR_LIVE_MAX_REQUESTS": "5"}, "批准和冻结的调用设置不一致：max_requests"),
        ({"ROLERADAR_LIVE_MAX_COST_USD": "1.20"}, "批准和冻结的调用设置不一致：max_cost_micro_usd"),
        ({"ROLERADAR_LIVE_REASONING_EFFORT": "medium"}, "批准和冻结的调用设置不一致：reasoning_effort"),
        ({"ROLERADAR_LIVE_TIMEOUT_SECONDS": "60"}, "批准和冻结的调用设置不一致：timeout_seconds"),
        ({"ROLERADAR_LIVE_PRICE_OUTPUT": "30.00"}, "超过应用内预算 $1.30：单价变了要重算并重新确认"),
        ({"ROLERADAR_ANALYSIS_MODE": ""}, "没有给出新的批准"),
    ],
    ids=["mixed", "8001", "4000", "5-requests", "budget", "effort", "timeout", "dearer", "no-approval"],
)
def test_the_command_takes_nothing_but_the_frozen_settings(monkeypatch, tmp_path, capsys, change, says):
    monkeypatch.setattr(live_config, "make_live_provider", lambda *args, **kwargs: pytest.fail("a service was made"))
    folder = tmp_path / "c4"

    assert live_check.main(["--send", str(folder), "--digest", frozen.FROZEN_DIGEST], environ={**C4_TERMS, **change}) == 2
    assert says in capsys.readouterr().err and not folder.exists()
