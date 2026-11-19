"""The confirmed terms of the bilingual plan (rev. 2, section 7), held in both languages (Codex review F3: "落实已确认
的“反馈问题”等术语"). A label that is exactly one of the terms says it in both languages; the actions, page titles and
statuses the review named use the confirmed words, and the older words they replaced are gone from the interface.
"""
import pytest

from app.i18n import MESSAGES

# The plan's glossary: Chinese term → English term. Terms with a note in the plan carry it in the name only.
GLOSSARY = {
    "工作台": "Workspace", "继续处理": "Continue", "开始新任务": "Start a task", "最近保存": "Recently saved",
    "我的资料": "My profile", "经历库": "Experiences", "记录一段经历": "Add an experience", "经历内容": "Experience details",
    "参考经历": "Reference experiences", "保存草稿": "Save draft", "待核对": "Needs review", "已确认": "Confirmed",
    "修改后待核对": "Review changes", "方向探索": "Career exploration", "探索求职方向": "Explore career paths",
    "确认探索内容": "Review exploration", "可以探索的方向": "Career paths to explore", "探索记录": "Exploration history",
    "依据已变更": "Supporting information has changed", "重新探索": "Explore again", "岗位机会": "Jobs",
    "寻找机会": "Find opportunities", "发现岗位": "Discover jobs", "确认查找内容": "Review search", "发现的岗位": "Job results",
    "我的岗位": "My jobs", "补充岗位": "Add a job", "分析岗位": "Analyze job", "确认分析内容": "Review analysis",
    "结果语言": "Result language", "保存分析": "Save analysis", "加入我的岗位": "Add to my jobs", "保存查找摘要": "Save search summary",
    "我的决定": "My decision", "收藏": "Bookmarked", "准备申请": "Preparing to apply", "暂缓": "On hold", "跳过": "Skipped",
    "更新岗位": "Refresh job", "核对时仍公开发布": "Still listed at last check", "岗位状态待确认": "Job status unconfirmed",
    "反馈问题": "Report an issue", "查找与分析记录": "Search & analysis history", "部分完成": "Partially completed",
    "尚未分析": "Not analyzed", "临时结果已失效": "Temporary results unavailable", "重新查找": "Start a new search",
    "数据管理": "Data management", "回收站": "Trash", "恢复": "Restore", "永久删除": "Delete permanently", "导出资料": "Export data",
    "导出不完整": "Export incomplete", "下载可用内容": "Download available data", "必要要求": "Required skills",
    "加分项": "Preferred skills", "硬性条件": "Mandatory conditions", "写得不明确的要求": "Unclear requirements",
    "有经历支持": "Supported by selected experiences", "目前未找到支持材料": "No supporting evidence found",
    "待确认": "Needs confirmation", "不适用": "Not applicable", "查找时的核对": "Check at the time of search",
    "最近成功核对": "Last successful check", "读取额度不足": "Insufficient source request allowance",
    "模型请求额度不足": "Insufficient model request allowance", "应用内预算不足": "Insufficient in-app budget",
}
# Labels that are the Chinese term but stand for something else in their place, with why.
NOT_THE_TERM = {
    # A word inside a sentence built from pieces (the trash named mid-sentence, lower case in English).
    "dir.direction_input.14", "dir.direction_task.25", "dir.direction_records.18",
    # The rules' "to confirm" in a search's own sentence ("地点待确认"), not an analysis conclusion.
    "disc.rule.to_confirm",
    # The list's button refreshes every tracked job at once: the same action, said in the plural.
    "job.refresh.list_button",
}


def texts(key):
    zh, en = MESSAGES[key]

    return (zh if isinstance(zh, str) else zh[-1]), (en if isinstance(en, str) else en[-1])


def test_a_label_that_is_a_confirmed_term_says_it_in_both_languages():
    wrong = []

    for key in MESSAGES:
        zh, en = texts(key)

        if zh in GLOSSARY and key not in NOT_THE_TERM and en.strip().rstrip("…").strip() != GLOSSARY[zh]:
            wrong.append((key, zh, en))

    assert wrong == []


