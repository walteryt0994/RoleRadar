"""What the local simulation answers to a direction exploration (U3-C2): fixed rules, not a model.

It runs inside this process through app.job_analysis.simulation.SimulatedProvider, which hands it the requests
of this schema; nothing goes over the network and nothing is kept. Every answer it gives passes the same checks
a real service's answer would (app.directions.explore).

The rules, roughly: a small fixed catalogue of general directions, each with a few words. The directions whose
words the material uses most are chosen (the experiences sent, and what the preferences sent say is ideal or
also acceptable about interests and work), three to five of them, and fixed fallbacks make up three. A sentence
of an experience field using a direction's words is quoted once in that direction, labelled by its wording as
the job analysis simulation labels it. A preference about interests or work using them is related to the
direction, fitting or possibly in conflict by its stance, on the direction's own fixed basis; the other
preferences sent are related to the first direction as unknown.

The material is read by its JSON structure, so markers in the user's own words are only text. A prompt that
does not carry it whole is a damaged request, answered with the service's error, never with a result.

The catalogue holds no AI or FDE direction, so that no one's own direction becomes everyone's default answer.
It is a simulation's catalogue, not a list of allowed directions: nothing in the contracts or the local checks
limits what a direction may be. Its answers show that the workflow and its checks work, not what a model would
say.
"""
import json
import re
from dataclasses import dataclass
from typing import NotRequired, TypedDict

from pydantic import TypeAdapter, ValidationError

from app.ai_provider import AIProviderResponseError
# SCHEMA_NAME is what app.job_analysis.simulation compares a request's schema with before handing it here.
from app.directions.explore import MATERIAL_END, MATERIAL_START, OUTPUT_LANGUAGE_LINES, QUOTE_CHARS, SCHEMA_NAME  # noqa: F401
from app.job_analysis.simulation import BASES, SENTENCE


@dataclass(frozen=True)
class _Entry:
    name: str
    words: tuple[str, ...]
    main_work: str
    basis: str
    gaps: tuple[str, ...]
    unknowns: tuple[str, ...]
    difference: str
    next_steps: tuple[str, ...]


