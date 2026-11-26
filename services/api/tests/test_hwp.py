"""HWP 5 rendering from record models (the shapes pyhwp produces), without a real .hwp file."""
import base64
import io
import re

import pytest

from app.hwp import render


class Flags(int):
    def __new__(cls, value=0, **fields):
        obj = super().__new__(cls, value)
        obj.__dict__.update(fields)
        return obj


def kind(name: str):
    return type(name, (), {})


def rec(record: str, level: int, **content) -> dict:
    return {"type": kind(record), "level": level, "content": content}


def para(level: int, text: str, char: int = 0, shape: int = 0, extra=()) -> list[dict]:
    chunks = [((0, len(text)), text), ((len(text), len(text) + 1), {"code": 13})]
    return [rec("Paragraph", level, parashape_id=shape, style_id=0), rec("ParaText", level + 1, chunks=chunks),
            rec("ParaCharShape", level + 1, charshapes=[(0, char)]), *extra]


class Stream:
    def __init__(self, data: bytes):
        self.data = data

    def open(self):
        return io.BytesIO(self.data)


DOCINFO = [
    rec("CharShape", 0, basesize=1000, charshapeflags=Flags(bold=0, italic=0)),
    rec("CharShape", 0, basesize=1600, charshapeflags=Flags(bold=1, italic=0)),
    rec("ParaShape", 0, parashapeflags=Flags(head_shape=0, level=0)),
    rec("ParaShape", 0, parashapeflags=Flags(head_shape=1, level=0)),
    rec("Style", 0, name="Normal", local_name="바탕글"),
    rec("BinData", 0, bindata={"storage_id": 1, "ext": "png"}),
]


def test_records_become_headings_tables_pictures_and_footnotes(tmp_path):
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (2000, 500), "gray").save(png, "PNG")
    table = [
        rec("TableControl", 1, chid="tbl ", width=40000, height=3000),
        rec("TableBody", 2, rows=2, cols=2),
        rec("TableCell", 2, paragraphs=1, col=0, row=0, colspan=2, rowspan=1, width=40000),
        *para(2, "합계 표"),
        rec("TableCell", 2, paragraphs=1, col=0, row=1, colspan=1, rowspan=1, width=10000),
        *para(2, "화재"),
        rec("TableCell", 2, paragraphs=1, col=1, row=1, colspan=1, rowspan=1, width=30000),
        *para(2, "12건"),
    ]
    picture = [
        rec("GShapeObjectControl", 1, chid="gso ", width=150000, height=37500),
        rec("ShapeComponent", 2), rec("ShapePicture", 3, picture={"bindata_id": 1}),
    ]
    note = [rec("FootNote", 1, chid="fn  "), rec("ListHeader", 2, paragraphs=1), *para(2, "각주 내용")]
    section = [
        rec("PageDef", 0, width=59528, height=84186, left_offset=5669, right_offset=5669, attr=0),
        *para(0, "재난 상황 분석", char=1, shape=1),
        *para(0, "본문 첫 문장입니다.", extra=table),
        *para(0, "그림 아래 문장.", extra=picture + note),
    ]
    page = render([section], DOCINFO, {"BIN0001.png": Stream(png.getvalue())}, tmp_path, lambda body: body)
    out = page.read_text()
    assert '<h1><span style="font-size:16.0pt"><b>재난 상황 분석</b></span></h1>' in out
    assert "<p>본문 첫 문장입니다.</p><table" in out
    assert '<td colspan="2" width="100%"><p>합계 표</p></td>' in out
    assert '<td width="25%"><p>화재</p></td><td width="75%"><p>12건</p></td>' in out
    picture = re.search(r'<img src="data:image/png;base64,([^"]+)" width="(\d+)" height="(\d+)"', out)
    assert picture and base64.b64decode(picture[1], validate=True) == png.getvalue()
    width, height = map(int, picture.groups()[1:])
    assert width <= 640 and abs(width / height - 4) < 0.1  # shrunk to the text width, aspect kept
    assert out.endswith('<div class="footnotes"><p>각주 내용</p></div>')


def test_source_tables_replace_tables_read_off_the_page():
    from app.hwpx import table_grid
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    # A label spanning two rows repeats; text spanning two columns is written once.
    rows = [[(' colspan="2"', "<p>연도별 목표치</p>"), ("", "단위")],
            [(' rowspan="2"', "공통지표"), ("", "50%"), ("", "%")],
            [("", "80점"), ("", "점")]]
    assert table_grid(rows) == [["연도별 목표치", "", "단위"], ["공통지표", "50%", "%"], ["공통지표", "80점", "점"]]

    source = [[["소 속", "성 명", "직 위", "연락처"], ["우주모빌리티과", "장 윤 영", "스마트시티팀장", "064-710-4821"],
               ["우주모빌리티과", "김 하 민", "주 무 관", "064-710-4826"]]]
    shifted = "| 소 속 | 성 명 | 직 위 | 연락처 |\n| --- | --- | --- | --- |\n| 우주모빌리티과 | 장 윤 영 | 스마트시티팀장 064-710-4821 |  |\n| 김 | 하 민 | 주 무 관 | 064-710-4826 |"
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.hwpx", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "b0000", "readingOrder": 0, "type": "table", "text": shifted, "markdown": shifted, "regions": [],
                    "source": {"parser": "docling"}}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 1
    assert doc.blocks[0].text.splitlines()[3] == "| 우주모빌리티과 | 김 하 민 | 주 무 관 | 064-710-4826 |"


