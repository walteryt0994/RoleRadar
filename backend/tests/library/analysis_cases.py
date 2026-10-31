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
"""
import json
from dataclasses import dataclass, field
from decimal import Decimal

CASE_VERSION = "2"
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
                ]
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
)
