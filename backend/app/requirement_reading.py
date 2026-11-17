"""What a requirement's own words list, and whether a posting's words lay down a condition (U3-D6 R4-R9; Claude).

Two local readings for the analysis (app.experience_analysis), by fixed rules over the words alone. They only ever
lower a conclusion, never raise one, and they are no full reading of the text. The model still says what each cited
passage is (practice, claim, ...); these rules only check what such a passage can stand for. What they cannot read
plainly they leave uncertain: never as support of the whole by part of it, never as a definite "does not apply".

1. A requirement whose own words list several items (F1). A passage of the user's own practice supports it only as
   far as it names the items. The words are read one of three ways (R6): plainly one item, which a passage of
   practice supports as before; plainly a list, whose items it must name; or something that may list items but does
   not read plainly, which practice alone never supports (it needs the user's confirmation). Words that may part
   items are never taken for one item unless one of the two forms below plainly parts none (R6: R5 took two items
   without a lead-in, and items parted by full-width commas, for one item, so one named item "supported" all).
   R8: words with nothing in them that may part items are not yet one item either. They are one item only when they
   plainly ask for one thing (_single: once the wording is left out, one short phrase with no word that may join two
   asks and at most one run of names); otherwise they are unread (UNREAD), which practice alone never supports either.
   R9: one item is still no proof that a passage shows all of it. A short phrase may be one name or several things
   ("Microsoft Power BI", "Python SQL"); the rules cannot tell which, so once two or more parts are left of what it
   asks for (reading(), _asks), a passage of practice must name every part to support it, or it needs confirmation.
   One part ("Python") cannot be shown only in part, and is supported on the model's label as before.
   - All of them are asked for unless the words plainly ask for any one: "one of", "any of", "either", "at least
     one", "之一", "任一", an item joined by "or" / "或", an "or similar ..." end, or a lead-in of examples ("such as",
     "including", "like", "e.g.", "例如", "比如", "如").
   - "all of", "each of", "both", "全部", "所有", "每项" ask for all, whatever lead-in follows: "all of these required
     tools, including Python, SQL, and Tableau" asks for the three (R5: R4 read "including" as any one).
   - Unclear, and supported only when every item is named (true however it is read):
     - a lead-in of examples after "required", "must" or "必须" (all or examples?);
     - "and" and "or" in the same list (which binds?);
     - "all of" and "one of" together;
     - a list inside another ("Python, SQL, and data tools such as Tableau or Power BI");
     - something listed after a lead-in that does not part into plain items;
     - without a lead-in (R6), things joined that are not plain items ("Ability to communicate and collaborate":
       what "Ability to" asks for does not part plainly), or a full-width comma ending a clause that may ask for
       more ("掌握 Python，熟悉 SQL").
   - Read as a list: two or more items after a lead-in (with, in, of, using, such as, including, one of, 熟悉, 掌握,
     包括, ...): "Experience with Python and SQL", "Experience with Python, SQL", "熟悉 Python 和 SQL" ask for both (R5).
     Without a lead-in (R6), two or more plain items (short, and with no "of", "in", "with", "for", "to", ...),
     however joined: "Python and SQL", "Python, SQL", "Python、SQL", "Python 和 SQL" ask for both, as the same words
     after a lead-in do. The words before the first item's names are no part of it ("Must know Python and SQL",
     "需要 Python 和 SQL" list Python and SQL). A plain series of three or more ("A, B, and C") is read as before.
   - One item though a comma or "and" stands in it: words of a fixed set that describe one thing, joined
     ("Ability to write clean, effective code", "a fast, reliable pipeline", "written and verbal communication
     skills"; DESCRIBING_WORDS). A word not in the set is never taken for one: "Ability to build dashboards, data
     pipelines", the same shape, reads unclear. What the set misses is read as a list or as unclear, never as one
     item.
   - The words before a lead-in (R7) are either only its wording, and left out, or a requirement in their own right,
     which stays an item the passage must name too: "machine learning with Python and SQL" and "database
     administration using Python and SQL" list it with the two (R6 listed only what followed the last lead-in, so a
     passage naming the tools "supported" machine learning); "machine learning with Python" lists two. Wording is a
     fixed set (GENERIC_WORDS: "Experience", "3+ years of professional experience", "Hands-on", "Must know", "熟练",
     ...); any other word asks for something, so what the set misses only ever asks for more. The same holds for the
     words before the first item's names without a lead-in ("Machine learning via Python and SQL").
     - Before a lead-in of examples stands what they are examples of when its last word names a kind of thing
       (CATEGORY_WORDS: "programming languages such as Java, C++", "data visualization tools such as Tableau"):
       left out, as before. Otherwise it is a requirement ("Data analysis, including SQL and Excel"), and whether the
       examples are asked for too is not plain: unclear.
     - With a list any one of which is asked for, or unclear, only every item named shows both: unclear.
   - An open end ("and other technical tools", "etc.", "等") asks for nothing in particular and is left out; a bound
     ("2027 or later") is no item.
   - English items part at commas and at and, or, and/or, &, " + ", "plus" (not "a plus": "Docker is a plus") and
     "as well as" (R7: "plus" and "+" added); "along with", "together with" and the like through the word before
     "with", which is wording. Chinese ones at 、 and at 和, 以及, 及, 与, 或, 或者. A full-width comma or semicolon (，；)
     parts items only between short terms (R6: "熟悉 Python，SQL，Tableau" and "Python，SQL" list them); otherwise it
     ends a clause. A clause that is only a year bound ("2027 年及以后毕业", "2026 届毕业生") is no item and is left
     out ("必须掌握 Python，2027 年及以后毕业" lists nothing); any other may ask for more, so the words read unclear.
     及以后, 及以上, 及其, 及时 or 参与 join nothing.
   - A slash (R7) between two words that are not one term joins items, and says neither "and" nor "or": unclear,
     unless the words say which ("one of Python/R/SQL"). "Python/SQL" and "CI/CD" need both named. One term, and no
     join: two single letters ("A/B testing", "I/O"), a number on either side ("24/7"), "and/or".
   - Unread (R8; R7 read these as one item, which the model's labelled practice then supported whole): words that
     join with no lead-in ("Python alongside SQL", "Python besides SQL"), prepositions that are no lead-in ("Python for
     data analysis", "SQL for reporting"), Chinese joining words ("使用 Python 进行数据分析"), and anything longer than
     one short phrase. Their words are kept as they stand, and no items are made up from them.
     What the fixed sets miss: a joining word outside LINKING_WORDS between two words starting low ("data analysis
     amid reporting"), or one outside LINKING_CHINESE in Chinese words, and two names with nothing between them
     ("Python SQL"), still read as one item; since R9 a passage must name every part of it (above), so they no longer
     get support from part of it (R8 let them).
   - Marks typed either way (R9, _marks, _judging): curly quotes and apostrophes, dashes, odd spaces, full-width forms
     read as their plain marks, and only Han characters are Chinese, so they get no words past the checks ("“Python
     for data analysis”", "Python for client’s data analysis", "Ｒ for data analysis" are unread as the plain words
     are). A mark left after that (an arrow, a bullet, a middle dot) or a letter of another script is not read, and
     the words are unread. The words are kept and shown as written: only the reading takes them as plain marks.
   - A stretch before a lead-in that is a phrase of its own is no item (R10, U3-D6-3 F2): what is left of it once
     the wording is out holds a linking word ("Curiosity about connectivity in cloud systems" leaves "about
     connectivity"; "Experience across teams with Python"). R7 took it for an item, so the page said "所列各项都要:
     about connectivity、cloud systems", items the posting never listed. Such words are unread: kept as they stand,
     no items made up, and practice alone never supports them. A stretch that is a plain item ("machine learning",
     "database administration") stays one, as before.
   - Only the requirement's own words are read, never its quote: a posting line "Python, SQL and Tableau" that the
     parse split into three requirements, each quoting the whole line, is three single requirements.
   - An item is named when its words stand in one passage (case, plural, hyphen or space aside; Chinese as written,
     each run of Han characters, in any order with the English words beside it; R9: another form of the same word,
     "built" for "building", "wrote" for "write", with irregular past forms from a fixed set, IRREGULAR). Another way
     of saying it (React for "front-end frameworks", "数据分析" for "data analysis") is not recognised: such support
     needs the user's confirmation.

2. A posting quote that lays down a condition (F2). "If the posting is specified as Onsite, you are required to work
   from an office" asks for the office only if the posting is onsite; the condition is kept with the requirement,
   word for word, never dropped.
   - Only "if", "unless", "provided (that)", "in case", "in the event (that)" and 如果, 假如, 倘若, 若, 如遇, 如需 start
     one; "when" does not ("ask questions when needed" lays none down).
   - It is held met or not met (R5) only when both of these hold, and is unknown otherwise:
     - it is one plain, positive condition on the posting's work mode, in a set form ("If the posting / role /
       position is (specified / listed / marked as) onsite / remote / hybrid", "如果岗位是现场办公 / 远程 / 混合办公");
       "unless", "not", a second condition ("and based in London") or anything else leaves it unknown;
     - the posting's own fact: the line "工作方式：onsite" (remote, hybrid) in the head of a posting text a source
       adapter built (app.discovery.lever writes the source's workplaceType there, before a blank line and the
       source's own description), the value exactly one of those three. "工作方式：not remote", a value of another
       form, a second such line or one in the description shows nothing.
   - The caller decides whether a text is a source adapter's (AnalysisRequest.posting_from_source): words a user
     pasted, however much like that line they look, are never taken for it.

3. How firmly a posting's own words ask (R10, U3-D6-3 F1; strength). The parse writes each requirement in its own
   words beside the posting's quote, and puts it in a group; the quote's being in the posting proves neither its
   words' firmness nor its group. "This should be your final internship before graduating." came back as "This must
   be the candidate's final internship before graduating", a hard constraint. strength reads the quote alone, each
   sentence of it (a decimal point ends none):
   - a sentence that requires: must, required, requires, mandatory, essential, necessary, need(s) (R11: on its own
     too, "You need a degree"), have to, ..., 必须, 务必, 须, 需要, 要求 (OBLIGED);
   - a sentence that only expects or prefers: should, ideally, preferred, desired, nice to have, a plus, a bonus, an
     asset, optional, ..., 最好, 优先, 加分, 希望, 建议, 应该, or one that says something is not required ("not
     required", "No degree required", "do not need", 不要求, 不需要, 无需, 不必) (EXPECTED; R12: only where these are
     on the whole of what it asks, below);
   - any other sentence states (STATED): it says nothing either way ("Bachelor's degree in Computer Science").
   The quote requires when any sentence of it does: a hard condition worded firmly beside a "should" keeps its
   firmness, and a mixed quote is never weakened. It only expects when every sentence of it does; a "should" beside
   a plain statement ("Bachelor's degree. You should have Go experience.") leaves the quote as it stands. "must not"
   still requires. obliges says whether words (the parse's own) require, its negations aside.
   R11 (Codex's review of R10): R10 read two firm sentences as soft. "Valid work authorization is not optional."
   counted "optional" as an expectation, its "not" unread; "SQL is not preferred but required." took "not preferred
   but required" for one denial, so "required" was denied. A sentence is now read as soft only when it plainly is;
   what is unclear keeps the group the parse put it in:
   - what is said not to be optional ("not optional", "non-optional", "by no means optional", "anything but
     optional", "far from optional") requires;
   - an expectation said not to hold ("not preferred", "not a plus", "hardly a plus") expects nothing: the sentence
     stands as stated;
   - a denial of a requirement reaches only so far (NOT_OBLIGED, _DENY): after "not", "n't", "never", "no longer", "by
     no means" or "in no way", only up to three adverbs in -ly (not "only"), "a"/"an" or "be"/"been" may stand before
     the word it denies ("not strictly required", "will not be required", "do not need"); "no" denies up to three
     words on only where it begins its clause, at a sentence's start or right after a mark ("No prior experience is
     required", "Python preferred, no degree required"), so inside a clause it denies nothing ("Candidates with no
     degree are required to ...", "Applicants holding no degree are required to ..."); and no denial reaches past a
     mark or a word that turns the sentence (but, however, yet, ...). "without" denies nothing, and nor does a
     clause between ("Applicants who have not yet graduated are required to ..."). Where a denial's span is taken out
     a mark stays (R12: DENIED), so the words on either side never join into a new phrase;
   - a negation left in a sentence after that is one the rules do not read ("Python is not, however, preferred.",
     "Neither SQL nor Go is preferred.", "Docker 并不算加分项"): how firmly the sentence asks is unclear, so it is never
     read as soft (UNREAD_DENIAL). One right after "should", "ideally" or "preferably" denies what is asked, not how
     firmly ("You should not have graduated before 2027."), and is read (SOFT_NOT).
   R12 (Codex's review of R11): R11 still read a sentence as soft wherever a soft word stood in it, so a soft word on
   one part demoted a firm hard condition: "Bachelor's degree in Computer Science, Master's preferred." (only "Master's"
   is preferred), "Valid work authorization for your preferred office location." ("preferred" is on the office
   location). A sentence now only expects when it asks softly for the whole of what it asks (_wholly_soft). It is
   parted into clauses at commas, colons, brackets, dashes set off by spaces (a long dash is read as one) and the words
   that turn it (CLAUSE_MARK), and each clause is read on its own (_clause_kind):
   - denied: a requirement said not to hold stood in it ("A degree is not required", "No degree required");
   - waived: an expectation said not to hold stood in it ("Python is not preferred"); it asks nothing;
   - under a condition ("If you live in New York", 如果你在纽约); it asks nothing of its own;
   - bare: a soft word and nothing else ("Ideally", "preferably", "(preferred)", "Bonus", "Nice to have", "Preferred
     qualifications", 可选, 优先);
   - soft: what it asks, asked softly as a whole, in one of three fixed places:
     - led by a soft way of asking ("Ideally you have ...", "We prefer ...", "It would be great if ...", "The ideal
       candidate ...", 最好是…, 希望你…; the Chinese only before a verb or a way of addressing the candidate, never
       before a noun they make, 建议书; "Preferred" and "Desired" only as a label, since they may begin a name,
       "Preferred Provider Organization");
     - "should" after its subject ("You should have Go experience", "This should be your final internship");
     - a soft predicate at its end, on all it names ("SQL is preferred", "GPA 3.0 or above preferred", "Experience
       with Go is a plus", "Docker would be nice to have", "有 SQL 经验者优先");
     neither the subject of "should" nor that of a soft predicate runs past "that", "which", "who", "where", "when"
     (RELATIVE: "Work authorization for the office where SQL is preferred" is on the office; a "that" that begins the
     clause points back, "that's a plus") or joins two things (JOINED: "Bachelor's degree in Computer Science and
     Master's preferred" may be on both or on the last; "or above", "or related" and the like join nothing); and a
     soft word with no "is" before it is not on its clause right after "the", "a", "your", ... and one word, where it
     may be a verb with a subject of its own (OWN_SUBJECT: "Valid work authorization for the location the candidate
     preferred");
   - stated: anything else that names something, a soft word inside it or not.
   The sentence only expects when the clauses before its first colon are all soft, bare or denied (a label:
   "Preferred: ...", "Not required but preferred: Go"), when its first clause is bare (a sentence adverb: "Ideally,
   ..."), or when every clause that names something is soft or denied, a bare clause making soft the one beside it
   (the one before at the end, "SQL, preferably.", "Python (preferred)."; else the one after). So a soft clause beside a
   stated one, a soft word inside a clause, a list parted by commas ("Experience with Python, SQL, or Tableau is a
   plus."), a soft word on one part ("Bachelor's degree, preferably in Computer Science", "Python (preferably 3.10 or
   later)", "Bachelor's degree (Computer Science preferred)") and a soft word in another sense ("Optional Practical
   Training", "an annual bonus") all read as stated, and keep the parse's group. The soft words are R10's and R11's,
   in fewer places, and the denials R11's: R12 reads no sentence as soft that R11 did not. The cost falls the other
   way: a soft sentence in a form these miss keeps its group (R11's: "Candidates who have not yet graduated should
   submit a transcript.", "There is no degree required."; and now a list, parted by commas or joined ("Experience with
   Python and SQL is a plus."), a "should" or a soft predicate past a relative word ("Candidates who live in New York
   should ...", "Candidates who have Go experience are preferred."), and a soft word with no "is" right after "a" and
   one word ("An MBA preferred."). Nothing is ever moved to a firmer group.

It imports nothing of the analysis, so the pages and the rules may use it freely.
"""
import re
import unicodedata
from dataclasses import dataclass