@pytest.mark.parametrize(
    ("key", "zh", "en"),
    [
        # Report an issue, wherever the user reports one.
        ("job.report_wrong", "反馈问题…", "Report an issue…"),
        ("job.job_keep.33", "反馈问题", "Report an issue"),
        ("job.job_record.71", "反馈的问题", "Reported issues"),
        ("job.rweb.notices.3", "已记下你反馈的问题。", "Your issue report has been recorded."),
        # The review pages, the results and the analysis page.
        ("job.discover_review.1", "确认查找内容", "Review search"),
        ("job.job_review.1", "确认分析内容", "Review analysis"),
        ("job.discover_input.33", "下一步：确认查找内容", "Next: review search"),
        ("job.job_input.16", "下一步：确认分析内容", "Next: review analysis"),
        ("job.discover_task.3", "发现的岗位", "Job results"),
        # The search page itself (Codex R1 review: the plan and its reference index come before an older image's words).
        ("job.discover_input.1", "寻找机会", "Find opportunities"),
        ("job.dweb.71", "返回寻找机会", "Back to Find opportunities"),
        ("job.job_input.1", "分析岗位", "Analyze job"),
        ("job.discover_task.15", "保存查找摘要", "Save search summary"),
        ("job.discover_task.85", "重新查找", "Start a new search"),
        # Temporary results that are gone, the export, and what a check said.
        ("job.dweb.72", "临时结果已失效", "Temporary results unavailable"),
        ("job.aweb.10", "临时结果已失效", "Temporary results unavailable"),
        ("dir.web.7", "临时结果已失效", "Temporary results unavailable"),
        ("dir.direction_records.20", "导出资料", "Export data"),
        ("data.export.download_available", "下载可用内容", "Download available data"),
        ("job.job_keep.8", "查找时的核对", "Check at the time of search"),
        ("job.job_apply_check.5", "岗位状态待确认：", "Job status unconfirmed: "),
        ("imp.rv.33", "待核对", "Needs review"),
    ],
)
def test_the_terms_the_review_named_are_the_confirmed_ones(key, zh, en):
    assert MESSAGES[key] == (zh, en)


@pytest.mark.parametrize(
    ("key", "zh", "en"),
    [
        ("job.batch.requests_short", "读取额度不足：", "Insufficient source request allowance: "),
        ("job.batch.model_requests_short", "模型请求额度不足：", "Insufficient model request allowance: "),
        ("job.batch.budget_short", "应用内预算不足：", "Insufficient in-app budget: "),
        ("data.export.incomplete", "导出不完整：", "Export incomplete: "),
        ("job.choice.basis_words.2", "依据已变更：", "Supporting information has changed: "),
        ("job.choice.82", "；最近成功核对：", "; last successful check: "),
        ("job.choice.68", "岗位状态待确认：", "Job status unconfirmed: "),
    ],
)
def test_a_reason_or_a_status_begins_with_its_confirmed_name(key, zh, en):
    chinese, english = texts(key)

    assert chinese.startswith(zh) and english.startswith(en)


def test_the_words_the_terms_replaced_are_gone_from_the_interface():
    replaced = (
        "指出不准", "指出的不准", "哪里不准", "哪一项不准", "部分不准", "记下不准", "核对这次查找", "核对发送内容", "保存这次查找的摘要",
        "上次成功核对是", "本次查找时的来源核对", "依据已修改", "找岗位",
    )
    english = (
        "Report an error", "Report something wrong", "error report", "Errors you reported", "Reported errors",
        "Review this search", "Review what will be sent",
    )

    assert [key for key in MESSAGES if any(word in texts(key)[0] for word in replaced)] == []
    assert [key for key in MESSAGES if any(word in texts(key)[1] for word in english)] == []