CATALOGUE = (
    _Entry(
        "数据分析",
        ("数据", "分析", "sql", "python", "excel", "报表", "统计", "可视化"),
        "整理和清洗业务数据，做统计分析，制作报表和图表，把结论讲给业务同事听。",
        "这个方向的日常通常是处理数据、做统计分析和写报告",
        ("一般需要熟练使用 SQL 和一种分析工具", "需要能把分析结论讲清楚、讲给不懂数据的人听"),
        ("不同行业和团队对工具和业务知识的要求差别很大", "城市、类型和入职时间要看具体岗位"),
        "比用户研究更偏向处理数字和报表，比产品运营更少做活动执行。",
        ("找一份公开数据集，从清洗到结论完整做一次分析", "读几份数据分析岗位的描述，记下反复出现的要求"),
    ),
    _Entry(
        "用户研究",
        ("访谈", "调研", "问卷", "用户", "可用性", "观察"),
        "设计和执行用户访谈、问卷和可用性测试，整理发现，帮助团队理解用户的需要。",
        "这个方向的日常通常是和用户交谈、观察使用过程并整理发现",
        ("一般需要掌握访谈和问卷的基本方法", "需要能把零散的发现整理成团队能用的结论"),
        ("有的团队偏定性研究，有的偏定量，做法差别较大", "城市、类型和入职时间要看具体岗位"),
        "比数据分析更多直接和人打交道，比产品运营更少负责上线后的执行。",
        ("找三位同学做一次小型访谈，练习提问和整理", "读几份用户研究报告，看它们怎样从发现走到建议"),
    ),
    _Entry(
        "产品运营",
        ("运营", "活动", "社群", "增长", "推广"),
        "策划和执行活动、维护用户社群，跟踪数据，想办法让更多用户留下来并用起来。",
        "这个方向的日常通常是策划活动、维护社群和跟进效果",
        ("一般需要能独立策划并落地一次活动", "需要能看懂基础的数据，判断活动效果"),
        ("不同产品的运营重点不同，有的偏内容，有的偏活动", "城市、类型和入职时间要看具体岗位"),
        "比内容策划更关注活动效果和用户留存，比市场营销更贴近产品本身的使用。",
        ("给一个社团或小群策划一次线上活动，并记录效果", "读几篇运营复盘，看他们怎样衡量一次活动"),
    ),
    _Entry(
        "内容策划与编辑",
        ("写作", "编辑", "内容", "文案", "公众号", "采访"),
        "选题、写作和编辑文章或视频脚本，维护内容栏目，根据读者反馈调整方向。",
        "这个方向的日常通常是选题、写作和修改稿件",
        ("一般需要有稳定产出的作品", "需要了解目标读者，能调整写作风格"),
        ("不同平台对内容形式和节奏的要求不同", "城市、类型和入职时间要看具体岗位"),
        "比产品运营更专注内容本身，比市场营销更少涉及投放和预算。",
        ("连续一个月每周写一篇短文，观察读者的反应", "挑三个喜欢的栏目，分析它们的选题方式"),
    ),
    _Entry(
        "项目协调与管理",
        ("协调", "项目", "进度", "组织", "统筹", "沟通"),
        "拆分任务、排定进度，协调不同成员和部门，跟进风险，让项目按时完成。",
        "这个方向的日常通常是安排任务、跟进进度和协调各方",
        ("一般需要能把一件事拆成可以跟进的步骤", "需要能在意见不同时推动大家达成一致"),
        ("不同团队对项目管理方法和工具的要求不同", "城市、类型和入职时间要看具体岗位"),
        "比其他方向更少做具体的专业产出，更多负责让大家把事情做完。",
        ("在下一次小组作业里主动负责排进度和跟进", "了解一种常用的项目管理方法，比如看板"),
    ),
    _Entry(
        "教育培训",
        ("教学", "助教", "辅导", "课程", "培训", "讲课"),
        "设计和讲授课程或培训，辅导学员，根据反馈调整教学内容。",
        "这个方向的日常通常是备课、讲课和辅导学员",
        ("一般需要有讲课或辅导的实际经验", "需要能把复杂内容讲得简单清楚"),
        ("学校、培训机构和企业内训的要求差别很大", "城市、类型和入职时间要看具体岗位"),
        "比内容策划更多面对面讲解，比项目协调更专注某一门知识。",
        ("找机会做一次助教或给低年级同学讲一次课", "听几节公开课，留意讲课的人怎样安排节奏"),
    ),
    _Entry(
        "市场营销",
        ("市场", "品牌", "营销", "广告", "传播"),
        "研究目标人群，策划品牌传播和推广活动，评估效果，配合销售和产品。",
        "这个方向的日常通常是策划推广、做传播和评估效果",
        ("一般需要了解基本的营销方法和渠道", "需要能根据效果数据调整方案"),
        ("不同公司的市场部门分工差别很大，有的偏品牌，有的偏投放", "城市、类型和入职时间要看具体岗位"),
        "比产品运营更面向还没有用产品的人，比内容策划更关注推广效果。",
        ("拆解一个你喜欢的品牌最近的一次推广", "读几份市场类岗位的描述，记下常见要求"),
    ),
)
# What a task asking for English results gets instead (UI bilingual batch, plan 6.3), by the Chinese name: the same
# directions in English, with English words matched besides the Chinese ones. Only such a task reads them, so a
# Chinese task is answered exactly as before. Quotes are never translated: they stay as the material wrote them.
ENGLISH = {
    "数据分析": _Entry(
        "Data analysis",
        ("data", "analysis", "analytics", "report", "statistic", "visuali", "dashboard"),
        "Cleaning and organizing business data, running statistical analysis, building reports and charts, and "
        "explaining the findings to colleagues.",
        "Day to day, this direction usually means working with data, running statistical analysis and writing reports",
        ("Usually needs fluent SQL and one analysis tool", "Needs to explain findings clearly, including to people who do not work with data"),
        ("Tool and domain requirements differ a lot between industries and teams", "City, job type and start date depend on the specific posting"),
        "More focused on numbers and reports than user research, with less event work than product operations.",
        ("Take a public dataset and carry one analysis from cleaning to conclusions", "Read a few data analyst postings and note the requirements that keep coming up"),
    ),
    "用户研究": _Entry(
        "User research",
        ("interview", "survey", "questionnaire", "user", "usability", "observ"),
        "Designing and running user interviews, surveys and usability tests, organizing the findings and helping the "
        "team understand what users need.",
        "Day to day, this direction usually means talking with users, watching how they use things and organizing the findings",
        ("Usually needs the basic methods of interviews and surveys", "Needs to turn scattered findings into conclusions a team can use"),
        ("Some teams lean qualitative and some quantitative, so the work varies a lot", "City, job type and start date depend on the specific posting"),
        "More direct contact with people than data analysis, with less responsibility for running things after launch than product operations.",
        ("Run a small interview with three classmates to practice asking and summarising", "Read a few user research reports and see how they move from findings to recommendations"),
    ),
    "产品运营": _Entry(
        "Product operations",
        ("operation", "event", "campaign", "community", "growth", "promotion"),
        "Planning and running events, looking after user communities, tracking the numbers and finding ways to keep "
        "more users engaged.",
        "Day to day, this direction usually means planning events, looking after communities and following up on results",
        ("Usually needs to plan and deliver an event on your own", "Needs to read basic data to judge how an event went"),
        ("Operations focus differs by product: some lean on content, some on events", "City, job type and start date depend on the specific posting"),
        "More focused on event results and user retention than content planning, and closer to the product itself than marketing.",
        ("Plan an online event for a club or small group and record how it went", "Read a few operations retrospectives and see how they measure an event"),
    ),
    "内容策划与编辑": _Entry(
        "Content planning and editing",
        ("writing", "editing", "editor", "content", "copywriting", "article", "newsletter"),
        "Choosing topics, writing and editing articles or video scripts, running content columns and adjusting "
        "direction from reader feedback.",
        "Day to day, this direction usually means choosing topics, writing and revising drafts",
        ("Usually needs a steady body of published work", "Needs to know the target readers and adapt the writing style"),
        ("Platforms differ in the form and pace of content they expect", "City, job type and start date depend on the specific posting"),
        "More focused on the content itself than product operations, and less involved in ad spend and budgets than marketing.",
        ("Write one short piece a week for a month and watch how readers respond", "Pick three columns you like and analyze how they choose topics"),
    ),
    "项目协调与管理": _Entry(
        "Project coordination and management",
        ("coordinat", "project", "schedule", "organiz", "organis", "planning", "communicat"),
        "Breaking work into tasks, setting schedules, coordinating people and departments, following up on risks and "
        "keeping projects on time.",
        "Day to day, this direction usually means assigning tasks, tracking progress and coordinating everyone involved",
        ("Usually needs to break a piece of work into steps that can be tracked", "Needs to move people towards agreement when opinions differ"),
        ("Teams differ in the project management methods and tools they expect", "City, job type and start date depend on the specific posting"),
        "Less hands-on specialist output than the other directions, and more about getting things done together.",
        ("In your next group assignment, volunteer to set the schedule and follow up", "Learn one common project management method, such as a kanban board"),
    ),
    "教育培训": _Entry(
        "Education and training",
        ("teaching", "teach", "tutor", "course", "training", "lecture"),
        "Designing and delivering courses or training, coaching learners and adjusting the content from their feedback.",
        "Day to day, this direction usually means preparing lessons, teaching and coaching learners",
        ("Usually needs real experience of teaching or tutoring", "Needs to explain complex material simply and clearly"),
        ("Schools, training companies and in-house training expect very different things", "City, job type and start date depend on the specific posting"),
        "More face-to-face explanation than content planning, and more focused on one subject than project coordination.",
        ("Find a chance to be a teaching assistant or give one lesson to younger students", "Sit in on a few open classes and notice how the teacher paces them"),
    ),
    "市场营销": _Entry(
        "Marketing",
        ("marketing", "market", "brand", "advertis", "campaign"),
        "Studying target audiences, planning brand communication and promotion, measuring results and working with "
        "sales and product.",
        "Day to day, this direction usually means planning promotion, running communication and measuring results",
        ("Usually needs the basic marketing methods and channels", "Needs to adjust plans based on performance data"),
        ("Marketing teams divide the work very differently: some lean on brand, some on paid channels", "City, job type and start date depend on the specific posting"),
        "More aimed at people who do not use the product yet than product operations, and more focused on promotion results than content planning.",
        ("Take apart a recent promotion by a brand you like", "Read a few marketing postings and note the common requirements"),
    ),
}

