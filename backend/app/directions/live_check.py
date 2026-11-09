"""The U3-C4 validation of the direction workflow with a real model: the frozen synthetic cases run, kept and checked.

    python -m app.directions.live_check                      what a run would send and do; sends nothing, reads no
                                                             approval or key, makes nothing
    python -m app.directions.live_check --offline FOLDER     every case on this machine, answered by the local
                                                             simulation; nothing goes out
    python -m app.directions.live_check --send FOLDER --digest SHA
                                                             C4 only, under a new approval for direction alone: each
                                                             case's one request goes to the model the approval names

FOLDER must not exist yet: the command makes it, with its own evaluation library and ledger, and refuses one inside
the repository. The cases, their checks, the notice and the call settings are frozen (app.directions.live_cases);
a run checks them against FROZEN_DIGEST first, and --send needs the digest given and an approval naming exactly the
frozen settings for the purpose direction alone. Nothing here is a permission: the approval is the Owner's.

What goes where. Each case is one exploration of the fixed workflow (app.directions.explore), as the pages run it:
its plan and request built from the case's synthetic material, confirmed for exactly that request, and one request
that is never tried again. Offline, the local simulation answers (the workflow's default). With --send, the request
goes through the gate (Purpose.DIRECTION, one task per case, one approval and one ledger for the batch) to the
approved service, the workflow told live=True, so it answers from that service only and never from the simulation.
The direction pages are untouched: they keep the simulation, whatever this command does.

What is kept (U3-C pre-check 22.2). For every case: its synthetic material, the request and its fingerprint, the
prompt and the request body exactly as measured, the raw answer whole (the report may shorten what it shows, the
records never), its usage, the outcome and result, how each request came back, the gate's own counts and the
ledger's rows. What really went out is the gate's count (GatedProvider.handed) and the ledger's, never the
workflow's requests_sent, which also counts an attempt the gate stopped before sending. Each record is read back
after it is written: evidence that cannot be kept reliably stops the batch. A case's record is the batch's from its
start, and an answer is kept the moment the service gives it, before the gate settles (Receiving): whatever fails
after that, the case keeps what it sent and what came back (C4-0 R1). A ledger or gate state that cannot be read is
unknown, never taken for no rows, settled or nothing sent.

What stops the batch (22.2, 4). An answer that is no valid result (JSON, structure or quotes) is the case's failure,
kept, and the next case runs. Anything at the edges stops it, kept as far as it got, no further case started:
the gate refusing or pausing (even after an answer came back), a request whose usage is unknown or above its
reservation, a refusal before sending, an answer from another recipient, a service failure (a timeout, no answer,
an answer not finished), evidence or the ledger not kept, and the ledger or the gate's state not readable, as a
failure. Nothing is tried again, nothing is run again, and the checks are never changed during a run.

What a run tells. Whether each case ran; the automatic checks (live_cases.judge: exact problems, and words screened
for); and then the semantic review, which this command never does: the frozen checklist goes in the report with an
empty place for the evidence, for Claude to fill in from the records and Codex to check. A case whose automatic
checks pass is awaiting that review, never passed. And a run of this command, however it ends, says nothing of the
pages: they are not connected to a real service.

Exit status: 0 every case ran (whatever the checks show); 2 nothing was run; 3 the batch stopped at an edge, the
records kept; 4 the command or the making ready failed, the records made by then kept.
"""
import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path

from sqlalchemy import select

from app.ai_provider import AIProvider, AIProviderError, OpenAIProvider, how_far, request_bytes, responses_request
from app.analysis_contracts import AnalysisFailure, Recipient, TaskApproval
from app.directions import live_cases as frozen
from app.directions.explore import ExplorationInputs, ExplorationStatus, build_plan, build_request, fingerprint, run_exploration
from app.job_analysis.live import analysis_setup
from app.job_analysis.simulation import RECIPIENT as SIMULATION
from app.job_analysis.simulation import SimulatedProvider
from app.library.clock import SystemClock
from app.library.db import Database, make_engine
from app.library.errors import ConfigError
from app.library.models import LiveRequest, LiveRequestState, create_schema
from app.library.sends import Call, Delivery
from app.live_gate import LiveBlocked, LiveRefused, Purpose, open_gate, reservation

