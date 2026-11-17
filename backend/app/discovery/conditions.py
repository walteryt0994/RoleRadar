"""What a search's words and filters may be, and how a source's list is taken, shared by the search tools and by
每日查新 (U3-D4c): moved here unchanged from app.discovery.tools, which imports them as before.

Nothing here reads the library, a model's provider, an experience or the preferences: only a scope, words, a
source's declared keyword scope and its list. 每日查新 holds the searches a kept summary records to the very checks
a search made them under before anything is sent again.
"""
import re

from app.discovery import rules
from app.discovery.contracts import DIMENSION_LABELS, Dimension, Posting, PostingRef, SearchScope
from app.discovery.sources import KeywordScope, Listing, ListingSource, PartialListing, SourceAnomaly
from app.outbound_review import review

# Keywords are words of conditions: letters, digits and a few signs (C++, C#, .NET), never a sentence.
KEYWORDS = re.compile(r"[\w\s+#./-]+")
# A source's or a posting's id as summaries show it and later steps name it: letters, digits and . _ : -,
# and nothing the outbound rules stop (a phone number, an identity number). A posting named otherwise,
# or pointing to an official posting named otherwise, is not taken: its id would be a way around them.
PLAIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
# A name written without spaces between its words (Chinese): found within a keyword, as such a source matches it.
NON_ASCII = re.compile(r"[^\x00-\x7f]")


def _whole_list(adapter: ListingSource, timeout: float) -> Listing:
    """A source's list read once (U3-D2-1): to its end, or in part only as PartialListing says, with what was read.
    Any other answer is a failure, never a list read in part."""
    try:
        listing = adapter.listing(timeout)
    except PartialListing as cut:
        if cut.listing.complete or cut.listing.read <= 0:
            raise SourceAnomaly("来源说列表只读了一部分，给的却不是这样的列表") from None

        return cut.listing

    if not listing.complete:
        raise SourceAnomaly("来源给的列表没有读完，却没有说明是读到上限")

    return listing


def _words_problem(label: str, words: str, most: int) -> str | None:
    """Why words may not go to a source as a condition of a search."""
    if not words:
        return f"{label}是空的"

    if len(words) > most:
        return f"{label}最多 {most} 个字"

    if not KEYWORDS.fullmatch(words):
        return f"{label}只能是岗位、技能、城市这类条件词，不能带标点或整句话"

    if review(words):
        return f"{label}里有像联系方式、证件号、地址或保密内容的文字"

    return None


def named_words(declared: KeywordScope, scope: SearchScope, words: str) -> tuple[tuple[tuple[str, Dimension], ...], str]:
    """The words of keywords naming what a source matches by a filter (KeywordScope, U3-D2-E4 F1), each with its
    dimension, in their order; and the keywords left without them.

    Bounded, and never a guess: a city the scope names (listed, ideal or not accepted) or the source knows by name,
    a word the source reads as a job type, a type the scope names. A name of several words ("New York") is found as
    those words in a row; one written without spaces (北京, 实习) within a word, as such a source matches it, and
    then the whole word is named. Nothing else is taken for one: "Internal" is no type, "Yorkshire" no city."""
    terms = words.split()
    folded = [term.casefold() for term in terms]
    # Where each name found starts, how many words it has and what it names; and every word of them.
    taken: dict[int, tuple[int, Dimension]] = {}
    covered: set[int] = set()

    def free(start: int, length: int) -> bool:
        return all(index not in covered for index in range(start, start + length))

    def mark(start: int, length: int, dimension: Dimension) -> None:
        taken[start] = (length, dimension)
        covered.update(range(start, start + length))

    def find(names, dimension: Dimension) -> None:
        # Longer names first: "New York" before "York", were both named.
        for name in sorted(names, key=lambda name: (-len(name.split()), name)):
            tokens = name.split()

            if not tokens:
                continue

            if NON_ASCII.search(name):
                for index, term in enumerate(folded):
                    if free(index, 1) and name in term:
                        mark(index, 1, dimension)

                continue

            for start in range(len(folded) - len(tokens) + 1):
                if free(start, len(tokens)) and folded[start : start + len(tokens)] == tokens:
                    mark(start, len(tokens), dimension)

    def of(dimension: Dimension) -> set[str]:
        part = scope.of(dimension)

        return {rules.normalized(dimension, value) for value in (*part.values, *part.ideal, *part.excluded)}

    if Dimension.CITY in declared.filtered:
        find(of(Dimension.CITY) | set(declared.places), Dimension.CITY)

    if Dimension.JOB_TYPE in declared.filtered:
        find(of(Dimension.JOB_TYPE), Dimension.JOB_TYPE)

        if declared.type_of is not None:
            for index, term in enumerate(terms):
                if free(index, 1) and declared.type_of(term) is not None:
                    mark(index, 1, Dimension.JOB_TYPE)

    named = tuple((" ".join(terms[start : start + length]), dimension) for start, (length, dimension) in sorted(taken.items()))

    return named, " ".join(term for index, term in enumerate(terms) if index not in covered)


def _filters_problem(scope: SearchScope, filters, most: int) -> str | None:
    for dimension, value in filters:
        problem = None if value is None else _words_problem(DIMENSION_LABELS[dimension], value, most)

        if problem is None:
            problem = rules.allows(scope, dimension, value)

        if problem is not None:
            return problem

    return None


def _barred_problem(scope: SearchScope, words: str) -> str | None:
    """Keywords naming what the user cannot accept: the scope keeps it out of every search, keywords too."""
    folded = words.casefold()

    for dimension in Dimension:
        for value in scope.of(dimension).excluded:
            if rules.normalized(dimension, value) in folded:
                return f"检索词里有不能接受的{DIMENSION_LABELS[dimension]}：{value}"

    return None


def _plain(ref: PostingRef) -> bool:
    """Whether a reference may be shown and named again: plain ids, nothing the outbound rules would stop."""
    return all(PLAIN_ID.fullmatch(part) and not review(part) for part in (ref.source_id, ref.posting_id))


def _plain_posting(posting: Posting) -> bool:
    return _plain(posting.ref) and (posting.official is None or _plain(posting.official))