if set(ENGLISH) != {entry.name for entry in CATALOGUE}:
    raise RuntimeError("every direction of the catalogue needs its English answer")

# What makes up three directions when the material points to fewer.
FALLBACK = ("项目协调与管理", "内容策划与编辑", "产品运营")
# The preferences a direction's words are compared with, and those only ever related as unknown.
RELATED_TOPICS = ("感兴趣的行业或方向", "理想的工作内容", "希望减少的内容")
UNKNOWN_TOPICS = ("工作方式", "目标城市", "求职类型", "现实必要条件")
# Any other stance (不能接受, 希望减少) makes a relation a possible conflict.
POSITIVE = ("理想", "也可以接受")
MOST_QUOTES = 3
MOST_LINKS = 8
# What the service says of a request that does not carry its material whole.
DAMAGED = "The simulated service could not read the material of this request."


class _Source(TypedDict):
    source_id: str
    category: str
    fields: dict[str, str]


class _Preference(TypedDict):
    topic: str
    stance: str
    preference_id: NotRequired[str]
    text: NotRequired[str]


class _Material(TypedDict):
    experiences: list[_Source]
    preferences: list[_Preference]


# What the rules below read of the material, as app.directions.explore.build_prompt writes it.
_MATERIAL = TypeAdapter(_Material)
_DECODER = json.JSONDecoder()
_SPACE = re.compile(r"\s*")