RECORD_VERSION = "1"
EXIT_DONE, EXIT_NOT_RUN, EXIT_STOPPED, EXIT_FAILED = 0, 2, 3, 4
EVALUATION_DB = "evaluation.db"
OFFLINE, REHEARSAL, SEND = "offline", "rehearsal", "send"
MODE_LABELS = {
    OFFLINE: "离线演练（本机模拟服务按固定规则回答，什么都没有发出）",
    REHEARSAL: "合成服务演练（测试给定的回答，经真实闸门、按合成价格记账，什么都没有发出）",
    SEND: "真实验证（经闸门发给批准写明的接收方）",
}
# How a case ran.
COMPLETE = "complete"
CONTENT_FAILED = "content_failed"
STOPPED = "stopped"
FAILED = "failed"
EXECUTION_LABELS = {
    COMPLETE: "跑完，回答通过本机核对",
    CONTENT_FAILED: "跑完，但回答没有通过本机核对（这一例不通过，接着跑下一例）",
    STOPPED: "在边界处停下（整批停止）",
    FAILED: "没能运行完（整批停止）",
}
# An answer that is no valid result: the case's failure, not the batch's (22.2, 4).
CONTENT_FAILURES = {AnalysisFailure.INVALID_JSON.value, AnalysisFailure.SCHEMA.value, AnalysisFailure.CITATION.value}
# Ledger rows whose request went out or may have: all but those released unsent.
SENT_STATES = {LiveRequestState.RESERVED.value, LiveRequestState.SETTLED.value, LiveRequestState.OVER.value, LiveRequestState.UNKNOWN.value}
# Rows that leave a request's cost not settled within its reservation: the batch stops.
UNSETTLED_STATES = {LiveRequestState.RESERVED.value, LiveRequestState.OVER.value, LiveRequestState.UNKNOWN.value}
PENDING_REVIEW = "待语义复核"
NO_REVIEW = "不适用（没有完整结果）"
UNFINISHED = "未定（这一例没有处理完：验证命令出错）"
REPOSITORY = Path(__file__).resolve().parents[3]
# What the report shows of a long text; the records keep it whole.
SHOWN_CHARS = 300


class EvidenceNotKept(Exception):
    """A record could not be written and read back as it was: the batch stops."""


class BatchStop:
    """Whether the batch has to stop, why, and whether it was a failure rather than an edge."""

    def __init__(self):
        self.reason: str | None = None
        self.failed = False

    @property
    def stopped(self) -> bool:
        return self.reason is not None

    def stop(self, reason: str, *, failed: bool = False) -> None:
        if self.reason is None:
            self.reason = reason
            self.failed = failed


class CaseSending:
    """Where a case's request is told of on its way through the gate (app.live_gate.Sending): kept as it came back."""

    def __init__(self):
        self.came = []

    def hand_on(self, call: Call) -> None:
        # The batch runs one case at a time and checks before each: nothing to keep back here.
        return None

    def came_back(self, call: Call, delivered: Delivery) -> None:
        self.came.append([call.value, delivered.value])


class Recording(AIProvider):
    """The provider the workflow is given: what it hands on, kept whole, on its way to the gate or the simulation.

    body_of(prompt, output_format) is the request body the case is measured by: under the gate the very body it
    reserves for (the service's own, with the approval's output limit and reasoning effort); offline the body the
    frozen real request would have, for the bytes it would take. Under the gate the answer is kept as the service
    gives it, before the gate settles (Receiving): a fault after it never takes the answer away, nor makes it settled.
    """

    def __init__(self, inner: AIProvider, body_of, calls: list):
        self._inner = inner
        self._body_of = body_of
        self._calls = calls

    def identity(self):
        return self._inner.identity()

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        body = self._body_of(prompt, json_schema_format)
        entry = {"prompt": prompt, "prompt_chars": len(prompt), "request_body": body, "request_bytes": request_bytes(body)}
        self._calls.append(entry)

        try:
            result = self._inner.generate_text(
                prompt, max_output_tokens=max_output_tokens, reasoning_effort=reasoning_effort, json_schema_format=json_schema_format
            )
        except LiveBlocked as blocked:
            # Stopped by the gate before it went out: released, nothing sent.
            entry["status"] = f"gate_{blocked.reason.value}"
            raise
        except AIProviderError as error:
            sent, usage = how_far(error)
            entry.update(status=f"error_{type(error).__name__}", sent=sent.value, usage=_usage(usage))
            raise
        except BaseException as error:
            # received: the service answered and the gate failed after it, settling its cost or later. The answer and
            # usage stay as the service gave them; whether its cost was settled is the ledger's to say, never assumed.
            entry["status"] = f"{'answered_then_' if entry.get('received') else ''}failed_{type(error).__name__}"
            raise

        entry.update(status="answered", **_answered(result))

        return result


class Receiving(AIProvider):
    """The service as the gate is given it in a run through the gate: its answer is kept in the case's open call the
    moment it comes back, before the gate settles the request, so a fault while settling loses nothing received.

    It only keeps what the service gives: nothing is changed, held back or sent again. The gate checks it as it would
    the service itself (app.live_gate.LiveGate._check_provider): the same identity and request body, and synthetic
    only when the service is one the gate takes for synthetic.
    """

    def __init__(self, service: AIProvider, calls: list):
        self._service = service
        self._calls = calls
        self.SYNTHETIC = getattr(service, "SYNTHETIC", False) and not isinstance(service, OpenAIProvider)

    def identity(self):
        return self._service.identity()

    def request_body(self, *args, **kwargs) -> dict:
        return self._service.request_body(*args, **kwargs)

    def count_input_tokens(self, *args, **kwargs) -> int:
        return self._service.count_input_tokens(*args, **kwargs)

    def generate_text(self, prompt, **options):
        result = self._service.generate_text(prompt, **options)

        if self._calls:
            # The call Recording opened for this request: a case has one, and it is the only one out.
            self._calls[-1].update(received=True, **_answered(result))

        return result


