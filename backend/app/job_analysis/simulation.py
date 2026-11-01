"""The simulated analysis service the pages use by default (U2-A2).

It runs inside this process: nothing goes over the network and nothing is
kept. It is not a model. It answers from fixed rules over the words it is
given, so its answers change with the posting and the experiences, and
every answer has to pass the same checks a real service's would. The pages
always say when a result came from it.

The rules, roughly: a known skill named in a line of the posting is a
requirement (preferred under a "加分" or "preferred" heading), a line about
graduation, degrees or work permission is a hard condition; an experience
sentence naming a requirement is cited, labelled by its wording; and a
preference's words found in a line of the posting are compared with it.
The simulation never judges a hard condition and never says a must-have is
met: those stay unknown or unclear.
"""
import json
import re
import threading

from app.ai_provider import AIProvider, AIProviderResponseError, GenerationResult, ProviderIdentity
from app.analysis_contracts import SIMULATION_PROVIDER, Recipient
from app.experience_analysis import (
    ANALYSIS_SCHEMA_NAME,
    EXPERIENCES_END,
    EXPERIENCES_START,
    PREFERENCES_END,
    PREFERENCES_START,
    REQUIREMENTS_END,
    REQUIREMENTS_START,
)
from app.jd_service import JOB_POSTING_END, JOB_POSTING_START, SCHEMA_NAME
from app.parser import KNOWN_SKILLS

SIMULATION_MODEL = "roleradar-rules-1"
RECIPIENT = Recipient(SIMULATION_PROVIDER, SIMULATION_MODEL)

PREFERRED_MARKERS = ("加分", "优先", "更好", "preferred", "nice to have", "bonus", "a plus")
CONDITION_MARKERS = re.compile(
    r"毕业|届|学历|本科|硕士|博士|工作许可|签证|degree|graduat|work authori[sz]ation|visa",
    re.IGNORECASE,
)
EDUCATION_MARKERS = re.compile(r"学历|本科|硕士|博士|degree", re.IGNORECASE)
PERMIT_MARKERS = re.compile(r"工作许可|签证|work authori[sz]ation|visa", re.IGNORECASE)
SENTENCE = re.compile(r"[^。！？!?；;\n]+")
BULLET = re.compile(r"^[-*•·]\s*")
WORDS = re.compile(r"[、,，;；/\s]+|和|及|与|或")
BASES = (
    ("negated", ("没有", "未使用", "没用", "不会", "did not", "didn't", "never", "haven't", "have not")),
    ("plan", ("计划", "打算", "准备学", "plan to", "will learn", "intend to")),
    ("other_person", ("队友", "同学负责", "别人", "teammate", "someone else")),
    ("claim", ("熟悉", "掌握", "精通", "了解", "familiar", "proficient", "knowledge of")),
)
# What the simulation says about a preference whose words the posting mentions.
RELATIONS = {
    "ideal": "consistent",
    "also_acceptable": "consistent",
    "cannot_accept": "conflicts",
    "less_of": "conflicts",
    # A rule cannot tell whether a must-have is met.
    "must_have": "unclear",
}


class SimulatedProvider(AIProvider):
    """Answers the two requests of a task from fixed rules.

    delay_seconds: only for trying the pages locally (web_main
    --simulation-delay): each answer then waits that long, so the waiting,
    cancelling and late answers can be seen. It is off by default.
    """

    def __init__(self, delay_seconds: float = 0.0, stop: threading.Event | None = None):
        self.delay_seconds = delay_seconds
        self.stop = stop

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(provider=SIMULATION_PROVIDER, requested_model=SIMULATION_MODEL)

    def generate_text(self, prompt, max_output_tokens=None, reasoning_effort=None, json_schema_format=None):
        if self.delay_seconds and self.stop is not None:
            self.stop.wait(self.delay_seconds)

        name = json_schema_format.name if json_schema_format is not None else ""

        if name == SCHEMA_NAME:
            text = parse_posting(_between(prompt, JOB_POSTING_START, JOB_POSTING_END))
        elif name == ANALYSIS_SCHEMA_NAME:
            text = analyse(prompt)
        else:
            raise AIProviderResponseError("The simulated service does not know this request.")

        return GenerationResult(
            text=text,
            provider=SIMULATION_PROVIDER,
            requested_model=SIMULATION_MODEL,
            model=SIMULATION_MODEL,
            latency_seconds=0.0,
            requested_max_output_tokens=max_output_tokens,
            requested_reasoning_effort=reasoning_effort,
        )


