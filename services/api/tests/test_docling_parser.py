"""Docling is the default parser, so its adapter is tested on the bundled samples (about 30 s)."""
from pathlib import Path

import pytest

from app.config import get_settings
from app.parsers import docling_parser

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
MODELS = get_settings().models_dir

pytestmark = pytest.mark.skipif(not (MODELS / "docling-project--docling-layout-heron").exists(),
                                reason="Docling models not downloaded (scripts/download-models.sh)")


def parse(name: str):
    progress = []
    doc = docling_parser.parse(SAMPLES / name, file_meta={"id": "t", "filename": name, "fileHash": "h"},
                               models_dir=MODELS, threads=4, ocr=False,
                               progress=lambda done, total: progress.append((done, total)))
    return doc, progress


@pytest.fixture(scope="module")
def english():
    return parse("chunking-field-guide.pdf")


@pytest.fixture(scope="module")
def korean():
    return parse("chunking-guide-ko.pdf")


def test_reports_progress_and_pages(english):
    doc, progress = english
    assert progress[-1] == (2, 2)
    assert [p.index for p in doc.pages] == [0, 1]
    assert doc.document.parser == "docling" and doc.document.page_count == 2


def test_structure_is_detected(english):
    doc, _ = english
    types = [b.type for b in doc.blocks]
    for expected in ("section_header", "paragraph", "list_item", "table", "caption", "page_header", "page_footer"):
        assert expected in types, expected
    table = next(b for b in doc.blocks if b.type == "table")
    assert table.text.startswith("| Content type |") and "| --- |" in table.text
    # The caption stays a separate block right after its table, not duplicated inside it.
    after = doc.blocks[doc.blocks.index(table) + 1]
    assert after.type == "caption" and "Table 1." not in table.text


def test_ids_order_and_coordinates(english, korean):
    for doc, _ in (english, korean):
        assert [b.id for b in doc.blocks] == [f"b{i:04d}" for i in range(len(doc.blocks))]
        assert [b.reading_order for b in doc.blocks] == list(range(len(doc.blocks)))
        for b in doc.blocks:
            for r in b.regions:
                assert 0 <= r.bbox.x0 < r.bbox.x1 <= 1 and 0 <= r.bbox.y0 < r.bbox.y1 <= 1
                assert r.page_index in (0, 1)


def test_paragraph_continued_on_next_page_is_one_block(korean):
    doc, _ = korean
    merged = [b for b in doc.blocks if b.type == "paragraph" and len({r.page_index for r in b.regions}) == 2]
    assert len(merged) == 1
    assert "업로드한 원본과 파싱 결과는" in merged[0].text and "편집 상태가 모두 자동으로 삭제됩니다" in merged[0].text


def test_korean_caption_is_recognized(korean):
    doc, _ = korean
    caption = next(b for b in doc.blocks if b.text.startswith("표 1."))
    assert caption.type == "caption"


def test_fingerprint_is_stable(english):
    doc, _ = english
    again, _ = parse("chunking-field-guide.pdf")
    assert again.blocks_fingerprint == doc.blocks_fingerprint


@pytest.mark.parametrize("parser", ["docling", "hybrid"])
def test_legitimate_repeated_phrase_is_preserved(tmp_path, parser):
    import pymupdf
    from app.parsers import hybrid_parser
    pdf = pymupdf.open()
    page = pdf.new_page()
    phrase = "Repeat this statement. Repeat this statement."
    page.insert_text((72, 100), phrase, fontsize=12)
    path = tmp_path / "repeat.pdf"
    pdf.save(path)
    module = hybrid_parser if parser == "hybrid" else docling_parser
    doc = module.parse(path, file_meta={"id": "repeat", "filename": path.name, "fileHash": "h"},
                       models_dir=MODELS, threads=2, ocr=False, progress=lambda *a: None)
    assert " ".join(b.text for b in doc.blocks).count("Repeat this statement.") == 2


# --- Hybrid: Docling structure + PyMuPDF text layer --------------------------------------------

from app.parsers import hybrid_parser  # noqa: E402
from app.parsers.textlayer import Char, Fragment, PageText  # noqa: E402


@pytest.fixture(scope="module")
def hybrid_en():
    return hybrid_parser.parse(SAMPLES / "chunking-field-guide.pdf", file_meta={"id": "t", "filename": "a.pdf", "fileHash": "h"},
                               models_dir=MODELS, threads=4, progress=lambda d, t: None)


@pytest.fixture(scope="module")
def hybrid_ko():
    return hybrid_parser.parse(SAMPLES / "chunking-guide-ko.pdf", file_meta={"id": "t", "filename": "k.pdf", "fileHash": "h"},
                               models_dir=MODELS, threads=4, progress=lambda d, t: None)


def test_hybrid_recovers_title_and_heading_levels(hybrid_en, hybrid_ko):
    doc = hybrid_en
    by_text = {b.text: b for b in doc.blocks}
    assert by_text["The Chunking Field Guide"].type == "title"
    assert by_text["1. Why chunking matters"].level == 1
    assert by_text["3.1 Rules that usually help"].level == 2
    assert by_text["3.1 Rules that usually help"].markdown == "### 3.1 Rules that usually help"
    ko = {b.text: b for b in hybrid_ko.blocks}
    assert ko["문서 청킹 운영 가이드"].type == "title"
    assert ko["제2장 청크 기준"].level == 1 and ko["2.1 기본 크기"].level == 2
    assert doc.document.parser == "hybrid" and "pymupdf" in doc.document.parser_version


