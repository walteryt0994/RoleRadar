"""Whose doing a quote of an experience field tells (U3-C4, business F1; C4 report 9.4, R2 after 11.3).

A quote standing word for word in a field shows that the words are there, not that the user did what they say. A
field may tell what someone else did beside what the user did ("组员负责搭建模型，我整理了会议记录"), so the direction
workflow (app.directions.explore) takes a quote as the user's own basis only when, read where it stands, everything
it touches is the user's own doing.

How a field is read. Each sentence (the breaks of U2's quote reading, app.experience_analysis.SENTENCE_BREAK) is cut
into stretches at clause punctuation, at a turn (app.experience_analysis.TURN), and where a connective brings in
someone new ("……而组员负责……", "... while my teammate ..."). A stretch says who acts:

- when it names the doer of a passive ("由组员完成", "by my teammate", "由我完成"): that one;
- else when it begins with someone, after its connectives and a few leading adverbs ("组员负责……", "另一位同学……",
  "我的队友……", "他……", "my teammate ...", "我整理了……", "I ..."): that one. The user together with others ("我和组
  员", "组员和我", "我们", "my teammate and I", "we") is the user, with others (below);
- else when it names someone in a place this reading cannot account for: that one ("建模组员负责");
- else nobody: it goes on with the stretch before it in its sentence, and a sentence begins with the user, whose
  field it is.

Someone named elsewhere in a stretch is read by what stands right before them (with the words of MODIFIERS between):

- the one something is done to or for, spoken to or asked about, a role or a circumstance ("为组员", "采访老师",
  "请教导师", "担任组长", "在导师指导下", "组员的数据", "for my teammate"): what follows is still the user's doing;
- the one the user lets, arranges, asks, guides, teaches or watches ("让组员", "安排队友", "指导组员", "观察组员",
  "asked my teammate", "had my teammate"): what follows is that person's doing. A quote of it is the user's (their
  arranging, guiding or watching) only with the words that say so: "安排队友搭建模型" is, "搭建模型" cut from it is not;
- the one the user works with or helps ("和组员", "帮助组员", "with my teammate"), and the same for "我们", "一起" and
  "together": what follows was done with others. A quote of it is the user's only with words that say so ("和组员一起
  搭建了模型", "一起搭建了模型", "帮助组员调试代码"), never as if the user had done it alone.

A stretch that names no doer of its own right after such words goes on needing the same words: nobody can tell
whether what it says is still the other person's ("我安排队友搭建模型，并整理了会议记录"). Someone named in any
other place beside a doer of the other kind makes the stretch unsure.

A quote is the user's own (SELF) when every stretch it touches, at every place it stands in the field, is the
user's, with the words each part it touches needs. A short quote is read with the stretch it lies in, so cutting off
a subject or a qualifier changes nothing: "搭建模型" in "组员负责搭建模型" or in "我安排队友搭建模型" is someone else's
doing (OTHER). A quote touching both the user's doing and someone else's is MIXED; one touching a stretch this
reading cannot place, or work done with others without the words that say so, is UNSURE. Only SELF is taken: what
it recognises as unclear, the workflow lowers, it never guesses. This reads nothing but the field the quote names,
it never raises what any other rule lowers (plans, denials, claims: app.experience_analysis.quote_flags), and it is
no full reading of language: wordings it does not know may still be read wrongly.
"""
import re

from app.experience_analysis import SENTENCE_BREAK, TURN

SELF = "self"
OTHER = "other"
MIXED = "mixed"
UNSURE = "unsure"

# Where someone named in a stretch stands (see the module docstring).
OBJECT = "object"
DIRECTED = "directed"
SHARED = "shared"

# Someone other than the user, as a stretch may name them: longer words first; "他" not that of "其他" or "吉他".
OTHERS_ZH = (
    "其他组员|其他成员|其他同学|其他人|其余人|组员|队员|队友|组长|队长|同学|同事|同伴|伙伴|搭档|合作者|合伙人|成员|"
    "导师|老师|教授|助教|学长|学姐|学弟|学妹|师兄|师姐|师弟|师妹|室友|朋友|主管|经理|领导|上级|前辈|别人|他人|旁人|"
    "他们|她们|(?<![其吉])他|她"
)
OTHERS_EN = (
    r"teammates?|team\s+members?|group\s*mates?|classmates?|colleagues?|co-?workers?|partners?|members?|others|"
    r"someone\s+else|somebody\s+else|supervisors?|mentors?|advisors?|professors?|instructors?|managers?|leaders?|"
    r"he|she|they"
)
# The user. Not "自己", which is whoever the stretch is about; not the "我" of "自我", "我校" or "我司".
ME_ZH = r"我们|咱们|本人|(?<!自)我(?![校司方国院系])"
ME_EN = r"\bI\b|(?i:\bwe\b|\bmyself\b)"
# The user as one of a group, and words saying something was done with others.
GROUP = re.compile(r"我们|咱们|(?i:we)")
JOINTLY = re.compile(r"一起|共同|携手|(?i:\btogether\b|\bjointly\b)")