def _between(prompt: str, start: str, end: str) -> str:
    begin = prompt.index(start) + len(start)

    return prompt[begin:prompt.index(end, begin)].strip("\n")


def _skills(text: str) -> list[str]:
    return [
        skill
        for skill in KNOWN_SKILLS
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(skill)}(?![A-Za-z0-9+])", text, re.IGNORECASE)
    ]


def _is_heading(line: str) -> bool:
    return line.rstrip().endswith(("：", ":")) and len(line) <= 40


def parse_posting(posting: str) -> str:
    """A structured job description of the posting, every requirement quoting a line of it."""
    required, preferred, conditions, seen = [], [], [], set()
    education = permit = None
    in_preferred = False
    # A line without its list mark: still word for word part of the posting.
    lines = [BULLET.sub("", line.strip()) for line in posting.split("\n")]

    for line in lines:
        if not line:
            continue

        marked = any(marker in line.casefold() for marker in PREFERRED_MARKERS)

        if _is_heading(line):
            in_preferred = marked

        for skill in _skills(line):
            if skill.casefold() not in seen:
                seen.add(skill.casefold())
                (preferred if marked or in_preferred else required).append({"text": skill, "evidence_text": line})

        if CONDITION_MARKERS.search(line) and not (marked or in_preferred) and not _is_heading(line):
            conditions.append({"text": line[:120], "evidence_text": line})
            education = education or (line if EDUCATION_MARKERS.search(line) else None)
            permit = permit or (line if PERMIT_MARKERS.search(line) else None)

    first = next((line for line in lines if line), None)

    return json.dumps(
        {
            "job_title": first if first and len(first) <= 60 and not _skills(first) else None,
            "company": None,
            "location": None,
            "work_mode": None,
            "seniority": None,
            "minimum_experience": None,
            "education_requirement": education,
            "work_authorization": permit,
            "responsibilities": [],
            "required_skills": required,
            "preferred_skills": preferred,
            "hard_constraints": conditions,
            "uncertain_requirements": [],
        },
        ensure_ascii=False,
    )


def _basis(sentence: str) -> str:
    folded = sentence.casefold()

    return next((basis for basis, markers in BASES if any(marker in folded for marker in markers)), "practice")


def analyse(prompt: str) -> str:
    """An analysis answer: sentences naming each requirement, and the posting's lines on each preference."""
    requirements = json.loads(_between(prompt, REQUIREMENTS_START, REQUIREMENTS_END))
    experiences = json.loads(_between(prompt, EXPERIENCES_START, EXPERIENCES_END))
    assessments = []

    for requirement in requirements:
        citations = []

        if requirement["group"] != "hard_constraint":
            term = requirement["text"].casefold()

            for source in experiences:
                for field, text in source["fields"].items():
                    for found in SENTENCE.finditer(text):
                        sentence = found.group().strip()

                        if sentence and term in sentence.casefold() and len(citations) < 2:
                            citations.append(
                                {"source_id": source["source_id"], "field": field, "quote": sentence, "basis": _basis(sentence)}
                            )

        assessments.append({"requirement_id": requirement["requirement_id"], "citations": citations})

    answer = {"assessments": assessments}

    if PREFERENCES_START in prompt:
        posting_lines = [line.strip() for line in _between(prompt, JOB_POSTING_START, JOB_POSTING_END).split("\n")]
        answer["preferences"] = [
            _compare(preference, posting_lines)
            for preference in json.loads(_between(prompt, PREFERENCES_START, PREFERENCES_END))
        ]

    return json.dumps(answer, ensure_ascii=False)


def _compare(preference: dict, posting_lines: list[str]) -> dict:
    words = [word for word in WORDS.split(preference["text"]) if len(word) >= 2]
    quotes = [line for line in posting_lines if line and any(word.casefold() in line.casefold() for word in words)][:2]

    # Rules cannot tell what a preference or a line is about: both are "other", so the
    # relation stands or falls as before.
    return {
        "preference_id": preference["preference_id"],
        "aspect": "other",
        "relation": RELATIONS[preference["stance"]] if quotes else "not_stated",
        "job_quotes": quotes,
        "job_quotes_aspect": "other",
    }
