"""The U3-B comparison: the agent and the fixed flow on the frozen synthetic tasks, run, recorded and judged (U3-B0).

    python -m app.discovery.compare                      what a run would send and do; sends nothing, reads no key
    python -m app.discovery.compare --offline FOLDER     both flows of every task on this machine, the agent's
                                                         steps decided by the simulated decider (fixed rules)
    python -m app.discovery.compare --send FOLDER --digest SHA
                                                         B-1 only, under a new approval: the agent's decisions go to
                                                         the model the approval names. Not approved in B-0.

FOLDER must not exist yet: the command creates it, with its own evaluation library and ledger, and refuses one inside
the repository. The tasks, their truth and the decision settings are frozen (app.discovery.compare_tasks); a run
checks them against FROZEN_DIGEST first.

What goes where (U3-B pre-check, 7.3.2 and 7.3.4). The searches, reads and checks of the synthetic sources and every
analysis stay on this machine: each analysis goes to the app's simulation, the recipient its confirmation names. Only
the agent's decisions may leave, and only with --send: each decision's prompt goes to the model the approval names,
through the gate, which each agent run enters once for Purpose.DISCOVERY. A prompt holds the instructions, the job
titles and the scope as conditions (cities, job types, directions, and what the user cannot accept), what is left of
each limit, the combinations not searched, the sources, the postings found as their public summary lines with short
masked excerpts, the tool feedback and the earlier steps. No experience text, preference record, private note or
attachment goes with a decision. The fixed flow decides nothing and sends nothing.

One batch, one ledger (7.2): the tasks run in order under one approval and one gate. Each agent run enters and leaves
the gate, and what it used stays counted: a task never gets a fresh allowance. Whatever stops a run abnormally stops
the batch (BatchStop): a request the gate refused, an approval paused, an answer with no usable text, a timeout, a
request larger than the evaluation's limit, a run ending out of time or cancelled, two agent runs in a row ending on
refused actions, a hard check failing. Nothing more is sent then, a request kept back goes nowhere and is released,
no further task starts, and the report names the tasks not run. Bytes are what is measured here, never tokens: the
gate keeps its fixed reservation and settles each request from the usage reported, pausing on usage above the
reservation or unknown.

Records stay in the folder: per run its outcome, its steps with what each search newly found, the actions decided,
and with --send each prompt exactly as sent, the raw answer, its usage and the run's ledger rows; then the judgement
and a report. They hold the frozen synthetic material only.

A run that fails (an error of its own, the gate not letting it start) never ends: its record says so (status
"failed", end None) and keeps what it had done by then: its steps, the actions decided, the decisions sent with
their usage, its searches, the postings found with their reads and checks, and its ledger rows. It is never
compared, and it stops the batch; so does a failure in making ready (the library, the gate, the service). Either
way the records made so far, the summary and the report are still written.

Exit status: 0 the batch ran to its end (whatever it shows); 2 nothing was run; 3 the batch stopped early under
its rules, every run that ran having ended; 4 a run, or the making ready, failed. With 3 and 4 the folder holds the
records and the report.
"""
import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.ai_provider import AIProvider, AIProviderError, Usage, how_far, request_bytes
from app.discovery import compare_tasks as frozen
from app.discovery import rules, tools
from app.discovery.agent import DecisionNotSent, ModelDecider, SimulatedDecider, parse_action, run_agent
from app.discovery.baseline import run_baseline
from app.discovery.contracts import DIMENSION_LABELS, END_LABELS, CheckStatus, Dimension, EndReason, Outcome, Unlisted
from app.discovery.runner import Guarded, ParentStopped, RequestKeptBack, tracing_variables
from app.job_analysis.live import analysis_setup
from app.job_analysis.simulation import SimulatedProvider
from app.library.clock import SystemClock
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.models import LiveRequest, LiveRequestState, create_schema
from app.live_gate import LiveBlocked, LiveRefused, Purpose, open_gate

# 2 (U3-B0 R1): a run's status; a failed run keeps what it had done; each posting found with its read and check;
# a result with its title, company and apply address; each search with what it could not take.
RECORD_VERSION = "2"
FINISHED, FAILED = "finished", "failed"
EXIT_DONE, EXIT_NOT_RUN, EXIT_STOPPED, EXIT_FAILED = 0, 2, 3, 4
EVALUATION_DB = "evaluation.db"
AGENT, BASELINE = "agent", "baseline"
# A prompt is kept whole up to this many characters, an answer up to this many: more is cut and said to be.
PROMPT_CHARS = 20_000
ANSWER_CHARS = 4_000
# When every run reads and checks: both flows of a task see the same time.
RUN_AT = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
# How a run may end without stopping the batch; any other end does.
USUAL_ENDS = {EndReason.ENOUGH, EndReason.SCOPE_DONE, EndReason.NO_RESULTS, EndReason.PARTLY_NO_RESULTS, EndReason.LIMIT, EndReason.REFUSED}
# The words the tools refuse an action outside the scope with (rules.allows, tools._barred_problem).
OUT_OF_SCOPE = ("是不能接受的", "不在本次确认的", "检索时必须指定", "检索词里有不能接受的")
MODE_LABELS = {
    "offline": "离线（决策来自本机规则，什么都没有发出）",
    "rehearsal": "合成服务演练（决策是测试给定的回答，经真实闸门、按合成价格记账，什么都没有发出）",
    "send": "真实决策（经闸门发给批准写明的接收方）",
}
CHOICE_LABELS = {"source_id": "来源", "keywords": "检索词", "city": "城市", "job_type": "求职类型", "direction": "岗位方向"}
# What a result shows of where its posting comes from, each held to the frozen data (compare_tasks.Provenance).
PROVENANCE_LABELS = {
    "source_id": "来源",
    "source": "来源名称",
    "evidence": "证据等级",
    "title": "岗位名称",
    "company": "公司",
    "url": "链接",
    "apply_url": "申请链接",
    "content_sha256": "内容指纹",
}
# Ledger rows whose request went out or may have: all but those released unsent.
SENT_STATES = {LiveRequestState.RESERVED.value, LiveRequestState.SETTLED.value, LiveRequestState.OVER.value, LiveRequestState.UNKNOWN.value}
REPOSITORY = Path(__file__).resolve().parents[3]


class FixedClock:
    """The one time runs read and check postings at."""

    def __init__(self, at: datetime = RUN_AT):
        self.at = at

    def now(self) -> datetime:
        return self.at