# Words that may stand between a marker and the one it names: how many, which, whose.
COUNT_ZH = r"(?:[0-9０-９一二两三四五六七八九十百几多]+\s*(?:位|名|个))"
MODIFIERS_ZH = (
    rf"(?:{COUNT_ZH}|另一位|另一名|另外一位|另外的|另外|其中一位|其中一名|其中|其他|其余|各位|所有|全体|小组|组内|组里|"
    r"团队|项目组|课题组|本组|同组|我们组|我们小组|我们团队|我们的|我的|的)*"
)
MODIFIERS_EN = r"(?:\b(?:my|our|a|an|the|other|another|fellow|some|several|all|two|three|one\s+of\s+(?:my|our|the))\s+)*"

# What a stretch may begin with before whoever acts in it: connectives, turns and a few adverbs of time or order.
LEAD = re.compile(
    r"^(?:\s|但是|但|不过|可是|然而|而是|而且|并且|同时|然后|之后|随后|后来|最后|最终|期间|当时|首先|其次|起初|一开始|目前|"
    r"现在|此外|另外|以及|而|并|也|还|又|再|则|就|且|"
    r"(?i:and|but|however|instead|whereas|although|though|while|then|later|finally|first|next|also|meanwhile|"
    r"afterwards|eventually|initially)\b)*"
)
# The doer of a passive, of either kind.
BY_OTHER = re.compile(rf"(?:由|被|交由|交给){MODIFIERS_ZH}(?:{OTHERS_ZH})|(?i:\bby\s+{MODIFIERS_EN}(?:{OTHERS_EN})\b)")
BY_ME = re.compile(r"(?:由|被|交由)(?:我们|我|本人)|(?i:\bby\s+(?:me|myself|us)\b)")
# Who begins a stretch: the user with others, someone the user's, the user, or someone else.
WITH_ME = re.compile(
    rf"^(?:(?:我|本人)(?:和|与|跟|同|及){MODIFIERS_ZH}(?:{OTHERS_ZH})|{MODIFIERS_ZH}(?:{OTHERS_ZH})(?:和|与|跟|同|及)(?:我|本人)"
    rf"|(?i:{MODIFIERS_EN}(?:{OTHERS_EN})\s+and\s+(?:I|me)\b|I\s+and\s+{MODIFIERS_EN}(?:{OTHERS_EN})\b))"
)
MINE = re.compile(
    rf"^(?:(?:我的|我们的|我们组的?|我们小组的?|我们团队的?|本组的?){MODIFIERS_ZH}(?:{OTHERS_ZH})(?!的)"
    rf"|(?i:(?:my|our)\s+{MODIFIERS_EN}(?:{OTHERS_EN})\b))"
)
ME_FIRST = re.compile(rf"^(?:{ME_ZH}|{ME_EN})")
OTHER_FIRST = re.compile(rf"^(?:{MODIFIERS_ZH}(?:{OTHERS_ZH})(?!的)|(?i:{MODIFIERS_EN}(?:{OTHERS_EN})\b))")

