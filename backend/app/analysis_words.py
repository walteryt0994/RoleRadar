"""The words an analysis's judgments are said in, in Chinese (U2-A2; one shared place since U3-D3b F1).

What a requirement's conclusion, a preference's conclusion, a reason, a quote's basis and a requirement's group are
called: the job analysis pages (app.job_analysis.presentation), the search's result pages and a kept analysis, on its
record page and in its export (app.library.job_export), all say them from here, so a judgment reads the same wherever
it is read. "目前未找到支持材料" is about the chosen material, never "the user cannot"; "岗位未说明" is what the posting
does not say; nothing reads as a hiring probability.

Keyed by the codes of the analysis contracts (app.analysis_contracts), whose StrEnum members look them up as they are.
It imports nothing, so the experience library may use it and still load no analysis code.
"""
from app.i18n import MessageMap, lazy, t

# A requirement of this group is held against the user's conditions; any other against the chosen material.
HARD_CONSTRAINT = "hard_constraint"

EVIDENCE_LABELS = MessageMap({
    "supported": "job.words.evidence_labels.1",
    "needs_confirmation": "job.words.evidence_labels.2",
    "no_evidence": "job.words.evidence_labels.3",
})
CONDITION_LABELS = MessageMap({
    "met": "job.words.condition_labels.1",
    "conflict": "job.words.condition_labels.2",
    "needs_confirmation": "job.words.condition_labels.3",
    "unknown": "job.words.condition_labels.4",
    # U3-D6 R4: the posting's own condition for it does not hold, by a fact the posting states.
    "not_applicable": "job.words.condition_labels.5",
})
PREFERENCE_LABELS = MessageMap({
    "consistent": "job.words.preference_labels.1",
    "different": "job.words.preference_labels.2",
    "conflict": "job.words.preference_labels.3",
    "needs_confirmation": "job.words.preference_labels.4",
    "not_stated": "job.words.preference_labels.5",
})
REASONS = MessageMap({
    "practice": "job.words.reasons.1",
    "claim": "job.words.reasons.2",
    "plan": "job.words.reasons.3",
    "other_person": "job.words.reasons.4",
    "negated": "job.words.reasons.5",
    "fact": "job.words.reasons.6",
    "conflict": "job.words.reasons.7",
    "contradiction": "job.words.reasons.8",
    "no_material": "job.words.reasons.9",
    "flag:plan": "job.words.reasons.10",
    "flag:other_person": "job.words.reasons.11",
    "flag:negated": "job.words.reasons.12",
    "flag:claim": "job.words.reasons.13",
    "consistent": "job.words.reasons.14",
    "conflicts": "job.words.reasons.15",
    "unclear": "job.words.reasons.16",
    "no_quote": "job.words.reasons.17",
    "other_aspect": "job.words.reasons.18",
    "not_stated": "job.words.reasons.19",
    # U3-D6 R4 (app.requirement_reading).
    "listed_partly": "job.words.reasons.20",
    "listed_unnamed": "job.words.reasons.21",
    "listed_unclear": "job.words.reasons.22",
    # U3-D6 R8.
    "listed_unread": "job.words.reasons.23",
    # U3-D6 R9.
    "single_unnamed": "job.words.reasons.24",
    "condition_unknown": "job.words.reasons.25",
    "condition_met": "job.words.reasons.26",
    "condition_not_met": "job.words.reasons.27",
    "conditional_quote": "job.words.reasons.28",
})
# How a requirement listing several items asks for them (U3-D6 R4).
LISTED_MODES = MessageMap({
    "all": "job.words.listed_modes.1",
    "any": "job.words.listed_modes.2",
    # U3-D6 R5: read as all of them, to be safe.
    "unclear": "job.words.listed_modes.3",
    # U3-D6 R8: never with items, so never shown as a list; here only so that every mode has its words.
    "unread": "job.words.listed_modes.4",
})
BASIS_LABELS = MessageMap({
    "practice": "job.words.basis_labels.1",
    "claim": "job.words.basis_labels.2",
    "plan": "job.words.basis_labels.3",
    "other_person": "job.words.basis_labels.4",
    "negated": "job.words.basis_labels.5",
    "fact": "job.words.basis_labels.6",
    "conflict": "job.words.basis_labels.7",
})
GROUP_HEADINGS = MessageMap({
    "required": "job.words.group_headings.1",
    "preferred": "job.words.group_headings.2",
    "uncertain": "job.words.group_headings.3",
})
# What the pages call the hard constraints ("岗位里没有找到明确的硬性条件").
HARD_CONSTRAINT_HEADING = lazy("job.words.56")


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

    return t("job.words.57", p0=text, p1=t("job.words.condition_met") if state == "met" else t("job.words.condition_quoted"), p2=condition)


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
    return str(HARD_CONSTRAINT_HEADING) if group == HARD_CONSTRAINT else GROUP_HEADINGS[group]


def requirement_group(group: str) -> str:
    """The group a requirement belongs to, as every analysis row names it (U3-D6 close-out): in the words the U2 page
    heads its groups with, so that a row says it alone on U2, on a search result and on a kept analysis alike, and two
    analyses of one posting show where each put a requirement. An unknown group is an error, never shown as it is."""
    return group_heading(group)
