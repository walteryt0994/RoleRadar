"""Messages of 分析结果：要求分组、结论、依据和条件的说法（与 app/analysis_words.py 同一来源）."""

MESSAGES = {
    "cov.title": ("岗位要求依据覆盖", "Requirement evidence coverage"),
    "cov.sub": ("技能要求", "Skill requirements"),
    "cov.count": ("{n}／{d} 项有依据", "{n} of {d} with evidence"),
    "cov.percent": ("{p}%", "{p}%"),
    "cov.lead": ("{n}／{d} 项技能要求找到了依据", "Evidence found for {n} of {d} skill requirements"),
    "cov.detail": ("待确认 {c} 项，暂缺依据 {e} 项。这不是录用的可能性；“暂缺依据”也不等于不具备。", "{c} need confirmation and {e} have no evidence yet. This is not a chance of being hired, and no evidence yet does not mean you lack it."),
    "cov.none.not_complete": ("分析没有完整完成，不显示依据覆盖。", "The analysis did not complete, so no coverage is shown."),
    "cov.none.no_skills": ("岗位原文里没有可计数的技能要求，不显示依据覆盖。", "The posting has no countable skill requirements, so no coverage is shown."),
    "cov.none.inconsistent": ("分析结构无法核对，不显示依据覆盖。", "The analysis could not be checked, so no coverage is shown."),
    "cov.none.not_recorded": ("这份分析保存时没有记录依据覆盖，不按现在的资料补算。", "This analysis was saved without its coverage count; it is not worked out again from your material now."),
    "cov.conflict": ("另有硬性条件冲突，请先看下面的硬性条件。", "A hard condition is in conflict: check the hard conditions below first."),
    "cov.related": ("与岗位相关的经历", "Experiences related to the job"),
    "cov.pending": ("待补充与待确认", "To add or confirm"),
    "cov.none_related": ("还没有找到有依据的要求。", "No requirement has evidence yet."),
    "cov.none_pending": ("没有待补充或待确认的技能要求。", "No skill requirement to add or confirm."),
}