def test_a_table_continued_on_a_page_gets_the_rows_printed_there():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    first = ["압축 여부", "암호 설정 여부", "배포용 문서 여부", "스크립트 저장 여부", "DRM 보안 문서 여부"]
    second = ["CCL 라이선스 정보", "복제 제한 여부", "동일 조건 하에 복제 허가 여부"]
    group = lambda names: [["DWORD", "4", "속성", ""], ["DWORD", "4", "범위", "설명"]] + \
        [["DWORD", "4", f"bit {i}", name] for i, name in enumerate(names)]
    source = [[["자료형", "길이(바이트)", "설명", ""], *group(first), *group(second), ["전체 길이", "256", "", ""]]]
    # The page after the first group: the second group's rows, which look like the first group's.
    shown = "\n".join(["| DWORD | 4 | 속성 |", "| --- | --- | --- |", "| DWORD | 4 | 범위 설명 |"]
                      + [f"| DWORD | 4 | bit {i} {name} |" for i, name in enumerate(second)] + ["| 전체 길이 | 256 | |"])
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.hwp", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": shown, "regions": [], "source": {"parser": "docling"}}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 1
    rows = doc.blocks[0].text.splitlines()
    assert rows[0] == "| 자료형 | 길이(바이트) | 설명 |  |" and "| DWORD | 4 | bit 1 | 복제 제한 여부 |" in rows
    assert not any(name in doc.blocks[0].text for name in first)


def test_short_rows_and_a_note_under_a_table_stay_with_it():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    title = "Sample CSV File - Numbers and their Squares"
    source = [[[title, "", ""], ["", "Number", "Square"], ["", "", ""],
               *[["", str(n), str(n * n)] for n in range(1, 16)], ["", "", ""], ["Written and saved in Apache OpenOffice.", "", ""]]]
    numbers = "\n".join(f"| {n} | {n * n} |" for n in range(1, 15))
    blocks = [{"id": "a", "readingOrder": 0, "type": "table", "regions": [], "source": {"parser": "docling"},
               "text": f"| {title} |  |\n| --- | --- |\n| Number | Square |\n{numbers}"},
              # The layout parser made the title and the note under the table a table of their own.
              {"id": "b", "readingOrder": 1, "type": "table", "regions": [], "source": {"parser": "docling"},
               "text": f"| {title} |\n| --- |\n| Written and saved in Apache OpenOffice. |"}]
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.csv", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}], "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 2
    # Rows too short to look for ("| 2 | 4 |") are no gap; the note comes along, the rows between do not.
    assert "|  | 2 | 4 |" in doc.blocks[0].text.splitlines() and "|  | 14 | 196 |" in doc.blocks[0].text.splitlines()
    assert "Written and saved in Apache OpenOffice." in doc.blocks[1].text and "196" not in doc.blocks[1].text


def test_rows_of_figures_are_placed_by_their_figures():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    import random

    header = ["Country Name", "Country Code", "Year", "Value"]
    rng = random.Random(7)
    names = [("".join(rng.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(7)).title(), f"C{i:02d}") for i in range(90)]
    rows = [[name, code, str(y), f"{rng.randrange(10**9, 10**10)}.{rng.randrange(10**6, 10**7)}"]
            for name, code in names for y in range(1990, 2024)]
    source = [[header, *rows]]
    # A later page of the table: the last rows of one country and the first of the next. Their
    # names and years are in many rows, their figures in one.
    page = rows[40 * 34 + 28:41 * 34 + 6]
    shown = "| " + " | ".join(header) + " |\n| --- | --- | --- | --- |\n" + "\n".join("| " + " | ".join(r) + " |" for r in page)
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.csv", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": shown, "regions": [], "source": {"parser": "docling"}}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 1
    lines = doc.blocks[0].text.splitlines()
    assert lines[2:] == ["| " + " | ".join(r) + " |" for r in page] and "Value" in lines[0]