ALL = "all"
ANY = "any"
# R5: something is listed, but not plainly how many of it are asked for, or not plainly what.
UNCLEAR = "unclear"

NON_ASCII = re.compile(r"[^\x00-\x7f]")

# Where a list may start. A lead-in of examples (such as, including, 例如, ...) makes any one item enough, unless the
# words say all of them are.
LEAD_IN = re.compile(
    r"\b(?:with|in|of|using|such as|including|like|e\.g\.?|for example|one of|any of|either|both)\b\s*:?\s*"
    # R7: not the colon of an address ("https://").
    r"|[:：](?!//)\s*"
    r"|(?:熟悉|掌握|使用|了解|具备|包括|例如|比如|如(?![果何需遇]))\s*",
    re.IGNORECASE,
)
EXAMPLES = re.compile(r"^(?:such as|including|like|e\.g\.?|for example|one of|any of|either|例如|比如|如)", re.IGNORECASE)
ALL_OF = re.compile(r"\b(?:all of|each of|every one of|both)\b|全部|所有|每项|每一项", re.IGNORECASE)
ANY_OF = re.compile(r"\b(?:one of|any of|either|at least one|any one)\b|之一|任一|任何一|其中一", re.IGNORECASE)
REQUIRED = re.compile(r"\b(?:required|requires|must|mandatory|necessary)\b|必须|要求", re.IGNORECASE)
# An open end, which asks for nothing in particular; "or ..." and "之一" make any one item enough. A bound ("2027 or
# later", "3.0 or above") is no item either, and leaves the mode as it is.
TAIL = re.compile(
    r"[,，、]?\s*(?:\b(?P<word>and|or)\s+(?:an?\s+|the\s+)?(?:other|similar|related|equivalent)\b.*"
    r"|\bor (?:later|above|higher|more|newer|earlier)\b.*"
    r"|\betc\b\.?"
    r"|\band (?:more|so on)\b.*"
    r"|(?P<one>(?:其中)?之一|中的一种|任一)"
    r"|等+\S{0,8})$",
    re.IGNORECASE,
)
# R7: "plus" and " + " add an item ("a plus" does not: "Docker is a plus"); " / " is a slash between two words that
# are not one term (see SLASH), which says neither "and" nor "or".
ENGLISH_SEPARATOR = re.compile(
    r"\s*(?:[,;]\s*(?:and/or\b|and\b|or\b|&|as well as\b|plus\b)?"
    r"|\s(?:and/or|and|or|&|\+|/|as well as)\s"
    r"|(?<!\ba)\splus\s)\s*",
    re.IGNORECASE,
)
CHINESE_JOIN = re.compile(r"以及|和|及(?![以其时])|(?<![参给赋])与|或者|或|\s/\s")
# Anything that may part items, to tell a single item from a list that does not part plainly.
LISTED = re.compile(
    r"[,;、]|\s(?:and/or|and|or|&|\+|/|as well as)\s|(?<!\ba)\splus\s|以及|和|及(?![以其时])|(?<![参给赋])与|或者|或",
    re.IGNORECASE,
)
OR_WORDS = {"or", "and/or", "或", "或者"}
AND_WORDS = {"and", "&", "+", "plus", "as well as", "以及", "和", "及", "与"}
SLASHED = "/"
MAX_WORDS = 4
MAX_CHARS = 40
MAX_CHINESE = 12

STOP_WORDS = {"a", "an", "the", "of", "and", "or", "in", "with", "for", "to", "on", "at", "by"}
TOKEN = re.compile(r"[a-z0-9]+[+#]*")

# R6. A full-width comma or semicolon: between short terms it parts items, otherwise it ends a clause.
CLAUSE = re.compile(r"[，；]")
DIGIT = re.compile(r"\d")
# A clause that is only a year bound, who may apply, names no item ("2027 年及以后毕业", "2026 届毕业生").
YEAR_BOUND = re.compile(
    r"\d{4}\s*(?:年|届)(?:\s*\d{1,2}\s*月)?\s*(?:及以后|及以前|及之后|及之前|以后|以前|之后|之前)?\s*(?:毕业|入学)?"
    r"\s*(?:的)?\s*(?:学生|毕业生|生|候选人)?"
)
# Without a lead-in, an item is plain when none of these stand in it: "Ability to communicate" is no plain item.
FUNCTION_WORDS = STOP_WORDS - {"and", "or"}
# Before the first item, without a lead-in: a word starting low (or a Chinese one) before the item's names is no
# part of it ("Must know Python" -> "Python", "需要 Python" -> "Python").
NAME_WORD = re.compile(r"[A-Z0-9.][\x21-\x7e]*")
HEAD_WORD = re.compile(r"[a-z]|[^\x00-\x7f]")
# Words that describe one thing (R6): a comma or "and" between two of them parts no items ("clean, effective code",
# "written and verbal communication skills"). A fixed set: a word not in it is never taken for one ("dashboards, data
# pipelines" stays a list), so what it misses reads as a list or as unclear, never as one item.
DESCRIBING_WORDS = frozenset(
    {
        "accurate", "clean", "clear", "collaborative", "concise", "consistent", "correct", "creative", "documented",
        "dynamic", "effective", "efficient", "elegant", "excellent", "fast", "fast-paced", "flexible", "good", "great",
        "high-quality", "independent", "innovative", "intuitive", "maintainable", "modern", "modular", "oral",
        "organized", "performant", "proactive", "production-ready", "readable", "reliable", "resilient", "reusable",
        "rigorous", "robust", "scalable", "secure", "simple", "solid", "stable", "strong", "testable", "tested",
        "thorough", "timely", "user-friendly", "verbal", "well-documented", "well-tested", "written",
    }
)
_DESCRIBING = "|".join(sorted(map(re.escape, DESCRIBING_WORDS), key=len, reverse=True))
DESCRIBED = re.compile(rf"\b({_DESCRIBING})(?:\s*,\s*(?:and\s+)?|\s+and\s+)(?=(?:{_DESCRIBING})\b)", re.IGNORECASE)