def test_hybrid_lines_map_text_to_positions(hybrid_en, hybrid_ko):
    for doc in (hybrid_en, hybrid_ko):
        lined = [b for b in doc.blocks if b.lines]
        assert len(lined) >= 15
        for b in lined:
            assert b.type in ("paragraph", "list_item", "caption", "footnote")
            prev_end = 0
            for ln in b.lines:
                assert len(ln.xs) == ln.end - ln.start > 0
                assert ln.start >= prev_end and b.text[ln.start:ln.end] == b.text[ln.start:ln.end].strip()
                assert all(ln.bbox.x0 - 1e-4 <= x <= ln.bbox.x1 for x in ln.xs)
                assert ln.xs == sorted(ln.xs)
                prev_end = ln.end
            # Every line lies inside one of the block's regions on its page.
            for ln in b.lines:
                assert any(r.page_index == ln.page_index and r.bbox.y0 - 0.01 <= (ln.bbox.y0 + ln.bbox.y1) / 2 <= r.bbox.y1 + 0.01
                           for r in b.regions)


def test_hybrid_keeps_numbered_markers_and_cross_page_lines(hybrid_en, hybrid_ko):
    numbered = [b for b in hybrid_ko.blocks if b.type == "list_item" and b.markdown.startswith("1.")]
    assert numbered and not numbered[0].text.startswith("1.")
    multi = [b for b in hybrid_ko.blocks if b.lines and len({ln.page_index for ln in b.lines}) == 2]
    assert len(multi) == 1 and multi[0].text.endswith("자동으로 삭제됩니다.")
    assert not any(b.source.ref == "recovered" for b in hybrid_en.blocks + hybrid_ko.blocks)


def _char(c, x, y):
    return Char(c=c, x0=x, y0=y, x1=x + 0.01, y1=y + 0.015, size=10, bold=False)


def test_text_no_block_covers_is_recovered_in_reading_order():
    covered = Fragment([_char("A", 0.1, 0.1)])
    covered.chars[0].used = True
    missed = Fragment([_char(c, 0.1 + i * 0.01, 0.30) for i, c in enumerate("Lost text")])
    page = PageText(index=0, width=595, height=842, fragments=[covered, missed])
    above = {"type": "paragraph", "text": "Above", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}}], "source": {"parser": "docling"}}
    below = {"type": "paragraph", "text": "Below", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.5, "x1": 0.9, "y1": 0.6}}], "source": {"parser": "docling"}}
    stats = {"recovered": 0}
    out = hybrid_parser._recover_uncovered([above, below], [page], stats)
    assert [b["text"] for b in out] == ["Above", "Lost text", "Below"]
    assert out[1]["source"] == {"parser": "pymupdf", "ref": "recovered"} and out[1]["lines"][0]["end"] == 9
    assert stats["recovered"] == 1


def test_recovery_ignores_partly_covered_lines_and_tiny_fragments_and_marks_margins():
    spill = Fragment([_char(c, 0.1 + i * 0.01, 0.40) for i, c in enumerate("Spillover word")])
    spill.chars[0].used = True
    number = Fragment([_char(c, 0.8 + i * 0.01, 0.45) for i, c in enumerate("(1)")])
    footer = Fragment([_char(c, 0.4 + i * 0.01, 0.96) for i, c in enumerate("Page 3 of 9")])
    page = PageText(index=0, width=595, height=842, fragments=[spill, number, footer])
    out = hybrid_parser._recover_uncovered([], [page], {"recovered": 0})
    assert [(b["type"], b["text"]) for b in out] == [("page_footer", "Page 3 of 9")]
    assert out[0]["lines"] is None


def test_pages_docling_could_not_read_come_from_the_fast_parser():
    docling = [
        {"type": "paragraph", "text": "Docling p1", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}}], "source": {"parser": "docling"}},
        {"type": "picture", "text": "", "regions": [{"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.9}}], "source": {"parser": "docling"}},
        {"type": "paragraph", "text": "Docling p3", "regions": [{"pageIndex": 2, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}}], "source": {"parser": "docling"}},
    ]
    fast = [
        {"type": "section_header", "text": "Fast title", "regions": [{"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.15}}], "source": {"parser": "pymupdf"}},
        {"type": "list_item", "text": "Fast item", "regions": [{"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.9, "y1": 0.25}}], "source": {"parser": "pymupdf"}},
        {"type": "paragraph", "text": "Fast p1", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2}}], "source": {"parser": "pymupdf"}},
    ]
    out = hybrid_parser._replace_pages(docling, fast, {1})
    assert [b["text"] for b in out] == ["Docling p1", "Fast title", "Fast item", "Docling p3"]
    assert out[1]["source"] == {"parser": "pymupdf", "ref": "fallback"}


def test_an_item_outside_a_picture_is_no_text_of_it():
    from types import SimpleNamespace as NS

    def box(l, t, r, b):
        bb = NS(l=l, t=t, r=r, b=b)
        bb.to_top_left_origin = lambda page_height: bb
        return bb

    def item(text, l, t, r, b, children=()):
        it = NS(text=text, prov=[NS(page_no=1, bbox=box(l, t, r, b))], children=[])
        it.children = [NS(resolve=lambda doc, c=c: c) for c in children]
        return it

    label = item("QR", 500, 600, 520, 620)
    note = item("*9 Computer services, royalties and consulting.", 80, 780, 520, 790)
    qr = item("", 490, 590, 540, 640, children=[label, note])
    doc = NS(pages={1: NS(size=NS(width=595, height=842))})
    outside: list = []
    assert docling_parser._picture_text(qr, doc, outside=outside) == "QR"
    assert outside == [note]