def test_rows_whose_cells_mix_words_and_figures_are_placed_though_their_lines_part():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    address = "(우) 463-400 경기도 성남시 분당구 대왕판교로 644번길 49 한컴타워 10층 전화: (031) 627-7000 팩스: (031) 627-7709"
    colophon = [["한글 문서 파일 구조 - 수식", ""], ["", ""], ["발행처", "(주) 한글과컴퓨터"], ["주 소", address], ["", ""]]
    commands = [["명령어", "설명", "입력 예"], ["LSUB", "문자 왼쪽에 아래첨자를 표시해줍니다.", "x LSUB y"],
                ["LONGDIV", "나눗셈을 표현해줍니다.", "LONGDIV {6}{422}"],
                ["Color", "두 번째 인자에 내용을 넣어주면 지정한 색상으로 표시해줍니다.", "{COLOR {255,0,255} {3}} over {4}"]]
    blocks = [
        # The layout parser read each line of the address as a row of its own.
        {"id": "a", "readingOrder": 0, "type": "table", "regions": [], "source": {"parser": "docling"},
         "text": "| 한글 문서 발행처 | 파일 구조 - 수식 (주) 한글과컴퓨터 |\n| --- | --- |\n| 주 소 | (우) 463-400 |\n"
                 "| 주 소 | 경기도 성남시 분당구 대왕판교로 644 번길 49 한컴타워 10층 |\n| 주 소 | 전화: (031) 627-7000 |\n"
                 "| 주 소 | 팩스: (031) 627-7709 |"},
        # The last row goes on over the page: its example ends on the next one.
        {"id": "b", "readingOrder": 1, "type": "table", "regions": [], "source": {"parser": "docling"},
         "text": "| 명령어 | 설명 | 입력 예 |\n| --- | --- | --- |\n| LSUB | 문자 왼쪽에 아래첨자를 표시해줍니다. | x LSUB y |\n"
                 "| LONGDIV | 나눗셈을 표현해줍니다. | LONGDIV {6}{422} |\n| Color | 두 번째 인자에 내용을 | {COLOR {255,0,255} {3}} |"}]
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.hwp", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}], "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [colophon, commands]) == 2
    assert f"| 주 소 | {address} |" in doc.blocks[0].text.splitlines()
    assert doc.blocks[1].text.splitlines()[-1] == "| Color | 두 번째 인자에 내용을 | {COLOR {255,0,255} {3}} over {4} |"
    assert "지정한 색상" not in doc.blocks[1].text


def test_a_row_going_on_over_the_page_stays_with_the_page_it_starts_on():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    names = ["초거대 언어모델 학습 기술", "다층적 상황 이해 추론 기술", "뇌 인지과학 기반 학습 기술", "기억 메커니즘 모사 기술",
             "컴퓨팅자원 집중형 인공지능 응용 기술 개발 (3개 과제 × 15억원)", "통합 감각 인공지능 모델 개발"]
    source = [[["번호", "과제명", "분야"], *[[str(i + 10), name, f"분야{i}"] for i, name in enumerate(names)]]]
    shown = "| 번호 | 과제명 | 분야 |\n| --- | --- | --- |\n" + "\n".join(f"| {i + 10} | {n} | 분야{i} |" for i, n in enumerate(names[:4]))
    # The page ends inside the fifth row: its first lines are printed here, the rest on the next page.
    shown += "\n| 14 | 컴퓨팅자원 집중형 인공지능 | 분야4 |"
    for foot, expected in ((0.95, True), (0.6, False)):
        doc = ParsedDocument.model_validate({
            "document": {"id": "x", "filename": "x.hwp", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
            "pages": [{"index": 0, "width": 595, "height": 842}],
            "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": shown, "source": {"parser": "docling"},
                        "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": foot}}]}],
            "blocksFingerprint": "f"})
        # Away from the foot, a partial row has no reliable placement. Keep
        # its PDF text rather than accepting a match that would drop the row.
        assert apply_source_tables(doc, source) == int(expected)
        assert "| 14 | 컴퓨팅자원 집중형 인공지능 | 분야4 |" in doc.blocks[0].text
        # A placed source row still contains only this page's printed cell fragment.
        assert "3개 과제" not in doc.blocks[0].text
        assert "컴퓨팅자원 집중형 인공지능" in doc.blocks[0].text
        assert names[5] not in doc.blocks[0].text