# R7. A slash between two words. It joins two items ("Python/SQL", "CI/CD": " / ") unless it stands inside one term:
# two single letters ("A/B testing", "I/O"), a number on either side ("24/7") or "and/or".
SLASH = re.compile(r"(?<=\S)\s*[/／]\s*(?=\S)")
# R7. The words before a lead-in (other than one of examples), or before the first item's names without one, are
# either only the wording of a lead-in, and are left out, or a requirement in their own right, which stays an item:
# "machine learning with Python and SQL" asks for machine learning too. A fixed set of lead-in words; any other word
# is a requirement, so what the set misses only ever asks for more, never for less.
GENERIC_WORDS = frozenset(
    {
        "experience", "experiences", "experienced", "knowledge", "knowledgeable", "familiarity", "familiar",
        "proficiency", "proficient", "expertise", "expert", "skill", "skills", "skilled", "ability", "abilities",
        "able", "capability", "capabilities", "capable", "understanding", "understand", "background", "exposure",
        "competence", "competency", "competencies", "competent", "fluency", "fluent", "mastery", "command",
        "comfort", "comfortable", "hands-on", "work", "working", "know", "knows", "use", "uses", "interest",
        "interested", "passion", "passionate", "coursework", "training", "trained", "education", "study", "studies",
        "years", "year", "months", "month", "prior", "previous", "relevant", "professional", "demonstrated",
        "demonstrable", "proven", "deep", "broad", "extensive", "advanced", "basic", "intermediate", "some",
        "general", "sound", "practical", "required", "requires", "require", "preferred", "desired", "desirable",
        "must", "should", "ideally", "preferably", "have", "has", "having", "possess", "tools", "tool",
        "technologies", "technology",
        # Who is asked, and how: "You will work with ...", "Candidates should ...".
        "i", "we", "you", "they", "your", "our", "candidate", "candidates", "applicant", "applicants", "will", "would",
        "can", "may", "shall", "be", "is", "are",
        # The word before "with" in "along with", "together with", ...: what follows joins, it is no item.
        "via", "through", "together", "combined", "coupled", "along", "alongside", "paired",
        # R8: a lead-in made of a noun and "to" ("Attention to detail", "Willingness to travel"), what picks out
        # ("all of these"), and the doing of the work ("Experience coding in ...").
        "attention", "commitment", "dedication", "willingness", "eagerness", "desire", "drive", "motivation",
        "aptitude", "curiosity", "enthusiasm", "openness", "readiness", "all", "these", "those", "each", "every",
        "following", "coding",
    }
)
# Before a colon stands a label ("Requirements:", "岗位要求：", "工作地点："): its words are wording too, there only.
LABEL_WORDS = frozenset(
    {
        "requirements", "requirement", "qualifications", "qualification", "responsibilities", "responsibility",
        "duties", "nice", "nice-to-have", "must-have", "must-haves", "bonus", "plus", "tech", "stack", "location",
        "schedule", "role", "summary", "about", "details", "note", "notes", "minimum", "basic", "additional",
    }
)
LABEL_CHINESE = (
    "岗位", "职位", "任职", "入职", "到岗", "招聘", "要求", "职责", "说明", "内容", "地点", "时间", "方式", "条件",
    "资格", "加分项", "加分", "福利", "待遇", "技术栈",
)
# Neither a word nor a number: a list mark or stray punctuation ("- 掌握 SQL").
NO_WORD = re.compile(r"[^\w一-鿿]+")
NUMBER = re.compile(r"\d+(?:\.\d+)?\+?|\d+-\d+|\+")
GENERIC_CHINESE = tuple(
    sorted(
        (
            "熟练", "精通", "能够", "可以", "必须", "需要", "要求", "具有", "拥有", "良好的", "良好", "扎实的", "扎实",
            "较强的", "较强", "较好的", "较好", "优秀的", "优秀", "出色的", "出色", "一定的", "一定", "相关的", "相关",
            "基本的", "基本", "基础", "深入的", "深入", "丰富的", "丰富", "充分的", "充分", "工作", "经验", "能力",
            "知识", "技能", "背景", "优先", "以上", "左右", "熟悉", "了解", "掌握", "使用", "具备",
            "我们", "你们", "候选人", "应聘者", "申请人", "我", "你", "您",
        ),
        key=len,
        reverse=True,
    )
)
GENERIC_CHINESE_ALONE = {"会", "能", "有", "要", "需", "可"}
CHINESE_NUMBER = re.compile(r"\d+\s*(?:\+|年|个月)?")
# R7. Before a lead-in of examples stands what they are examples of when its last word names a kind of thing
# ("programming languages such as Java, C++"): left out, as before. Otherwise it asks for something in its own right
# ("Data analysis, including SQL and Excel"). A fixed set: any other last word is such a requirement.
CATEGORY_WORDS = frozenset(
    {
        "language", "languages", "framework", "frameworks", "tool", "tools", "toolkit", "toolkits", "technology",
        "technologies", "platform", "platforms", "library", "libraries", "database", "databases", "system", "systems",
        "service", "services", "package", "packages", "software", "application", "applications", "app", "apps",
        "environment", "environments", "stack", "stacks", "editor", "editors", "ide", "ides", "method", "methods",
        "methodology", "methodologies", "technique", "techniques", "model", "models", "algorithm", "algorithms",
        "protocol", "protocols", "format", "formats", "api", "apis", "provider", "providers", "solution", "solutions",
        "product", "products", "suite", "suites", "engine", "engines", "utility", "utilities", "resource",
        "resources", "skill", "skills", "area", "areas", "field", "fields", "domain", "domains", "topic", "topics",
        "concept", "concepts",
    }
)
CATEGORY_CHINESE = ("语言", "框架", "工具", "技术", "平台", "库", "数据库", "系统", "软件", "方法", "领域", "技能", "模型", "算法", "服务", "产品")

# R8. listing() says "one item" (None) only for words that plainly ask for one thing: once the wording of a lead-in,
# a label before a colon, a preferred or required end ("is a plus") and an open end are left out, what is left is one
# phrase of at most SINGLE_WORDS words with no word that may join two asks (LINKING_WORDS, LINKING_CHINESE) and at most
# one run of names. Words read neither as one item nor as a list are unread (UNREAD): a passage of practice alone never
# supports them ("Python for data analysis", "Python alongside SQL", "使用 Python 进行数据分析"). The sets are fixed: a
# joining word they miss between two lower-case words would still read as one item; between two names it would not.
# R9: one item of two or more parts is supported only by a passage naming all of them (reading), whatever the sets miss.
UNREAD = "unread"
SINGLE_WORDS = 6
SINGLE_CHARS = 60
LINKING_WORDS = frozenset(
    {
        "of", "in", "with", "for", "to", "on", "at", "by", "from", "into", "onto", "within", "across", "through",
        "throughout", "via", "alongside", "besides", "beside", "except", "against", "over", "under", "per", "versus",
        "vs", "than", "as", "without", "toward", "towards", "around", "between", "among", "amongst", "during", "after",
        "before", "upon", "about", "beyond", "until", "since", "despite", "while", "when", "where", "which", "that",
        "who", "whom", "whose", "whether", "if", "unless", "but", "so", "then", "plus", "along", "together",
        "including", "like", "such", "using", "both", "either", "neither", "nor", "also", "combined", "coupled",
        "paired", "following", "regarding", "concerning", "involving", "inside", "outside", "behind", "near",
        "instead", "rather", "and", "or", "&", "+", "/",
    }
)
LINKING_CHINESE = (
    "进行", "用于", "用来", "用以", "以便", "对于", "为了", "通过", "基于", "结合", "配合", "并且", "从而", "进而", "以及",
    "使用", "利用", "借助", "依托", "运用", "来做", "去做",
)
# A Chinese phrase with a name: a short word with it is one thing ("Python 编程", "SQL 查询"); more may ask for more.
NAME_WITH_CHINESE = 2
PREFERRED_END = re.compile(
    r"[\s,，]*(?:(?:is|are|would be|will be)\s+)?"
    r"(?:(?:a|an)\s+(?:big\s+|huge\s+|strong\s+|nice\s+|definite\s+|major\s+|real\s+)?(?:plus|bonus|advantage)"
    r"|preferred|desired|desirable|highly desirable|required|a must|nice to have|optional)"
    r"(?:\s+(?:for|in)\s+(?:this|the)\s+(?:role|position|job|team))?\s*$"
    r"|(?:者)?优先(?:考虑)?$|(?:为|是)?加分项?$",
    re.IGNORECASE,
)
LATIN_WORD = re.compile(r"[\x21-\x7e]+")

# R9. The words as judged, never as kept or shown: full-width forms read as plain ones (NFKC), and marks typed either
# way read as their plain marks (curly quotes and apostrophes, dashes and hyphens, odd spaces); a trademark sign or an
# emoji is no part of the words. Only Han characters are Chinese: R8 took any character outside ASCII for Chinese, so
# a curly quote, a dash or an apostrophe sent English words past the English checks ("“Python for data analysis”",
# "Python for client’s data analysis"). A mark left after that (an arrow, a bullet, a middle dot) may join two asks,
# and a letter of another script is not read: the words are unread, never one item.
_HAN = "㐀-䶿一-鿿豈-﫿\U00020000-\U0002ebef"
CJK = re.compile(f"[{_HAN}]")
HAN_RUN = re.compile(f"[{_HAN}]+")
NOT_HAN = re.compile(f"[^\\s{_HAN}]+")
APOSTROPHES = "‘’‚‛′ʼ"
# What is left at either end of the words once the wording is out: punctuation and list marks.
EDGE = " ,，、;；。.-*•·"
# R9. Words a passage need not name to show a requirement read as one item: wording, and words that describe it.
NOT_ASKED = STOP_WORDS | GENERIC_WORDS | DESCRIBING_WORDS