def _answered(result) -> dict:
    """What an answer the service gave is kept as."""
    return {
        "answer": result.text,
        "answer_chars": len(result.text),
        "provider": result.provider,
        "requested_model": result.requested_model,
        "model": result.model,
        "service_tier": result.service_tier,
        "latency_seconds": result.latency_seconds,
        "usage": _result_usage(result),
    }


def _planned_body(prompt, output_format) -> dict:
    """The body the frozen real request of this prompt would have: what its bytes are measured by offline."""
    return responses_request(
        frozen.MODEL,
        prompt,
        max_output_tokens=frozen.OUTPUT_TOKENS,
        reasoning_effort=frozen.REASONING_EFFORT,
        service_tier=frozen.SERVICE_TIER,
        json_schema_format=output_format,
    )


def _gated_body(service: AIProvider, terms, prompt, output_format) -> dict:
    """The body the gate measures and reserves for, as app.live_gate.LiveGate.reserve_for builds it."""
    return service.request_body(
        prompt,
        max_output_tokens=terms.output_limit(Purpose.DIRECTION.value),
        reasoning_effort=terms.reasoning_effort,
        json_schema_format=output_format,
    )


# --- the batch -------------------------------------------------------------------------------------------------------


def run_batch(folder: Path, *, live=None) -> dict:
    """Every frozen case, in order, into the new folder; check each; write the records, the summary and the report.

    Offline (live None): the local simulation answers, nothing leaves this machine. With live, a LiveSetup of a new
    approval for direction alone: its gate opens on the folder's own ledger, and each case's request goes through it
    to live.make_provider()'s service. Returns the summary.
    """
    if live is not None:
        # Before anything is made: an approval for this validation and its frozen settings alone.
        _check_terms(live.terms)

    folder = Path(folder)
    folder.mkdir()
    (folder / "cases").mkdir()
    db_path = folder / EVALUATION_DB
    engine = make_engine(db_path)
    batch = BatchStop()
    records = []
    gate = service = gate_status = None
    clock = SystemClock(timedelta(0))

    try:
        try:
            create_schema(engine)
            database = Database(engine)
            seeded = frozen.seed(database, clock)

            if live is not None:
                gate = open_gate(database, db_path, live.terms, live.basis, clock)
                service = live.make_provider()
        except (LiveBlocked, LiveRefused) as refused:
            batch.stop(f"闸门没有打开（{getattr(refused, 'reason', refused)}）", failed=True)
        except Exception as error:  # nothing runs on a library, gate or service not made
            batch.stop(f"准备验证时出错（{type(error).__name__}）", failed=True)

        for case in frozen.CASES:
            if batch.stopped:
                break

            try:
                _run_case(case, seeded[case.id], database, clock, batch, live, gate, service, records)
            except Exception as error:
                # A fault of the command once the case had begun: its record, as far as it got, is kept all the same.
                reason = f"验证命令出错（{type(error).__name__}）"
                batch.stop(f"{case.id}：{reason}", failed=True)

                if records and records[-1]["case"] == case.id:
                    records[-1]["error"] = records[-1]["error"] or reason
                    records[-1]["review"]["status"] = UNFINISHED

            if records and records[-1]["case"] == case.id:
                try:
                    _keep(folder / "cases" / f"{case.id}.json", records[-1])
                except EvidenceNotKept as missing:
                    batch.stop(f"{case.id} 的记录没能可靠保存（{missing}）", failed=True)
    except Exception as error:  # a fault of the command itself: what was kept stays, and is reported
        batch.stop(f"验证命令出错（{type(error).__name__}）", failed=True)
    finally:
        gate_status, closing = _finish(gate, engine, batch)

    summary = _summary(records, batch, live, gate_status, closing)

    try:
        _keep(folder / "batch.json", summary)
        (folder / "report.md").write_text(report(summary, records), encoding="utf-8")
    except (EvidenceNotKept, OSError) as missing:
        batch.stop(f"汇总没能可靠保存（{missing}）", failed=True)
        summary.update(stopped=batch.reason, failed=batch.failed)

    return summary


