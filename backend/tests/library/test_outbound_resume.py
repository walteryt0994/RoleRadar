"""U2-C3: what a real structuring request sends of a resume, and what it keeps back.

Everything is synthetic, and nothing is sent: these test the rules alone.
"""
import json

from app.live_structuring.service import INSTRUCTIONS, SEGMENTS_END, SEGMENTS_START, request_for
from app.library.outbound_resume import CITY, LINK, NAME, outbound
from app.outbound_review import ADDRESS, CONFIDENTIAL, EMAIL, ID_NUMBER, PHONE


def pasted(*lines) -> list[dict]:
    return [{"kind": "text", "number": 1, "text": "\n".join(lines)}]


def test_contact_details_are_masked_and_labelled_lines_are_not_sent():
    sent = outbound(
        pasted(
            "合成学生 Synthetic Student",
            "姓名：林合成 | 城市：杭州",
            "电话：138 0000 0000 | 邮箱：lin.synthetic@example.com | 证件：11010120000101001X",
            "作品集：https://example.com/portfolio  主页：www.example.com/lin",
            "林合成负责每周的数据清洗",
        )
    )

    # A name without a label goes out as it is; the page says so.
    assert sent.texts == (
        "合成学生 Synthetic Student\n"
        "电话：［已隐去电话号码］ | 邮箱：［已隐去邮箱］ | 证件：［已隐去证件号码］\n"
        "作品集：［已隐去链接］  主页：［已隐去链接］\n"
        "［已隐去姓名］负责每周的数据清洗",
    )
    assert sent.masked == {PHONE: 1, EMAIL: 1, ID_NUMBER: 1, LINK: 2, NAME: 1}
    assert sent.withheld == {NAME: 1}


def test_lines_with_an_address_or_a_confidential_word_are_not_sent_at_all():
    sent = outbound(pasted("住址：合成市合成路 88 号", "负责保密项目的数据整理", "城市：杭州", "使用 Python 清洗数据"))

    assert sent.texts == ("使用 Python 清洗数据",)
    assert sent.withheld == {ADDRESS: 1, CONFIDENTIAL: 1, CITY: 1}


def test_segments_keep_their_numbers_and_one_with_nothing_left_is_not_sent():
    segments = [
        {"kind": "page", "number": 1, "text": "姓名：林合成"},
        {"kind": "page", "number": 2, "text": "", "unreadable": True},
        {"kind": "table", "number": 1, "rows": [["城市：杭州", "合成"], ["合成大学", "本科"]]},
        {"kind": "paragraph", "number": 1, "text": "负责数据清洗"},
    ]
    sent = outbound(segments)

    assert sent.texts == (None, None, "合成大学 | 本科", "负责数据清洗")
    assert sent.sent == [(3, "合成大学 | 本科"), (4, "负责数据清洗")]
    assert sent.size == len("合成大学 | 本科".encode()) + len("负责数据清洗".encode())
    # As the checks read it: a segment not sent has no text, so nothing can be quoted from it.
    assert sent.segments[0] == {"kind": "text", "number": 1, "text": ""}


def test_a_labelled_name_is_masked_as_a_whole_word_and_a_short_one_is_left_alone():
    sent = outbound(
        pasted("Name: Lin", "Lin built the LinkedIn scraper with lin.", "姓名：王", "王老师指导", "姓名：林合成（男）", "林合成写了接口")
    )

    assert sent.texts == ("［已隐去姓名］ built the LinkedIn scraper with ［已隐去姓名］.\n王老师指导\n［已隐去姓名］写了接口",)
    assert sent.masked == {NAME: 3}
    assert sent.withheld == {NAME: 3}


def test_of_overlapping_values_the_one_that_begins_first_is_masked_whole():
    sent = outbound(pasted("Name: Lin", "邮箱 lin@example.com，主页 https://example.com/?mail=a@example.com"))

    assert sent.texts == ("邮箱 ［已隐去邮箱］，主页 ［已隐去链接］",)
    assert sent.masked == {EMAIL: 1, LINK: 1}


def test_the_request_holds_the_instructions_then_only_what_is_sent_as_json_data():
    segments = pasted("姓名：林合成", "邮箱：lin@example.com", "教育经历", "合成大学 | 本科")
    request = request_for(segments)
    instructions, rest = request.prompt.split(f"\n\n{SEGMENTS_START}\n")
    data = json.loads(rest.removesuffix(f"\n{SEGMENTS_END}"))

    assert instructions == INSTRUCTIONS
    assert data == [{"segment": 1, "text": "邮箱：［已隐去邮箱］\n教育经历\n合成大学 | 本科"}]
    assert "林合成" not in request.prompt and "lin@example.com" not in request.prompt
    # The same text gives the same request; any change gives another.
    assert request_for(segments).digest == request.digest
    assert request_for(pasted("邮箱：lin@example.com", "教育经历", "合成大学 | 硕士")).digest != request.digest