CONDITION_BEFORE = re.compile(
    r"^(?P<condition>(?:only )?(?:if|unless|provided(?: that)?|in case|in the event(?: that)?)\b[^,;:]*?)\s*[,;:]",
    re.IGNORECASE,
)
CONDITION_AFTER = re.compile(r"[\s,](?P<condition>(?:only )?(?:if|unless|provided(?: that)?)\b[^.;:]*?)[.;。]?$", re.IGNORECASE)
CONDITION_CHINESE = re.compile(r"^(?P<condition>(?:如果|假如|倘若|若|如遇|如需)[^，,；;。]*)[，,]")

# The one form of condition that may be held met or not met (R5): a plain, positive one on the work mode.
WORK_MODE_CONDITION = re.compile(
    r"^(?:only )?if (?:the |this )?(?:job )?(?:posting|role|position|job|opening)\s+(?:is|are)\s+"
    r"(?:(?:specified|listed|marked|designated|advertised|posted|classified)\s+as\s+)?(?:an?\s+)?"
    r"(?P<mode>on-?site|on site|remote|hybrid)(?:\s+(?:role|position|job|posting|opening))?$",
    re.IGNORECASE,
)
WORK_MODE_CONDITION_CHINESE = re.compile(
    r"^(?:如果|若|假如|倘若)(?:该|这个|本)?(?:岗位|职位|工作)(?:是|为|标为|标注为|写明为|注明为)"
    r"(?P<mode>现场办公|现场|驻场|坐班|远程办公|远程|混合办公|混合)(?:办公|工作)?$"
)
MODE_WORDS = {
    "onsite": "onsite", "on-site": "onsite", "on site": "onsite", "remote": "remote", "hybrid": "hybrid",
    "现场办公": "onsite", "现场": "onsite", "驻场": "onsite", "坐班": "onsite",
    "远程办公": "remote", "远程": "remote", "混合办公": "hybrid", "混合": "hybrid",
}
# The line a source adapter writes in a posting's head, with the source's own value, exactly (app.discovery.lever).
WORK_MODE_LINE = re.compile(r"工作方式：(?P<value>onsite|remote|hybrid)")
WORK_MODE_LABEL = "工作方式："

MET = "met"
NOT_MET = "not_met"
UNKNOWN = "unknown"

# R10 (U3-D6-3 F1): how firmly a posting's own words ask (strength; module docstring, part 3).
OBLIGED = "obliged"
EXPECTED = "expected"
STATED = "stated"
# R11: "need" requires on its own too ("You need a degree, ideally in Computer Science."), not only as "need to".
OBLIGES = re.compile(
    r"\b(?:must|required|requires|require|requirement|mandatory|compulsory|obligatory|essential|necessary|needed"
    r"|needs?|have\s+to|has\s+to|having\s+to|is\s+a\s+must)\b"
    r"|必须|务必|必需|须|需要|要求|一定要",
    re.IGNORECASE,
)
# R11 (U3-D6-3 R10 review, F1): how far a negation reaches, in plain forms only, never past a mark (parted by spaces
# only) or a word that turns the sentence ("not preferred but required" denies "preferred", never "required"):
# - "not", "n't", "never" and the turns of phrase that say the same plainly ("no longer", "by no means", "in no way")
#   deny the word right after them, or after up to three adverbs in -ly (not "only"), "a"/"an" or "be"/"been" ("not
#   required", "isn't strictly required", "will not be required", "not a must") (_DENY); a clause between them and the
#   word ("Applicants who have not yet graduated are required to ...") is no denial of it;
# - "no" denies what stands within three words of it only where it begins its clause: at the start of a sentence or
#   right after a mark ("No degree required", "No prior experience is required", "Python preferred, no degree
#   required"). Inside a clause it names a kind of applicant or a thing, and denies nothing ("Candidates with no degree
#   are required to ...", "Applicants holding no degree are required to ..."). A word it reaches over starts with a
#   letter or a digit: a dash is a mark, no word.
# Only a denial of these forms makes a sentence soft (NOT_OBLIGED); each is narrower than R10's (any of not, no, never,
# without, three words of any kind before the word, across marks). Where a denial only keeps a sentence from reading as
# soft ("anything but optional", "hardly a plus"; NOT_OPTIONAL, NOT_EXPECTED, UNREAD_DENIAL), "cannot", "hardly",
# "barely", "scarcely", "far from" and "anything but" deny too (_NOT). "without" denies nothing here. What these miss
# is read as requiring or stated, so its group stays as the parse put it.
_TURN = r"(?:but|however|yet|though|although|instead|rather|whereas|while|except|only)"
_DENY = r"(?:\b(?:not|never)\b|n't|\bno\s+longer\b|\bby\s+no\s+means\b|\bin\s+no\s+way\b)"
_NOT = rf"(?:{_DENY}|\b(?:cannot|hardly|barely|scarcely)\b|\bfar\s+from\b|\banything\s+but\b)"
_NOT_REACH = r"(?:\s+(?:(?!only\b)[a-z]+ly|an?|be|been)){0,3}\s+"
_NO = r"(?:^|(?<=[.!?;:,(\[\-。；：，！？、]))[^\w]*?\bno\b"
_NO_REACH = rf"(?:\s+(?!{_TURN}\b)[\w'’][\w'’-]*){{0,3}}?\s+"
_REQUIRED_WORD = r"(?:required|requires?|necessary|mandatory|needed|needs?|compulsory|essential|a\s+must)\b"
# A requirement said not to hold: its span is taken out before OBLIGES is looked for ("No degree required").
NOT_OBLIGED = re.compile(
    rf"{_DENY}{_NOT_REACH}{_REQUIRED_WORD}|{_NO}{_NO_REACH}{_REQUIRED_WORD}"
    r"|\bno\s+need\b|\bneed\s+not\b"
    r"|不要求|不需要|无需|无须|不必|不用|非必须|不是必须|不一定要",
    re.IGNORECASE,
)
# R11: what is said not to be optional is required ("Valid work authorization is not optional.", "by no means
# optional").
NOT_OPTIONAL = re.compile(
    rf"{_NOT}{_NOT_REACH}optional\b|{_NO}{_NO_REACH}optional\b|\bnon[\s-]?optional\b|不是可选|并非可选|不可选|非可选",
    re.IGNORECASE,
)
# R11: an expectation said not to hold expects nothing ("SQL is not preferred", "Go is not a plus"): its span is taken
# out (R12: marked WAIVED) before the clauses are read, so the clause it stood in asks nothing, never softly.
NOT_EXPECTED = re.compile(
    rf"{_NOT}{_NOT_REACH}(?:optional|preferred|preferable|desired|desirable|ideal|ideally|encouraged|advantageous"
    r"|nice[\s-]+to[\s-]+have|an?\s+(?:big\s+|huge\s+|strong\s+|nice\s+|definite\s+|major\s+|real\s+|added\s+)?"
    r"(?:plus|bonus|advantage|asset))\b"
    r"|不是加分项?|并非加分项?|不是优先|不是可选|并非可选|不可选|非可选",
    re.IGNORECASE,
)
# R12 (Codex's review of R11): a soft word makes a sentence soft only where it is on the whole of what the sentence
# asks (_wholly_soft; module docstring, part 3). Its words are R10's and R11's, in fixed places only.
# Where a requirement said not to hold (NOT_OBLIGED) or an expectation said not to hold (NOT_EXPECTED) was taken out:
# marks no pattern reads across, which tell the clause they stand in from any other.
DENIED = " ⟂ "
WAIVED = " ⊘ "
# Where a sentence parts into clauses: a comma, a colon (when no label leads), brackets, a dash set off by spaces (an
# em dash, or an en dash set off by spaces, is read as one: strength) and the words that turn a sentence.
CLAUSE_MARK = re.compile(r"[,:()\[\]]|\s-\s|\b(?:but|however|although|though|whereas|except)\b", re.IGNORECASE)
_SOFT_ADVERB = r"(?:highly|strongly|very|much|definitely|especially|really|greatly|also)"
_PLUS = (
    r"(?:a\s+(?:(?:big|huge|strong|nice|definite|major|real|added)\s+)?(?:plus|bonus|advantage|asset)"
    r"|an\s+(?:added\s+)?(?:advantage|asset))"
)
# A clause that is a soft word and nothing else ("Ideally", "preferably", "(preferred)", "(highly preferred)",
# "Bonus", "Nice to have", "Preferred qualifications", "if possible", 可选, 优先, 加分项): it is on a clause beside it.
BARE = re.compile(
    rf"^\W*(?:{_SOFT_ADVERB}\s+)?(?:preferred|desired|desirable|optional|ideally|ideal|preferably|hopefully"
    rf"|advantageous|bonus(?:\s+points?)?|{_PLUS}|a\s+nice[\s-]+to[\s-]+have|nice[\s-]+to[\s-]+have|if\s+possible"
    r"|where\s+possible|(?:preferred|desired|bonus|nice[\s-]+to[\s-]+have)\s+(?:qualifications?|skills?|experience"
    r"|requirements?|criteria)|可选|优先|加分项?|优先考虑|最好)\W*$",
    re.IGNORECASE,
)
# A clause under a condition ("If you live in New York", 如果你在纽约): it asks nothing of its own.
CONDITION = re.compile(
    r"^\W*(?:(?:if|when|where|while|once|unless|whether|provided)\b|in\s+case\b|as\s+long\s+as\b|如果|若|假如)",
    re.IGNORECASE,
)
# What a clause asks, asked softly as a whole, in three fixed places. At its start, a soft way of asking ("Ideally you
# have ...", "We prefer ...", "It would be great if ...", "The ideal candidate ...", "Nice to have Docker", "Bonus points
# for Go", "A plus if ..."); "Preferred", "Desired" with no colon after may begin a name ("Preferred Provider
# Organization"), so only as a label ("Preferred: ...", BARE). In Chinese, 最好, 希望, 建议, 欢迎 right before a verb
# or a way of addressing the candidate (最好是…, 希望你…, 建议具备…, 欢迎有…), never before a noun they make
# (建议书 "proposal", 最好的 "the best").
SOFT_LEAD = re.compile(
    r"^\W*(?:(?:ideally|preferably|hopefully|nice[\s-]+to[\s-]+have"
    r"|(?:we|i)(?:'d|\s+would)\s+(?:prefer|love)|we\s+(?:also\s+|strongly\s+|highly\s+)?prefer"
    r"|it\s+would\s+be\s+(?:great|nice|good|helpful|beneficial|ideal)|(?:the|our|an?)\s+ideal\s+candidates?"
    r"|ideal\s+candidates?|bonus\s+(?:points?\s+(?:for|if)|if)|a\s+plus\s+if|if\s+possible|where\s+possible)\b"
    r"|(?:最好|希望|建议|欢迎)\s*(?:是|有|能|会|可以|具备|熟悉|掌握|拥有|了解|懂|到|在|你|您|候选人|应聘者|同学|加入|申请|投递))",
    re.IGNORECASE,
)
# After its subject, "should" (or "ideally", "preferably", "is/are encouraged to"): "You should have Go experience",
# "This should be your final internship". The subject is up to six words and never runs past "that", "which", "who",
# "where", "when", "and", "or" or "plus" (a "should" there may be in a clause of its own, or on one of two things);
# 应该 likewise.
SOFT_MODAL = re.compile(
    r"^\W*(?:that\s+)?(?:(?!(?:that|which|who|whom|whose|where|when|and|or|plus)\b)[\w'’.+#/-]+\s+){0,6}?"
    r"(?:should|ideally|preferably|would\s+ideally|(?:is|are)\s+encouraged\s+to)\b"
    r"|^\W*[^\s,]{0,12}应该",
    re.IGNORECASE,
)
# At its end, a soft predicate on all it names ("SQL is preferred", "Knowledge of SQL is strongly preferred",
# "Experience with Go is a plus for this role", "Docker would be nice to have", "GPA 3.0 or above preferred", "Go
# experience a plus", "有 SQL 经验者优先", "Docker 是加分项"); never "preferred" after "you", "they" or "we".
SOFT_TAIL = re.compile(
    rf"(?:\b(?:is|are|be|being|would\s+be|will\s+be)\s+(?:(?:considered|seen\s+as|regarded\s+as)\s+)?"
    rf"(?:{_SOFT_ADVERB}\s+)?(?:preferred|desired|desirable|optional|advantageous|encouraged"
    rf"|a\s+nice[\s-]+to[\s-]+have|nice[\s-]+to[\s-]+have|{_PLUS})(?:\s+(?:for|in|on|at|with|if|when|to)\b[^,;:()]*)?"
    rf"|\bwould\s+be\s+(?:{_SOFT_ADVERB}\s+)?(?:great|nice|good|helpful|beneficial|ideal)"
    rf"(?:\s+(?:for|in|on|at|with|if|when|to)\b[^,;:()]*)?"
    rf"|(?<!\byou\s)(?<!\bthey\s)(?<!\bwe\s)(?<![\w'’])(?:{_SOFT_ADVERB}\s+)?(?:preferred|desired|desirable|optional"
    rf"|nice[\s-]+to[\s-]+have|a\s+(?:(?:big|huge|strong|nice|definite|major|real|added)\s+)?plus)"
    r"|者?优先(?:考虑)?|加分|是加分项)\W*$",
    re.IGNORECASE,
)
# Two things joined in what a soft predicate, or a bare clause beside, is on ("Bachelor's degree in Computer Science
# and Master's preferred"): it may be on both or on the last, so it is on neither. "or above", "or later", "or related"
# and the like join no two things ("GPA 3.0 or above preferred").
JOINED = re.compile(
    r"\b(?:and|or|plus)\b(?!\s+(?:above|higher|better|more|later|newer|greater|equivalent|similar|related)\b)|&",
    re.IGNORECASE,
)
# A soft predicate is never on what stands before "that", "which", "who", "where", "when" in its clause: a clause of
# its own may hold it ("Work authorization for the office where SQL is preferred"). A "that" that begins the clause
# points at what came before ("If you have a degree in CS, that's a plus") and is none of these.
RELATIVE = re.compile(r"\b(?:that|which|who|whom|whose|where|when)\b", re.IGNORECASE)
POINTING = re.compile(r"^\W*that\b", re.IGNORECASE)
# A soft word at a clause's end with no "is" before it, right after "the", "a", "your", ... and one word, may be a verb
# with a subject of its own ("Valid work authorization for the location the candidate preferred"): not on the clause.
OWN_SUBJECT = re.compile(r"\b(?:the|a|an|your|their|our|his|her|its|this|that)\s+[\w'’-]+\s*$", re.IGNORECASE)
COPULA = re.compile(r"\W*(?:is|are|be|being|would\s+be|will\s+be)\b", re.IGNORECASE)
# Where a sentence of a quote ends: a full stop that is no decimal point, or ; ! ? and their full-width forms.
SENTENCE_END = re.compile(r"(?<!\d)\.(?!\d)|[;!?。；！？\n]")
# R11: what stands where a denial's span was taken out of the parse's own words (obliges): a mark no pattern reads
# across. In a posting's sentence the marks are DENIED and WAIVED (R12).
CUT = " | "
# R11: a negation still in a sentence once the spans above are taken out is one the rules do not read ("Python is not,
# however, preferred.", "Neither SQL nor Go is preferred.", "Docker 并不算加分项"): how firmly the sentence asks is
# unclear, so it is never read as soft, and keeps the group the parse put it in. In Chinese, only a negation right
# before 可选, 加分, 优先, 择优 or 欢迎 is one.
UNREAD_DENIAL = re.compile(
    rf"{_NOT}|\b(?:neither|nor)\b|[不没非未无绝并](?:是|算|太|再|属于)?(?:可选|加分|优先|择优|欢迎)",
    re.IGNORECASE,
)
# R11: a negation right after "should", "ideally" or "preferably" denies what is asked, not how firmly ("You should not
# have graduated before 2027." still only expects): it is read, never left unread.
SOFT_NOT = re.compile(r"\b(should|ideally|preferably)(?:\s+(?:not|never)\b|n't)", re.IGNORECASE)
# An em dash, or an en dash set off by spaces, parts clauses as a dash set off by spaces does (an en dash between years
# does not: "2026–2027").
LONG_DASH = re.compile(r"\s*[—―]\s*|\s+–\s+")
# R10: the parse's own words without the firmness they added (without_obligation), in fixed forms only.
LEADING_OBLIGATION = re.compile(
    r"^\s*(?:(?:the|a|an)\s+)?(?:candidates?\s+|applicants?\s+|students?\s+|you\s+|they\s+)?"
    r"(?:must|is\s+required\s+to|are\s+required\s+to|required\s+to|needs?\s+to|have\s+to|has\s+to|will\s+need\s+to)\s+"
    r"(?:(?:have|be|know|possess|hold|demonstrate)\s+)?",
    re.IGNORECASE,
)
REQUIRED_LABEL = re.compile(r"^\s*(?:required|mandatory|must[\s-]+have|requirement)\s*[:：-]\s*", re.IGNORECASE)
TRAILING_OBLIGATION = re.compile(
    r"[\s,，(（-]*(?:(?:is|are)\s+)?(?:required|mandatory|a\s+must|necessary|essential|needed|not\s+optional)[)）]?[\s.。]*$",
    re.IGNORECASE,
)
CHINESE_OBLIGATION = re.compile(r"^\s*(?:必须|务必|须|需要|要求|一定要)\s*(?:具备|拥有|掌握|持有|有|是)?\s*")
ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class Listing:
    # ALL, ANY or UNCLEAR; an unclear listing may have no items it could tell apart. UNREAD (R8), never with items:
    # read neither as one item nor as a list.
    mode: str
    items: tuple[str, ...]


