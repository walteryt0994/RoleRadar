"""What must not go out to an analysis service, as far as simple rules can tell (U2-A2).

Before anything of a task goes out, its words are checked for contact
details, identity numbers, addresses and words that mark content as
confidential. An experience field or a preference with any of them is not
sent at all; the user can rewrite it and confirm it again. A job posting
keeps its text, but email addresses, phone numbers and identity numbers in
it (usually the recruiter's) are replaced by a placeholder before it goes
out: their patterns cover the whole value. An address is different: the
patterns find a label or a house number, not where the address ends, so a
posting with one is not sent at all, like one marked as confidential. The
user removes that part first; nothing is masked half-way.

These are a few patterns, not a full reading of the text: they miss names
of other people, unusual formats and secrets that do not say they are
secret, and they flag some harmless text. Passing them does not make text
anonymous, and the pages never say so.

U3-D2-2 adds North American phone numbers and English street addresses
(a house number and a street, a suite or floor, a PO box, a city with its
state and ZIP code). For the user's own material and for postings they
paste nothing changes but what is found. Only an admitted official posting
may go out with an English address masked (masked_official_posting): each
short line holding one is masked whole; one too long to mask whole, one
running across lines, a Chinese address, or anything still found after
masking stops the posting as before. The caller decides which postings
those are, by their source, never by what a posting says of itself.
"""
import re
from dataclasses import dataclass
from app.i18n import MessageMap

EMAIL = "email"
PHONE = "phone"
ID_NUMBER = "id_number"
ADDRESS = "address"
CONFIDENTIAL = "confidential"

LABELS = {
    EMAIL: "邮箱",
    PHONE: "电话号码",
    ID_NUMBER: "证件号码",
    ADDRESS: "详细地址",
    CONFIDENTIAL: "保密或内部内容",
}
# The same kinds as the pages name them, in the interface language (UI bilingual batch). LABELS stays as it is: a
# masked placeholder sent to a service is written with it.
NAMES = MessageMap(
    {
        EMAIL: "found.email",
        PHONE: "found.phone",
        ID_NUMBER: "found.id_number",
        ADDRESS: "found.address",
        CONFIDENTIAL: "found.confidential",
    }
)