class BatchStop:
    """Whether the batch has to stop, and why; stopping may also cancel the run in progress at its next check."""

    def __init__(self):
        self.reason: str | None = None
        self.run_cancel: threading.Event | None = None

    @property
    def stopped(self) -> bool:
        return self.reason is not None

    def stop(self, reason: str, *, cancel: bool = True) -> None:
        if self.reason is None:
            self.reason = reason

        if cancel and self.run_cancel is not None:
            self.run_cancel.set()


class LocalAnalyses(AIProvider):
    """The app's simulated analysis on this machine, counted; never through the gate."""

    def __init__(self):
        self._inner = SimulatedProvider()
        self.requests = 0

    def identity(self):
        return self._inner.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.requests += 1

        return self._inner.generate_text(
            prompt, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort, json_schema_format=json_schema_format
        )


class EvalSending:
    """The agent run's last word before each decision request is handed on (app.live_gate.Sending)."""

    def __init__(self, batch: BatchStop, run: tools.SearchRun):
        self.batch = batch
        self.run = run
        self.came = []

    def hand_on(self, call) -> None:
        # Nothing more goes once the batch stopped or the run may not go on: the gate releases the request.
        if self.batch.stopped or self.run.stopping():
            raise ParentStopped()

    def came_back(self, call, delivered) -> None:
        self.came.append([call.value, delivered.value])


class DecisionChannel(AIProvider):
    """The provider the agent's ModelDecider is given in a real run: the gate's for this run, on its way out.

    request_body is the body the gate measures and the service sends: the service's own, with the approval's reasoning
    effort and the output limit the gate allows for Purpose.DISCOVERY (app.live_gate.GatedProvider). Every request is
    recorded as it went: the prompt, its size, then the answer and its usage, or how it failed. A failure, a refusal of
    the gate, or an approval paused after an answer stops the batch.
    """

    def __init__(self, guarded: Guarded, service: AIProvider, gate, batch: BatchStop, decisions: list):
        self._guarded = guarded
        self._service = service
        self._gate = gate
        self._batch = batch
        self._decisions = decisions

    def identity(self):
        return self._service.identity()

    def request_body(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None) -> dict:
        terms = self._gate.terms
        limit = terms.output_limit(Purpose.DISCOVERY)

        if max_output_tokens is not None:
            limit = min(max_output_tokens, limit)

        return self._service.request_body(
            prompt, max_output_tokens=limit, reasoning_effort=terms.reasoning_effort, json_schema_format=json_schema_format
        )

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        body = self.request_body(prompt, max_output_tokens, None, json_schema_format)
        entry = {
            "prompt": prompt[:PROMPT_CHARS],
            "prompt_chars": len(prompt),
            "prompt_truncated": len(prompt) > PROMPT_CHARS,
            "request_bytes": request_bytes(body),
            "max_output_tokens": body.get("max_output_tokens"),
        }
        self._decisions.append(entry)

        try:
            result = self._guarded.generate_text(
                prompt, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort, json_schema_format=json_schema_format
            )
        except RequestKeptBack:
            # Kept back before it was handed on: nothing went out, and the gate released it.
            entry["status"] = "kept_back"
            raise
        except LiveBlocked as blocked:
            entry["status"] = f"gate_{blocked.reason.value}"
            # The loop ends the run at the gate by itself; the batch goes no further.
            self._batch.stop(f"闸门拦下了决策请求（{blocked.reason.value}）", cancel=False)
            raise
        except AIProviderError as error:
            sent, usage = how_far(error)
            entry.update(status=f"error_{type(error).__name__}", sent=sent.value, usage=_usage(usage))
            self._batch.stop(f"服务没有给出可用的回答（{type(error).__name__}）")
            raise
        except BaseException as error:
            entry["status"] = f"failed_{type(error).__name__}"
            self._batch.stop(f"决策请求失败（{type(error).__name__}）")
            raise

        entry.update(
            status="answered",
            answer=result.text[:ANSWER_CHARS],
            answer_chars=len(result.text),
            answer_truncated=len(result.text) > ANSWER_CHARS,
            usage=_result_usage(result),
        )
        paused = self._gate.status().paused_reason

        if paused is not None:
            entry["paused"] = paused
            self._batch.stop(f"批准已暂停（{paused}）")

        return result


class RecordingDecider:
    """A decider, with each action it decided kept for the record; a decision not sent stops the batch."""

    def __init__(self, inner, batch: BatchStop, actions: list):
        self._inner = inner
        self._batch = batch
        self._actions = actions
        self.kind = inner.kind

    def decide(self, observation) -> str:
        try:
            raw = self._inner.decide(observation)
        except DecisionNotSent as not_sent:
            self._actions.append({"number": observation.number, "status": "not_sent", "reason": not_sent.reason})
            self._batch.stop(f"决策请求没有发出：{not_sent.reason}", cancel=False)
            raise
        except BaseException as error:
            self._actions.append({"number": observation.number, "status": f"failed_{type(error).__name__}"})
            raise

        action, tool, problem = parse_action(raw)
        self._actions.append(
            {
                "number": observation.number,
                "status": "decided",
                "tool": tool.value if tool is not None else None,
                "action": None if action is None else action.model_dump(mode="json"),
                "problem": problem,
            }
        )

        return raw


class StepRecorder:
    """Each step of an agent run as it is recorded (on_step), with what the step newly found, as the run saw it."""

    def __init__(self, run: tools.SearchRun):
        self.run = run
        self.known: set = set()
        self.steps: list[dict] = []

    def __call__(self, step) -> None:
        new = [found for found in sorted(self.run.found.values(), key=lambda found: found.order) if found.ref not in self.known]
        self.known.update(found.ref for found in new)
        self.steps.append({**_step(step), "new": [_found(self.run, found) for found in new]})


@dataclass
class Settings:
    """What an evaluation run decides with; the frozen values unless a test says otherwise."""

    output_tokens: int = frozen.DECISION_OUTPUT_TOKENS
    request_bytes: int = frozen.DECISION_REQUEST_BYTES
    monotonic: object = time.monotonic


# --- running a batch --------------------------------------------------------------------------------------------