def test_a_document_table_keeps_a_column_whose_merged_label_is_printed_on_another_page():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    items = ["개인 맞춤형 식품 솔루션", "고령친화 식품 개발", "식품 안전 관리 플랫폼", "스마트 양식 시스템", "대체 단백질 소재"]
    source = [[["분야", "전략분야", "전략품목"], *[["바이오 헬스 산업군 (40개 품목)", "기능성식품 (5개 품목)", item] for item in items]]]
    # The label merged over all the rows is printed on the page before; this page shows the rest.
    shown = "| 전략분야 | 전략품목 |\n| --- | --- |\n" + "\n".join(f"| 기능성식품 (5개 품목) | {item} |" for item in items)
    for overflow, kept in ((False, True), (True, False)):
        doc = ParsedDocument.model_validate({
            "document": {"id": "x", "filename": "x.hwp", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
            "pages": [{"index": 0, "width": 595, "height": 842}],
            "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": shown, "source": {"parser": "docling"},
                        "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.5}}]}],
            "blocksFingerprint": "f"})
        assert apply_source_tables(doc, source, columns_overflow=overflow) == 1
        assert ("바이오 헬스 산업군 (40개 품목)" in doc.blocks[0].text) is kept


def test_a_sheet_page_read_column_by_column_becomes_its_rows():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    names = ["applicant name", "father name", "gender", "marital status", "date of birth", "nationality",
             "residential status", "proof of identity", "permanent address", "correspondence city", "state code",
             "pin code", "country code", "office phone", "mobile number", "email address", "occupation", "annual income",
             "net worth", "politically exposed"]
    sheet = [["S.No", "Field", "Field Type", "Length", "New", "Old", "Non-Individual", "Existing", "XML Tag"]]
    sheet += [[str(i + 1), name.upper(), "varchar" if i % 3 else "integer", str(10 + 5 * i),
               "M" if i % 2 else "O", "NULL" if i % 3 else "M", "M" if i % 4 else "NULL", "O" if i % 5 else "M",
               "APP_" + "_".join(w[:4].upper() for w in name.split())] for i, name in enumerate(names)]
    # The layout parser read the page column by column into one paragraph.
    columns = " ".join(" ".join(r[c] for r in [sheet[0]] + sheet[8:16]) for c in range(9))
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xlsx", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 842, "height": 595}],
        "blocks": [{"id": "p", "readingOrder": 0, "type": "paragraph", "text": columns, "source": {"parser": "docling"},
                    "regions": [{"pageIndex": 0, "bbox": {"x0": 0.05, "y0": 0.05, "x1": 0.95, "y1": 0.9}}]}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [sheet], sheets=True) == 1
    rows = doc.blocks[0].text.splitlines()
    assert doc.blocks[0].type == "table" and rows[0] == "| " + " | ".join(sheet[0]) + " |"
    assert rows[2] == "| " + " | ".join(sheet[8]) + " |" and rows[-1] == "| " + " | ".join(sheet[15]) + " |"


def test_rows_are_placed_by_figures_with_words_in_them_found_whole():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    header = ["Code", "Month", "Series", "Settle", "Open", "High", "Low", "Volume"]
    rows = []
    for label, month, base in (("December, 2014", "31/12/2014", 1800), ("March, 2015", "31/03/2015", 1800),
                               ("June, 2015", "30/06/2015", 1800)):
        for k in range(12):
            rows.append([f"MYOKLIP{len(rows):02d}P{30 + k}", month[-4:] + month[3:5], f"OKLI {label} Put {month} {base + 20 * k:,}",
                         f"{20.1 + 7.3 * k:.2f}", "0.00", "0.00", "0.00", "0"])
    # The page prints rows 14-21 without the code and month columns.
    page = rows[14:22]
    shown = "| Series | Settle | Open | High | Low | Volume |\n| --- | --- | --- | --- | --- | --- |\n" + "\n".join(
        "| " + " | ".join(r[2:]) + " |" for r in page)
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.csv", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 842, "height": 595}],
        "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": shown, "source": {"parser": "docling"},
                    "regions": [{"pageIndex": 0, "bbox": {"x0": 0.05, "y0": 0.05, "x1": 0.95, "y1": 0.5}}]}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [[header, *rows]]) == 1
    text = doc.blocks[0].text
    assert all(r[2] in text for r in page) and rows[13][2] not in text and rows[22][2] not in text


def test_tables_of_an_html_export_become_grids(tmp_path):
    from app.office_tables import html_tables

    page = tmp_path / "export.html"
    page.write_text("""<html><body><table>
      <tr><td colspan="2">Totals<!-- note --></td><td rowspan="2">Unit\x07</td></tr>
      <tr><td>Cost</td><td>$889,017 &#xf094;</td></tr>
      <tr><td>Nested<table><tr><td>inner</td><td>table</td></tr><tr><td>row two</td></tr></table>after</td><td></td><td>x</td></tr>
    </table></body></html>""", encoding="utf-8")
    outer, inner = html_tables(page)
    # The nested table is a table of its own and stays part of its cell's text.
    assert outer == [["Totals", "", "Unit"], ["Cost", "$889,017", "Unit"], ["Nested inner table row two after", "", "x"]]
    assert inner == [["inner", "table"], ["row two", ""]]


def test_a_long_sheet_keeps_its_first_rows_and_the_sheets_after_it(tmp_path, monkeypatch):
    from app import office_tables

    monkeypatch.setattr(office_tables, "MAX_ROWS", 3)
    rows = "".join(f"<tr><td>{i}</td><td>row {i}</td></tr>" for i in range(10))
    page = tmp_path / "export.html"
    page.write_text(f"<html><body><table>{rows}</table><table><tr><td>next</td><td>sheet</td></tr></table></body></html>")
    first, second = office_tables.html_tables(page)
    assert first == [["0", "row 0"], ["1", "row 1"], ["2", "row 2"]]
    assert second == [["next", "sheet"]]


