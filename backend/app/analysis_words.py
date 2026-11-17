"""The words an analysis's judgments are said in, in Chinese (U2-A2; one shared place since U3-D3b F1).

What a requirement's conclusion, a preference's conclusion, a reason, a quote's basis and a requirement's group are
called: the job analysis pages (app.job_analysis.presentation), the search's result pages and a kept analysis, on its
record page and in its export (app.library.job_export), all say them from here, so a judgment reads the same wherever
it is read. "目前未找到支持材料" is about the chosen material, never "the user cannot"; "岗位未说明" is what the posting
does not say; nothing reads as a hiring probability.

Keyed by the codes of the analysis contracts (app.analysis_contracts), whose StrEnum members look them up as they are.
It imports nothing, so the experience library may use it and still load no analysis code.
"""

# A requirement of this group is held against the user's conditions; any other against the chosen material.
HARD_CONSTRAINT = "hard_constraint"

EVIDENCE_LABELS = {
    "supported": "有经历支持",
    "needs_confirmation": "待确认",
    "no_evidence": "目前未找到支持材料",
}
CONDITION_LABELS = {
    "met": "符合",
    "conflict": "冲突",
    "needs_confirmation": "待确认",
    "unknown": "未知",
    # U3-D6 R4: the posting's own condition for it does not hold, by a fact the posting states.
    "not_applicable": "不适用",
}
PREFERENCE_LABELS = {
    "consistent": "一致",
    "different": "不一致",
    "conflict": "冲突",
    "needs_confirmation": "待确认",
    "not_stated": "岗位未说明",
}
REASONS = {
    "practice": "写明了你自己做过",
    "claim": "只写了熟悉或掌握，没有写做过什么",
    "plan": "写的是打算学或打算做",
    "other_person": "写的是别人做的",
    "negated": "写明没有做过或没有用过",
    "fact": "写明的事实",
    "conflict": "写明的事实和要求不符",
    "contradiction": "所选经历里有相互矛盾的说法",
    "no_material": "所选经历里没有相关内容",
    "flag:plan": "引文所在的句子提到“计划”或“打算”",
    "flag:other_person": "引文所在的句子提到了别人",
    "flag:negated": "引文所在的句子有否定的说法",
    "flag:claim": "引文所在的句子只是说熟悉或掌握",
    "consistent": "岗位原文与偏好一致",
    "conflicts": "岗位原文与偏好相反",
    "unclear": "岗位原文提到了，但看不出是否符合",
    "no_quote": "没有岗位原文能说明",
    "other_aspect": "引用的岗位原文说的是别的方面，不能作为这一项的依据",
    "not_stated": "岗位未明确说明这一条件",
    # U3-D6 R4 (app.requirement_reading).
    "listed_partly": "这一条列了几项，引文只写到其中一部分，其余还没有依据",
    "listed_unnamed": "这一条列了几项，引文写了做过的事，但没有写到其中任何一项",
    "listed_unclear": "这一条像是列了几项，本机读不准要全部还是任一、或列的是哪几项，引文没有写到每一项，需要确认",
    # U3-D6 R8.
    "listed_unread": "这一条的写法本机没有认出是一项还是几项，引文写了做过的事，是否覆盖整条要求需要确认",
    # U3-D6 R9.
    "single_unnamed": "引文写了做过的事，但没有把这一条要求的各部分都写到，是否覆盖整条要求需要确认",
    "condition_unknown": "岗位原文是条件句，岗位没有写明条件是否成立",
    "condition_met": "岗位原文是条件句，岗位写明的事实说明条件成立",
    "condition_not_met": "岗位原文是条件句，岗位写明的事实说明条件不成立，这一条不适用",
    "conditional_quote": "引用的岗位原文是条件句，岗位没有写明条件成立，不能当作无条件的说法",
}
# How a requirement listing several items asks for them (U3-D6 R4).
LISTED_MODES = {
    "all": "所列各项都要",
    "any": "所列任一即可",
    # U3-D6 R5: read as all of them, to be safe.
    "unclear": "读不准要全部还是任一，按全部来核对",
    # U3-D6 R8: never with items, so never shown as a list; here only so that every mode has its words.
    "unread": "没有认出是一项还是几项",
}
BASIS_LABELS = {
    "practice": "实践",
    "claim": "自述",
    "plan": "计划",
    "other_person": "他人",
    "negated": "否定",
    "fact": "事实",
    "conflict": "不符的事实",
}
GROUP_HEADINGS = {
    "required": "必要要求",
    "preferred": "加分项",
    "uncertain": "写得不明确的要求",
}
# What the pages call the hard constraints ("岗位里没有找到明确的硬性条件").
HARD_CONSTRAINT_HEADING = "硬性条件"


def conclusion_text(group: str, conclusion: str) -> str:
    """A requirement's conclusion in words: a hard constraint's against the user's conditions, any other's against
    the chosen material. An unknown code is an error, never shown as it is."""
    return (CONDITION_LABELS if group == HARD_CONSTRAINT else EVIDENCE_LABELS)[conclusion]


def conclusion_label(finding) -> str:
    return conclusion_text(finding.group, finding.conclusion)


def requirement_text(text: str, condition: str = "", state: str = "") -> str:
    """A requirement's words as the pages say them (U3-D6 R4): with the condition its posting quote lays down, word
    for word, so a conditional demand never reads as an unconditional one."""
    if not condition:
        return text

    return f"{text}（{'条件成立' if state == 'met' else '原文条件'}：{condition}）"


def requirement_words(finding) -> str:
    """requirement_text of a finding; one kept before R4 has no condition and reads as it did."""
    return requirement_text(finding.text, getattr(finding, "condition", ""), getattr(finding, "condition_state", ""))


def listed_parts(listed, named) -> tuple[list[str], list[str]]:
    """A listed requirement's items the cited practice names, and those it does not."""
    return [item for item in listed if item in named], [item for item in listed if item not in named]


def preference_text(conclusion: str) -> str:
    return PREFERENCE_LABELS[conclusion]


def preference_label(finding) -> str:
    return preference_text(finding.conclusion)


def reason_labels(reasons) -> list[str]:
    return [REASONS.get(reason, reason) for reason in reasons]


def group_heading(group: str) -> str:
    return HARD_CONSTRAINT_HEADING if group == HARD_CONSTRAINT else GROUP_HEADINGS[group]


def requirement_group(group: str) -> str:
    """The group a requirement belongs to, as every analysis row names it (U3-D6 close-out): in the words the U2 page
    heads its groups with, so that a row says it alone on U2, on a search result and on a kept analysis alike, and two
    analyses of one posting show where each put a requirement. An unknown group is an error, never shown as it is."""
    return group_heading(group)