# What stands right before someone named (the words of MODIFIERS aside), by the place it gives them. When two kinds
# end there, the longer words decide ("请教" is asking about, not "教"; "arranged for" is arranging, not "for").
AFTER_MARKER = r"(?:了|过|着)?\s*$"
AS_OBJECT = re.compile(
    r"(?:面向|服务|采访|访谈|询问|请教|咨询|调研|调查|访问|拜访|看望|测试|招募|联系|感谢|回应|答复|担任|作为|成为|当选|我是|"
    r"我们是|本人是|代表|替|(?<![认因以称较行])为|(?<!交)给|(?<![方倾导意])向|(?<![绝相])对|(?<![现正存所实自内外])在|(?<!自)从)"
    rf"{AFTER_MARKER}"
    r"|(?i:\b(?:for|to|of|among|as|from|than|was|were|am|is|are|been|became|become|interviewed|interviewing|surveyed|"
    r"surveying|thanked|contacted|visited)\s*$)"
)
DIRECTING = re.compile(
    r"(?:让|叫|(?<!申)请|安排|要求|派|指派|委派|委托|督促|鼓励|提醒|通知|告诉|说服|邀请|组织|召集|动员|带领|领导|指导|辅导|"
    r"教|培训|协调|管理|观察|观看|旁观|看着|看到|看见|看|监督)"
    rf"{AFTER_MARKER}"
    r"|(?i:\b(?:asked\s+for|arranged\s+for|asked|ask|asking|told|tell|telling|had|have|has|let|lets|made|make|got|get|"
    r"assigned|assign|assigning|instructed|instruct|directed|direct|arranged|organised|organized|organising|organizing|"
    r"coordinated|coordinating|led|lead|leading|managed|managing|supervised|supervising|oversaw|mentored|mentoring|"
    r"taught|teaching|trained|training|guided|guiding|encouraged|invited|watched|watching|observed|observing|saw|"
    r"seeing)\s*$)"
)
SHARING = re.compile(
    r"(?:帮助|帮忙|帮|协助|配合|支持|陪同|陪伴|陪|跟随|参与|携手|和|与|跟|及|(?<![共相不合如认赞])同)"
    rf"{AFTER_MARKER}"
    r"|(?i:\b(?:together\s+with|with|alongside|helped|help|helping|assisted|assist|assisting|supported|support|"
    r"supporting|joined|accompanied)\s*$)"
)
PLACES = ((OBJECT, AS_OBJECT), (DIRECTED, DIRECTING), (SHARED, SHARING))
# Matched at the end of the name (re.match from a position, so no "^", which would only hold at the text's start).
OWNED_AFTER = re.compile(r"的|'s\b")
TRAILING_MODIFIERS = re.compile(rf"(?:{MODIFIERS_ZH}|(?i:\s*{MODIFIERS_EN}))$")
SOMEONE = re.compile(rf"{OTHERS_ZH}|(?i:\b(?:{OTHERS_EN})\b)")
ME = re.compile(rf"{ME_ZH}|{ME_EN}")
# Clause punctuation, and a connective that brings in someone new.
CLAUSE = re.compile(r"[，,、：:]")
SWITCH = re.compile(
    rf"(?:而且|并且|同时|然后|之后|随后|以及|而(?![是且]))(?=\s*{MODIFIERS_ZH}(?:{OTHERS_ZH}|{ME_ZH}))"
    rf"|(?i:\b(?:and|while|whereas|then)\s+(?={MODIFIERS_EN}(?:{OTHERS_EN})\b|(?-i:I\b)|we\b))"
)
# An English subject before "and": "my teammate and I" is one subject, not two stretches.
ONLY_SOMEONE = re.compile(rf"^\s*(?i:{MODIFIERS_EN}(?:{OTHERS_EN}))\s*$")


def whose(quote: str, field_text: str) -> str | None:
    """SELF, OTHER, MIXED or UNSURE for the quote as it stands in the field (see the module docstring); None when it
    stands nowhere in it. Only whitespace may differ, as everywhere quotes are checked."""
    places = _places(quote, field_text)

    if not places:
        return None

    stretches = _stretches(field_text)
    touched = set()

    for start, end in places:
        for first, last, state, parts in stretches:
            if not (first < end and start < last):
                continue

            if state == SELF:
                # Someone else's doing, or work done with others, that the quote touches without the words it needs.
                lacking = {
                    kind
                    for begin, finish, needs, kind in parts
                    if begin < finish and begin < end and start < finish and not any(start <= a and b <= end for a, b in needs)
                }
                state = OTHER if OTHER in lacking else UNSURE if lacking else SELF

            touched.add(state)

    if OTHER in touched and SELF in touched:
        return MIXED

    if OTHER in touched:
        return OTHER

    if UNSURE in touched or not touched:
        return UNSURE

    return SELF


def _places(quote: str, text: str) -> list[tuple[int, int]]:
    """Every place the quote stands in the text, overlapping ones included."""
    pattern = re.compile(r"\s+".join(re.escape(word) for word in quote.split()))
    places = []
    found = pattern.search(text)

    while found is not None:
        places.append((found.start(), found.end()))
        found = pattern.search(text, found.start() + 1)

    return places


def _stretches(text: str) -> list[tuple[int, int, str, tuple]]:
    """The stretches of the text, each with whose doing it tells after carrying on from the stretch before, and its
    parts that are someone else's doing or done with others: (start, end, the words a quote of it needs, kind)."""
    stretches = []
    begin = 0

    for end in [found.start() for found in SENTENCE_BREAK.finditer(text)] + [len(text)]:
        current, needing = SELF, None

        for first, last in _cut(text, begin, end):
            state, parts = _state(text[first:last])
            parts = [(first + a, first + b, tuple((first + s, first + e) for s, e in needs), kind) for a, b, needs, kind in parts]

            if state is None:
                state = current

                # No doer of its own right after someone else's doing or work done with others: the same words needed.
                if needing is not None:
                    parts.insert(0, (first, last, needing, UNSURE))

            current = state
            trailing = [needs for a, b, needs, kind in sorted(parts) if b == last and a < b]
            needing = trailing[0] if trailing else None
            stretches.append((first, last, state, tuple(parts)))

        next_break = SENTENCE_BREAK.match(text, end)
        begin = next_break.end() if next_break else end

    return stretches


