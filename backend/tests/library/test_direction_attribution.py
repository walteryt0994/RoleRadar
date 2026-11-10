"""Whose doing a quote of an experience field tells (app.directions.attribution; U3-C4 business F1).

Only SELF is ever taken as the user's own basis. These cases are synthetic; the real answer that showed the gap is
replayed in test_direction_c4_replay.
"""
import pytest

from app.directions.attribution import MIXED, OTHER, SELF, UNSURE, whose

D2 = "组员负责搭建模型，我整理了会议记录。"

MIXED_SUBJECTS = [
    # The run's own sentence: the whole, someone else's clause, a short quote of it with its subject cut off, a quote
    # across both clauses, and the user's clause with or without its subject.
    ("D2 整句", D2, D2, MIXED),
    ("D2 他人分句", D2, "组员负责搭建模型", OTHER),
    ("D2 截掉主语的短引文", D2, "搭建模型", OTHER),
    ("D2 截掉主语的短引文 2", D2, "负责搭建模型", OTHER),
    ("D2 跨两个分句", D2, "模型，我整理了会议记录", MIXED),
    ("D2 本人分句", D2, "我整理了会议记录", SELF),
    ("D2 本人分句，截掉主语", D2, "整理了会议记录", SELF),
    # The other order.
    ("倒序 本人分句", "我整理了会议记录，组员负责搭建模型。", "我整理了会议记录", SELF),
    ("倒序 他人短引文", "我整理了会议记录，组员负责搭建模型。", "搭建模型", OTHER),
    ("倒序 整句", "我整理了会议记录，组员负责搭建模型。", "我整理了会议记录，组员负责搭建模型。", MIXED),
]

WORDINGS = [
    ("队友", "队友完成了建模，我写了报告。", "完成了建模", OTHER),
    ("被动：由组员", "模型由组员搭建，我整理会议纪要。", "模型由组员搭建", OTHER),
    ("被动：由我", "由我负责建模，组员负责记录。", "由我负责建模", SELF),
    ("被动决定：我负责的由组员测试", "我负责的模块由组员测试。", "我负责的模块由组员测试", OTHER),
    ("被动决定：组员负责的由我完成", "组员负责的部分由我完成。", "组员负责的部分由我完成", SELF),
    ("另一位同学，分号", "另一位同学负责数据清洗；我负责可视化。", "负责数据清洗", OTHER),
    ("我的队友", "我的队友负责建模，我负责汇报。", "负责建模", OTHER),
    ("我们组的另一位同学", "我们组的另一位同学负责建模。", "负责建模", OTHER),
    ("他", "他负责前端开发，我负责后端。", "负责前端开发", OTHER),
    ("他们", "他们负责建模，我负责汇报。", "负责建模", OTHER),
    ("老师", "老师给我们布置了任务，我完成了问卷设计。", "布置了任务", OTHER),
    ("连接词换人：而", "我负责记录而组员负责建模。", "组员负责建模", OTHER),
    ("连接词换人：而，本人一侧", "我负责记录而组员负责建模。", "我负责记录", SELF),
    ("没有主语，接着上一个人：并", "组员负责搭建模型并完成调参。", "完成调参", OTHER),
    ("没有主语，接着上一个人：逗号", "组员负责搭建模型，并完成了调参。", "完成了调参", OTHER),
    ("没有主语，接着本人", "组员负责搭建模型，我整理了会议记录，并汇报了结果。", "汇报了结果", SELF),
    ("新句子从本人开始", "组员负责搭建模型。整理了会议记录。", "整理了会议记录", SELF),
    ("时间副词在前", "后来组员负责建模。", "负责建模", OTHER),
    ("宾语提前", "建模组员负责，记录我负责。", "建模组员负责", OTHER),
    ("组员自己", "组员自己负责建模。", "负责建模", OTHER),
    ("English: comma and", "My teammate built the model, and I took the meeting notes.", "built the model", OTHER),
    ("English: own clause", "My teammate built the model, and I took the meeting notes.", "I took the meeting notes", SELF),
    (
        "English: whole",
        "My teammate built the model, and I took the meeting notes.",
        "My teammate built the model, and I took the meeting notes.",
        MIXED,
    ),
    ("English: while", "I took notes while my teammate built the model.", "my teammate built the model", OTHER),
    ("English: while, own", "I took notes while my teammate built the model.", "I took notes", SELF),
    ("English: passive", "The model was built by my teammate.", "The model was built", OTHER),
    ("English: semicolon", "Teammates handled the modelling; I wrote the minutes.", "handled the modelling", OTHER),
    ("English: then", "One of my classmates collected the data, then I cleaned it.", "collected the data", OTHER),
    ("English: then, own", "One of my classmates collected the data, then I cleaned it.", "I cleaned it", SELF),
]