def test_the_pages_of_a_workbook_become_the_parts_of_its_sheet_they_print():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["Cabbage exports by country", "", ""], ["Country", "Production", "Export value"]]
    sheet += [[f"Country {chr(65 + i)}{chr(65 + i)}", f"{1000 + 37 * i:,}", f"{52000 + 911 * i:,}"] for i in range(40)]

    def block(id_, page, text, y, kind="paragraph", x=(0.1, 0.9)):
        return {"id": id_, "readingOrder": 0, "type": kind, "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": page, "bbox": {"x0": x[0], "y0": y, "x1": x[1], "y1": y + 0.02}}]}

    def row(r):
        return " ".join(sheet[r])

    blocks = [block("t", 0, sheet[0][0], 0.02, "section_header"),
              # The layout parser ran columns together on the first page and saw lines elsewhere.
              block("a", 0, "Country Production Export value " + " ".join(row(r) for r in range(2, 22)), 0.05, "table"),
              block("chart", 0, "", 0.6, "picture"), block("label", 0, sheet[3][1], 0.7, x=(0.3, 0.4)),
              *[block(f"p{r}", 1, row(r), 0.02 * r) for r in range(22, 42)]]
    blocks[2]["regions"][0]["bbox"] = {"x0": 0.1, "y0": 0.55, "x1": 0.9, "y1": 0.9}
    for i, b in enumerate(blocks):
        b["readingOrder"] = i
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xlsx", "fileHash": "h", "pageCount": 2, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 842, "height": 595}, {"index": 1, "width": 842, "height": 595}],
        "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [sheet], sheets=True) == 2
    title, first, picture, second = doc.blocks
    # The sheet's title row is a heading; the table's header row is the column names.
    assert title.type == "section_header" and title.text == "Cabbage exports by country"
    assert first.type == "table" and first.text.splitlines()[:3] == [
        "| Country | Production | Export value |", "| --- | --- | --- |", "| Country AA | 1,000 | 52,000 |"]
    assert first.text.splitlines()[-1] == "| Country TT | 1,703 | 69,309 |"
    # The chart's label repeats a cell and goes; the second page repeats the header row.
    assert picture.type == "picture"
    assert second.text.splitlines()[:3] == ["| Country | Production | Export value |", "| --- | --- | --- |", "| Country UU | 1,740 | 70,220 |"]
    assert len(second.regions) == 20 and [b.reading_order for b in doc.blocks] == [0, 1, 2, 3]


def test_cells_of_a_page_its_sheet_part_does_not_hold_stay():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["", "Report Name", "Expense report", "", "Report Date", "01-Nov-26", "", "", ""],
             ["", "", "", "", "", "", "", "", ""],
             ["", "Business Purpose", "Example purpose", "", "Comment", "", "", "", ""],
             ["", "", "", "", "", "", "", "", ""],
             ["", "Expense Type", "Amount", "Currency", "Business Purpose", "Comment", "City", "State", "Vendor"]]

    def block(id_, text, y, kind="paragraph"):
        return {"id": id_, "readingOrder": 0, "type": kind, "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": y, "x1": 0.9, "y1": y + 0.02}}]}

    # The right half of the sheet: two cells of the rows above, then the column names.
    blocks = [block("d", "Report Date", 0.1), block("v", "01-Nov-26", 0.13), block("c", "Comment", 0.16),
              block("t", "| Business Purpose | Comment | City | State | Vendor |\n| --- | --- | --- | --- | --- |", 0.2, "table")]
    for i, b in enumerate(blocks):
        b["readingOrder"] = i
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xls", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 842, "height": 595}], "blocks": blocks, "blocksFingerprint": "f"})
    apply_source_tables(doc, [sheet], sheets=True)
    text = " ".join(b.text for b in doc.blocks)
    assert "Report Date" in text and "01-Nov-26" in text and "Vendor" in text


def test_column_names_atop_every_printed_page_do_not_place_the_page(tmp_path):
    import pymupdf

    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["No", "Field", "Type", "Length", "New", "New entity", "Old", "Old entity", "Remarks", "Tag"]]
    sheet += [[str(i), f"FIELD {i}", "DATE", "", "O", "O", "M", "M", "", f"TAG_{i}"] for i in range(1, 80)]
    # Page 2 prints the column names again, then rows 71 and 72; the layout parser ran each row's
    # number into its field. The text layer has the cells apart.
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    for y, row in ((100, sheet[0]), (130, sheet[71]), (160, sheet[72])):
        for x, cell in zip((20, 45, 120, 160, 200, 240, 290, 330, 380, 460), row):
            if cell:
                page.insert_text((x, y), cell, fontsize=9)
    pdf.save(tmp_path / "source.pdf")
    table = ("| No Field | Type | Length | New | New entity | Old | Old entity | Remarks | Tag |\n" + "| --- " * 9 + "|\n"
             "| 71FIELD 71 | DATE |  | O O | M | M |  |  | TAG_71 |\n| 72FIELD 72 | DATE |  | O O | M | M |  |  | TAG_72 |")
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xlsx", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 600, "height": 800}],
        "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": table, "source": {"parser": "docling"},
                    "regions": [{"pageIndex": 0, "bbox": {"x0": 0.05, "y0": 0.1, "x1": 0.9, "y1": 0.22}}]}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [sheet], sheets=True, pdf=tmp_path / "source.pdf") == 1
    assert [row.split(" | ")[:2] for row in doc.blocks[0].text.splitlines()[2:]] == [["| 71", "FIELD 71"], ["| 72", "FIELD 72"]]