def listing(words: str) -> Listing | None:
    """The items a requirement's own words list, and whether all or any one of them is asked for (or that it is
    unclear). None only when its words plainly ask for one thing (R8, _single); words read neither as that nor as a
    list are UNREAD, never taken for one item. The last lead-in that starts a list wins. Before a lead-in of examples
    stands what they are examples of: "programming languages, such as Java, C++, Python" lists the languages, not
    "programming languages". Before any other lead-in stands either the wording of a lead-in ("Experience with Python
    and SQL") or requirements in their own right, which stay items (R7, R8): "machine learning with Python and SQL"
    lists machine learning, Python and SQL; "database administration with automation using Python and SQL" lists
    database administration and automation too."""
    return reading(words)[0]


def reading(words: str) -> tuple[Listing | None, tuple[str, ...]]:
    """listing(), and for words read as one item what a passage of practice must name to show it (R9, _asks): its
    words once the wording is left out, when two or more are left ("Python SQL", "Microsoft Power BI", "数据分析报表制
    作"). () when there is nothing to check that way: a list or unread words (their own rules hold), or one item of
    one word or none ("Python", "Relevant experience"), which cannot be shown only in part. More than one ask means any
    one of them will do: the kind of thing asked for or the example of it ("data visualization tools such as Microsoft
    Power BI")."""
    # Describing words joined part no items (R6): "clean, effective code" reads as "clean effective code". Marks typed
    # either way read as their plain marks first (R9, _marks).
    text = _clauses(_slashes(DESCRIBED.sub(r"\1 ", " ".join(_marks(words).split()))).rstrip("。.；;，,！!？? "))

    if text is None:
        # A clause after a full-width comma may ask for more (R6).
        return Listing(UNCLEAR, ()), ()

    for lead in reversed(list(LEAD_IN.finditer(text))):
        rest = text[lead.end():]
        tail = TAIL.search(rest)
        bare = rest[: tail.start()] if tail else rest
        examples = bool(EXAMPLES.match(lead.group().strip()))
        # A label: what stands before a colon, as the colon's lead-in or ending the words before another one.
        label = lead.group().strip() in (":", "：") or text[:lead.start()].rstrip().endswith((":", "："))
        head = text[:lead.start()].rstrip(" ,，、;；:：")

        if not LISTED.search(bare):
            # One item after this lead-in. Words before it that ask for something are items too (R7, R8); otherwise
            # an earlier lead-in may start a list that holds it.
            asked = () if LISTED.search(head) else _asked(head, examples, label)

            if asked == UNREAD:
                # R10: a stretch before the lead-in is a phrase of its own ("Curiosity about connectivity in cloud
                # systems"): neither one item nor a list, and no items made up of it.
                return Listing(UNREAD, ()), ()

            if asked == () and examples and head and not LISTED.search(head):
                # The kind of thing asked for and one example of it ("ML frameworks such as TensorFlow"): one ask
                # (R8), when the example reads plainly; either of the two shows it (R9).
                return (None, _either(head, bare, label)) if _single(bare) else (Listing(UNREAD, ()), ())

            if asked == ():
                continue

            item = _bare(bare.strip(" ,，、;；:："))

            if asked is None or not _fits(item):
                return Listing(UNCLEAR, ()), ()

            # An example after a requirement: whether it is asked for too is not plain.
            return Listing(UNCLEAR if examples else ALL, (*asked, item)), ()

        found = _list(rest, said=text[: lead.end()], examples=examples)

        if found is None or LISTED.search(head):
            # Something is listed, but its items do not part plainly, or it is a list inside another one.
            return Listing(UNCLEAR, ()), ()

        asked = _asked(head, examples, label)

        if asked == UNREAD:
            return Listing(UNREAD, ()), ()

        if asked is None:
            return Listing(UNCLEAR, ()), ()

        return _with_head(asked, found), ()

    return _unled(text)