def run_batch(folder: Path, *, live=None, decider_for=None, settings: Settings | None = None, tasks=frozen.TASKS) -> dict:
    """Run every task, the fixed flow then the agent, into the new folder; judge each run; write the records and report.

    Offline (live None): the agent's decisions come from decider_for() (the simulated decider by default); nothing
    leaves this machine. With live, a LiveSetup of a new approval (B-1): its gate opens on the folder's own ledger,
    and the agent's decisions go through it to live.make_provider()'s service. Returns the batch summary.

    A failure stops the batch like any stop and is listed as one (failed): a run's own (its record keeps what it had
    done), or one in making ready. The records made by then, the summary and the report are written all the same,
    the gate closed and the library let go first.
    """
    settings = settings or Settings()

    if live is not None:
        # Before anything is made: an approval for this comparison alone.
        _check_terms(live.terms)

    folder = Path(folder)
    folder.mkdir()
    (folder / "runs").mkdir()
    db_path = folder / EVALUATION_DB
    engine = make_engine(db_path)
    batch = BatchStop()
    records, comparisons, failed = [], [], []
    gate = service = gate_status = None

    try:
        try:
            create_schema(engine)
            database = Database(engine)
            clock = FixedClock()
            material = frozen.seed_material(database, clock)

            if live is not None:
                gate = open_gate(database, db_path, live.terms, live.basis, SystemClock(timedelta(0)))
                service = live.make_provider()
        except (LiveBlocked, LiveRefused) as refused:
            failed.append(f"闸门没有打开（{getattr(refused, 'reason', refused)}）")
            batch.stop(failed[-1], cancel=False)
        except Exception as error:  # nothing runs on a library, gate or service not made
            failed.append(f"准备评估时出错（{type(error).__name__}）")
            batch.stop(failed[-1], cancel=False)

        refused_in_a_row = 0

        for task in tasks:
            if batch.stopped:
                break

            confirmation = frozen.confirmation_of(task, material)
            done = {}

            for flow in (BASELINE, AGENT):
                if batch.stopped:
                    break

                record = _run_once(task, flow, confirmation, database, clock, material, batch, settings, gate, service, decider_for, live)
                record["hard_problems"] = hard_checks(task, record)
                record["metrics"] = metrics(task, record)
                record["adjustments"] = adjustments(record) if flow == AGENT else []
                done[flow] = record
                records.append(record)
                _write(folder / "runs" / f"{task.id}-{flow}.json", record)

                if record["hard_problems"]:
                    batch.stop(f"{task.id} 的{_flow_label(flow)}违反硬性判据", cancel=False)

                if record["status"] == FAILED:
                    failed.append(f"{task.id} 的{_flow_label(flow)}：{record['error']}")
                    batch.stop(f"{task.id} 的{_flow_label(flow)}没有正常运行：{record['error']}", cancel=False)
                elif EndReason(record["end"]) not in USUAL_ENDS:
                    batch.stop(f"{task.id} 的{_flow_label(flow)}以“{END_LABELS[EndReason(record['end'])]}”结束", cancel=False)

                if flow == AGENT:
                    refused_in_a_row = refused_in_a_row + 1 if record["end"] == EndReason.REFUSED.value else 0

                    if refused_in_a_row >= 2:
                        batch.stop("连续两次 Agent 运行都以“被拒的动作达到上限”结束", cancel=False)

            if set(done) == {BASELINE, AGENT}:
                comparisons.append(compare(task, done[BASELINE], done[AGENT]))
    except Exception as error:  # a fault of the evaluation itself: what was recorded stays, and is reported
        failed.append(f"评估工具出错（{type(error).__name__}）")
        batch.stop(failed[-1], cancel=False)
    finally:
        try:
            # The whole batch's ledger, read before the gate closes.
            gate_status = None if gate is None else _status(gate.status())
        finally:
            if gate is not None:
                gate.close()

            engine.dispose()

    summary = {
        "record_version": RECORD_VERSION,
        "taskset_version": frozen.TASKSET_VERSION,
        "judge_version": frozen.JUDGE_VERSION,
        "taskset_digest": frozen.taskset_digest(),
        "mode": _mode(live),
        "settings": {"output_tokens": settings.output_tokens, "request_bytes": settings.request_bytes},
        "decision_recipient": None if live is None else _recipient(live.terms),
        "approval": None if live is None else live.terms.snapshot(),
        "gate": gate_status,
        "stopped": batch.reason,
        "failed": failed,
        "not_run": _not_run(tasks, records),
        "comparisons": comparisons,
        "conclusion": conclusion(records, comparisons, batch, _mode(live), len(tasks), failed),
    }
    _write(folder / "batch.json", summary)
    (folder / "report.md").write_text(report(summary, records), encoding="utf-8")

    return summary


def _not_run(tasks, records: list) -> list[str]:
    """The tasks the batch never got to, and those it got to in part: a run that failed did run."""
    not_run = []

    for task in tasks:
        ran = {record["flow"] for record in records if record["task"] == task.id}

        if not ran:
            not_run.append(task.id)
        elif ran != {BASELINE, AGENT}:
            not_run.append(f"{task.id}（{'、'.join(_flow_label(flow) for flow in (BASELINE, AGENT) if flow not in ran)}没有运行）")

    return not_run


def _run_once(task, flow, confirmation, database, clock, material, batch, settings, gate, service, decider_for, live) -> dict:
    cancel = threading.Event()
    batch.run_cancel = cancel
    analyses = LocalAnalyses()
    actions, decisions = [], []
    before = _ledger_ids(database)
    started = settings.monotonic()
    record = {
        "record_version": RECORD_VERSION,
        "task": task.id,
        "title": task.title,
        "flow": flow,
        "mode": _mode(live),
        "analysis_recipient": _recipient(confirmation.recipient),
        "confirmation_fingerprint": frozen.approval_of(confirmation).fingerprint,
        "decider": None,
        # Failed until it ends.
        "status": FAILED,
        "error": None,
    }
    run = steps = sending = finished = None

    try:
        run = tools.start(
            database,
            confirmation,
            frozen.approval_of(confirmation),
            analyses,
            register=task.register(),
            clock=clock,
            cancel=cancel,
            monotonic=settings.monotonic,
        )
        steps = StepRecorder(run)

        if flow == BASELINE:
            finished = run_baseline(run)
        elif live is None:
            decider = RecordingDecider((decider_for or SimulatedDecider)(), batch, actions)
            record["decider"] = decider.kind
            finished = run_agent(run, decider, on_step=steps)
        else:
            record["decider"] = "model"
            record["decision_recipient"] = _recipient(gate.terms)
            sending = EvalSending(batch, run)

            with gate.task(Purpose.DISCOVERY, material.owner_id, service, sending=sending) as gated:
                channel = DecisionChannel(Guarded(gated), service, gate, batch, decisions)
                model = ModelDecider(channel, output_tokens=settings.output_tokens, max_request_bytes=settings.request_bytes)
                finished = run_agent(run, RecordingDecider(model, batch, actions), on_step=steps)
    except (LiveBlocked, LiveRefused) as refused:
        record["error"] = f"闸门没有放行这次运行（{getattr(refused, 'reason', refused)}）"
    except Exception as error:  # the batch must learn that a run failed
        record["error"] = f"运行出错（{type(error).__name__}）"
    finally:
        batch.run_cancel = None

    record["seconds"] = round(settings.monotonic() - started, 3)
    record["local_analysis_requests"] = analyses.requests
    record["actions"] = actions
    record["decisions"] = decisions
    record["ledger"] = _ledger_rows(database, before)

    if sending is not None:
        record["handed"] = sending.came

    if finished is None or record["error"] is not None:
        # It never ended, or not cleanly: what it had done by then stays, and no conclusion is made up for it.
        record.update(_left_by(run, steps, confirmation.scope, actions))
    else:
        record["status"] = FINISHED
        record.update(_outcome(finished, run))
        record["steps"] = steps.steps if flow == AGENT else [_step(step) for step in finished.trace.steps]
        record["decision_count"] = finished.trace.decisions

    return record