def _cut(text: str, begin: int, end: int) -> list[tuple[int, int]]:
    """One sentence cut into stretches: punctuation belongs to none; a turn or a connective begins the next."""
    cuts = [(found.start(), found.end()) for found in CLAUSE.finditer(text, begin, end)]
    cuts += [(found.start(), found.start()) for found in TURN.finditer(text, begin, end)]

    for found in SWITCH.finditer(text, begin, end):
        previous = max((cut_end for _, cut_end in cuts if cut_end <= found.start()), default=begin)

        if not ONLY_SOMEONE.match(text[previous:found.start()]):
            cuts.append((found.start(), found.start()))

    pieces, start = [], begin

    for cut_start, cut_end in sorted(cuts):
        if cut_start > start:
            pieces.append((start, cut_start))

        start = max(start, cut_end)

    if end > start:
        pieces.append((start, end))

    return [(first, last) for first, last in pieces if text[first:last].strip()]


def _state(stretch: str) -> tuple[str | None, list]:
    """Whose doing one stretch tells: SELF, OTHER or UNSURE, or None when it names nobody; and its parts that are
    someone else's doing (OTHER) or done with others (UNSURE), each with the words a quote of it needs."""
    lead = LEAD.match(stretch).end()
    body = stretch[lead:]
    by_other = [(found.start(), found.end()) for found in BY_OTHER.finditer(stretch)]
    by_me = [(found.start(), found.end()) for found in BY_ME.finditer(stretch)]
    taken = by_other + by_me
    # Words saying the user did something with others, and where what was done with others begins; and the words
    # of the user letting, arranging or watching someone, after which it is that person's doing.
    jointly, shared_from, directing = [], [], []
    first = None

    for pattern, state in ((WITH_ME, SELF), (MINE, OTHER), (ME_FIRST, SELF), (OTHER_FIRST, OTHER)):
        found = pattern.match(body)

        if found is not None:
            first = state
            span = (lead + found.start(), lead + found.end())
            taken.append(span)

            if pattern is WITH_ME or (pattern is ME_FIRST and GROUP.fullmatch(found.group())):
                jointly.append(span)
                shared_from.append(span[1])

            break

    someone = False

    for found in SOMEONE.finditer(stretch):
        if _within(found, taken):
            continue

        kind, marker = _placement(stretch, found)

        if kind is None:
            someone = True
        elif kind == DIRECTED:
            directing.append((marker, found.end()))
        elif kind == SHARED:
            jointly.append((marker, found.end()))
            shared_from.append(found.end())

    me = False

    for found in ME.finditer(stretch):
        if _within(found, taken) or _placement(stretch, found)[0] is not None:
            continue

        me = True

        if GROUP.fullmatch(found.group()):
            jointly.append((found.start(), found.end()))
            shared_from.append(found.end())

    for found in JOINTLY.finditer(stretch):
        jointly.append((found.start(), found.end()))
        shared_from.append(found.end())

    length = len(stretch)
    parts = [(words[1], length, (words,), OTHER) for words in directing]
    parts += [(start, length, tuple(jointly), UNSURE) for start in shared_from]

    if by_other and by_me:
        return UNSURE, parts

    doer = OTHER if by_other else SELF if by_me else first

    if doer == SELF:
        return (UNSURE if someone else SELF), parts

    if doer == OTHER:
        return (UNSURE if me else OTHER), parts

    if someone and me:
        return UNSURE, parts

    return (OTHER if someone else SELF if me else None), parts


def _within(found: re.Match, spans) -> bool:
    return any(start <= found.start() and found.end() <= end for start, end in spans)


def _placement(stretch: str, found: re.Match) -> tuple[str | None, int | None]:
    """Where someone named here stands (OBJECT, DIRECTED or SHARED), and where the words saying so begin; (None,
    None) when this reading cannot account for them (see the module docstring)."""
    if OWNED_AFTER.match(stretch, found.end()):
        return OBJECT, found.start()

    before = TRAILING_MODIFIERS.sub("", stretch[: found.start()])
    best = (None, None)

    for kind, pattern in PLACES:
        marker = pattern.search(before)

        if marker is not None and (best[1] is None or marker.start() < best[1]):
            best = (kind, marker.start())

    return best