def test_a_figure_the_sheet_part_does_not_hold_stays_though_its_digits_are_in_it():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["Kurs", "Wert"], ["1.0000", "21.16"], ["1.0000", "17.93"], ["1.0000", "29.06"]]
    sheet += [[f"1.{i:04d}", f"{40 + i}.25"] for i in range(1, 40)] + [["1.2681", "16.17"]]

    def block(id_, kind, text, y):
        return {"id": id_, "readingOrder": 0, "type": kind, "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": y, "x1": 0.5, "y1": y + 0.02}}]}

    table = "| Kurs | Wert |\n| --- | --- |\n| 1.0000 | 21.16 |\n| 1.0000 | 17.93 |\n| 1.0000 | 29.06 |"
    blocks = [block("t", "table", table, 0.1), block("p", "paragraph", "16.17", 0.3)]
    for i, b in enumerate(blocks):
        b["readingOrder"] = i
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xls", "fileHash": "h", "pageCount": 1, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}], "blocks": blocks, "blocksFingerprint": "f"})
    apply_source_tables(doc, [sheet], sheets=True)
    assert "| 1.0000 | 17.93 |" in doc.blocks[0].text and any(b.text == "16.17" for b in doc.blocks)


def test_tables_get_a_line_per_row_spread_over_their_pages():
    from app.models import ParsedDocument
    from app.parsers.base import add_table_lines

    text = "| k | v |\n| --- | --- |\n" + "\n".join(f"| r{i} | {i} |" for i in range(8))
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.pdf", "fileHash": "h", "pageCount": 2, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}, {"index": 1, "width": 595, "height": 842}],
        "blocks": [{"id": "t", "readingOrder": 0, "type": "table", "text": text, "markdown": text, "source": {"parser": "docling"},
                    "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.5, "x1": 0.9, "y1": 0.9}},
                                {"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.3}}]}],
        "blocksFingerprint": "f"})
    add_table_lines(doc)
    lines = doc.blocks[0].lines
    assert [text[ln.start:ln.end] for ln in lines] == [text.split("\n")[0], *text.split("\n")[2:]]
    # Twice the height on the first page holds about twice the rows.
    assert [ln.page_index for ln in lines].count(0) == 6 and lines[-1].page_index == 1
    assert all(len(ln.xs) == ln.end - ln.start for ln in lines)


def test_text_without_positions_gets_lines_filling_its_regions():
    from app.models import ParsedDocument
    from app.parsers.base import add_text_lines

    text = "첫 문장입니다. " * 40
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.pdf", "fileHash": "h", "pageCount": 1, "parser": "docling-ocr", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "p", "readingOrder": 0, "type": "paragraph", "text": text, "source": {"parser": "docling-ocr"},
                    "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.36}}]}],
        "blocksFingerprint": "f"})
    add_text_lines(doc)
    lines = doc.blocks[0].lines
    assert len(lines) == 10 and lines[0].start == 0 and lines[-1].end == len(text)
    assert all(a.end == b.start for a, b in zip(lines, lines[1:]))
    assert all(len(ln.xs) == ln.end - ln.start for ln in lines)


def test_a_header_row_left_on_the_page_before_its_table_joins_it():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    source = [[["구 분", "한국형발사체(누리호)", "시험발사체"], ["총 길이", "47.2 m", "25.8 m"], ["최대 직경", "3.5 m", "2.6 m"]]]
    shown = "| 총 길이 | 47.2 m | 25.8 m |\n| --- | --- | --- |\n| 최대 직경 | 3.5 m | 2.6 m |"

    def block(id_, kind, text, page):
        return {"id": id_, "readingOrder": 0, "type": kind, "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.8, "x1": 0.3, "y1": 0.82}}]}

    blocks = [block("a", "paragraph", "붙임 한국형발사체(누리호)와 시험발사체 비교", 0), block("b", "paragraph", "구 분", 0),
              block("c", "paragraph", "한국형발사체(누리호)", 0), block("d", "paragraph", "시험발사체", 0),
              block("e", "page_footer", "- 2 -", 0), block("t", "table", shown, 1)]
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.hwp", "fileHash": "h", "pageCount": 2, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 595, "height": 842}, {"index": 1, "width": 595, "height": 842}],
        "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 1
    assert [b.id for b in doc.blocks] == ["a", "e", "t"]
    table = doc.blocks[2]
    assert table.text.splitlines()[0] == "| 구 분 | 한국형발사체(누리호) | 시험발사체 |" and len(table.regions) == 4


def test_a_piece_cut_off_a_table_joins_it_only_nearby_and_when_the_table_says_it():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    source = [[["Item", "Cost"], ["Rent", "1,200"], ["Food", "300"],
               ["Vendor City Country, with a note on travel the page does not print", "x"]]]

    def block(id_, text, page):
        return {"id": id_, "readingOrder": 0, "type": "table", "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.8, "x1": 0.3, "y1": 0.82}}]}

    far = "| Vendor | City | Country |\n| --- | --- | --- |"
    blocks = [block("f", "| Item |\n| --- |", 0), block("t", "| Rent | 1,200 |\n| --- | --- |\n| Food | 300 |", 1),
              block("g", far, 4)]
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xls", "fileHash": "h", "pageCount": 5, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": i, "width": 595, "height": 842} for i in range(5)],
        "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, source) == 1
    # The header left on the page before joins its table; a table three pages on keeps its text.
    assert [b.id for b in doc.blocks] == ["t", "g"]
    assert [r.page_index for r in doc.blocks[0].regions] == [0, 1]
    assert doc.blocks[1].text == far