def _finish(gate, engine, batch: BatchStop) -> tuple[dict | None, list[str]]:
    """The gate's last status, then the gate closed and the evaluation library let go, each step whatever the others
    did. A step that fails is said, and stops the batch as a failure; a status not read is unknown, never taken for an
    empty or settled ledger, and hides none of what the cases kept (C4-0 R1)."""
    status, problems = None, []

    if gate is not None:
        try:
            status = _status(gate.status())
        except Exception as error:
            problems.append(f"最后读不出闸门和账本状态（{type(error).__name__}），账本状态未知")

        try:
            gate.close()
        except Exception as error:
            problems.append(f"关闭闸门出错（{type(error).__name__}）")

    try:
        engine.dispose()
    except Exception as error:
        problems.append(f"释放评估库出错（{type(error).__name__}）")

    for problem in problems:
        batch.stop(problem, failed=True)

    return status, problems


def _run_case(case, seeded, database, clock, batch: BatchStop, live, gate, service, records: list) -> None:
    """One case into its record, which is in records from the start: whatever fails once the case has begun, what it
    sent and what came back stay in it for the batch to keep (C4-0 R1)."""
    calls, sending, gated, outcome = [], None, None, None
    record = {
        "record_version": RECORD_VERSION,
        "case": case.id,
        "title": case.title,
        "mode": _mode(live),
        "material": {"experiences": [[category, fields] for category, fields in case.experiences], "preferences": case.preferences},
        "notice_version": frozen.NOTICE_VERSION,
        "execution": FAILED,
        "error": None,
        # The very list Recording and Receiving fill: a call and its answer are the record's as they happen.
        "calls": calls,
        "generations": _generations(calls, gated, live),
        "checks": {"passed": None, "problems": [], "flags": []},
        "review": {"status": NO_REVIEW, "items": []},
    }
    records.append(record)
    before = set()

    if live is not None:
        try:
            before = _ledger_ids(database)
        except Exception as error:
            # Its rows could not be told apart afterwards: nothing is sent.
            record["error"] = f"发送前读不出账本（{type(error).__name__}），这一例没有发送"
            batch.stop(f"{case.id}：{record['error']}", failed=True)
            return

    try:
        with database.transaction() as db:
            plan = build_plan(db, seeded.owner_id, ExplorationInputs(seeded.chosen), clock.now())

        recipient = SIMULATION if live is None else Recipient(gate.terms.provider, gate.terms.model, gate.terms.endpoint)
        request = build_request(seeded.owner_id, plan, recipient, notice_version=frozen.NOTICE_VERSION)
        record.update(recipient=_recipient(recipient), request=_request(request), fingerprint=fingerprint(request))
        # Confirmed for exactly this request: the frozen case is what the batch's approval covers.
        approval = TaskApproval(fingerprint(request))

        if live is None:
            channel = Recording(SimulatedProvider(), _planned_body, calls)
            outcome = run_exploration(database, request, approval, channel, clock=clock)
        else:
            sending = CaseSending()

            with gate.task(Purpose.DIRECTION.value, seeded.owner_id, Receiving(service, calls), sending=sending) as gated:
                channel = Recording(gated, partial(_gated_body, service, gate.terms), calls)
                outcome = run_exploration(database, request, approval, channel, clock=clock, live=True)
    except (LiveBlocked, LiveRefused) as refused:
        record["error"] = f"闸门没有放行（{getattr(refused, 'reason', refused)}）"
        record["execution"] = STOPPED
        batch.stop(f"{case.id}：{record['error']}")
    except Exception as error:  # the batch must learn that a case failed
        record["error"] = f"运行出错（{type(error).__name__}）"
        batch.stop(f"{case.id}：{record['error']}", failed=True)

    record["deliveries"] = [] if sending is None else sending.came
    record["generations"] = _generations(calls, gated, live)

    if outcome is not None:
        record["outcome"] = _outcome(outcome)
        record["execution"] = _execution(outcome)

        if record["execution"] == STOPPED:
            batch.stop(f"{case.id} 以“{outcome.status.value}（{outcome.problem}）”结束")

    if live is not None:
        # Read after the case, each on its own: one that cannot be read is unknown, never no rows, settled or
        # nothing sent, and the gate's own count above stands (11.3).
        try:
            record["ledger"] = _ledger_rows(database, before)
        except Exception as error:
            record["ledger"] = None
            record["ledger_unknown"] = f"这一例之后读不出账本（{type(error).__name__}），账本行未知"
            batch.stop(f"{case.id}：{record['ledger_unknown']}", failed=True)

        try:
            record["gate_after"] = _status(gate.status())
        except Exception as error:
            record["gate_after"] = None
            record["gate_unknown"] = f"这一例之后读不出闸门状态（{type(error).__name__}），是否暂停、用了多少都未知"
            batch.stop(f"{case.id}：{record['gate_unknown']}", failed=True)

        _edges(case, record, batch)

    record["checks"] = _checks(case, seeded, record, outcome)
    record["review"] = {
        "status": PENDING_REVIEW if record["execution"] == COMPLETE else NO_REVIEW,
        "items": [{"id": item, "text": text, "evidence": None, "verdict": None} for item, text in frozen.review_items(case.id)],
    }