# An address as the Chinese rules find it: a label, a road and house number, a building or a room.
CN_ADDRESS = re.compile(
    r"(?:住址|家庭地址|详细地址|通讯地址|收件地址|home address)"
    r"|(?:路|街|大道|巷|弄|胡同)\s?\d+\s?号"
    r"|\d+\s?(?:号楼|栋|幢)\s?\d+"
    r"|\d{3,4}\s?室",
    re.IGNORECASE,
)
# English street addresses (U3-D2-2), by forms written with capitals: a house number and a street ("1 Hacker Way",
# "200 Park Ave", "1585 Broadway"), a suite or floor with its number, a PO box, and a city with its state and ZIP code
# ("New York, NY 10001"). Not one of them is a place on its own: "New York, NY" names a city and is not found.
STREET_TYPES = (
    "Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Parkway|Pkwy|Highway|Hwy|"
    "Square|Sq|Terrace|Ter|Circle|Cir|Plaza|Plz|Alley|Expressway|Expy|Freeway|Fwy|Trail|Trl|Crescent|Row|Walk"
)
US_STATES = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|"
    "OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|PR"
)
EN_ADDRESS = re.compile(
    rf"(?<![\w-])\d{{1,6}}[A-Za-z]?(?:-\d{{1,6}})?\s+(?:(?:[NSEW]|N[EW]|S[EW]|North|South|East|West)\.?\s+)?"
    rf"(?:(?:[A-Z0-9][A-Za-z0-9'.-]*\s+){{1,4}}(?:{STREET_TYPES})(?![\w-])\.?|Broadway(?![\w-]))"
    r"|(?<![\w-])(?:Suite|Ste|Apt|Apartment|Floor|Fl)\.?\s*#?\s*\d{1,5}[A-Za-z]?(?![\w-])"
    r"|(?<![\w-])\d{1,3}(?:st|nd|rd|th)\s+Floor(?![\w-])"
    r"|(?<![\w-])P\.?\s?O\.?\s+Box\s+\d+"
    rf"|(?<![\w-])[A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){{0,3}},\s*(?:{US_STATES})\s+\d{{5}}(?:-\d{{4}})?(?![\w-])"
)
# Contact details and identity numbers, found by their usual written forms; and addresses.
PATTERNS = (
    (EMAIL, re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
    # A mainland resident identity number: 18 characters, the last may be X.
    (ID_NUMBER, re.compile(r"(?<![0-9A-Za-z])[1-9]\d{16}[\dXx](?![0-9A-Za-z])")),
    (
        PHONE,
        re.compile(
            r"(?<![\d+])(?:\+?86[-\s]?)?1[3-9]\d(?:[-\s]?\d{4}){2}(?!\d)"
            r"|(?<!\d)0\d{2,3}[-\s]\d{7,8}(?!\d)"
            r"|(?<!\d)\+\d{1,3}(?:[-\s]?\d{2,4}){2,4}(?!\d)"
            # A North American number (U3-D2-2): area code and exchange each [2-9]XX, then four digits, with
            # separators or parentheses between the groups. Ten digits in a row are not taken for one.
            r"|(?<![\w+])(?:\+?1[-.\s]?)?(?:\(\s*[2-9]\d{2}\s*\)\s?|[2-9]\d{2}[-.\s])[2-9]\d{2}[-.\s]\d{4}(?!\w)"
        ),
    ),
    (ADDRESS, CN_ADDRESS),
    (ADDRESS, EN_ADDRESS),
)
# Words that say content is not meant to leave where it came from.
CONFIDENTIAL_MARKERS = (
    "机密",
    "内部资料",
    "内部数据",
    "内部文件",
    "不得外传",
    "未公开",
    "保密",
    "confidential",
    "internal only",
    "nda",
    "proprietary",
)
# What a posting may have masked: these patterns match the whole value.
MASKED_IN_POSTING = (EMAIL, PHONE, ID_NUMBER)
# What a masked value is replaced by, with the label of its kind: ［已隐去邮箱］.
MASK = "［已隐去{}］"
# How every placeholder begins: words that hold it were masked before they went out.
MASK_START = "［已隐去"
# Postings often say "薪资保密" or "公司名称保密" about themselves: that alone
# does not mark them as confidential material.
POSTING_MARKERS = tuple(marker for marker in CONFIDENTIAL_MARKERS if marker != "保密")
NDA = re.compile(r"\bnda\b", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def review(text: str, *, posting: bool = False) -> tuple[Finding, ...]:
    """Everything in the text that may not go out, in order of where it stands."""
    findings = [
        Finding(kind, found.start(), found.end())
        for kind, pattern in PATTERNS
        for found in pattern.finditer(text)
    ]
    folded = text.casefold()

    for marker in POSTING_MARKERS if posting else CONFIDENTIAL_MARKERS:
        if marker == "nda":
            findings.extend(Finding(CONFIDENTIAL, found.start(), found.end()) for found in NDA.finditer(text))
            continue

        start = folded.find(marker)

        while start != -1:
            findings.append(Finding(CONFIDENTIAL, start, start + len(marker)))
            start = folded.find(marker, start + 1)

    return tuple(sorted(findings, key=lambda finding: (finding.start, finding.end)))


def kinds(findings) -> tuple[str, ...]:
    """The kinds found, each once, in the order of LABELS."""
    found = {finding.kind for finding in findings}

    return tuple(kind for kind in LABELS if kind in found)


def masked_posting(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """A posting as it may go out: (text, kinds masked, kinds that stop it).

    Email addresses, phone numbers and identity numbers are replaced by
    "［已隐去…］". An address or a confidential marker stops the posting: it
    is left in place and the posting is not sent until the user removes it.
    """
    findings = review(text, posting=True)
    stopping = kinds(finding for finding in findings if finding.kind not in MASKED_IN_POSTING)
    masked = [finding for finding in findings if finding.kind in MASKED_IN_POSTING]
    pieces, position, hidden = [], 0, []

    for finding in masked:
        if finding.start < position:
            # Overlaps the one just replaced.
            continue

        pieces.append(text[position:finding.start])
        pieces.append(MASK.format(LABELS[finding.kind]))
        position = finding.end
        hidden.append(finding.kind)

    pieces.append(text[position:])

    return "".join(pieces), kinds(Finding(kind, 0, 0) for kind in hidden), stopping


# The longest line masked whole for holding an English address: in a longer one, where the address ends is not plain.
ADDRESS_LINE_CHARS = 120
# What such a line is replaced by.
ADDRESS_LINE = "［已遮盖地址行］"
# What may be left of an address block on a line right next to a masked one (D2-2 R1): a unit, room or building
# number, a number sign, a ZIP code. Looked for there only, never in the rest of a posting.
CONTINUATION = re.compile(
    r"(?<![\w-])(?:Unit|Room|Rm|Bldg|Building|Level|Lvl|Tower|Wing|Mailstop|Suite|Ste|Apt|Floor|Fl)\.?\s*#?\s*\d"
    r"|#\s*\d"
    r"|(?<![\w-])\d{5}(?:-\d{4})?(?![\w-])",
    re.IGNORECASE,
)


def masked_official_posting(text: str) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """An admitted official posting as it may go out (U3-D2-2): (text, kinds masked, kinds that stop it).

    Only for postings of a source admitted and confirmed for the search, and
    only English addresses: each line holding one, no longer than
    ADDRESS_LINE_CHARS, is replaced whole by "［已遮盖地址行］", and nothing
    of it is kept or read back. Then the posting is masked as any other
    (masked_posting). It is stopped instead, as before, when an address runs
    across lines, sits in a longer line, is a Chinese one, or is still found
    after masking; and when it is marked confidential.

    Masking can only clear the English addresses it masks (D2-2 R1): what
    else stops the posting as written stops it still, wherever it stands,
    the masked lines included. A line right next to a masked one holding
    what may be left of the address (CONTINUATION) stops it too. A stopped
    posting comes back as masked_posting gives it, no line masked.
    """
    original = masked_posting(text)

    if any(kind != ADDRESS for kind in original[2]) or CN_ADDRESS.search(text):
        # A confidential marker or a Chinese address, as written: no masking makes it fit to go out.
        return original

    lines = text.split("\n")
    # Lines joined as one: an address that only reads as one across a line break cannot be masked line by line.
    flat = " ".join(lines)
    breaks = {index for index, char in enumerate(text) if char == "\n"}

    if any(any(found.start() <= at < found.end() for at in breaks) for found in EN_ADDRESS.finditer(flat)):
        return original

    masked_at = {
        index for index, line in enumerate(lines) if EN_ADDRESS.search(line) and len(line.strip()) <= ADDRESS_LINE_CHARS
    }

    for index in masked_at:
        for near in (index - 1, index + 1):
            if 0 <= near < len(lines) and near not in masked_at and CONTINUATION.search(lines[near]):
                # Part of the address block may be left beside it, where its end is not plain: not sent.
                return original

    kept = [ADDRESS_LINE if index in masked_at else line for index, line in enumerate(lines)]
    masked_lines = len(masked_at)
    masked, hidden, stopping = masked_posting("\n".join(kept))

    if stopping or any(finding.kind == ADDRESS for finding in review(" ".join(masked.split("\n")), posting=True)):
        # Something of an address is still there, maybe across the lines left: not sent, and no line masked.
        return original[0], original[1], original[2] or (ADDRESS,)

    if masked_lines:
        hidden = kinds([*(Finding(kind, 0, 0) for kind in hidden), Finding(ADDRESS, 0, 0)])

    return masked, hidden, stopping
