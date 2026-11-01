"""Frozen semantic cases for the U2-A1 evidence analysis.

Each case is synthetic. The expected conclusions were written from the
meaning of the experience text and the rules of the analysis, before the
cases were run. The scripted answer is what a model might say, mistakes
included: the cases check that the rules turn it into honest conclusions,
not how well any real model reads. Change an expectation only with a
reason, and bump CASE_VERSION when you do.

CASE_VERSION 2 (U2-A1 R1) adds the last five cases: the review's two
reproductions, the control it asked for, and two more of the same rules.
No earlier expectation changed.

CASE_VERSION 3 (U2-A2) adds six cases of job preferences compared with the
posting. No earlier expectation changed.

CASE_VERSION 4 (U2-A2 R5): the scripted comparisons also name what each
preference and its quotes are about, as the answer contract now asks. No
earlier expectation changed. Adds three cases: a graduation date that meets
its requirement, a start date the posting leaves unstated though a
graduation year is quoted for it, and a start date it mentions without
saying enough.

CASE_VERSION 5 (U2-A2 R6): the R5 case of a start date quoted from a
graduation year expected "not stated". That was wrong: a quote about another
aspect only shows the judgement has no basis, not that the posting is silent
(Codex's R5 review, F2). It now expects needs confirmation. Adds the case
the review reproduced: the posting states a start date, the model quotes
the graduation year.

CASE_VERSION 6 (U2-A2 R7): adds three cases of start dates beside graduation
requirements. A posting with only a graduation requirement, answered right:
the start date is not stated, the graduation conflict stands apart. A
posting that starts later than the student's earliest start: consistent.
And the real R6 reply replayed: it only shows how the local rules treat that
reply, not that the revised prompt works. No earlier expectation changed.

CASE_VERSION 7 (U2-A2 R8): adds three cases of skills beside hard
conditions. The real GPT-5.6 Sol parse of the English posting replayed, with
Python and Tableau repeated as hard conditions word for word, and the
model's own citations for the three requirements that remain. A skill and a
graduation condition quoted from one line. Two hard conditions quoted from
one line. No earlier expectation changed.
"""
import json
from dataclasses import dataclass, field
from decimal import Decimal

CASE_VERSION = "7"
CONSISTENT = "consistent"
DIFFERENT = "different"
NOT_STATED = "not_stated"
SUPPORTED = "supported"
NEEDS_CONFIRMATION = "needs_confirmation"
NO_EVIDENCE = "no_evidence"
MET = "met"
CONFLICT = "conflict"
UNKNOWN = "unknown"
UNWRITTEN = object()


@dataclass(frozen=True)
class SemanticCase:
    name: str
    why: str
    # (category, fields) in the order they are chosen: S1, S2, ...
    experiences: tuple
    posting: str
    # Requirements as the parse returns them: (text, the posting's words).
    required: tuple = ()
    preferred: tuple = ()
    hard: tuple = ()
    # What the scripted model answers: requirement id -> [(source, field, quote, basis)].
    answer: dict = field(default_factory=dict)
    expected: dict = field(default_factory=dict)
    # Written for every case: a forgotten one fails rather than defaulting.
    expected_overall: Decimal | None | object = UNWRITTEN
    expected_conflicts: tuple = ()
    # Job preferences chosen for the task (field -> words), what the scripted
    # model says of each (preference id -> (relation, posting quotes, what the
    # preference is about, what the quotes are about)), and the conclusions
    # expected. Ids follow the order of the preferences page.
    preferences: dict = field(default_factory=dict)
    preference_answer: dict = field(default_factory=dict)
    expected_preferences: dict = field(default_factory=dict)
    expected_preference_conflicts: tuple = ()

    def parsed(self) -> str:
        def requirements(pairs):
            return [{"text": text, "evidence_text": quote} for text, quote in pairs]

        return json.dumps(
            {
                "job_title": None, "company": None, "location": None, "work_mode": None, "seniority": None,
                "minimum_experience": None, "education_requirement": None, "work_authorization": None,
                "responsibilities": [],
                "required_skills": requirements(self.required),
                "preferred_skills": requirements(self.preferred),
                "hard_constraints": requirements(self.hard),
                "uncertain_requirements": [],
            },
            ensure_ascii=False,
        )

    def scripted_answer(self) -> str:
        return json.dumps(
            {
                "assessments": [
                    {
                        "requirement_id": requirement_id,
                        "citations": [
                            {"source_id": source, "field": name, "quote": quote, "basis": basis}
                            for source, name, quote, basis in citations
                        ],
                    }
                    for requirement_id, citations in self.answer.items()
                ],
                **(
                    {
                        "preferences": [
                            {
                                "preference_id": preference_id,
                                "aspect": aspect,
                                "relation": relation,
                                "job_quotes": list(quotes),
                                "job_quotes_aspect": quotes_aspect,
                            }
                            for preference_id, (relation, quotes, aspect, quotes_aspect) in self.preference_answer.items()
                        ]
                    }
                    if self.preferences
                    else {}
                ),
            },
            ensure_ascii=False,
        )


