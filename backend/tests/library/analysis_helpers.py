"""Shared pieces of the U2-A1 analysis tests: the simulated service, a
synthetic posting with its parsed form, and confirmed synthetic experiences.

The simulated service answers from a script and keeps every prompt it was
given; nothing leaves the process. Its identity and metadata say
simulation, with no made-up tokens or latency.
"""
import json

from app.ai_provider import AIProvider, GenerationResult, ProviderIdentity
from app.analysis_contracts import (
    SIMULATION_PROVIDER,
    AnalysisRequest,
    Recipient,
    SelectedSource,
    TaskApproval,
    task_fingerprint,
)
from app.library.content import parse_experience_form
from app.library.service import active_item, confirm_experience, create_experience

SIMULATION = Recipient(provider=SIMULATION_PROVIDER, model="simulation")


class ScriptedProvider(AIProvider):
    """Answers from a script, in order: text, an exception to raise, or a function of the prompt.

    provider and model are what it is configured for, as identity() tells
    before anything is sent. answered_as, a (provider, model) pair, is what
    its answers report having requested instead, as a faulty adapter might;
    returned_model is the version its answers name (by default the model).
    """

    def __init__(self, *answers, model="simulation", provider=SIMULATION_PROVIDER, answered_as=None, returned_model=None):
        self.answers = list(answers)
        self.prompts = []
        self.model = model
        self.provider = provider
        self.answered_as = answered_as
        self.returned_model = returned_model

    def identity(self):
        return ProviderIdentity(provider=self.provider, requested_model=self.model)

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        self.prompts.append(prompt)
        answer = self.answers.pop(0)

        if callable(answer):
            answer = answer(prompt)

        if isinstance(answer, Exception):
            raise answer

        provider, requested_model = self.answered_as or (self.provider, self.model)

        return GenerationResult(
            text=answer,
            provider=provider,
            requested_model=requested_model,
            model=self.returned_model or requested_model,
            latency_seconds=0.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
        )


POSTING = """数据分析实习生（合成岗位）
合成科技 - 上海（混合办公）

岗位要求：
- 熟练使用 Python 进行数据处理
- 掌握 SQL 查询
- 2027 年及以后毕业

加分项：
- 了解 Docker

有云平台经验更好，我们也会培训。
"""


def parsed_posting(**overrides) -> str:
    """What a parsing model returns for POSTING: every requirement quotes it."""
    payload = {
        "job_title": "数据分析实习生（合成岗位）",
        "company": "合成科技",
        "location": "上海",
        "work_mode": "hybrid",
        "seniority": "intern",
        "minimum_experience": None,
        "education_requirement": "2027 年及以后毕业",
        "work_authorization": None,
        "responsibilities": [],
        "required_skills": [
            {"text": "Python", "evidence_text": "熟练使用 Python 进行数据处理"},
            {"text": "SQL", "evidence_text": "掌握 SQL 查询"},
        ],
        "preferred_skills": [{"text": "Docker", "evidence_text": "了解 Docker"}],
        "hard_constraints": [{"text": "2027 年及以后毕业", "evidence_text": "2027 年及以后毕业"}],
        "uncertain_requirements": [{"text": "云平台经验", "evidence_text": "有云平台经验更好"}],
    }
    payload.update(overrides)

    return json.dumps(payload, ensure_ascii=False)


def cite(source_id: str, field: str, quote: str, basis: str) -> dict:
    return {"source_id": source_id, "field": field, "quote": quote, "basis": basis}


def assessed(**citations) -> str:
    """An analysis answer: requirement id -> list of citations (none: nothing found)."""
    return json.dumps(
        {"assessments": [{"requirement_id": requirement_id, "citations": cited} for requirement_id, cited in citations.items()]},
        ensure_ascii=False,
    )


def confirmed(database, owner, clock, category, private_note="", **fields) -> tuple[str, str]:
    """Save and confirm a synthetic experience; return its id and confirmed version id."""
    with database.transaction() as db:
        item = create_experience(db, owner, parse_experience_form({**fields, "private_note": private_note}, category), clock.now())

    with database.transaction() as db:
        confirm_experience(db, owner, item.id, active_item(db, owner, item.id).revision, clock.now())

    with database.transaction() as db:
        return item.id, active_item(db, owner, item.id).confirmed_version_id


def request_for(owner, chosen, posting=POSTING, recipient=SIMULATION, **task) -> AnalysisRequest:
    return AnalysisRequest(
        owner_id=owner,
        selection=tuple(SelectedSource(item_id, version_id) for item_id, version_id in chosen),
        job_posting=posting,
        recipient=recipient,
        **task,
    )


def approved(request: AnalysisRequest) -> TaskApproval:
    """A confirmation of exactly this task, as a test builds it (not a user's permission)."""
    return TaskApproval(task_fingerprint(request))