THE_USER = [
    ("我和组员一起", "我和组员一起搭建了模型。", "我和组员一起搭建了模型", SELF),
    ("组员和我一起", "组员和我一起搭建了模型。", "组员和我一起搭建了模型", SELF),
    ("和同学一起", "和同学一起访谈了五位校园店主。", "和同学一起访谈了五位校园店主", SELF),
    ("帮助组员", "帮助组员调试代码。", "帮助组员调试代码", SELF),
    ("在导师的指导下", "在导师的指导下完成了论文。", "完成了论文", SELF),
    ("在导师指导下", "在导师指导下完成了论文。", "在导师指导下完成了论文", SELF),
    ("担任组长", "担任组长，带领组员完成项目。", "担任组长，带领组员完成项目", SELF),
    ("作为团队成员", "作为团队成员参与项目。", "作为团队成员参与项目", SELF),
    ("我们小组", "我们小组完成了需求分析。", "我们小组完成了需求分析", SELF),
    ("组员的（所有格）", "合并了组员的数据。", "合并了组员的数据", SELF),
    ("调研了 50 名同学", "调研了 50 名同学的使用习惯。", "调研了 50 名同学的使用习惯", SELF),
    ("为团队成员", "为团队成员提供数据支持。", "为团队成员提供数据支持", SELF),
    ("其他（不是人）", "其他准备工作由我完成。", "其他准备工作由我完成", SELF),
    ("我教组员", "我教组员使用 Python。", "我教组员使用 Python", SELF),
    ("English: worked with", "I worked with my teammate to build the model.", "I worked with my teammate to build the model.", SELF),
    ("English: my teammate and I", "My teammate and I built the model.", "My teammate and I built the model.", SELF),
    ("English: one of the members", "I was one of the members of the data team.", "I was one of the members of the data team.", SELF),
    ("English: alongside", "Worked alongside two classmates on the survey design.", "Worked alongside two classmates on the survey design.", SELF),
    ("没有提到任何人", "用 Python 清洗订单数据，并用 SQL 汇总每周销量。", "用 Python 清洗订单数据，并用 SQL 汇总每周销量。", SELF),
]

CANNOT_TELL = [
    # Someone named beside the user in a way this reading cannot account for: lowered, never guessed.
    ("我检查了组员负责的部分", "我检查了组员负责的部分。", "我检查了组员负责的部分", UNSURE),
    ("没有标点的两个人", "组员负责搭建模型我整理了会议记录。", "我整理了会议记录", UNSURE),
    ("同一分句里两个被动", "前半部分由组员完成后半部分由我完成。", "前半部分由组员完成后半部分由我完成", UNSURE),
]