def _execution(outcome) -> str:
    if outcome.status is ExplorationStatus.COMPLETE:
        return COMPLETE

    if outcome.status is ExplorationStatus.FAILED and outcome.problem in CONTENT_FAILURES:
        return CONTENT_FAILED

    return STOPPED


def _edges(case, record: dict, batch: BatchStop) -> None:
    """What the gate and the ledger say once the case is over, whatever its outcome: never ignored for an answer.
    What could not be read (None) is unknown and has stopped the batch already."""
    gate_after, rows = record["gate_after"], record["ledger"]
    handed = record["generations"]["handed"]

    if gate_after is not None and gate_after["paused"] is not None:
        batch.stop(f"{case.id} 之后批准已暂停（{gate_after['paused']}）")

    if rows is None:
        return

    sent = sum(1 for row in rows if row["state"] in SENT_STATES)

    if any(row["state"] in UNSETTLED_STATES for row in rows):
        batch.stop(f"{case.id} 的请求费用没有在预留内核算（{'、'.join(row['state'] for row in rows)}）")

    if sent != handed or handed > 1:
        batch.stop(f"{case.id} 的账本记下 {sent} 次发出，闸门数到 {handed} 次", failed=True)


def _generations(calls: list, gated, live) -> dict:
    """How many requests were asked for, kept back and really handed on (22.2, 3)."""
    if live is None:
        # Answered on this machine: nothing went out.
        return {"attempts": len(calls), "blocked": 0, "handed": 0, "local": len(calls)}

    if gated is None:
        return {"attempts": len(calls), "blocked": 0, "handed": 0, "local": 0}

    return {"attempts": gated.attempts, "blocked": gated.blocked, "handed": gated.handed, "local": 0}


def _checks(case, seeded, record: dict, outcome) -> dict:
    """The automatic checks of the case: exact problems and screen hits; none when no result is there to check."""
    problems, flags = [], []

    for index, call in enumerate(record["calls"], start=1):
        if call["request_bytes"] > frozen.REQUEST_BYTES:
            problems.append(f"第 {index} 个请求体 {call['request_bytes']} 字节，超过 {frozen.REQUEST_BYTES}")

    if record["execution"] == CONTENT_FAILED:
        problems.append(f"回答没有通过本机核对（{outcome.problem}）")
    elif record["execution"] == COMPLETE:
        # Missing evidence passes nothing (22.2, 3).
        problems += _evidence_problems(record)
        judgement = frozen.judge(case, outcome.result, seeded.categories)
        problems += judgement.problems
        flags += judgement.flags

    passed = None if record["execution"] not in (COMPLETE, CONTENT_FAILED) else not problems

    return {"passed": passed, "problems": problems, "flags": flags}


def _evidence_problems(record: dict) -> list[str]:
    calls = record["calls"]

    if len(calls) != 1:
        return [f"应有 1 个请求的记录，实有 {len(calls)} 个"]

    call = calls[0]
    needed = ("prompt", "request_body", "answer", "usage")

    if any(call.get(name) in (None, "") for name in needed) or call.get("answer_chars") != len(call.get("answer") or ""):
        return ["证据不完整：提示词、请求体、原始回答或用量缺失"]

    return []


# --- what a record holds ---------------------------------------------------------------------------------------------


def _request(request) -> dict:
    preferences = request.preferences

    return {
        "owner_id": request.owner_id,
        "purpose": request.purpose,
        "mode": request.mode,
        "notice_version": request.notice_version,
        "prompt_version": request.prompt_version,
        "sources": [{"item_id": s.item_id, "version_id": s.version_id, "fields": list(s.fields)} for s in request.sources],
        "preferences": {
            "record_id": preferences.record_id,
            "revision": preferences.revision,
            "version_id": preferences.version_id,
            "statuses": [list(pair) for pair in preferences.statuses],
            "sent": [[item.field, item.text] for item in preferences.sent],
        },
    }