def _asked(head: str, examples: bool = False, label: bool = False) -> tuple[str, ...] | str | None:
    """What the words before a lead-in ask for in their own right (R7, R8): an item for each stretch between the
    lead-ins among them that is more than the wording of a lead-in or a label before a colon ("database administration
    with automation" -> both; "Experience in machine learning" -> machine learning; "3+ years of professional
    experience" -> none). Before a lead-in of examples, the last stretch is what they are examples of when it names a
    kind of thing, and no item. None when a stretch does not read as one plain item. UNREAD (R10, U3-D6-3 F2) when
    what a stretch leaves is a phrase of its own (_phrased): "Curiosity about connectivity" leaves "about
    connectivity", which is no item of the requirement's, so the words are read neither as one item nor as a list."""
    if not head:
        return ()

    stretches = _stretches(head, label)

    if examples and stretches and _category(stretches[-1][0]):
        stretches = stretches[:-1]

    items = []

    for stretch, is_label in stretches:
        item = _stretch_item(stretch, is_label)

        if item is None:
            return None

        if item and _phrased(item):
            return UNREAD

        if item:
            items.append(item)

    return tuple(items)


def _phrased(item: str) -> bool:
    """Whether what a stretch before a lead-in leaves is a phrase of its own rather than one item (R10, U3-D6-3 F2): a
    linking word stands in it, as judged ("about connectivity", "across teams"; in Chinese one of LINKING_CHINESE).
    _single never takes such words for one item either. The set is fixed: a linking word it misses leaves the stretch
    an item, as before R10."""
    judged = _judging(item)

    if CJK.search(judged):
        return any(word in judged for word in LINKING_CHINESE)

    return any(word.casefold().strip(".,;:()'\"") in LINKING_WORDS for word in judged.split())


def _stretches(head: str, label: bool = False) -> list[tuple[str, bool]]:
    """The stretches of the words before a lead-in between the lead-ins among them, each with whether a colon follows
    it (a label). A lead-in at their very end belongs to what follows ("Experience with" before "one of ...", "machine
    learning in" before "one of ..."). R8: every stretch, not only the last (R7 kept only what followed the last lead-in
    among them, so "database administration with automation" lost database administration)."""
    part = head.strip(" ,，、;；:：")

    while True:
        leads = list(LEAD_IN.finditer(part))

        if not leads or part[leads[-1].end():].strip(" ,，、;；:："):
            break

        part = part[: leads[-1].start()].strip(" ,，、;；:：")

    stretches, start = [], 0

    for lead in LEAD_IN.finditer(part):
        stretches.append((part[start:lead.start()].strip(" ,，、;；:："), lead.group().strip() in (":", "：")))
        start = lead.end()

    stretches.append((part[start:].strip(" ,，、;；:："), label))

    return [(stretch, is_label) for stretch, is_label in stretches if stretch]


def _category(part: str) -> bool:
    if NON_ASCII.search(part):
        return part.endswith(CATEGORY_CHINESE)

    words = part.split()

    return bool(words) and words[-1].casefold().strip(".,;:()'\"") in CATEGORY_WORDS


def _with_head(asked: tuple[str, ...], found: "Listing") -> "Listing":
    """A list and the requirements before it (R7): all of them when all of the list is asked for; otherwise (any one of
    the list, or unclear) only every item named can show both, whichever way it is read."""
    if not asked:
        return found

    return Listing(ALL if found.mode == ALL else UNCLEAR, (*asked, *found.items))


def _single(text: str) -> bool:
    """Whether the words plainly ask for one thing (R8): what _core leaves of them, as judged (R9, _judging), is one
    phrase of at most SINGLE_WORDS words, with no word that may join two asks and at most one run of names; in Chinese,
    no joining word, and with a name only a short word ("Python 编程"), whose other words are held as English words are
    (R9). A mark or a letter not read (_strange) is never one item. Nothing left (only wording) is one, unnamed, ask."""
    core = _core(_judging(text))

    if not core:
        return True

    if _strange(core):
        return False

    chinese = "".join(CJK.findall(core))

    if chinese:
        if any(word in core for word in LINKING_CHINESE):
            return False

        latin = NOT_HAN.findall(core)

        if not latin:
            return len(chinese) <= MAX_CHINESE

        # R9: the words beside a short Chinese word pass the English checks too ("Python for data 分析" does not).
        return len(chinese) <= NAME_WITH_CHINESE and _english_one(latin)

    return _english_one(core.split())


def _english_one(words) -> bool:
    if len(words) > SINGLE_WORDS or len(" ".join(words)) > SINGLE_CHARS:
        return False

    if any(word.casefold().strip(".,;:()'\"") in LINKING_WORDS for word in words):
        return False

    return _name_runs(words) <= 1


def _name_runs(words) -> int:
    """How many runs of names stand in the words: "Python alongside SQL" has two, "Microsoft Power BI" one."""
    runs, inside = 0, False

    for word in words:
        # R9: a name starts with a capital, a digit or a dot, whatever follows ("Café", "Python's").
        name = bool(word) and (word[0].isupper() or word[0].isdigit() or word[0] == ".")
        runs += name and not inside
        inside = name

    return runs


def _marks(text: str) -> str:
    """Marks typed either way read as their plain marks (R9), for the reading only: curly apostrophes as "'", any dash
    or hyphen as "-", curly quotes, brackets, odd spaces, trademark signs and emoji as a space. Han characters, ASCII
    and everything else (full-width forms, ，、。；：) stay as they are, so a full-width comma still means what R6 reads it
    as; reading() parts the words after this ("‘A/B testing’" is A/B testing, "“Python and SQL”" lists two)."""
    marked = []

    for index, character in enumerate(text):
        kind = unicodedata.category(character)

        if character.isascii() or CJK.match(character):
            marked.append(character)
        elif character in APOSTROPHES:
            # An apostrophe inside a word ("client’s"); otherwise a single quote ("‘A/B testing’").
            inside = 0 < index < len(text) - 1 and text[index - 1].isalnum() and text[index + 1].isalnum()
            marked.append("'" if inside or character in "′ʼ" else " ")
        elif kind == "Pd" or character == "−":
            marked.append("-")
        elif kind in ("Pi", "Pf", "Ps", "Pe", "So", "Sk") or kind.startswith("Z"):
            marked.append(" ")
        else:
            marked.append(character)

    return "".join(marked)


def _judging(text: str) -> str:
    """The words as the rules judge one item and name it (R9), never as they are kept or shown: full-width forms as
    plain ones (NFKC, ，as ",") and marks as _marks reads them; any other character is left for _strange."""
    return _marks(unicodedata.normalize("NFKC", text))


def _strange(text: str) -> bool:
    """Whether judged words hold a character the rules do not read (R9): a mark other than plain punctuation (an
    arrow, a bullet, a middle dot, 。 or 、 inside them), or a letter of a script other than Latin and Han."""
    for character in text:
        if character.isascii() or CJK.match(character):
            continue

        kind = unicodedata.category(character)

        if kind[0] == "M" or (kind[0] in "LN" and unicodedata.name(character, "").startswith("LATIN")):
            continue

        return True

    return False


def _asks(text: str) -> tuple[str, ...]:
    """What a passage of practice must name to show words read as one item (R9): the words _core leaves of them, as
    judged, without wording and describing words (NOT_ASKED: "Ability to write clean, effective code" -> "write
    code"), when two or more parts are left; () when one part or none is, as nothing can then be shown only in part."""
    ask = _asked_words(text)

    return (ask,) if _parts(ask) > 1 else ()


def _asked_words(text: str) -> str:
    return " ".join(word for word in _core(_judging(text)).split() if word.casefold().strip(".,;:()'\"") not in NOT_ASKED)


def _parts(ask: str) -> int:
    """How many parts a passage must name to show the words: each English word (stop words aside) and each run of Han
    characters; Chinese alone is one part only when it is one short word (NAME_WITH_CHINESE characters or fewer)."""
    runs = HAN_RUN.findall(ask)
    words = {word for word in _tokens(HAN_RUN.sub(" ", ask), False) if word not in STOP_WORDS}

    if runs and not words:
        return 1 if len("".join(runs)) <= NAME_WITH_CHINESE else 2

    return len(words) + len(runs)


def _either(head: str, example: str, label: bool) -> tuple[str, ...]:
    """What shows a kind of thing asked for with one example of it (R9). The kind is what is asked for (the last
    stretch of the words before the lead-in, when it names a kind of thing): when two or more parts are left of it, a
    passage shows it by naming them all or by naming the example ("ML frameworks such as TensorFlow"); when one part or
    none is, it cannot be shown only in part, as for any one-part ask. Without a kind, the example is what is asked."""
    stretches = _stretches(head, label)

    if not (stretches and _category(stretches[-1][0])):
        return _asks(example)

    kind = _asks(stretches[-1][0])
    named_example = _asked_words(example)

    return (*kind, named_example) if kind and named_example else kind


def _core(text: str) -> str:
    """What is left of a requirement's words once the wording of a lead-in, a label before a colon, a preferred or
    required end ("is a plus", "者优先") and an open end are left out (R8): "Familiarity with Docker is a plus" ->
    "Docker"; "岗位要求：熟练使用 Python" -> "Python"; "Python for data analysis" -> itself. R9: it takes judged words
    (_judging), and a dash or a list mark at either end goes too ("Docker - a plus" -> "Docker")."""
    core = text.strip(EDGE)
    changed = True

    while changed and core:
        changed = False
        end = PREFERRED_END.search(core)
        tail = TAIL.search(core)

        for found in (end, tail):
            if found is not None and found.end() > found.start():
                core, changed = core[: found.start()].strip(EDGE), True
                break

    label = re.match(r"([^:：]{1,40})[:：](?!//)\s*", core)

    if label and (_stretch_item(label.group(1), True) == ""):
        core = core[label.end():]

    changed = True

    while changed and core:
        changed = False
        lead = LEAD_IN.match(core)
        years = re.match(r"\d+\s*\+?\s*(?:年|个月)", core)
        first = core.split(maxsplit=1)
        word = next((word for word in GENERIC_CHINESE if core.startswith(word)), None)

        if lead and lead.end():
            core, changed = core[lead.end():].strip(), True
        elif years:
            core, changed = core[years.end():].strip(), True
        elif first and not CJK.search(first[0]) and _generic(first[0]):
            core, changed = (first[1] if len(first) > 1 else ""), True
        elif word:
            core, changed = core[len(word):].strip(), True

    words = core.split()

    while words and not CJK.search(words[-1]) and _generic(words[-1]):
        words.pop()

    core = " ".join(words)
    changed = True

    while changed and core:
        changed = False
        word = next((word for word in GENERIC_CHINESE if core.endswith(word)), None)

        if word:
            core, changed = core[: -len(word)].strip(), True

    return _bare(core)


