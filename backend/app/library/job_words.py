"""Words for a job's facts that this app writes itself, shared by the search, its pages, the job records and the
export (Codex review F3): the names of the sources the app registers itself, and the job types its rules read a
posting's type as. Data keeps the registered name and the normalized type as they are; these words only say them in
the language of the text they go into, the same words in Chinese. Nothing here reads the library or a search.
"""
from app.i18n import MessageMap, t

# The names this app gives the sources it registers itself, by the name registered: said in the language of the text
# they go into, on a page or in a search's own words (Codex review F3). Data keeps the name as registered (a search's
# confirmation, a saved record, a watch's comparison); any other name, a real board's company among them, is shown as
# it is.
SOURCE_NAMES = MessageMap(
    {
        "合成招聘官网": "disc.src.name_careers",
        "合成搜索线索": "disc.src.name_leads",
        "合成招聘官网（类型筛选不可靠）": "disc.src.name_paged",
        "合成招聘官网（全职在前、实习在后）": "disc.src.name_mixed",
    }
)
# A board's name: its company, then what it is: a synthetic replay (app.discovery.lever_demo.entry) or a real board's
# public postings interface (app.discovery.lever.LeverBoard.name). The company is never translated.
REPLAY_MARK = "（Lever 格式合成回放）"
LEVER_MARK = "（Lever 公开职位接口）"
BOARD_MARKS = ((REPLAY_MARK, "disc.src.name_replay"), (LEVER_MARK, "disc.src.name_lever"))


def source_label(name: str | None) -> str:
    """A source's name as a page or a search writes it: one this app registers in the text's language, any other as
    it is (an empty one stays empty)."""
    if not name:
        return name or ""

    if name in SOURCE_NAMES:
        return SOURCE_NAMES[name]

    for mark, key in BOARD_MARKS:
        if name.endswith(mark) and len(name) > len(mark):
            return t(key, company=name[: -len(mark)])

    return name


# The job types these rules read a posting's type as (normalized), said in the language of the text they go into: the
# same words in Chinese. A type as the source wrote it (type_stated) is never translated (Codex review F3).
TYPE_WORDS = MessageMap({"实习": "job.type_value.internship", "校招": "job.type_value.campus"})


def type_word(value: str | None) -> str:
    """A posting's normalized job type as a page or a search writes it; another value as it is, none as empty."""
    if not value:
        return value or ""

    return TYPE_WORDS[value] if value in TYPE_WORDS else value