def test_a_note_on_a_sheet_cell_is_not_part_of_its_text(tmp_path):
    from app.office_tables import html_tables

    page = tmp_path / "export.html"
    page.write_text("""<html><body><table><tr><td><a class="comment-indicator"></a>
      <comment>Select a number to add the same row several times.</comment>Item?</td><td>Amount</td></tr>
    </table></body></html>""", encoding="utf-8")
    assert html_tables(page) == [[["Item?", "Amount"]]]
    # A figure written with places it may wrap at is one figure.
    page.write_text('<html><head><meta charset="utf-8"></head><body><table><tr><td>352141\u200b8059.9\u200b23445</td>'
                    "<td>x</td></tr></table></body></html>", encoding="utf-8")
    assert html_tables(page) == [[["3521418059.923445", "x"]]]


def test_a_sheet_printed_small_and_taken_for_a_picture_is_its_cells():
    import pymupdf

    from app.parsers.hybrid_parser import _cells_taken_for_pictures

    pdf = pymupdf.open()
    pdf.new_page(width=595, height=842)
    pdf.new_page(width=595, height=842).insert_image(pymupdf.Rect(100, 100, 300, 300),
                                                     pixmap=pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), 0))
    rows = [f"{n}.00 0.{n:02d} $ {6.16 * n:.2f} 0.02 $ {4.15 * n:.2f} 0.01 $ {2.91 * n:.2f}" for n in range(1, 31)]
    text = " ".join(rows)

    def picture(page, text, rows):
        return {"type": "picture", "text": text, "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.6, "y1": 0.4}}],
                "_lines": [{"pageIndex": page, "start": 0, "end": 1} for _ in range(rows)]}

    cells, chart, photo = picture(0, text, 30), picture(0, "Sales 2019 2020 2021 January February March", 4), picture(1, text, 30)
    _cells_taken_for_pictures([cells, chart, photo], pdf)
    assert (cells["type"], chart["type"], photo["type"]) == ("paragraph", "picture", "picture")
    assert cells["lines"] == cells["_lines"]


def test_a_table_is_drawn_with_lines_when_each_row_fits_a_chunk():
    from app import hwpx

    hwpx.SOURCE_TABLES.clear()
    # A requirement sheet: one long description, short rows otherwise.
    sheet = hwpx.table_html([[("", "요구사항 고유번호"), ("", "SFR-010")], [("", "세부내용"), ("", "데이터를 수집한다. " * 30)]], 0, 0)
    # A form: a row holding whole paragraphs stays text that can be cut at sentences.
    form = hwpx.table_html([[("", "추진 배경"), ("", "정책 목표를 설명하는 문단입니다. " * 30)], [("", "비고"), ("", "-")]], 0, 0)
    # A frame: a title tab between spacer cells, over the one cell that holds a boxed summary.
    frame = hwpx.table_html([[("", ""), (' rowspan="2"', "<p>대설 피해현황</p>"), ("", "")], [("", " "), ("", "")],
                             [(' colspan="3"', "<p>▷ 지역 : 11개 시·도</p>")]], 0, 0)
    assert sheet.startswith('<table width="100%" border="1"') and form.startswith('<table width="100%" border="0"')
    assert frame == '<div class="box"><div><p>대설 피해현황</p></div><div><p>▷ 지역 : 11개 시·도</p></div></div>'
    assert len(hwpx.SOURCE_TABLES) == 1 and hwpx.SOURCE_TABLES[0][0] == ["요구사항 고유번호", "SFR-010"]


def test_hancom_private_use_numbers_become_circled_digits():
    from app.parsers.base import HANCOM
    from app.parsers.textlayer import private_glyph

    assert "\U000f02b1 (보안성) \U000f02b4 (AI 운영) 글".translate(HANCOM) == "① (보안성) ④ (AI 운영) 한글"
    assert private_glyph("NotoSansCJKkr-Bold", "\U000f02b6") == "⑥"


def test_a_sheet_printed_over_pages_becomes_tables_where_its_rows_can_be_placed():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["0", "data_year", "valueA", "total"]]
    sheet += [[str(i), "2013", "Alabama", str(1000 + i * 37)] for i in range(1, 41)]
    sheet += [["0", "2013", "Arkansas", "0"]] * 40
    page1 = " ".join(" ".join(row[:3]) for row in sheet[:41])
    page2 = " ".join(" ".join(row[:3]) for row in sheet[41:])
    text = f"{page1} {page2}"
    lines = [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.9}, "start": 0, "end": len(page1), "xs": [0.1] * len(page1)},
             {"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.9}, "start": len(page1) + 1, "end": len(text),
              "xs": [0.1] * len(page2)}]
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xlsx", "fileHash": "h", "pageCount": 2, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 842, "height": 595}, {"index": 1, "width": 842, "height": 595}],
        "blocks": [{"id": "p", "readingOrder": 0, "type": "paragraph", "text": text, "source": {"parser": "docling"}, "lines": lines,
                    "regions": [{"pageIndex": p, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.9}} for p in (0, 1)]}],
        "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [sheet], sheets=True) == 1
    table, rest = doc.blocks
    # The first page's rows are placed by row number and year side by side, three columns of four.
    assert table.type == "table" and table.text.splitlines()[0] == "| 0 | data_year | valueA |" and len(table.text.splitlines()) == 42
    # Forty identical rows cannot be placed: that page keeps its text.
    assert rest.type == "paragraph" and rest.text == page2 and [r.page_index for r in rest.regions] == [1]