def _mode(live) -> str:
    """offline: no gate; rehearsal: a gate with made-up prices and a made-up service (a test's); send: a real approval."""
    if live is None:
        return "offline"

    return "rehearsal" if live.terms.prices.synthetic else "send"


def _check_terms(terms) -> None:
    """A real run's approval is for this comparison alone: its purposes are the searches', no other."""
    if set(terms.purposes) != {Purpose.DISCOVERY.value}:
        raise LiveRefused("对照运行的批准只能写 discovery 一个用途。")


# --- what a run left -----------------------------------------------------------------------------------------------


def _outcome(finished, run) -> dict:
    outcome = finished.outcome
    ordered = _ordered(run)

    return {
        "end": outcome.end.value,
        "personalised": outcome.personalised,
        "results": [
            {
                "source_id": result.ref.source_id,
                "posting_id": result.ref.posting_id,
                "title": result.title,
                "company": result.company,
                "category": result.category.value,
                "source": result.source,
                "evidence": result.evidence.value,
                "url": result.url,
                "apply_url": result.apply_url,
                "read_at": _time(result.read_at),
                "content_sha256": result.content_sha256,
                "checked_at": _time(result.checked_at),
                "first_checks": list(result.first_checks),
                "analysed": result.analysis is not None and result.analysis.status.value,
            }
            for result in outcome.results
        ],
        "leads": [{"posting_id": lead.ref.posting_id, "why": lead.why} for lead in outcome.leads],
        "more_leads": outcome.more_leads,
        "unlisted": {reason.value: [ref.posting_id for ref in refs] for reason, refs in outcome.unlisted},
        "merged": [ref.posting_id for ref in outcome.merged],
        "found": [found.ref.posting_id for found in ordered],
        "postings": [_snapshot(run, found) for found in ordered],
        "unverifiable": outcome.unverifiable,
        "foreign": outcome.foreign,
        "unsearched": list(outcome.unsearched),
        "searches": [_search(search) for search in outcome.searches],
        "refusals": list(outcome.refusals),
        "trace": [_step(step) for step in finished.trace.steps],
    }


def _left_by(run, steps, scope, actions: list) -> dict:
    """What a run that failed had done by then, read from its own state (U3-B0 R1): the steps recorded, what it
    searched, found, read and checked, and what was refused. Never a conclusion it did not reach: no end, results,
    leads or postings left out. Nothing of the kind when it never started."""
    recorded = [] if steps is None else steps.steps
    left = {
        **_empty_outcome(),
        "end": None,
        "personalised": None,
        "unsearched": list(rules.unsearched(scope, set())),
        "steps": recorded,
        "trace": [{key: value for key, value in step.items() if key != "new"} for step in recorded],
        # Each decision asked for, whether it was answered, failed or never sent.
        "decision_count": len(actions),
    }

    if run is not None:
        ordered = _ordered(run)
        left.update(
            personalised=run.personalised,
            found=[found.ref.posting_id for found in ordered],
            postings=[_snapshot(run, found) for found in ordered],
            unverifiable=sum(search.unverifiable for search in run.searches),
            foreign=sum(search.foreign for search in run.searches),
            unsearched=list(rules.unsearched(run.scope, run.searched)),
            searches=[_search(search) for search in run.searches],
            refusals=list(run.refusals),
        )

    return left


def _empty_outcome() -> dict:
    return {
        "results": [], "leads": [], "more_leads": 0, "unlisted": {}, "merged": [], "found": [], "postings": [],
        "unverifiable": 0, "foreign": 0, "unsearched": [], "searches": [], "refusals": [], "decision_count": 0,
    }


def _ordered(run) -> list:
    return sorted(run.found.values(), key=lambda found: found.order)


def _snapshot(run, found) -> dict:
    """What the run itself learned of one posting it found (U3-B0 R1): where it is from, how the scope screened it,
    its read (the addresses, when, the SHA-256 of its words), the last check (what was answered, when the source
    last answered) and whether an analysis stands, was stopped, or did not finish."""
    detail = found.detail

    return {
        "source_id": found.ref.source_id,
        "posting_id": found.ref.posting_id,
        "evidence": run.evidence(found).value,
        "screen": found.screening.status.value,
        "read": None
        if detail is None
        else {"url": detail.url, "apply_url": detail.apply_url, "read_at": _time(found.read_at), "content_sha256": found.content_sha256},
        "check": None if found.status is None else {"status": found.status.value, "checked_at": _time(found.checked_at)},
        "analysed": found.analysis is not None,
        "blocked": bool(found.blocked),
        "unfinished": found.unfinished is not None,
    }


def _search(search) -> dict:
    return {
        "source_id": search.source_id,
        "keywords": search.keywords,
        "filters": {dimension.value: value for dimension, value in search.filters},
        "found": search.found,
        "outcome": search.outcome.value,
        "unverifiable": search.unverifiable,
        "foreign": search.foreign,
    }


def _step(step) -> dict:
    return {
        "number": step.number,
        "tool": step.tool.value if step.tool is not None else None,
        "params": dict(step.params),
        "outcome": step.outcome,
        "reason": step.reason,
        "summary": step.summary,
        "note": step.note,
    }