# R2 (C4 report 11.2, 11.3): letting, arranging, asking, guiding or watching someone is not doing what they did. The
# user's own part is the arranging or the watching, quoted with the words that say so; what they did is theirs.
ARRANGED_OR_WATCHED = [
    ("让：他人的动作", "我整理了会议记录，并让组员负责搭建模型。", "搭建模型", OTHER),
    ("让：截掉“让”", "我整理了会议记录，并让组员负责搭建模型。", "组员负责搭建模型", OTHER),
    ("让：本人分句", "我整理了会议记录，并让组员负责搭建模型。", "我整理了会议记录", SELF),
    ("让：本人的安排，原话", "我整理了会议记录，并让组员负责搭建模型。", "让组员负责搭建模型", SELF),
    ("安排：他人的动作", "我安排队友搭建模型，并整理了会议记录。", "搭建模型", OTHER),
    ("安排：截掉“安排”", "我安排队友搭建模型，并整理了会议记录。", "队友搭建模型", OTHER),
    ("安排：本人的安排，原话", "我安排队友搭建模型，并整理了会议记录。", "我安排队友搭建模型", SELF),
    ("安排之后没有主语：分不清是谁", "我安排队友搭建模型，并整理了会议记录。", "整理了会议记录", UNSURE),
    ("安排：整句带着安排", "我安排队友搭建模型，并整理了会议记录。", "我安排队友搭建模型，并整理了会议记录。", SELF),
    ("asked：他人的动作", "I asked my teammate to build the model, and I took notes.", "build the model", OTHER),
    ("asked：截掉 asked", "I asked my teammate to build the model, and I took notes.", "my teammate to build the model", OTHER),
    ("asked：本人分句", "I asked my teammate to build the model, and I took notes.", "I took notes", SELF),
    ("asked：本人的安排，原话", "I asked my teammate to build the model, and I took notes.", "I asked my teammate to build the model", SELF),
    ("观察：他人的动作", "我观察组员搭建模型，并整理了会议记录。", "搭建模型", OTHER),
    ("观察：本人的观察，原话", "我观察组员搭建模型，并整理了会议记录。", "我观察组员搭建模型", SELF),
    ("看到", "我看到组员搭建模型。", "搭建模型", OTHER),
    ("指导", "我指导组员搭建模型。", "搭建模型", OTHER),
    ("教", "我教组员使用 Python。", "使用 Python", OTHER),
    ("English: had", "I had my teammate build the model.", "build the model", OTHER),
    ("English: arranged for", "I arranged for my teammate to build the model.", "build the model", OTHER),
    # Words that look alike but give someone no doing of their own.
    ("请教（不是“教”）", "我请教了导师建模方法。", "我请教了导师建模方法", SELF),
    ("向导师汇报", "向导师汇报了进度。", "汇报了进度", SELF),
    ("为组员整理", "为组员整理资料。", "整理资料", SELF),
    ("替组员完成", "替组员完成了汇报。", "完成了汇报", SELF),
]

# Work done with others is the user's only with the words saying so: never cut down to the user doing it alone.
WITH_OTHERS_CUT = [
    ("我和组员一起：只剩动作", "我和组员一起搭建了模型。", "搭建了模型", UNSURE),
    ("我和组员一起：保留“和组员一起”", "我和组员一起搭建了模型。", "和组员一起搭建了模型", SELF),
    ("我和组员一起：保留“一起”", "我和组员一起搭建了模型。", "一起搭建了模型", SELF),
    ("组员和我一起：只剩动作", "组员和我一起搭建了模型。", "搭建了模型", UNSURE),
    ("一起之后没有主语", "我和组员一起搭建了模型，并完成了调参。", "完成了调参", UNSURE),
    ("帮助：只剩动作", "帮助组员调试代码。", "调试代码", UNSURE),
    ("我们小组：只剩动作", "我们小组完成了需求分析。", "完成了需求分析", UNSURE),
    ("共同：只剩动作", "与组员共同完成了需求分析。", "完成了需求分析", UNSURE),
    ("共同：保留“共同”", "与组员共同完成了需求分析。", "共同完成了需求分析", SELF),
    ("English: my teammate and I, only the doing", "My teammate and I built the model.", "built the model", UNSURE),
    ("English: helped, only the doing", "I helped my teammate debug the code.", "debug the code", UNSURE),
    ("English: helped, as written", "I helped my teammate debug the code.", "I helped my teammate debug the code.", SELF),
    ("English: worked with, only the doing", "I worked with my teammate to build the model.", "build the model", UNSURE),
]

CASES = MIXED_SUBJECTS + WORDINGS + THE_USER + CANNOT_TELL + ARRANGED_OR_WATCHED + WITH_OTHERS_CUT


@pytest.mark.parametrize(("field", "quote", "expected"), [case[1:] for case in CASES], ids=[case[0] for case in CASES])
def test_whose_doing_a_quote_tells(field, quote, expected):
    assert whose(quote, field) == expected


def test_a_quote_standing_twice_is_the_users_only_if_it_is_at_both_places():
    field = "我整理了会议记录。组员也整理了会议记录。"

    assert whose("整理了会议记录", field) == MIXED
    assert whose("我整理了会议记录", field) == SELF


def test_whitespace_may_differ_as_everywhere_quotes_are_checked_and_a_quote_found_nowhere_is_none():
    assert whose("用  Python  清洗 订单数据", "用 Python 清洗 订单数据。") == SELF
    # A space the field does not have: it stands nowhere, as the workflow's own reading of quotes finds it.
    assert whose("组员负责搭建模型， 我整理了会议记录", D2) is None
    assert whose("没有这句话", D2) is None