def _outcome(outcome) -> dict:
    return {
        "status": outcome.status.value,
        "problem": outcome.problem,
        # The workflow's own count: a request the gate stopped before sending counts here too.
        "workflow_requests_sent": outcome.requests_sent,
        "experiences_sent": outcome.experiences_sent,
        "preferences_sent": outcome.preferences_sent,
        "set_aside": list(outcome.set_aside),
        "result": None if outcome.result is None else outcome.result.model_dump(mode="json"),
        "trusted": outcome.trusted is not None,
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


def _usage(usage) -> dict | None:
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


def _mode(live) -> str:
    if live is None:
        return OFFLINE

    return REHEARSAL if live.terms.prices.synthetic else SEND


def _keep(path: Path, data: dict) -> None:
    """Write a record and read it back: what cannot be kept as it is stops the batch (22.2, 4)."""
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    try:
        path.write_text(text, encoding="utf-8")
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvidenceNotKept(type(error).__name__) from None

    if kept != json.loads(text):
        raise EvidenceNotKept("读回的内容不一致")


# --- the approval ------------------------------------------------------------------------------------------------------


def _check_terms(terms) -> None:
    """A real run's approval is for this validation alone: the purpose direction and nothing else, and, with real prices,
    exactly the frozen call settings, with room in its budget for every case's reservation."""
    if set(terms.purposes) != {Purpose.DIRECTION.value}:
        raise LiveRefused("方向验证的批准只能写 direction 一个用途。")

    if terms.prices.synthetic:
        # A rehearsal's made-up approval (the tests'): only its purpose is held to this validation.
        return

    wanted = {
        "provider": frozen.PROVIDER,
        "model": frozen.MODEL,
        "endpoint": frozen.ENDPOINT,
        "service_tier": frozen.SERVICE_TIER,
        "reasoning_effort": frozen.REASONING_EFFORT,
        "max_output_tokens": frozen.OUTPUT_TOKENS,
        "timeout_seconds": float(frozen.TIMEOUT_SECONDS),
        "max_requests": frozen.MAX_REQUESTS,
        "max_cost_micro_usd": frozen.MAX_COST_MICRO_USD,
        "max_payload_bytes": frozen.REQUEST_BYTES,
    }
    differing = [name for name, value in wanted.items() if getattr(terms, name) != value]

    if differing:
        raise LiveRefused("批准和冻结的调用设置不一致：" + "、".join(differing) + "。")

    each = reservation(terms.reserve_input_tokens, terms.max_output_tokens, terms.prices)

    if each * terms.max_requests > terms.max_cost_micro_usd:
        raise LiveRefused(
            f"按这份批准的单价，每次预留 {_dollars(each)}，{terms.max_requests} 次共 {_dollars(each * terms.max_requests)}，"
            f"超过应用内预算 {_dollars(terms.max_cost_micro_usd)}：单价变了要重算并重新确认。"
        )


def _dollars(micro: int) -> str:
    return f"${micro / 1_000_000:.2f}"


# --- the summary and the report -----------------------------------------------------------------------------------------


def _summary(records: list, batch: BatchStop, live, gate_status, closing: list) -> dict:
    ran = {record["case"] for record in records}
    complete = [record for record in records if record["execution"] == COMPLETE]

    return {
        "record_version": RECORD_VERSION,
        "caseset_version": frozen.CASESET_VERSION,
        "judge_version": frozen.JUDGE_VERSION,
        "caseset_digest": frozen.caseset_digest(),
        "mode": _mode(live),
        "notice": {"version": frozen.NOTICE_VERSION, "text": list(frozen.NOTICE)},
        "recipient": None if live is None else {"provider": live.terms.provider, "model": live.terms.model, "endpoint": live.terms.endpoint},
        "approval": None if live is None else live.terms.snapshot(),
        # None through a gate: its last status could not be read (closing says why), unknown.
        "gate": gate_status,
        "closing": closing,
        "stopped": batch.reason,
        "failed": batch.failed,
        "not_run": [case.id for case in frozen.CASES if case.id not in ran],
        "cases": [
            {
                "case": record["case"],
                "execution": record["execution"],
                "error": record["error"],
                "request_bytes": [call["request_bytes"] for call in record["calls"]],
                "generations": record["generations"],
                "checks_passed": record["checks"]["passed"],
                "problems": len(record["checks"]["problems"]),
                "flags": len(record["checks"]["flags"]),
                "review": record["review"]["status"],
            }
            for record in records
        ],
        "handed": sum(record["generations"]["handed"] for record in records),
        "attempts": sum(record["generations"]["attempts"] for record in records),
        "conclusion": conclusion(records, batch, _mode(live), len(complete)),
    }


def conclusion(records: list, batch: BatchStop, mode: str, complete: int) -> str:
    ran = len(records)
    passed = sum(1 for record in records if record["checks"]["passed"])
    head = f"{MODE_LABELS[mode]}：运行了 {ran}/{len(frozen.CASES)} 例，{complete} 例得到完整结果，自动检查 {passed} 例没有发现问题。"

    if batch.stopped:
        head += f"整批{'因失败' if batch.failed else '在边界处'}停止：{batch.reason}。"

    return head + (
        "自动检查通过不等于语义通过，也不等于整批通过：每一例都待 Claude 按核对表逐项给出证据、Codex 复核。"
        "这是命令行验证，方向探索网页仍只用本机模拟服务。"
    )


def report(summary: dict, records: list) -> str:
    lines = [
        "# U3-C4 方向探索真实验证：记录和自动检查",
        "",
        f"- 方式：{MODE_LABELS[summary['mode']]}",
        f"- 样例集 v{summary['caseset_version']}，判据 v{summary['judge_version']}，冻结摘要 `{summary['caseset_digest']}`",
        f"- 处理说明 `{summary['notice']['version']}`",
        f"- 真实发出的生成请求（闸门计数）：{summary['handed']} 次；请求尝试：{summary['attempts']} 次；计数请求 0 次",
        f"- 结论：{summary['conclusion']}",
    ]

    if summary["gate"] is not None:
        gate = summary["gate"]
        lines.append(
            f"- 账本：已用 {gate['requests_used']} 次、剩 {gate['requests_left']} 次；已核算 {_dollars(gate['settled_micro_usd'])}，"
            f"未结 {_dollars(gate['reserved_micro_usd'])}，待核实 {_dollars(gate['unknown_micro_usd'])}；暂停：{gate['paused'] or '没有'}"
            "（应用内核算，不是账单）"
        )
    elif summary["mode"] != OFFLINE:
        lines.append("- 账本：没有读到最后的状态，未知（不当作空账本，也不当作已结算）")

    if summary["closing"]:
        lines.append("- 收尾出错：" + "；".join(summary["closing"]))

    if summary["not_run"]:
        lines.append(f"- 没有运行：{'、'.join(summary['not_run'])}")

    for record in records:
        lines += _case_lines(record)

    lines += ["", f"_{frozen.OBSERVATIONS}_", ""]

    return "\n".join(lines)


def _case_lines(record: dict) -> list[str]:
    checks, outcome = record["checks"], record.get("outcome") or {}
    generations = record["generations"]
    lines = [
        "",
        f"## {record['case']} {record['title']}",
        "",
        f"- 运行：{EXECUTION_LABELS[record['execution']]}" + (f"（{record['error']}）" if record["error"] else ""),
        f"- 结束状态：{outcome.get('status', '没有')}" + (f"（{outcome['problem']}）" if outcome.get("problem") else ""),
        f"- 请求：真实发出 {generations['handed']} 次，尝试 {generations['attempts']} 次，闸门拦下 {generations['blocked']} 次，"
        f"本机模拟回答 {generations['local']} 次；请求体 {'、'.join(str(call['request_bytes']) for call in record['calls']) or '无'} 字节",
    ]

    for call in record["calls"]:
        if (call.get("status") or "").startswith("answered_then_"):
            lines.append(f"- 服务的回答已收到并保存（{call['answer_chars']} 字），之后闸门出错：不算结算成功，结算情况看账本行")

    if "ledger" in record:
        rows = record["ledger"]
        lines.append(f"- 账本行：{record['ledger_unknown']}" if rows is None else "- 账本行：" + ("、".join(row["state"] for row in rows) or "没有"))

    if record.get("gate_unknown"):
        lines.append(f"- 闸门：{record['gate_unknown']}")

    lines += [
        f"- 本机规则降级的引文：{outcome.get('set_aside', [])}",
        "- 自动检查："
        + (
            ("没有完整结果，未判定" if record["execution"] != COMPLETE else "没有做完，未判定")
            if checks["passed"] is None
            else ("没有发现问题" if checks["passed"] else f"{len(checks['problems'])} 个问题")
        ),
    ]
    lines += [f"  - {problem}" for problem in checks["problems"]]

    if checks["flags"]:
        lines.append(f"- 筛查标记（给语义复核看，不是结论）：{len(checks['flags'])} 处")
        lines += [f"  - {_shown(flag)}" for flag in checks["flags"]]

    result = outcome.get("result")

    if result:
        lines.append("- 方向：" + "、".join(f"“{direction['name']}”" for direction in result["directions"]))

    lines.append(f"- 语义复核：{record['review']['status']}")
    lines += [f"  - [ ] {item['id']} {item['text']}　证据：（待填）" for item in record["review"]["items"]]

    return lines


def _shown(text: str) -> str:
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "…（记录里是全文）"


def preview() -> str:
    """What a run would do and send; reading no approval or key, making nothing, sending nothing."""
    cases = []

    for case in frozen.CASES:
        sent = "、".join(f"{category}（{'、'.join(fields)}）" for category, fields in case.experiences) or "不选经历"
        prefs = "、".join(case.preferences) if case.preferences else "没有偏好记录"
        cases.append(f"  - {case.id} {case.title}：经历 {sent}；偏好 {prefs}")

    notice = "\n".join(f"  {index}. {text}" for index, text in enumerate(frozen.NOTICE, start=1))

    return f"""U3-C4 方向探索真实验证：预览（什么都不发送，没有读取批准或 Key，也没有建任何文件）

样例集 v{frozen.CASESET_VERSION}、判据 v{frozen.JUDGE_VERSION}，冻结摘要 {frozen.caseset_digest()}
{chr(10).join(cases)}

每例是一次方向探索的固定工作流，和网页上的一样：按样例的合成资料建发送计划和请求，确认的正是这个请求，只发一个请求，不重试。
  - 发出的：所选经历的全部栏目、偏好里填写的栏目（原文），以及工作流的说明和输出结构；没填写的偏好只写“尚未想好”。
  - 不发的：私有备注、附件、基本资料、以前的探索和决定；也不发任何编号（经历用 S1、偏好用 F1 这类代称）。
  - 离线（--offline）由本机模拟服务按固定规则回答；真实验证（--send，要新批准）经闸门发给批准写明的接收方，用途 direction。
  - 方向探索网页不受影响：仍只用本机模拟服务。

处理说明 {frozen.NOTICE_VERSION}（真实验证前由 Owner 确认，只限这批合成资料）：
{notice}

调用设置（冻结；批准必须逐项一致）：{frozen.PROVIDER} {frozen.MODEL}，地址 {frozen.ENDPOINT}，服务档 {frozen.SERVICE_TIER}，推理 {frozen.REASONING_EFFORT}；
  输出上限 {frozen.OUTPUT_TOKENS}；超时 {frozen.TIMEOUT_SECONDS} 秒（SDK 对每个阶段的设置，不是整个请求的耗时上限）；
  最多 {frozen.MAX_REQUESTS} 次生成、0 次计数；应用内预算 {_dollars(frozen.MAX_COST_MICRO_USD)}（不是账单上限）；请求体不超过 {frozen.REQUEST_BYTES} 字节（字节不是 token）。
记录：每例的合成资料、请求和指纹、提示词和请求体、原始回答全文、用量、结束状态和结果、闸门计数、账本行；每份记录写完再读回核对。
停下的规则：回答不合格（JSON、结构、引文）记为这一例不通过，接着跑下一例；闸门拦下或暂停（即使回答已经返回）、用量待核实或超出预留、
  发送前就被拒、接收方不一致、服务失败（超时、没有回答、没写完）、证据或账本没能保存，整批停止，不重试，不补跑。
结论：自动检查只是筛查，通过的例子“待语义复核”：由 Claude 按冻结的核对表逐项给证据、Codex 复核；不能据此说整批通过，也不能说网页已接真实服务。
退出状态：0 每例都跑了；2 没有运行；3 在边界处停止；4 命令或准备失败。2 以外都留下记录和报告。

离线演练：python -m app.directions.live_check --offline <还不存在的目录>
真实验证（C4，需要新批准；C4-0 没有批准）：python -m app.directions.live_check --send <还不存在的目录> --digest {frozen.FROZEN_DIGEST or '（冻结后填写）'}
"""


# --- the command -------------------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None, environ=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.directions.live_check", description="U3-C4 方向探索真实验证：默认只预览，不发送。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--offline", metavar="FOLDER", help="在本机跑完四例（本机模拟服务回答），写进这个还不存在的目录")
    mode.add_argument("--send", metavar="FOLDER", help="真实验证（需要新批准，C4 才可用），写进这个还不存在的目录")
    parser.add_argument("--digest", help="--send 必须给出的冻结摘要")
    args = parser.parse_args(argv)

    if args.offline is None and args.send is None:
        print(preview())
        return EXIT_DONE

    environ = os.environ if environ is None else environ
    folder = Path(args.offline or args.send).expanduser().resolve()
    problem = _folder_problem(folder) or _frozen_problem(args)
    live = None

    if not problem and args.send is not None:
        try:
            live = analysis_setup(environ)
        except ConfigError as error:
            problem = f"批准不能用：{error}"
        else:
            if live is None:
                problem = "没有给出新的批准（ROLERADAR_ANALYSIS_MODE 不是 live），什么都不会发送。"

    if problem:
        print(f"没有运行：{problem}", file=sys.stderr)
        return EXIT_NOT_RUN

    try:
        summary = run_batch(folder, live=live)
    except LiveRefused as refused:
        # Refused before the folder was made: nothing was run.
        print(f"没有运行：{refused}", file=sys.stderr)
        return EXIT_NOT_RUN

    if summary["stopped"] and summary["failed"]:
        print(f"已写入 {folder}，但没能运行完：{summary['stopped']}。{summary['conclusion']}", file=sys.stderr)
        return EXIT_FAILED

    if summary["stopped"]:
        print(f"已写入 {folder}，但整批在边界处停止：{summary['stopped']}。{summary['conclusion']}", file=sys.stderr)
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
    digest = frozen.caseset_digest()

    if digest != frozen.FROZEN_DIGEST:
        return "样例、判据、处理说明或调用设置和冻结时不一致，不运行。"

    if args.send is not None and args.digest != frozen.FROZEN_DIGEST:
        return "--send 要给出和冻结时一致的摘要（--digest）。"

    return None


if __name__ == "__main__":
    sys.exit(main())
