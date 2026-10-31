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
"""
import re
from dataclasses import dataclass

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

# Contact details and identity numbers, found by their usual written forms.
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
        ),
    ),
    (
        ADDRESS,
        re.compile(
            r"(?:住址|家庭地址|详细地址|通讯地址|收件地址|home address)"
            r"|(?:路|街|大道|巷|弄|胡同)\s?\d+\s?号"
            r"|\d+\s?(?:号楼|栋|幢)\s?\d+"
            r"|\d{3,4}\s?室",
            re.IGNORECASE,
        ),
    ),
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
        pieces.append(f"［已隐去{LABELS[finding.kind]}］")
        position = finding.end
        hidden.append(finding.kind)

    pieces.append(text[position:])

    return "".join(pieces), kinds(Finding(kind, 0, 0) for kind in hidden), stopping