def explore(prompt: str) -> str:
    """The answer to one exploration's prompt, as JSON text."""
    material = _material(prompt)
    catalogue = _english() if _asks_english(prompt) else CATALOGUE
    experiences, preferences = material["experiences"], material["preferences"]
    sent = [item for item in preferences if "preference_id" in item]
    wanted = " ".join(
        [text for source in experiences for text in source["fields"].values()]
        + [item["text"] for item in sent if item["topic"] in RELATED_TOPICS and item["stance"] in POSITIVE]
    ).casefold()

    scored = sorted(
        ((sum(word in wanted for word in entry.words), index, entry) for index, entry in enumerate(catalogue)),
        key=lambda scored: (-scored[0], scored[1]),
    )
    chosen = [entry for hits, _, entry in scored if hits][:5]

    for name in FALLBACK:
        if len(chosen) >= 3:
            break

        fallback = next(entry for chinese, entry in zip(CATALOGUE, catalogue) if chinese.name == name)

        if all(entry.name != fallback.name for entry in chosen):
            chosen.append(fallback)

    directions = [_direction(entry, experiences, sent, first=index == 0) for index, entry in enumerate(chosen)]

    return json.dumps({"directions": directions}, ensure_ascii=False)


def _asks_english(prompt: str) -> bool:
    """Whether the instructions, before the material, ask for English results; the user's words, inside the
    material, cannot ask it."""
    head = prompt.split(MATERIAL_START, 1)[0]

    return OUTPUT_LANGUAGE_LINES["en"] in head