CASES = (
    SemanticCase(
        name="practice_zh",
        why="写明自己用 Python 做了什么，是实践依据。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "actions", "用 Python 写脚本清洗了问卷数据", "practice")]},
        expected={"R1": SUPPORTED},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="claim_only",
        why="只说熟悉，没有说做过什么：自述需要用户确认，不算有经历支持。",
        experiences=(("other", {"title": "技能", "notes": "我熟悉 Python 和 SQL。"}),),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "notes", "我熟悉 Python", "claim")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="plan",
        why="打算学习不是已有经历：目前未找到支持材料。",
        experiences=(("activity", {"name": "学习计划", "reflection": "计划下学期学习 Docker。"}),),
        posting="岗位要求：需要会 Docker",
        required=(("Docker", "需要会 Docker"),),
        answer={"R1": [("S1", "reflection", "计划下学期学习 Docker", "plan")]},
        expected={"R1": NO_EVIDENCE},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="teammate",
        why="队友做的部署不是用户自己的实践。",
        experiences=(("project", {"title": "课程项目", "actions": "队友负责把服务部署到 AWS，我负责写文档。"}),),
        posting="岗位要求：熟悉 AWS",
        required=(("AWS", "熟悉 AWS"),),
        answer={"R1": [("S1", "actions", "队友负责把服务部署到 AWS", "other_person")]},
        expected={"R1": NO_EVIDENCE},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="negated",
        why="写明没有使用 Git：不能因为出现关键词就算支持。",
        experiences=(("project", {"title": "小组作业", "reflection": "这次没有使用 Git 管理版本，代码互相覆盖。"}),),
        posting="加分项：会用 Git",
        preferred=(("Git", "会用 Git"),),
        answer={"P1": [("S1", "reflection", "没有使用 Git 管理版本", "negated")]},
        expected={"P1": NO_EVIDENCE},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="plan_labelled_practice",
        why="模型把计划误标成实践：引文里的“计划”使它降为待确认，而不是支持。",
        experiences=(("activity", {"name": "学习计划", "reflection": "计划下学期学习 Docker。"}),),
        posting="岗位要求：需要会 Docker",
        required=(("Docker", "需要会 Docker"),),
        answer={"R1": [("S1", "reflection", "计划下学期学习 Docker", "practice")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="claim_labelled_practice",
        why="模型把“熟悉 SQL”误标成实践：自述的说法只能待确认。",
        experiences=(("other", {"title": "技能", "notes": "我熟悉 SQL。"}),),
        posting="岗位要求：掌握 SQL",
        required=(("SQL", "掌握 SQL"),),
        answer={"R1": [("S1", "notes", "我熟悉 SQL", "practice")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="contradiction",
        why="一条说用 SQL 汇总过，另一条说其实没用：矛盾要用户确认。",
        experiences=(
            ("project", {"title": "销量报表", "actions": "用 SQL 汇总了销量。"}),
            ("other", {"title": "补充", "notes": "其实那门课没有用 SQL，只用了表格。"}),
        ),
        posting="岗位要求：掌握 SQL",
        required=(("SQL", "掌握 SQL"),),
        answer={"R1": [("S1", "actions", "用 SQL 汇总了销量", "practice"), ("S2", "notes", "没有用 SQL", "negated")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="hard_conditions",
        why="毕业时间写明不符是冲突；材料没写工作许可就是未知，不猜。技能满分也不掩盖冲突。",
        experiences=(
            ("project", {"title": "数据清洗", "actions": "用 Python 清洗了订单数据。"}),
            ("education", {"school": "合成大学", "graduation": "2025 年 6 月"}),
        ),
        posting="岗位要求：熟练使用 Python；2027 年及以后毕业；需要有效的工作许可",
        required=(("Python", "熟练使用 Python"),),
        hard=(("2027 年及以后毕业", "2027 年及以后毕业"), ("有效的工作许可", "需要有效的工作许可")),
        answer={
            "R1": [("S1", "actions", "用 Python 清洗了订单数据", "practice")],
            "H1": [("S2", "graduation", "2025 年 6 月", "conflict")],
            "H2": [],
        },
        expected={"R1": SUPPORTED, "H1": CONFLICT, "H2": UNKNOWN},
        expected_overall=Decimal("100"),
        expected_conflicts=("H1",),
    ),
    SemanticCase(
        name="english_posting_mixed_experience",
        why="英文 JD 与中英混写的经历：实践原文照样可引，缺的加分项是未找到材料。",
        experiences=(("work", {"organization": "合成咨询", "actions": "用 Python (pandas) 清洗了 survey data，并做了 weekly report。"}),),
        posting="Required: Proficiency in Python. Preferred: Experience with Tableau.",
        required=(("Python", "Proficiency in Python"),),
        preferred=(("Tableau", "Experience with Tableau"),),
        answer={"R1": [("S1", "actions", "用 Python (pandas) 清洗了 survey data", "practice")], "P1": []},
        expected={"R1": SUPPORTED, "P1": NO_EVIDENCE},
        expected_overall=Decimal("80"),
    ),
    SemanticCase(
        name="english_negation_labelled_practice",
        why="英文写明没有用 Docker、由助教部署：误标的实践被降为待确认。",
        experiences=(("project", {"title": "Course project", "reflection": "I did not use Docker in this project; the TA deployed it."}),),
        posting="Required: Experience with Docker.",
        required=(("Docker", "Experience with Docker"),),
        answer={"R1": [("S1", "reflection", "I did not use Docker in this project", "practice")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="truncated_negated_quote",
        why="原句写明没有使用 Python，模型只引“使用 Python”并标成实践：同一分句里的“没有”照样读到，降为待确认，不给分。",
        experiences=(("project", {"title": "合成作业", "actions": "我没有使用 Python，数据清洗由队友完成。"}),),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "actions", "使用 Python", "practice")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="correction_beside_practice",
        why="一条写用 Python 清洗过，另一条更正说其实没用（模型误标为实践）：认出的否定不被前一条盖掉，矛盾待确认。",
        experiences=(
            ("project", {"title": "订单清洗", "actions": "用 Python 清洗了订单数据。"}),
            ("other", {"title": "更正", "notes": "更正：其实没有使用 Python，只用了表格。"}),
        ),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [
            ("S1", "actions", "用 Python 清洗了订单数据", "practice"),
            ("S2", "notes", "其实没有使用 Python，只用了表格", "practice"),
        ]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="denial_of_another_skill",
        why="否定的是 Docker，转折之后写的是自己用 Python 做了分析：Python 仍有实践支持，不被同一句里别的否定拖累；Docker 未找到支持材料。",
        experiences=(("project", {"title": "销量分析", "actions": "没有用 Docker，但用 Python 做了分析。"}),),
        posting="岗位要求：熟练使用 Python；加分项：会用 Docker",
        required=(("Python", "熟练使用 Python"),),
        preferred=(("Docker", "会用 Docker"),),
        answer={
            "R1": [("S1", "actions", "用 Python 做了分析", "practice")],
            "P1": [("S1", "actions", "没有用 Docker", "negated")],
        },
        expected={"R1": SUPPORTED, "P1": NO_EVIDENCE},
        expected_overall=Decimal("80"),
    ),
    SemanticCase(
        name="english_list_under_one_negation",
        why="英文列举共用一个 did not：只引列举的后半段，也读得到前面的否定。",
        experiences=(("project", {"title": "Course project", "actions": "I did not use Docker, Kubernetes or AWS."}),),
        posting="Required: Experience with Kubernetes.",
        required=(("Kubernetes", "Experience with Kubernetes"),),
        answer={"R1": [("S1", "actions", "Kubernetes or AWS", "practice")]},
        expected={"R1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("0"),
    ),
    SemanticCase(
        name="hard_condition_with_denial",
        why="一条写持有工作许可，另一条更正说目前没有：硬条件不能记为满足，矛盾待确认；没有技能要求就没有分数。",
        experiences=(
            ("other", {"title": "身份信息", "notes": "持有有效的工作许可。"}),
            ("other", {"title": "更正", "notes": "更正：目前没有工作许可，还在等待审批。"}),
        ),
        posting="岗位要求：需要有效的工作许可",
        hard=(("有效的工作许可", "需要有效的工作许可"),),
        answer={"H1": [
            ("S1", "notes", "持有有效的工作许可", "fact"),
            ("S2", "notes", "目前没有工作许可", "negated"),
        ]},
        expected={"H1": NEEDS_CONFIRMATION},
        expected_overall=None,
    ),
    SemanticCase(
        name="preference_city_named",
        why="理想城市是上海，岗位写明工作地点上海，并引了这句原文：一致。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="工作地点：上海",
        preferences={"cities_ideal": "上海"},
        preference_answer={"F1": (CONSISTENT, ["工作地点：上海"], "location", "location")},
        expected_preferences={"F1": CONSISTENT},
        expected_overall=None,
    ),
    SemanticCase(
        name="preference_ideal_city_differs_is_no_filter",
        why="理想是北京，岗位在上海：只是和理想不一致，不当成冲突或硬性筛选。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="工作地点：上海",
        preferences={"cities_ideal": "北京"},
        preference_answer={"F1": ("conflicts", ["工作地点：上海"], "location", "location")},
        expected_preferences={"F1": DIFFERENT},
        expected_overall=None,
    ),
    SemanticCase(
        name="preference_cannot_accept_travel",
        why="写明不能接受频繁出差，岗位要求经常出差：冲突，单独列出。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="岗位说明：需要经常出差",
        preferences={"work_style_no": "频繁出差"},
        preference_answer={"F1": ("conflicts", ["需要经常出差"], "travel", "travel")},
        expected_preferences={"F1": CONFLICT},
        expected_overall=None,
        expected_preference_conflicts=("F1",),
    ),
    SemanticCase(
        name="preference_not_stated_stays_unknown",
        why="想远程办公，岗位没提工作方式：岗位未说明，不按公司或职位猜。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "actions", "用 Python 写脚本清洗了问卷数据", "practice")]},
        preferences={"work_style_ideal": "远程办公"},
        preference_answer={"F1": (NOT_STATED, [], "work_mode", "work_mode")},
        expected={"R1": SUPPORTED},
        expected_preferences={"F1": NOT_STATED},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_claim_without_posting_words",
        why="模型说城市一致却没给岗位原文：没有原文依据的判断只能待确认。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="岗位要求：熟练使用 Python",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "actions", "用 Python 写脚本清洗了问卷数据", "practice")]},
        preferences={"cities_ideal": "上海"},
        preference_answer={"F1": (CONSISTENT, [], "location", "location")},
        expected={"R1": SUPPORTED},
        expected_preferences={"F1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_must_have_conflict_beside_full_score",
        why="技能全有支持，但现实必要条件（入职时间）与岗位冲突：冲突单独列出，不被满分掩盖，也不改变分数。",
        experiences=(("project", {"title": "问卷分析", "actions": "用 Python 写脚本清洗了问卷数据。"}),),
        posting="岗位要求：熟练使用 Python；需在 2027 年 3 月前到岗",
        required=(("Python", "熟练使用 Python"),),
        answer={"R1": [("S1", "actions", "用 Python 写脚本清洗了问卷数据", "practice")]},
        preferences={"must_haves": "2027 年 7 月之后才能入职"},
        preference_answer={"F1": ("conflicts", ["需在 2027 年 3 月前到岗"], "start_date", "start_date")},
        expected={"R1": SUPPORTED},
        expected_preferences={"F1": CONFLICT},
        expected_overall=Decimal("100"),
        expected_preference_conflicts=("F1",),
    ),
    SemanticCase(
        name="hard_condition_graduation_met",
        why="毕业时间写明 2027 年 6 月（预计），岗位要求 2027 年及以后毕业：事实符合，不是冲突。",
        experiences=(
            ("project", {"title": "数据清洗", "actions": "用 Python 清洗了订单数据。"}),
            ("education", {"school": "合成大学", "graduation": "2027 年 6 月（预计）"}),
        ),
        posting="岗位要求：熟练使用 Python；2027 年及以后毕业",
        required=(("Python", "熟练使用 Python"),),
        hard=(("2027 年及以后毕业", "2027 年及以后毕业"),),
        answer={
            "R1": [("S1", "actions", "用 Python 清洗了订单数据", "practice")],
            "H1": [("S2", "graduation", "2027 年 6 月（预计）", "fact")],
        },
        expected={"R1": SUPPORTED, "H1": MET},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_start_date_unstated_beside_a_graduation_year",
        why="岗位没写入职时间，只写了毕业年份。模型拿这句当入职时间冲突的依据，又说这句讲的是对候选人的要求：这次判断没有可靠依据，待确认；不是冲突，也不能由一句错引推断岗位未说明。",
        experiences=(("project", {"title": "销量汇总", "actions": "用 SQL 汇总了每周销量。"}),),
        posting="任职要求：熟练使用 SQL；2027 年及以后毕业",
        required=(("SQL", "熟练使用 SQL"),),
        hard=(("2027 年及以后毕业", "2027 年及以后毕业"),),
        answer={"R1": [("S1", "actions", "用 SQL 汇总了每周销量", "practice")], "H1": []},
        preferences={"must_haves": "2026 年 9 月之后才能全职入职"},
        preference_answer={"F1": ("conflicts", ["2027 年及以后毕业"], "start_date", "qualification")},
        expected={"R1": SUPPORTED, "H1": UNKNOWN},
        expected_preferences={"F1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_start_date_stated_but_misquoted",
        why="岗位写明需在 2026 年 7 月前到岗，模型说冲突却引了毕业年份那句：依据不可靠，待确认；岗位明明写了到岗时间，不能说岗位未说明。",
        experiences=(("project", {"title": "销量汇总", "actions": "用 SQL 汇总了每周销量。"}),),
        posting="任职要求：熟练使用 SQL；2027 年及以后毕业；需在 2026 年 7 月前到岗",
        required=(("SQL", "熟练使用 SQL"),),
        hard=(("2027 年及以后毕业", "2027 年及以后毕业"),),
        answer={"R1": [("S1", "actions", "用 SQL 汇总了每周销量", "practice")], "H1": []},
        preferences={"must_haves": "2026 年 9 月之后才能全职入职"},
        preference_answer={"F1": ("conflicts", ["2027 年及以后毕业"], "start_date", "qualification")},
        expected={"R1": SUPPORTED, "H1": UNKNOWN},
        expected_preferences={"F1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_start_date_mentioned_without_enough",
        why="岗位写了入职时间可以商量，却没写具体时间：说到了但不够判断，待确认，和完全没写的“岗位未说明”分开。",
        experiences=(("project", {"title": "销量汇总", "actions": "用 SQL 汇总了每周销量。"}),),
        posting="任职要求：熟练使用 SQL；入职时间可以商量",
        required=(("SQL", "熟练使用 SQL"),),
        answer={"R1": [("S1", "actions", "用 SQL 汇总了每周销量", "practice")]},
        preferences={"must_haves": "2026 年 9 月之后才能全职入职"},
        preference_answer={"F1": ("unclear", ["入职时间可以商量"], "start_date", "start_date")},
        expected={"R1": SUPPORTED},
        expected_preferences={"F1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="preference_start_date_unstated_with_only_a_graduation_requirement",
        why="实习岗位只写了毕业届别，没写到岗时间：毕业资格单独判冲突；入职时间按正确回答是岗位未说明。两者互不代替，也不被技能满分掩盖。",
        experiences=(
            ("project", {"title": "销量汇总", "actions": "用 SQL 汇总了每周销量。"}),
            ("education", {"school": "合成学院", "graduation": "2024 年 7 月"}),
        ),
        posting="实习生（合成岗位）\n任职要求：熟练使用 SQL；2026 届及以后毕业",
        required=(("SQL", "熟练使用 SQL"),),
        hard=(("2026 届及以后毕业", "2026 届及以后毕业"),),
        answer={
            "R1": [("S1", "actions", "用 SQL 汇总了每周销量", "practice")],
            "H1": [("S2", "graduation", "2024 年 7 月", "conflict")],
        },
        preferences={"must_haves": "2025 年 3 月之后才能全职入职"},
        preference_answer={"F1": (NOT_STATED, [], "start_date", "start_date")},
        expected={"R1": SUPPORTED, "H1": CONFLICT},
        expected_preferences={"F1": NOT_STATED},
        expected_overall=Decimal("100"),
        expected_conflicts=("H1",),
    ),
    SemanticCase(
        name="preference_start_date_later_than_the_earliest_is_no_conflict",
        why="用户最早 2027 年 1 月之后可以入职，岗位写明 2027 年 6 月起入职：比最早时间晚，一致；不能只因日期不同判冲突。",
        experiences=(("project", {"title": "销量汇总", "actions": "用 SQL 汇总了每周销量。"}),),
        posting="任职要求：熟练使用 SQL\n入职时间：2027 年 6 月起",
        required=(("SQL", "熟练使用 SQL"),),
        answer={"R1": [("S1", "actions", "用 SQL 汇总了每周销量", "practice")]},
        preferences={"must_haves": "2027 年 1 月之后才能入职"},
        preference_answer={"F1": (CONSISTENT, ["入职时间：2027 年 6 月起"], "start_date", "start_date")},
        expected={"R1": SUPPORTED},
        expected_preferences={"F1": CONSISTENT},
        expected_overall=Decimal("100"),
    ),
    SemanticCase(
        name="real_r6_reply_graduation_quoted_as_a_start_date",
        why="R6 真实复测样例 3 的原样回放：模型引“2027 年及以后毕业”作入职时间依据，把引文方面错标成 start_date，并回答 unclear。本地规则只能按 unclear 判待确认：结论安全，但业务上应为岗位未说明。这一例只说明本地规则怎样处理这份真实返回。",
        experiences=(
            ("project", {"title": "销量汇总（合成）", "actions": "用 SQL 汇总了每周销量，并写了检查脚本。"}),
            ("education", {"school": "合成学院", "graduation": "2025 年 6 月"}),
        ),
        posting="数据工程实习生（合成岗位）\n任职要求：\n- 熟练使用 SQL\n- 2027 年及以后毕业\n工作说明：需要经常出差，每月约两周在外地",
        required=(("熟练使用 SQL", "熟练使用 SQL"),),
        hard=(("2027 年及以后毕业", "2027 年及以后毕业"),),
        answer={
            "R1": [("S1", "actions", "用 SQL 汇总了每周销量，并写了检查脚本。", "practice")],
            "H1": [("S2", "graduation", "2025 年 6 月", "conflict")],
        },
        preferences={"work_style_no": "频繁出差", "must_haves": "2026 年 9 月之后才能全职入职"},
        preference_answer={
            "F1": ("conflicts", ["需要经常出差，每月约两周在外地"], "travel", "travel"),
            "F2": ("unclear", ["2027 年及以后毕业"], "start_date", "start_date"),
        },
        expected={"R1": SUPPORTED, "H1": CONFLICT},
        expected_preferences={"F1": CONFLICT, "F2": NEEDS_CONFIRMATION},
        expected_overall=Decimal("100"),
        expected_conflicts=("H1",),
        expected_preference_conflicts=("F1",),
    ),
    SemanticCase(
        name="real_sol_case2_skills_repeated_as_hard_conditions",
        why="GPT-5.6 Sol 真实解析的原样回放：Python、Tableau 两条要求连同原句被同时放进必备技能和硬条件。同一要求只按技能评价一次：Python 有支持；Tableau 是队友做的，未找到支持材料；AWS 只是自述，待确认；技能覆盖 40。不再出现重复的“符合”“待确认”。分析部分回放的是模型对剩下三条要求的真实引用。",
        experiences=(
            ("work", {"organization": "Synthetic Consulting", "role": "Data intern", "actions": "用 Python (pandas) 清洗了 survey data，并做了 weekly report。"}),
            ("project", {"title": "Course dashboard (synthetic)", "actions": "My teammate built the Tableau dashboard; I wrote the data dictionary."}),
            ("other", {"title": "Skills note", "notes": "I am familiar with AWS but have not used it in a project."}),
        ),
        posting="Junior Data Analyst (synthetic)\nRequirements:\n- Proficiency in Python for data analysis\n"
        "- Experience building Tableau dashboards\nNice to have:\n- Familiarity with AWS",
        required=(
            ("Proficiency in Python for data analysis", "Proficiency in Python for data analysis"),
            ("Experience building Tableau dashboards", "Experience building Tableau dashboards"),
        ),
        preferred=(("Familiarity with AWS", "Familiarity with AWS"),),
        hard=(
            ("Proficiency in Python for data analysis", "Proficiency in Python for data analysis"),
            ("Experience building Tableau dashboards", "Experience building Tableau dashboards"),
        ),
        answer={
            "R1": [("S1", "actions", "用 Python (pandas) 清洗了 survey data，并做了 weekly report。", "practice")],
            "R2": [("S2", "actions", "My teammate built the Tableau dashboard; I wrote the data dictionary.", "other_person")],
            "P1": [
                ("S3", "notes", "I am familiar with AWS but have not used it in a project.", "claim"),
                ("S3", "notes", "I am familiar with AWS but have not used it in a project.", "negated"),
            ],
        },
        expected={"R1": SUPPORTED, "R2": NO_EVIDENCE, "P1": NEEDS_CONFIRMATION},
        expected_overall=Decimal("40"),
    ),
    SemanticCase(
        name="skill_and_graduation_quoted_from_one_line",
        why="同一句里既有技能又有毕业条件，两条要求引同一句原文：要求文字不同，是两个要求，都保留；毕业不符照常判冲突，并单独列出。",
        experiences=(
            ("project", {"title": "数据清洗", "actions": "用 Python 清洗了订单数据。"}),
            ("education", {"school": "合成大学", "graduation": "2024 年 6 月"}),
        ),
        posting="岗位要求：熟练使用 Python，2026 届及以后毕业",
        required=(("Python", "熟练使用 Python，2026 届及以后毕业"),),
        hard=(("2026 届及以后毕业", "熟练使用 Python，2026 届及以后毕业"),),
        answer={
            "R1": [("S1", "actions", "用 Python 清洗了订单数据", "practice")],
            "H1": [("S2", "graduation", "2024 年 6 月", "conflict")],
        },
        expected={"R1": SUPPORTED, "H1": CONFLICT},
        expected_overall=Decimal("100"),
        expected_conflicts=("H1",),
    ),
    SemanticCase(
        name="two_hard_conditions_quoted_from_one_line",
        why="毕业条件和工作许可写在同一句，引同一句原文：是两个不同的硬条件，不合并；材料没写工作许可，就是未知。",
        experiences=(("education", {"school": "合成大学", "graduation": "2024 年 6 月"}),),
        posting="任职要求：2026 届及以后毕业，需要有效的工作许可",
        hard=(
            ("2026 届及以后毕业", "2026 届及以后毕业，需要有效的工作许可"),
            ("有效的工作许可", "2026 届及以后毕业，需要有效的工作许可"),
        ),
        answer={"H1": [("S1", "graduation", "2024 年 6 月", "conflict")], "H2": []},
        expected={"H1": CONFLICT, "H2": UNKNOWN},
        expected_overall=None,
        expected_conflicts=("H1",),
    ),
)
