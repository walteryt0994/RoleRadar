"""What a real structuring request sends of a resume, and what it keeps back (U2-C3).

The text goes out segment by segment, as the candidates' checks read it
(candidates.readable_text), line by line after these rules:

- A line with a detailed address or a word that marks it as confidential
  (app.outbound_review), or a labelled name or city (姓名：…, 城市：…, read
  as resume_rules reads them), is not sent at all.
- Email addresses, phone numbers, identity numbers and links are replaced
  by a placeholder (［已隐去邮箱］ …), and so is every other place where a
  labelled name stands: of at least MIN_NAME characters, and as a whole word
  where it is written in Latin letters.
- A segment with nothing left, or nothing read, is not sent. The others keep
  their numbers, so the pieces of an answer point at the import's own
  segments.

These are a few patterns, not a reading of the text: names written without
a label, other people's details, links without http(s):// or www. and
unusual formats go out as they are. The confirmation page says so, and shows
every word that will be sent. size is what a real structuring request's
limit is measured in: the UTF-8 bytes of the text that goes out.
"""
import re
from dataclasses import dataclass

from app.library.candidates import readable_text
from app.library.resume_rules import LABEL_FIELDS, LABELLED
from app.outbound_review import ADDRESS, CONFIDENTIAL, EMAIL, ID_NUMBER, LABELS, MASK, PATTERNS, PHONE, review

# Changing a rule changes this, and with it every confirmation's fingerprint.
RULES_VERSION = "1"
LINK = "link"
NAME = "name"
CITY = "city"
LINK_PATTERN = re.compile(r"(?:https?://|www\.)[^\s，。；、|｜<>\"'（）()]+", re.IGNORECASE)
MASKED_PATTERNS = tuple((kind, pattern) for kind, pattern in PATTERNS if kind in (EMAIL, PHONE, ID_NUMBER)) + (
    (LINK, LINK_PATTERN),
)
# What each placeholder says, in the order the confirmation page lists them.
MASK_LABELS = {EMAIL: LABELS[EMAIL], PHONE: LABELS[PHONE], ID_NUMBER: LABELS[ID_NUMBER], LINK: "链接", NAME: "姓名"}
# Why a line was kept back, in the same order.
WITHHELD_LABELS = {
    ADDRESS: "含详细地址",
    CONFIDENTIAL: "含保密或内部字样",
    NAME: "写着姓名",
    CITY: "写着所在城市",
}
# A shorter labelled name is not looked for elsewhere: it would mask ordinary words.
MIN_NAME = 2
LATIN = re.compile(r"[A-Za-z]")
# What follows a bracket is not part of the name: 林合成（男）.
NAME_END = re.compile(r"[（(]")


@dataclass(frozen=True)
class Outbound:
    """What goes out of an import's segments, and how much was masked or kept back."""

    # One per segment of the import, in its order: the text that goes out, or None.
    texts: tuple[str | None, ...]
    # Placeholders put in, by kind (MASK_LABELS).
    masked: dict[str, int]
    # Lines not sent, by reason (WITHHELD_LABELS).
    withheld: dict[str, int]

    @property
    def sent(self) -> list[tuple[int, str]]:
        """The segments that go out: (number, text)."""
        return [(number, text) for number, text in enumerate(self.texts, start=1) if text is not None]

    @property
    def segments(self) -> list[dict]:
        """What went out, as segments the candidates' checks read; a segment not sent has no text."""
        return [{"kind": "text", "number": number, "text": text or ""} for number, text in enumerate(self.texts, start=1)]

    @property
    def size(self) -> int:
        return sum(len(text.encode("utf-8")) for _, text in self.sent)


def outbound(segments: list[dict]) -> Outbound:
    """What a real structuring request would send of these segments."""
    lines = [text.split("\n") if text is not None else [] for text in map(readable_text, segments)]
    names = _name_patterns(lines)
    masked: dict[str, int] = {}
    withheld: dict[str, int] = {}
    texts = []

    for segment_lines in lines:
        kept = []

        for line in segment_lines:
            reason = _withheld(line)

            if reason is not None:
                withheld[reason] = withheld.get(reason, 0) + 1
            else:
                kept.append(_masked(line, names, masked))

        text = "\n".join(kept)
        texts.append(text if text.strip() else None)

    return Outbound(tuple(texts), masked, withheld)


def _labels(line: str):
    """The labelled values of a line: (is it a name, the value)."""
    for found in LABELLED.finditer(line):
        yield found.group("label").casefold() in LABEL_FIELDS, found.group("value")


def _withheld(line: str) -> str | None:
    """Why a line is not sent at all, or None."""
    kinds = {finding.kind for finding in review(line)}

    for kind in (ADDRESS, CONFIDENTIAL):
        if kind in kinds:
            return kind

    for is_name, _ in _labels(line):
        return NAME if is_name else CITY

    return None


def _name_patterns(lines: list[list[str]]) -> list[re.Pattern]:
    """Where the labelled names of the text stand anywhere else in it."""
    names = set()

    for segment_lines in lines:
        for line in segment_lines:
            for is_name, value in _labels(line):
                name = NAME_END.split(value)[0].strip()

                if is_name and len(name) >= MIN_NAME:
                    names.add(name)

    return [
        re.compile(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", re.IGNORECASE)
        if LATIN.search(name)
        else re.compile(re.escape(name))
        for name in sorted(names, key=len, reverse=True)
    ]


def _masked(line: str, names: list[re.Pattern], masked: dict[str, int]) -> str:
    """The line with each value replaced by its placeholder; of overlapping values, the one that begins first."""
    spans = [(found.start(), found.end(), kind) for kind, pattern in MASKED_PATTERNS for found in pattern.finditer(line)]
    spans += [(found.start(), found.end(), NAME) for pattern in names for found in pattern.finditer(line)]
    pieces, position = [], 0

    # The longer of two that begin at the same place covers the other.
    for start, end, kind in sorted(spans, key=lambda span: (span[0], -span[1])):
        if start < position:
            continue

        pieces.append(line[position:start])
        pieces.append(MASK.format(MASK_LABELS[kind]))
        masked[kind] = masked.get(kind, 0) + 1
        position = end

    pieces.append(line[position:])

    return "".join(pieces)