def _english() -> tuple[_Entry, ...]:
    """The catalogue in English, in the same order: English texts, the Chinese and English words both matched."""
    return tuple(
        _Entry(
            english.name,
            entry.words + english.words,
            english.main_work,
            english.basis,
            english.gaps,
            english.unknowns,
            english.difference,
            english.next_steps,
        )
        for entry in CATALOGUE
        for english in (ENGLISH[entry.name],)
    )


def _material(prompt: str) -> dict:
    """The material of a prompt: the one JSON value after the opening marker, read by its structure, with the
    closing marker right after it.

    The user's words travel inside JSON strings, so a marker they wrote is only text there: it neither ends the
    material early nor is taken out of it. A prompt that does not carry the material whole (a marker missing, the
    JSON cut short or of another shape) is a damaged request: the service answers it with an error, which the
    workflow ends as the service's failure, once and without trying again."""
    try:
        start = _SPACE.match(prompt, prompt.index(MATERIAL_START) + len(MATERIAL_START)).end()
        material, end = _DECODER.raw_decode(prompt, start)
        _MATERIAL.validate_python(material)
    except (ValueError, ValidationError):
        raise AIProviderResponseError(DAMAGED) from None

    closed = prompt.startswith(MATERIAL_END, _SPACE.match(prompt, end).end())

    # A preference is sent with its alias and its words together, or with neither.
    if not closed or any(("preference_id" in item) != ("text" in item) for item in material["preferences"]):
        raise AIProviderResponseError(DAMAGED)

    return material


def _direction(entry: _Entry, experiences, sent, *, first: bool) -> dict:
    return {
        "name": entry.name,
        "main_work": entry.main_work,
        "evidence": _quotes(entry, experiences),
        "ideal": _links(entry, sent, first),
        "gaps": list(entry.gaps),
        "unknowns": list(entry.unknowns),
        "difference": entry.difference,
        "next_steps": list(entry.next_steps),
    }


def _quotes(entry: _Entry, experiences) -> list[dict]:
    """A direction's quotes: the sentences using its words, each once. Sentences of one field of one source that
    read the same with every space left out, written twice or alike once cut to the longest quote, are one quote,
    as the checks count them (app.directions.explore._evidence); the limit counts different quotes only. Another
    field, source or direction quotes the same words for itself."""
    quotes, seen = [], set()

    for source in experiences:
        for name, text in source["fields"].items():
            for found in SENTENCE.finditer(text):
                sentence = found.group().strip()[:QUOTE_CHARS].strip()
                key = (source["source_id"], name, "".join(sentence.split()))

                if not sentence or key in seen or not any(word in sentence.casefold() for word in entry.words):
                    continue

                seen.add(key)
                quotes.append({"source_id": source["source_id"], "field": name, "quote": sentence, "basis": _basis(sentence, source)})

                if len(quotes) == MOST_QUOTES:
                    return quotes

    return quotes


def _basis(sentence: str, source) -> str:
    folded = sentence.casefold()
    said = next((basis for basis, markers in BASES if any(marker in folded for marker in markers)), None)

    return said or ("fact" if source["category"] == "教育" else "practice")


def _links(entry: _Entry, sent, first: bool) -> list[dict]:
    links = []

    for item in sent:
        quote = item["text"][:QUOTE_CHARS].strip()

        if item["topic"] in RELATED_TOPICS and any(word in item["text"].casefold() for word in entry.words):
            relation = "fits" if item["stance"] in POSITIVE else "may_conflict"
            links.append({"preference_id": item["preference_id"], "quote": quote, "relation": relation, "basis": entry.basis})
        elif first and item["topic"] in UNKNOWN_TOPICS:
            links.append({"preference_id": item["preference_id"], "quote": quote, "relation": "unknown", "basis": None})

    return links[:MOST_LINKS]