def _slashes(text: str) -> str:
    """Slashes between two words that are not one term read as joining items (R7): "Python/SQL" -> "Python / SQL"."""

    def read(found: re.Match) -> str:
        left = re.search(r"[^\s/／]+$", text[: found.start()])
        right = re.match(r"[^\s/／]+", text[found.end():])
        # R9: quotes and brackets around them are no part of the two words ("'A/B testing'", "(I/O)").
        left = left.group().lstrip("'\"([{") if left else ""
        right = right.group().rstrip(",.;:'\")]}") if right else ""
        token = re.search(r"\S*$", text[: found.start()]).group() + "/" + re.match(r"\S*", text[found.end():]).group()

        one_term = (
            not left
            or not right
            or (len(left) == 1 and len(right) == 1)
            or left.isdigit()
            or right.isdigit()
            or (left.casefold() == "and" and right.casefold() == "or")
            # An address is no list: "https://...", "www.example.com/careers".
            or "://" in token
            or token.casefold().startswith("www.")
        )

        return found.group() if one_term else " / "

    return SLASH.sub(read, text)


def _stretch_item(part: str, label: bool = False) -> str | None:
    """What one stretch of the words before a lead-in asks for in its own right (R7, R8): "" when it is only the
    wording of a lead-in ("Experience", "3+ years", "professional experience", "Must know", "熟练"), or a label before a
    colon ("岗位要求"); None when what is left does not read as one plain item."""
    part = part.strip(" ,，、;；:：")

    if NON_ASCII.search(part):
        return _chinese_head(part, label)

    words = part.split()

    while words and _generic(words[0], label):
        words.pop(0)

    while words and _generic(words[-1], label):
        words.pop()

    item = _bare(" ".join(words))

    if not item:
        return ""

    return item if _fits(item) else None


def _generic(word: str, label: bool = False) -> bool:
    folded = word.casefold().strip(".,;:()'\"")

    return (
        not folded
        or bool(NO_WORD.fullmatch(folded))
        or folded in GENERIC_WORDS
        or folded in DESCRIBING_WORDS
        or folded in STOP_WORDS
        or bool(NUMBER.fullmatch(folded))
        or (label and folded in LABEL_WORDS)
    )


def _chinese_head(part: str, label: bool = False) -> str | None:
    text = part.strip(" -*•·")
    changed = True
    words = GENERIC_CHINESE + (LABEL_CHINESE if label else ())

    while changed and text:
        changed = False

        for word in words:
            if text.startswith(word):
                text, changed = text[len(word):].strip(), True

            if text.endswith(word):
                text, changed = text[: -len(word)].strip(), True

        number = CHINESE_NUMBER.match(text)

        if number and number.end():
            text, changed = text[number.end():].strip(), True

    if not text or text in GENERIC_CHINESE_ALONE:
        return ""

    return text if _fits(text) else None


def _clauses(text: str) -> str | None:
    """The words with their full-width commas and semicolons read (R6): between short terms they part items, as 、
    does; otherwise they end clauses. A clause that is only a year bound is left out; when one clause is left, the
    words are that clause; when more are, any later one may ask for more, and None says so."""
    parts = [part.strip() for part in CLAUSE.split(text)]

    if len(parts) == 1:
        return text

    kept = [part for part in parts if not YEAR_BOUND.fullmatch(part)]

    if not kept:
        return ""

    leads = list(LEAD_IN.finditer(kept[0]))
    first = kept[0][leads[-1].end():] if leads else kept[0]

    if all(_terms(part) for part in (first, *kept[1:])):
        return "、".join(kept)

    return kept[0] if len(kept) == 1 else None


def _terms(part: str) -> bool:
    """Whether a part between full-width commas may stand as items: short terms, never a clause with a number."""
    tail = TAIL.search(part)
    part = (part[: tail.start()] if tail else part).strip()
    pieces, _ = _split(part)

    return bool(part) and all(_fits(item) and not (NON_ASCII.search(item) and DIGIT.search(item)) for item in map(_bare, pieces))


def _unled(text: str) -> tuple[Listing | None, tuple[str, ...]]:
    """No lead-in starts a list (R6): a plain series of the whole words ("A, B, and C"), or two or more plain items
    however joined ("Python and SQL", "Python, SQL", "Python、SQL"); one item only where nothing may part items and
    the words plainly ask for one thing (R8, _single), with what a passage must name to show it (R9, _asks); unread
    where nothing may part items but they do not; otherwise unclear, never one item."""
    found = _list(text, said="", examples=False, series=True)

    if found is not None:
        return found, ()

    tail = TAIL.search(text)
    rest = text[: tail.start()] if tail else text

    if not LISTED.search(rest):
        # R8: no separator is not yet one item ("Python for data analysis", "Python alongside SQL").
        return (None, _asks(text)) if _single(text) else (Listing(UNREAD, ()), ())

    found = _list(text, said="", examples=False, plain=True)

    if found is not None:
        return found, ()

    # Something may part items here, but not plainly: never one item.
    return Listing(UNCLEAR, ()), ()


def _list(rest: str, *, said: str, examples: bool, series: bool = False, plain: bool = False) -> Listing | None:
    tail = TAIL.search(rest)
    or_end = one_end = False

    if tail is not None:
        or_end = (tail.group("word") or "").casefold() == "or"
        one_end = tail.group("one") is not None
        rest = rest[: tail.start()]

    parts, joins = _split(rest)
    items = [_bare(item) for item in parts]
    asked = ()

    if not said and items:
        # Without a lead-in (R6): the words before the first item's names are no part of it. What they ask for in
        # their own right stays an item (R7): "Machine learning via Python and SQL" asks for machine learning too.
        before, items[0] = _trimmed(items[0])
        asked = _asked(before) if before else ()

        if asked == UNREAD:
            # R10: the words before the first item's names are a phrase of their own: no items made up of them.
            return Listing(UNREAD, ())

        if asked is None:
            return None

    items = tuple(items)

    if len(items) < 2 or any(not _fits(item) for item in items):
        return None

    if series and (len(items) < 3 or not joins or joins[-1] not in OR_WORDS | AND_WORDS):
        return None

    if plain and any(word.casefold() in FUNCTION_WORDS for item in items for word in item.split()):
        return None

    ors = [join for join in joins if join in OR_WORDS]
    ands = [join for join in joins if join in AND_WORDS]
    any_one = bool(ors) or or_end or one_end or bool(ANY_OF.search(said))
    all_of = bool(ALL_OF.search(said))
    # R7: a slash says neither "and" nor "or", unless the words say which ("one of", "all of").
    slashed = SLASHED in joins and not all_of and not ANY_OF.search(said)

    if (ors and ands) or (any_one and all_of) or slashed:
        mode = UNCLEAR
    elif all_of:
        mode = ALL
    elif any_one:
        mode = ANY
    elif examples:
        # Examples, said to be required: whether all of them are is not plain.
        mode = UNCLEAR if REQUIRED.search(said) else ANY
    else:
        mode = ALL

    return _with_head(asked, Listing(mode, items))


def _split(rest: str) -> tuple[list[str], list[str]]:
    """The items and the words joining them (see ENGLISH_SEPARATOR, CHINESE_JOIN)."""
    if "、" in rest:
        parts = rest.split("、")
        joins = ["、"] * (len(parts) - 1)
        found = CHINESE_JOIN.search(parts[-1])

        if found is not None:
            parts[-1:] = [parts[-1][: found.start()], parts[-1][found.end():]]
            joins.append(found.group().strip())

        return parts, joins

    if NON_ASCII.search(rest):
        found = CHINESE_JOIN.search(rest)

        return ([rest[: found.start()], rest[found.end():]], [found.group().strip()]) if found else ([rest], [])

    joins = [found.group().strip(" ,;").casefold() for found in ENGLISH_SEPARATOR.finditer(rest)]

    return ENGLISH_SEPARATOR.split(rest), joins


def _bare(item: str) -> str:
    # R7: punctuation a lead-in left at the start goes too ("e.g. regression": the lead-in ends before its last dot).
    return re.sub(r"^(?:a|an|the)\s+", "", item.strip().lstrip(".,;:").strip(), flags=re.IGNORECASE)


def _trimmed(item: str) -> tuple[str, str]:
    """The first item without a lead-in (R6), and the words before it: its trailing names, when a word starting low
    (or a Chinese one) stands before them ("Must know Python" -> "Must know", "Python"); as it stands otherwise
    ("Strong Python", "data analysis"), with nothing before it. What those words ask for is the caller's (R7)."""
    words = item.split()
    start = len(words)

    while start > 0 and NAME_WORD.fullmatch(words[start - 1]):
        start -= 1

    if 0 < start < len(words) and HEAD_WORD.match(words[start - 1]):
        return " ".join(words[:start]), " ".join(words[start:])

    return "", item


def _fits(item: str) -> bool:
    if not item or LEAD_IN.match(item) or EXAMPLES.match(item):
        return False

    if NON_ASCII.search(item):
        return len(item.replace(" ", "")) <= MAX_CHINESE

    return len(item) <= MAX_CHARS and len(item.split()) <= MAX_WORDS


def named(item: str, passages) -> bool:
    """Whether a passage names the item: its words all stand in one passage (case, plural, hyphen or space aside);
    Chinese as written, spaces aside. R9: item and passages as judged (_judging), so a curly apostrophe or a full-width
    letter names as its plain form does; only Han characters are matched as Chinese, each run of them as written, with
    the English words beside them matched as English words are (R8 matched any item with a character outside ASCII as
    one string, curly apostrophe and all)."""
    item = _judging(item)
    passages = [_judging(passage) for passage in passages if passage]
    runs = HAN_RUN.findall(item)

    if runs:
        words = [word for word in _tokens(HAN_RUN.sub(" ", item), False) if word not in STOP_WORDS]

        return any(
            all(run in "".join(passage.split()).casefold() for run in runs)
            and all(any(_same(word, token) for token in _tokens(passage, False)) for word in words)
            for passage in passages
        )

    for joined in (False, True):
        words = [word for word in _tokens(item, joined) if word not in STOP_WORDS]

        if words and any(all(any(_same(word, token) for token in tokens) for word in words) for tokens in (_tokens(passage, joined) for passage in passages)):
            return True

    return False


