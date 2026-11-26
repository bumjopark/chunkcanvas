"""CSV headings come from the file's own evidence, not from a vocabulary of field names."""
import pytest
from lxml import html

from app.convert import csv_body


def _read(text):
    body, notices = csv_body(text + "\n")
    tree = html.fromstring("<div>" + body + "</div>")
    headings = [cell.text_content() for cell in tree.xpath(".//th")]
    first = [cell.text_content() for cell in tree.xpath(".//tbody/tr[1]/td")]
    return notices, headings, first


@pytest.mark.parametrize("text", [
    # Plain text headings over text values: no field-name list may be required.
    "Student,Teacher,Subject\nAnn,Mr. Lee,Math\nBen,Ms. Kim,Art",
    "Country,Capital,Continent\nFrance,Paris,Europe\nPeru,Lima,South America",
    "Title,Author,Publisher\nDune,Frank Herbert,Chilton\nEmma,Jane Austen,Murray",
    "Name,Team,Role\nKim,Platform,Engineer\nLee,Design,Lead",
    "Product,Brand,Color\nShoe,Nike,Black\nBag,Gucci,Brown",
    "학생,담임,과목\n김민수,박선생,수학\n이서연,최선생,과학",
    "성명,소속,연락처\n김철수,기획팀,내선 1234\n이영희,재무팀,내선 5678",
    "제품명,제조사,원산지\n냉장고,가나전자,한국\n세탁기,다라전자,베트남",
    "질문,답변\n환불은 어떻게 하나요?,구매 후 7일 이내 신청\n배송은 얼마나 걸리나요?,보통 2~3일",
    # Typed columns, years, empty and trailing labels.
    "id,qty,price\n1,2,3.5\n2,4,1.25",
    "id,qty,price,\n1,2,3.5,\n2,4,1.25,",
    "Item,2022,2023,2024\nSales,10,12,15\nCosts,7,8,9",
    "Region,Q1,Q2,Q3\nNorth,10,12,15\nSouth,7,8,9",
    "date,value\n2024-01-01,3\n2024-01-02,5",
    "name\nalpha\nbeta\ngamma",
    "Date,Description,Amount\n2024-01-02,Coffee,$3.50\n2024-01-03,Rent,$1200.00",
])
def test_first_row_is_used_as_headings_without_record_evidence(text):
    notices, headings, first = _read(text)
    lines = text.splitlines()
    assert "csv_header_inferred" in notices
    assert headings == lines[0].split(",")
    assert first[:2] == lines[1].split(",")[:2]


def test_a_heading_row_repeated_by_the_export_is_not_record_evidence():
    notices, headings, _ = _read("Name,Status\nKim,open\nName,Status\nLee,closed")
    assert "csv_header_inferred" in notices
    assert headings == ["Name", "Status"]


@pytest.mark.parametrize("text", [
    "1,2,3\n4,5,6\n7,8,9",
    "apple,red,fruit\ncarrot,orange,vegetable\nbanana,yellow,fruit",
    "This first record is a long free-text note that clearly reads as data rather than a heading row,1\nNext,2",
    "only one record,with two fields",
])
def test_first_row_with_record_evidence_or_no_followers_stays_data(text):
    notices, headings, first = _read(text)
    assert "csv_header_unverified" in notices
    assert headings[0] == "Column 1"
    assert first == text.splitlines()[0].split(",")