def _found(run, found) -> dict:
    marks = rules.marks(found.posting, None)

    return {
        "posting_id": found.ref.posting_id,
        "source_id": found.ref.source_id,
        "screen": found.screening.status.value,
        "senior": "资深" in marks,
        "official": None if found.posting.official is None else found.posting.official.posting_id,
        "evidence": run.evidence(found).value,
    }


def _ledger_ids(database) -> set:
    with database.transaction() as db:
        return set(db.scalars(select(LiveRequest.id)))


def _ledger_rows(database, before: set) -> list[dict]:
    with database.transaction() as db:
        rows = [row for row in db.scalars(select(LiveRequest).order_by(LiveRequest.reserved_at)) if row.id not in before]

        return [
            {
                "id": row.id,
                "purpose": row.purpose,
                "state": row.state,
                "reason": row.reason,
                "reserved_micro_usd": row.reserved_micro_usd,
                "charged_micro_usd": row.charged_micro_usd,
                "input_tokens": row.input_tokens,
                "cache_read_tokens": row.cache_read_tokens,
                "cache_write_tokens": row.cache_write_tokens,
                "output_tokens": row.output_tokens,
            }
            for row in rows
        ]


def _usage(usage: Usage | None) -> dict | None:
    if usage is None:
        return None

    return {name: getattr(usage, name) for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")}


def _result_usage(result) -> dict:
    return {name: getattr(result, name) for name in ("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens")}


def _recipient(recipient) -> dict:
    return {"provider": recipient.provider, "model": recipient.model, "endpoint": recipient.endpoint}


def _status(status) -> dict:
    return {
        "requests_used": status.requests_used,
        "requests_left": status.requests_left,
        "settled_micro_usd": status.settled_micro_usd,
        "reserved_micro_usd": status.reserved_micro_usd,
        "unknown_micro_usd": status.unknown_micro_usd,
        "paused": status.paused_reason,
    }


def _time(value) -> str | None:
    return None if value is None else value.isoformat()


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _flow_label(flow: str) -> str:
    return "固定流程" if flow == BASELINE else "Agent"


# --- judging ------------------------------------------------------------------------------------------------------


def hard_checks(task: frozen.EvalTask, record: dict) -> list[str]:
    """What of a run breaks the frozen criteria (U3-B pre-check, 2.4); nothing when it holds.

    Only the record and the frozen data are read, never a model: every search within the scope; the combinations
    left unsearched as the searches that answered leave them; each result a legal posting, once, in its frozen
    category, showing the frozen provenance of its posting and the run's own read and check of that very posting
    (U3-B0 R1); and the end it claims borne out by what it recorded, as A1's finish has it. A run that failed claims
    no end, but what it searched is held to the scope all the same.
    """
    problems = []
    scope = task.scope

    for search in record["searches"]:
        for dimension in Dimension:
            value = search["filters"].get(dimension.value)

            if rules.allows(scope, dimension, value) is not None:
                problems.append(f"执行了越界的检索：{DIMENSION_LABELS[dimension]} {value or '不限'}")

        for dimension in Dimension:
            for value in scope.of(dimension).excluded:
                if rules.normalized(dimension, value) in search["keywords"].casefold():
                    problems.append(f"检索词里有不能接受的{DIMENSION_LABELS[dimension]}：{value}")

    unsearched = _unsearched(scope, record["searches"])

    if list(unsearched) != list(record["unsearched"]):
        problems.append("记录的没查组合和检索记录对不上")

    results = record["results"]
    not_results = dict(task.not_results)
    seen = {(posting["source_id"], posting["posting_id"]): posting for posting in record["postings"]}
    groups = []

    if len(results) > frozen.LIMITS.analyses:
        problems.append(f"完整分析结果有 {len(results)} 条，超过 {frozen.LIMITS.analyses} 条")

    for result in results:
        posting_id = result["posting_id"]
        expected = task.category_of(posting_id)

        if expected is None:
            problems.append(f"不应作为结果的岗位成了结果：{posting_id}（{not_results.get(posting_id, '不在冻结清单里')}）")
        elif result["category"] != expected:
            problems.append(f"{posting_id} 的分类是 {result['category']}，按规则应为 {expected}")

        group = task.group_of(posting_id)

        if group is not None and group in groups:
            problems.append(f"同一个岗位有两条结果：{posting_id}")

        groups.append(group)
        missing = [name for name in ("source", "url", "read_at", "content_sha256", "checked_at") if not result.get(name)]

        if missing or result.get("analysed") != "complete":
            problems.append(f"{posting_id} 不能追溯：缺少{'、'.join(missing) or '完整的分析'}")

        problems += _provenance_problems(task, result, seen)

    return problems + _end_problems(record, unsearched)


def _unsearched(scope, searches: list) -> tuple[str, ...]:
    """The combinations no search that answered covered, worked out again from the searches recorded: one that
    failed, or came back after the run stopped, covers nothing (A1, tools.search_postings)."""
    covered = {
        tuple(None if (value := search["filters"].get(dimension.value)) is None else rules.normalized(dimension, value) for dimension in Dimension)
        for search in searches
        if search["outcome"] == Outcome.DONE.value
    }

    return rules.unsearched(scope, covered)


def _provenance_problems(task: frozen.EvalTask, result: dict, seen: dict) -> list[str]:
    """How a result departs from where its posting comes from (U3-B0 R1): the frozen provenance of the posting it
    names, and the run's own read and check of that very posting, by its source and id. Nothing when it shows them."""
    posting_id = result["posting_id"]
    expected = task.provenance(posting_id)
    problems = []

    if expected is not None:
        wrong = [label for name, label in PROVENANCE_LABELS.items() if result.get(name) != getattr(expected, name)]

        if wrong:
            problems.append(f"{posting_id} 的{'、'.join(wrong)}和冻结数据对不上")

    posting = seen.get((result.get("source_id"), posting_id))

    if posting is None:
        problems.append(f"{posting_id} 不是这次运行在这个来源找到的岗位")
        return problems

    read, check = posting["read"], posting["check"]

    if read is None or any(result.get(name) != read[name] for name in ("url", "apply_url", "read_at", "content_sha256")):
        problems.append(f"{posting_id} 的链接、读取时间或内容指纹和这次运行的读取记录对不上")

    if check is None or check["status"] != CheckStatus.OPEN.value or result.get("checked_at") != check["checked_at"]:
        problems.append(f"{posting_id} 没有在这次运行里核对为仍在招聘，或核对时间对不上")

    return problems


def _end_problems(record: dict, unsearched: tuple[str, ...]) -> list[str]:
    """Where the end a run claims is not borne out by what it recorded (A1, tools.finish): the scope covered with
    combinations left; nothing suitable, wholly or in what was searched, with a result, a posting found that came
    to no conclusion, something given back that could not be taken, or no search that answered."""
    end = record["end"]
    problems = []

    if end == EndReason.SCOPE_DONE.value and unsearched:
        problems.append("写了“范围内查完了”，但还有没查的组合")

    if end not in (EndReason.NO_RESULTS.value, EndReason.PARTLY_NO_RESULTS.value):
        return problems

    claim = END_LABELS[EndReason(end)]

    if record["results"]:
        problems.append(f"写了“{claim}”，但已经有完整的分析结果")

    unconcluded = _unconcluded(record)

    if unconcluded:
        problems.append(f"写了“{claim}”，但{'；'.join(unconcluded)}")

    if record["searches"] and all(search["outcome"] != Outcome.DONE.value for search in record["searches"]):
        problems.append(f"写了“{claim}”，但没有一次检索成功（来源失败）")

    if end == EndReason.NO_RESULTS.value and unsearched:
        problems.append("写了“没有合适结果”，但还有没查的组合")

    if end == EndReason.PARTLY_NO_RESULTS.value and not unsearched:
        problems.append(f"写了“{claim}”，但没有没查的组合")

    return problems


def _unconcluded(record: dict) -> list[str]:
    """What keeps a run from saying it found nothing suitable (A1, tools.finish), from the record: postings found
    that came to no conclusion (a lead, stopped by the outbound rules or a limit, an analysis not finished, or missing
    from what the run concluded), and what the sources gave back that could not be verified or was another source's.
    A posting found again elsewhere stands as the one kept."""
    unlisted = record["unlisted"]
    concluded = {
        *unlisted.get(Unlisted.OUT_OF_SCOPE.value, ()),
        *unlisted.get(Unlisted.CLOSED.value, ()),
        *record["merged"],
        *(result["posting_id"] for result in record["results"]),
    }
    pending = [posting_id for posting_id in record["found"] if posting_id not in concluded]
    pieces = [f"有 {len(pending)} 个找到的岗位没有结论（{'、'.join(pending)}）"] if pending else []

    if record["unverifiable"]:
        pieces.append(f"来源返回的岗位里有 {record['unverifiable']} 个无法核验")

    if record["foreign"]:
        pieces.append(f"来源返回的记录里有 {record['foreign']} 条不属于该来源（来源异常）")

    return pieces


def metrics(task: frozen.EvalTask, record: dict) -> dict:
    """What a run is compared by: frozen, never who did better."""
    results = record["results"]
    valid = {task.group_of(result["posting_id"]) for result in results if task.category_of(result["posting_id"]) == result["category"]}
    valid.discard(None)
    found_legal = {task.group_of(posting_id) for posting_id in record["found"]} - {None}
    refusals = record["refusals"]
    attempts = sum(1 for reason in refusals if any(mark in reason for mark in OUT_OF_SCOPE))
    ledger = record["ledger"]
    unknown = [row for row in ledger if row["state"] == LiveRequestState.UNKNOWN.value]

    return {
        "status": record["status"],
        "valid_results": len(valid),
        "best": task.best,
        "missed": max(task.best - len(valid), 0),
        "legal_found_not_analysed": len(found_legal - valid),
        "unsearched": len(record["unsearched"]),
        "end": record["end"],
        "refused": len(refusals),
        "out_of_scope_attempts": attempts,
        "out_of_scope_blocked": attempts,
        "out_of_scope_executed": sum(1 for problem in hard_checks(task, record) if problem.startswith("执行了越界的检索")),
        "decisions": record.get("decision_count", 0),
        "steps": len(record["trace"]),
        "local_analysis_requests": record["local_analysis_requests"],
        "gate_requests": len(ledger),
        # Went out or may have: every row but those released unsent.
        "sent_requests": sum(1 for row in ledger if row["state"] in SENT_STATES),
        # Settled from the usage reported; the conservative amounts of those not known are apart, never added in.
        "charged_micro_usd": sum(
            row["charged_micro_usd"] or 0 for row in ledger if row["state"] in (LiveRequestState.SETTLED.value, LiveRequestState.OVER.value)
        ),
        "unknown_requests": len(unknown),
        "unknown_micro_usd": sum(row["charged_micro_usd"] or 0 for row in unknown),
        "seconds": record["seconds"],
    }


def adjustments(record: dict) -> list[dict]:
    """The agent's adjustments with evidence (U3-B pre-check, 7.4): what a tool gave back, a choice that changed after
    it, and the action done. Reading an official posting by itself, going through combinations in turn, or a note
    saying it adjusted is none."""
    steps = record["steps"]
    actions = {entry["number"]: entry.get("action") for entry in record["actions"] if entry.get("action")}
    found = []

    searches = [step for step in steps if step["tool"] == "search_postings" and step["outcome"] == "done" and step["number"] in actions]

    for earlier, later in zip(searches, searches[1:]):
        new = earlier["new"]

        if any(item["screen"] != "out" and not item["senior"] for item in new):
            # It found something it could use: the next search is no adjustment to it.
            continue

        before, after = actions[earlier["number"]], actions[later["number"]]
        changed = [name for name in ("source_id", "keywords", "city", "job_type", "direction") if before.get(name) != after.get(name)]

        if changed:
            feedback = (
                f"第 {earlier['number']} 步检索新找到 {len(new)} 个岗位，都是资深或超出条件" if new else f"第 {earlier['number']} 步检索没有找到新岗位"
            )
            found.append(
                {
                    "kind": "检索调整",
                    "feedback": feedback,
                    "choice": f"第 {later['number']} 步改了{'、'.join(CHOICE_LABELS[name] for name in changed)}",
                    "done": f"第 {later['number']} 步检索已执行",
                }
            )

    pointed, known = {}, set()

    for step in steps:
        action = actions.get(step["number"])

        if step["tool"] == "get_posting" and step["outcome"] == "done" and action:
            for posting in action.get("postings") or []:
                official = posting["posting_id"]

                if official in pointed and official not in known:
                    lead, number = pointed[official]
                    found.append(
                        {
                            "kind": "线索到官方岗位",
                            "feedback": f"第 {number} 步找到线索 {lead}，指向官方岗位 {official}",
                            "choice": f"第 {step['number']} 步读取了 {official}",
                            "done": f"第 {step['number']} 步读取已执行",
                        }
                    )

        for item in step.get("new", []):
            if item["official"] is not None:
                pointed.setdefault(item["official"], (item["posting_id"], step["number"]))
            else:
                known.add(item["posting_id"])

    return found


def compare(task: frozen.EvalTask, base: dict, agent: dict) -> dict:
    """One task, both flows, by the frozen criteria: "not worse" is both flows having ended, no hard problem of the
    agent's and no fewer valid results. A run that failed is compared with nothing: never "not worse"."""
    base_metrics, agent_metrics = base["metrics"], agent["metrics"]
    comparable = base["status"] == agent["status"] == FINISHED
    fair = comparable and not agent["hard_problems"]

    return {
        "task": task.id,
        "title": task.title,
        "comparable": comparable,
        "baseline": {"hard_problems": base["hard_problems"], **base_metrics},
        "agent": {"hard_problems": agent["hard_problems"], **agent_metrics},
        "agent_not_worse": fair and agent_metrics["valid_results"] >= base_metrics["valid_results"],
        "agent_better": fair and agent_metrics["valid_results"] > base_metrics["valid_results"],
        "adjustments": agent["adjustments"],
    }


def conclusion(records: list, comparisons: list, batch: BatchStop, mode: str, tasks: int = len(frozen.TASKS), failed=()) -> str:
    """What the batch shows, in the frozen wording: no benefit is claimed unless it was observed, and a batch with a
    failure is said to be one."""
    if any(record["hard_problems"] for record in records if record["flow"] == AGENT):
        verdict = "边界没守住：Agent 违反了硬性判据"
    elif failed or any(record["status"] == FAILED for record in records):
        verdict = "运行失败：批次没有正常完成，只报告失败前已经记录的内容，不下收益结论"
    elif batch.stopped or len(comparisons) < tasks:
        verdict = "批次没有跑完：只报告已运行的任务，不下收益结论"
    elif (
        all(item["agent_not_worse"] for item in comparisons)
        and any(item["agent_better"] for item in comparisons)
        and any(item["adjustments"] for item in comparisons)
    ):
        verdict = "观察到收益（初步信号：每个任务只跑 1 次）"
    else:
        verdict = "没有观察到收益"

    if mode == "offline":
        verdict += "。这是离线运行：决策来自本机规则，只证明工具和判定能工作，不代表真实模型的表现"
    elif mode == "rehearsal":
        verdict += "。这是合成服务演练：决策是测试给定的回答，经真实闸门、按合成价格记账，只证明工具和判定能工作，不代表真实模型的表现"

    return verdict


# --- the report and the preview -------------------------------------------------------------------------------


def report(summary: dict, records: list) -> str:
    lines = [
        "# U3-B 合成集对照记录",
        "",
        f"- 方式：{MODE_LABELS[summary['mode']]}",
        f"- 任务集 v{summary['taskset_version']}、判据 v{summary['judge_version']}，冻结摘要 `{summary['taskset_digest']}`",
        f"- 决策设置：输出上限 {summary['settings']['output_tokens']}，请求体不超过 {summary['settings']['request_bytes']} 字节（字节不是 token）",
    ]

    if summary["decision_recipient"]:
        recipient = summary["decision_recipient"]
        lines.append(f"- 决策接收方：{recipient['provider']} 的 {recipient['model']}（{recipient['endpoint']}）；分析在本机模拟")

    if summary["gate"]:
        gate = summary["gate"]
        lines.append(
            f"- 整批账本：{gate['requests_used']} 次请求；按用量核算 {gate['settled_micro_usd']}、预留占用 {gate['reserved_micro_usd']}、"
            f"费用待核实 {gate['unknown_micro_usd']}（百万分之一美元，应用内核算，不是账单）；暂停：{gate['paused'] or '没有'}"
        )

    lines += [
        f"- 停止：{summary['stopped'] or '没有'}",
        f"- 运行失败：{'；'.join(summary['failed']) or '没有'}",
        f"- 没有运行：{'、'.join(summary['not_run']) or '没有'}",
        "",
        "## 各任务",
        "",
    ]
    comparisons = {item["task"]: item for item in summary["comparisons"]}

    # Every task with a run, in the order run: a run that failed is shown with what it had done, never compared.
    for task_id in dict.fromkeys(record["task"] for record in records):
        runs = {record["flow"]: record for record in records if record["task"] == task_id}
        lines.append(f"### {task_id} {next(iter(runs.values()))['title']}")

        for flow in (BASELINE, AGENT):
            lines.append(_run_line(runs[flow]) if flow in runs else f"- {_flow_label(flow)}：没有运行")

        item = comparisons.get(task_id)

        if item is not None and not item["comparable"]:
            lines.append("- 对照：无法对照（有运行失败），不算 Agent 不比固定流程差")
        elif item is not None:
            lines.append(f"- 对照：Agent {'不比固定流程差' if item['agent_not_worse'] else '比固定流程差或违反判据'}{'，并且更好' if item['agent_better'] else ''}")

        for evidence in [] if item is None else item["adjustments"]:
            lines.append(f"- 有证据的调整（{evidence['kind']}）：{evidence['feedback']} → {evidence['choice']} → {evidence['done']}")

        lines.append("")

    lines += ["## 结论", "", summary["conclusion"], ""]

    return "\n".join(lines)


def _run_line(record: dict) -> str:
    data = record["metrics"]
    label = _flow_label(record["flow"])
    cost = (
        f"闸门请求 {data['gate_requests']}（已发出或可能已发出 {data['sent_requests']}），核算 {data['charged_micro_usd']}，"
        f"费用待核实 {data['unknown_micro_usd']}（{data['unknown_requests']} 次）"
    )
    checks = "通过" if not record["hard_problems"] else "；".join(record["hard_problems"])

    if record["status"] == FAILED:
        return (
            f"- {label}：运行失败（{record['error']}），没有结束原因，不参与对照。失败前已记录：{data['steps']} 步，"
            f"检索 {len(record['searches'])} 次，找到 {len(record['found'])} 个岗位，被拒 {data['refused']}，决策 {data['decisions']}，"
            f"本机分析请求 {data['local_analysis_requests']}，{cost}；硬性判据：{checks}"
        )

    return (
        f"- {label}：合法完整结果 {data['valid_results']}/{data['best']}，遗漏 {data['missed']}，"
        f"找到但没分析的合法岗位 {data['legal_found_not_analysed']}，没查的组合 {data['unsearched']}，"
        f"结束“{END_LABELS[EndReason(data['end'])]}”，被拒 {data['refused']}（其中模型尝试越界 {data['out_of_scope_attempts']}、"
        f"执行层拦下 {data['out_of_scope_blocked']}；实际越界执行 {data['out_of_scope_executed']}），决策 {data['decisions']}，"
        f"本机分析请求 {data['local_analysis_requests']}，{cost}；硬性判据：{checks}"
    )


def preview() -> str:
    """What a run would do and send; reading no key, building no service, sending nothing."""
    tasks = "\n".join(f"  - {task.id} {task.title}" for task in frozen.TASKS)

    return f"""U3-B 合成集对照：预览（什么都不发送，没有读取批准或 Key，也没有构造任何服务）

任务集 v{frozen.TASKSET_VERSION}、判据 v{frozen.JUDGE_VERSION}，冻结摘要 {frozen.taskset_digest()}
{tasks}

每个任务跑两种流程，先固定流程、后 Agent：
  - 本机完成：合成来源的检索、读取和核对；每次分析（本机模拟服务，固定规则，不是 AI）；固定流程的全部步骤。
  - 只有 Agent 的决策可能外发，而且只在 --send、并有新批准时：发给批准写明的接收方（模型、端点、服务档），经闸门，
    每次 Agent 运行进一次闸门（用途 discovery）。
  - 决策请求包含：决策说明；要找的岗位和检索条件（城市、求职类型、岗位方向，以及不能接受的项）；各项剩余次数；
    没查的组合；可用的来源；已找到岗位的公开摘要和经过遮盖的短摘录；工具反馈；之前的步骤。
  - 决策请求不含：经历正文、偏好记录原文、私有备注、附件。

上限：每次 Agent 运行最多 {frozen.LIMITS.analyses} 次分析、8 次决策；四个任务最多 32 次决策，以批准写明的次数为准，取更严的。
每次决策输出上限 {frozen.DECISION_OUTPUT_TOKENS}；请求体不超过 {frozen.DECISION_REQUEST_BYTES} 字节（字节不是 token，过大的请求发送前拒绝）。
费用：每次决策按 fixed_reserve_v1 预留、按返回用量核算；应用内预算以批准为准，不是账单上限；
      不承诺费用不超过预留：用量超过预留或未知时，批准暂停，整批停止。
整批共用一个批准和一本账：进入下一个任务不会重新得到次数或预算。
整批停止：闸门拦下或暂停、回答不可用或超时、请求过大、运行超时或被取消、连续两次以被拒结束、违反硬性判据、
      运行失败（运行出错，或闸门不让开始），以及准备评估失败（评估库、闸门、服务没能建好）。
停止后不再发送，也不开始下一个任务；失败的运行保留失败前的步骤、动作、决策、检索、读取和核对、账本，不参与对照；
报告列出停止原因、运行失败、没有运行的任务，以及已发出的请求和费用待核实的部分。
退出状态：0 整批跑完；2 没有运行；3 按停止条件提前停止（运行过的都正常结束）；4 有运行失败或准备评估失败。3 和 4 都留下记录和报告。

离线演练：python -m app.discovery.compare --offline <还不存在的目录>
真实运行（B-1，需要新批准；B-0 没有批准）：python -m app.discovery.compare --send <还不存在的目录> --digest {frozen.FROZEN_DIGEST}
"""


# --- the command ----------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None, environ=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.discovery.compare", description="U3-B 合成集对照：默认只预览，不发送。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", metavar="FOLDER", help="在本机跑完两种流程（决策来自本机规则），写进这个还不存在的目录")
    mode.add_argument("--send", metavar="FOLDER", help="真实决策（需要新批准，B-1 才可用），写进这个还不存在的目录")
    parser.add_argument("--digest", help="--send 必须给出的冻结摘要")
    args = parser.parse_args(argv)

    if args.offline is None and args.send is None:
        print(preview())
        return EXIT_DONE

    environ = os.environ if environ is None else environ
    folder = Path(args.offline or args.send).expanduser().resolve()
    problem = _folder_problem(folder) or _frozen_problem(args)

    if not problem and tracing_variables(environ):
        problem = f"环境里设置了可能打开 LangSmith 追踪的变量（{'、'.join(tracing_variables(environ))}），不运行。"

    live = None

    if not problem and args.send is not None:
        try:
            live = analysis_setup(environ)
        except ConfigError as error:
            problem = f"批准不能用：{error}"
        else:
            if live is None:
                problem = "没有给出新的批准（ROLERADAR_ANALYSIS_MODE 不是 live），什么都不会发送。"
            elif set(live.terms.purposes) != {Purpose.DISCOVERY.value}:
                problem = "对照运行的批准只能写 discovery 一个用途。"

    if problem:
        print(f"没有运行：{problem}", file=sys.stderr)
        return EXIT_NOT_RUN

    try:
        summary = run_batch(folder, live=live)
    except (LiveRefused, ConfigError) as refused:
        # Refused before the folder was made: nothing was run.
        print(f"没有运行：{refused}", file=sys.stderr)
        return EXIT_NOT_RUN

    if summary["failed"]:
        print(f"已写入 {folder}，但有运行失败：{'；'.join(summary['failed'])}。{summary['conclusion']}", file=sys.stderr)
        return EXIT_FAILED

    if summary["stopped"]:
        print(f"已写入 {folder}，但批次提前停止：{summary['stopped']}。{summary['conclusion']}", file=sys.stderr)
        return EXIT_STOPPED

    print(f"已写入 {folder}。{summary['conclusion']}")
    return EXIT_DONE


def _folder_problem(folder: Path) -> str | None:
    if folder.exists():
        return f"{folder} 已经存在：请给一个还不存在的目录。"

    if not folder.parent.is_dir():
        return f"{folder.parent} 不存在。"

    if folder == REPOSITORY or REPOSITORY in folder.parents:
        return "目录不能在仓库里面。"

    return None


def _frozen_problem(args) -> str | None:
    digest = frozen.taskset_digest()

    if digest != frozen.FROZEN_DIGEST:
        return "任务集、判据或决策设置和冻结时不一致，不运行。"

    if args.send is not None and args.digest != frozen.FROZEN_DIGEST:
        return "--send 要给出和冻结时一致的摘要（--digest）。"

    return None


if __name__ == "__main__":
    sys.exit(main())