def test_a_column_of_one_character_codes_stays_on_the_pages_after_the_first():
    from app.models import ParsedDocument
    from app.parsers.source_tables import apply_source_tables

    sheet = [["TYPE", "CODE", "NAME", "STREET"]]
    sheet += [["F" if i < 20 else "I", f"K{1000 + 37 * i}", f"Person {chr(65 + i % 26)}{i}", f"Road {i + 3}"] for i in range(60)]

    def block(id_, page, text, y, x=(0.1, 0.9)):
        return {"id": id_, "readingOrder": 0, "type": "paragraph", "text": text, "source": {"parser": "docling"},
                "regions": [{"pageIndex": page, "bbox": {"x0": x[0], "y0": y, "x1": x[1], "y1": y + 0.01}}]}

    # Page 1 reads row by row; page 2 has no header row, and its column of codes reads on its own.
    blocks = [*[block(f"a{r}", 0, " ".join(sheet[r]), 0.01 * r) for r in range(0, 31)],
              block("codes", 1, " ".join(sheet[r][0] for r in range(31, 61)), 0.01, x=(0.02, 0.05)),
              *[block(f"b{r}", 1, " ".join(sheet[r][1:]), 0.01 * (r - 30), x=(0.1, 0.9)) for r in range(31, 61)]]
    for i, b in enumerate(blocks):
        b["readingOrder"] = i
    doc = ParsedDocument.model_validate({
        "document": {"id": "x", "filename": "x.xls", "fileHash": "h", "pageCount": 2, "parser": "hybrid", "parserVersion": "t"},
        "pages": [{"index": 0, "width": 612, "height": 792}, {"index": 1, "width": 612, "height": 792}],
        "blocks": blocks, "blocksFingerprint": "f"})
    assert apply_source_tables(doc, [sheet], sheets=True) == 2
    first, second = doc.blocks
    assert first.text.splitlines()[0] == "| TYPE | CODE | NAME | STREET |"
    assert second.text.splitlines()[0] == "| TYPE | CODE | NAME | STREET |"
    assert second.text.splitlines()[2] == f"| I | K{1000 + 37 * 30} | Person E30 | Road 33 |"


def test_source_table_cells_read_hancom_private_codes():
    from app.hwpx import table_grid

    rows = [[("", "구분"), ("", "설명")], [("", "WCHAR"), ("", "\uf53a글의 기본 코드")]]
    assert table_grid(rows) == [["구분", "설명"], ["WCHAR", "한글의 기본 코드"]]


@pytest.mark.parametrize("oversized_member", [False, True])
def test_repeated_hwp_record_pictures_use_one_original_in_the_package(tmp_path, monkeypatch, oversized_member):
    import zipfile
    from lxml import etree
    from PIL import Image
    from app import hangul_pictures
    from app.config import get_settings
    from app.convert import page_html, run_soffice

    office = get_settings().soffice_path
    if not office:
        pytest.skip("LibreOffice not installed")
    monkeypatch.setattr(hangul_pictures, "INLINE_REPEAT_LIMIT", 1)
    image = io.BytesIO()
    Image.new("RGB", (16, 32), "green").save(image, "PNG")
    shape = [rec("GShapeObjectControl", 1, chid="gso ", width=1200, height=2400),
             rec("ShapeComponent", 2), rec("ShapePicture", 3, picture={"bindata_id": 1})]
    section = para(0, "First", extra=shape) + para(0, "Second", extra=shape)
    pictures = hangul_pictures.EmbeddedPictures(tmp_path)
    if oversized_member:
        monkeypatch.setattr(hangul_pictures, "MAX_IMAGE", len(image.getvalue()))
        assert pictures.get("oversized", lambda: io.BytesIO(image.getvalue() + b"!")) is None
    page = render([section], DOCINFO, {"BIN0001.png": Stream(image.getvalue())}, tmp_path, page_html, pictures)
    assert pictures.pack
    layout = run_soffice(str(office), page, tmp_path, "writerweb8_writer", None, 30, target="odt")
    with zipfile.ZipFile(pictures.fill_package(layout)) as package:
        members = [n for n in package.namelist() if n.startswith("Pictures/")]
        assert len(members) == 1 and package.read(members[0]) == image.getvalue()
        root = etree.fromstring(package.read("content.xml"))
        assert root.xpath('//*[local-name()="image"]/@*[local-name()="href"]') == members * 2
    assert pictures.omitted is oversized_member