def _tokens(text: str, joined: bool) -> list[str]:
    # R9: a possessive is no word of its own ("Bachelor's degree" asks for no "s").
    folded = re.sub(r"'s\b", "", text.casefold()).replace("_", " ").replace("-", "" if joined else " ")

    return TOKEN.findall(folded)


def _same(word: str, token: str) -> bool:
    return (
        token == word
        or token in (f"{word}s", f"{word}es")
        or (word.endswith("es") and token == word[:-2])
        or (word.endswith("s") and token == word[:-1])
        # R9: another form of the same word ("built" names "building", "developed" names "developing").
        or _stem(token) == _stem(word)
    )


# R9. Past forms that do not end in -ed, of verbs a requirement or a passage of practice commonly uses. A fixed set: a
# form it misses is not taken for the word, so it only ever leaves a requirement needing confirmation.
IRREGULAR = {
    "built": "build", "wrote": "write", "written": "write", "ran": "run", "led": "lead", "made": "make",
    "taught": "teach", "drew": "draw", "drawn": "draw", "drove": "drive", "driven": "drive", "grew": "grow",
    "grown": "grow", "did": "do", "done": "do", "took": "take", "taken": "take", "gave": "give", "given": "give",
    "spoke": "speak", "spoken": "speak", "sold": "sell", "brought": "bring", "thought": "think", "held": "hold",
    "kept": "keep", "met": "meet", "sent": "send", "spent": "spend", "won": "win", "began": "begin", "begun": "begin",
    "chose": "choose", "chosen": "choose", "knew": "know", "known": "know", "understood": "understand",
}


def _stem(word: str) -> str:
    """One stem for the forms of a word (R9): an irregular past form as its verb, then -ing or -ed off, a doubled last
    consonant single, a last e off ("writing", "write" and "wrote" -> "writ"; "running", "ran" -> "run")."""
    word = IRREGULAR.get(word, word)

    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]

    if len(word) > 2 and word[-1] == word[-2] and word[-1] not in "aeiouls":
        word = word[:-1]

    return word[:-1] if len(word) > 3 and word.endswith("e") else word


def condition(quote: str) -> str | None:
    """The condition a posting quote lays down, word for word as it stands there, or None."""
    text = " ".join(quote.split())

    for pattern in (CONDITION_BEFORE, CONDITION_CHINESE, CONDITION_AFTER):
        found = pattern.search(text)

        if found is not None:
            return found.group("condition").strip()

    return None


def condition_state(wanted: str, posting: str) -> tuple[str, str]:
    """Whether a condition holds for this posting, and the posting's line that shows it: met or not met only for a
    plain, positive condition on the work mode and a posting that states its work mode in the set line of its head;
    unknown otherwise, with no line. posting is a source adapter's text, or "" for one that is not (the caller's)."""
    mode = _condition_mode(wanted)
    posted = posted_work_mode(posting)

    if mode is None or posted is None:
        return UNKNOWN, ""

    value, line = posted

    return (MET if value == mode else NOT_MET), line


def _condition_mode(wanted: str) -> str | None:
    text = " ".join(wanted.split()).rstrip(".;:，,。")

    for pattern in (WORK_MODE_CONDITION, WORK_MODE_CONDITION_CHINESE):
        found = pattern.fullmatch(text)

        if found is not None:
            return MODE_WORDS[found.group("mode").casefold()]

    return None


def posted_work_mode(posting: str) -> tuple[str, str] | None:
    """The work mode a source adapter's text states in its head, and that line, or None: only one such line, in the
    lines before the first blank one, with exactly onsite, remote or hybrid."""
    head = (posting or "").split("\n\n", 1)[0]
    lines = [line.strip() for line in head.split("\n") if line.strip().startswith(WORK_MODE_LABEL)]

    if len(lines) != 1:
        return None

    found = WORK_MODE_LINE.fullmatch(lines[0])

    return (found.group("value"), lines[0]) if found else None


def strength(quote: str) -> str:
    """How firmly a posting's own words ask (R10, U3-D6-3 F1; module docstring, part 3): OBLIGED when any sentence of
    them requires, EXPECTED when every sentence only expects, prefers or says something is not required, STATED
    otherwise (nothing said either way, or a plain statement beside an expectation). R12: a sentence only expects when
    it asks softly for the whole of what it asks (_wholly_soft). Read as judged (R9, _judging), a long dash as one set
    off by spaces (LONG_DASH); the words are never changed."""
    judged = _judging(LONG_DASH.sub(" - ", quote or ""))
    kinds = [_sentence_strength(part) for part in SENTENCE_END.split(judged) if re.search(r"\w", part)]

    if OBLIGED in kinds:
        return OBLIGED

    if kinds and all(kind == EXPECTED for kind in kinds):
        return EXPECTED

    return STATED


def obliges(words: str) -> bool:
    """Whether words require (R10): a word of OBLIGES stands in them outside a negation of NOT_OBLIGED, or they say
    something is not optional (R11, NOT_OPTIONAL)."""
    judged = _judging(words or "")

    return bool(NOT_OPTIONAL.search(judged) or OBLIGES.search(NOT_OBLIGED.sub(CUT, judged)))


def without_obligation(words: str) -> str | None:
    """The parse's own words for a requirement without the firmness they say, in fixed forms only (R10, U3-D6-3 F1):
    "Must know Python" -> "Python", "Candidates must have a bachelor's degree" -> "bachelor's degree", "Python is
    required" -> "Python", "Required: SQL" -> "SQL", "必须掌握 SQL" -> "SQL". None when what is left still requires or
    nothing is left ("This must be the candidate's final internship"): the caller then shows the posting's own words.
    Each requirement keeps words of its own this way, where it can: a posting line that the parse split into several
    requirements, each quoting the whole line, is not read as the list of each of them."""
    text = " ".join((words or "").split())

    for pattern in (REQUIRED_LABEL, LEADING_OBLIGATION, CHINESE_OBLIGATION, TRAILING_OBLIGATION):
        text = pattern.sub("", text).strip()

    text = ARTICLE.sub("", text).strip(" ,，;；:：")

    return text if text and not obliges(text) else None


def _sentence_strength(sentence: str) -> str:
    """One sentence (R10). R11: "not optional" requires; a negation reaches no further than NOT_OBLIGED's span says,
    so a requirement after a turn ("not preferred but required") still requires; an expectation said not to hold ("not
    preferred", "not a plus") expects nothing; and a negation left unread (UNREAD_DENIAL) leaves the sentence unclear,
    never soft. R12: it only expects when it asks softly for the whole of what it asks (_wholly_soft); what is unclear
    keeps the parse's group."""
    if NOT_OPTIONAL.search(sentence):
        return OBLIGED

    # A span taken out leaves a mark, so the words on either side never join into a new phrase ("have" + "to").
    rest = NOT_OBLIGED.sub(DENIED, sentence)

    if OBLIGES.search(rest):
        return OBLIGED

    rest = NOT_EXPECTED.sub(WAIVED, rest)

    if UNREAD_DENIAL.search(SOFT_NOT.sub(r"\1", rest)):
        return STATED

    return EXPECTED if _wholly_soft(rest) else STATED


def _wholly_soft(sentence: str) -> bool:
    """Whether a sentence, its denials and waived expectations marked, asks softly for the whole of what it asks (R12;
    module docstring, part 3). Its clauses (CLAUSE_MARK) are read one by one (_clause_kind). It does when the clauses
    before its first colon are all soft, bare or denied (a label: "Preferred:", "Not required but preferred:"); when its
    first clause is bare (a sentence adverb: "Ideally, ..."); or when every clause that names something is soft or
    denied, a bare clause making soft the one beside it (the one before, at the end: "SQL, preferably.", "Python
    (preferred)."; else the one after). A clause under a condition, or whose expectation was said not to hold, asks
    nothing of its own; a bare clause never makes soft one that joins two things (JOINED). Anything else does not: a
    soft clause beside a stated one ("Bachelor's degree in Computer Science, Master's preferred."), a soft word inside
    a clause ("Valid work authorization for your preferred office location.")."""
    head, colon, _ = sentence.partition(":")

    if colon:
        label = [kind for kind in map(_clause_kind, CLAUSE_MARK.split(head)) if kind not in ("empty", "waived")]

        if label and all(kind in ("soft", "bare", "denied") for kind in label):
            return True

    named = [
        clause
        for clause in CLAUSE_MARK.split(sentence)
        if _clause_kind(clause) not in ("empty", "waived", "condition")
    ]
    kinds = [_clause_kind(clause) for clause in named]

    if not kinds:
        return False

    if kinds[0] == "bare":
        return True

    for index, kind in enumerate(kinds):
        if kind == "bare":
            beside = index - 1 if index == len(kinds) - 1 else index + 1

            # On one thing only ("SQL, preferably."), never on two joined ("Bachelor's degree and Master's (preferred)").
            if kinds[beside] == "stated" and not JOINED.search(named[beside]):
                kinds[beside] = "soft"

    return all(kind in ("soft", "denied", "bare") for kind in kinds)


def _clause_kind(clause: str) -> str:
    """What one clause of a sentence does (R12, _wholly_soft): "denied" (a requirement said not to hold stood in it),
    "waived" (an expectation said not to hold), "empty", "bare" (a soft word and nothing else, BARE), "condition"
    (CONDITION), "soft" (asked softly as a whole: SOFT_LEAD, SOFT_MODAL, SOFT_TAIL) or "stated" (anything else that
    names something). A leading "and", "or", "plus" or "&" is left out ("and Tableau preferred")."""
    if DENIED.strip() in clause:
        return "denied"

    if WAIVED.strip() in clause:
        return "waived"

    words = re.sub(r"^\W*(?:(?:and|or|plus)\b|&)\s*", "", clause, flags=re.IGNORECASE)

    if not re.search(r"\w", words):
        return "empty"

    if BARE.match(words):
        return "bare"

    if CONDITION.match(words):
        return "condition"

    if SOFT_LEAD.match(words) or SOFT_MODAL.match(words):
        return "soft"

    tail = SOFT_TAIL.search(words)

    # A soft predicate is on all its clause names, but never on two things joined ("Bachelor's degree in Computer
    # Science and Master's preferred"), never past "that", "where", ... (RELATIVE), nor, with no "is" before it, after a
    # subject of its own (OWN_SUBJECT).
    subject = words[: tail.start()] if tail else ""

    if tail and not JOINED.search(subject) and not RELATIVE.search(POINTING.sub("", subject)):
        if COPULA.match(tail.group(0)) or not OWN_SUBJECT.search(subject):
            return "soft"

    return "stated"
