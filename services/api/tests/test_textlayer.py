"""Text-layer reconstruction: word gaps, overlapping text boxes, CJK joining, code blocks."""
from pathlib import Path

import pytest

from app.config import get_settings
from app.parsers.textlayer import Char, Fragment, build_text, code_text

W = 595.0


def frag(text: str, x: float, y: float, advance: float = 0.012, size: float = 10, gap_after: dict | None = None) -> Fragment:
    """Characters laid out left to right; gap_after maps an index to extra space after it (normalized)."""
    chars, pos = [], x
    for i, c in enumerate(text):
        chars.append(Char(c=c, x0=pos, y0=y, x1=pos + advance, y1=y + 0.015, size=size, bold=False))
        pos += advance + (gap_after or {}).get(i, 0.0)
    return Fragment(chars)


def test_word_gaps_without_space_characters_become_spaces():
    # PowerPoint-style: "빠르게적용" drawn with a 0.25 em gap between the words and no space char.
    gap = 0.25 * 10 / W
    text, lines, _ = build_text([frag("빠르게적용", 0.1, 0.1, gap_after={2: gap})], 0, W)
    assert text == "빠르게 적용"
    assert len(lines[0]["xs"]) == len(text)


def test_kerning_gaps_do_not_split_words():
    text, _, _ = build_text([frag("kerning", 0.1, 0.1, gap_after={2: 0.05 * 10 / W})], 0, W)
    assert text == "kerning"


def test_overlapping_boxes_on_one_baseline_are_not_interleaved():
    a = frag("ABCDEF", 0.10, 0.2)
    b = frag("uvwxyz", 0.13, 0.2)  # overlaps a horizontally at the same height
    text, lines, _ = build_text([a, b], 0, W)
    assert "ABCDEF" in text and "uvwxyz" in text
    assert len(lines) == 2


def test_cjk_lines_join_without_space_and_hyphenation_is_undone():
    text, _, _ = build_text([frag("\u8fd9\u662f\u7b2c\u4e00", 0.1, 0.1), frag("\u884c\u6587\u672c", 0.1, 0.13)], 0, W)
    assert text == "\u8fd9\u662f\u7b2c\u4e00\u884c\u6587\u672c"
    text, _, _ = build_text([frag("chun-", 0.1, 0.1), frag("king works", 0.1, 0.13)], 0, W)
    assert text == "chunking works"


def test_code_keeps_line_breaks_and_indentation():
    cell = 0.012
    lines = [frag("def f():", 0.1, 0.10), frag("return 1", 0.1 + 4 * cell, 0.13), frag("print(f())", 0.1, 0.16)]
    assert code_text(lines, W) == "def f():\n    return 1\nprint(f())"


def test_tex_extension_glyphs_become_math_symbols():
    from app.parsers.textlayer import math_glyph

    # cmex10 codes as PyMuPDF reports them when the font has no ToUnicode map.
    assert [math_glyph("ABCDEF+CMEX10", c) for c in "XPYQZRpqs"] == list("∑∑∏∏∫∫√√√")
    assert [math_glyph("CMEX10", c) for c in "\x10\x11\x22\x23"] == list("()[]")
    assert math_glyph("CMEX10", "z") == "" and math_glyph("CMEX10", "{") == ""  # underbrace pieces
    assert math_glyph("LMMathExtension10-Regular", "p") == "√"
    assert math_glyph("MSBM10", "E") == "𝔼" and math_glyph("MSBM10", "R") == "ℝ"
    # Every other font is left alone, including spaces MuPDF synthesizes inside math fonts.
    assert math_glyph("CMR10", "X") == "X" and math_glyph("ArialMT", "p") == "p"
    assert math_glyph("CMEX10", " ") == " "


def test_stacked_delimiter_pieces_read_as_one():
    norm = [Char(c="‖", x0=0.3, y0=0.1 + i * 0.01, x1=0.31, y1=0.11 + i * 0.01, size=10, bold=False, tall=True) for i in range(4)]
    body = frag("x", 0.32, 0.115)
    text, _, _ = build_text([Fragment(norm[:2]), body, Fragment(norm[2:])], 0, W)
    assert text.count("‖") == 1 and "x" in text


def test_a_loose_formula_box_does_not_take_the_next_paragraph_line():
    from app.parsers.hybrid_parser import _attach_text_layer, _region_boxes
    from app.parsers.textlayer import PageText

    formula_line = frag("a=b+c", 0.3, 0.30)          # y 0.300-0.315
    paragraph_line = frag("where a is", 0.1, 0.312)  # y 0.312-0.327, starts just inside the formula box
    page = PageText(index=0, width=W, height=842, fragments=[formula_line, paragraph_line])
    blocks = [
        {"type": "formula", "text": "", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.25, "y0": 0.29, "x1": 0.8, "y1": 0.318}}]},
        {"type": "paragraph", "text": "where a is", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.31, "x1": 0.8, "y1": 0.33}}]},
    ]
    stats = {"replaced": 0, "kept": 0}
    boxes = _region_boxes(blocks)
    for block in blocks:
        _attach_text_layer(block, [page], stats, boxes)
    assert blocks[0]["text"] == "a=b+c"
    assert blocks[1]["text"] == "where a is" and blocks[1]["lines"]


def test_a_block_of_several_paragraphs_is_split_at_blank_lines():
    from app.parsers.hybrid_parser import _split_at_blank_lines

    # Three paragraphs of two lines; 0.008 between lines, 0.025 between paragraphs (line height 0.018).
    lines, text, y = [], "", 0.1
    for p in range(3):
        for k in range(2):
            words = f"para{p} line{k}" + ("." if k else "")
            start = len(text) + (1 if text else 0)
            text = f"{text} {words}" if text else words
            lines.append({"pageIndex": 0, "bbox": {"x0": 0.1, "y0": y, "x1": 0.8, "y1": y + 0.018},
                          "start": start, "end": len(text), "xs": []})
            y += 0.018 + 0.008
        y += 0.017
    block = {"type": "paragraph", "text": text, "level": None, "lines": lines, "_chars": [],
             "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.8, "y1": y}}]}
    stats = {"split": 0}
    out = _split_at_blank_lines([block], stats)
    assert [b["text"] for b in out] == ["para0 line0 para0 line1.", "para1 line0 para1 line1.", "para2 line0 para2 line1."]
    assert stats["split"] == 2 and "_chars" not in out[1]
    assert out[1]["lines"][0]["start"] == 0 and out[1]["text"][out[1]["lines"][1]["start"]:] == "para1 line1."
    assert out[2]["regions"][0]["bbox"]["y0"] == lines[4]["bbox"]["y0"]
    # No split without a sentence end before the gap (a line taken by an inline formula).
    open_ended = {**block, "text": block["text"].replace(".", ",")}
    assert len(_split_at_blank_lines([open_ended], {"split": 0})) == 1
    # Evenly spaced lines stay one paragraph.
    even = {**block, "lines": [{**ln, "bbox": {**ln["bbox"], "y0": 0.1 + i * 0.026, "y1": 0.118 + i * 0.026}} for i, ln in enumerate(lines)]}
    assert len(_split_at_blank_lines([even], {"split": 0})) == 1


def test_pictures_take_the_text_drawn_inside_them():
    from app.parsers.hybrid_parser import _attach_text_layer
    from app.parsers.textlayer import PageText

    page = PageText(index=0, width=W, height=842, fragments=[frag("Image Encoder", 0.2, 0.2), frag("Text Encoder", 0.2, 0.3)])
    picture = {"type": "picture", "text": "", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.4}}]}
    _attach_text_layer(picture, [page], {"replaced": 0, "kept": 0})
    assert picture["text"] == "Image Encoder Text Encoder" and not picture.get("lines")
    # A few stray marks (axis ticks) are not text worth keeping.
    ticks = {"type": "picture", "text": "", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.4}}]}
    _attach_text_layer(ticks, [PageText(index=0, width=W, height=842, fragments=[frag("0 5 10", 0.2, 0.2)])], {"replaced": 0, "kept": 0})
    assert ticks["text"] == ""
    # Two words already say something: a shape's "The end!", the label under an embedded file's icon.
    for words in ("The end!", "PPT.ppt"):
        shape = {"type": "picture", "text": "", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.4}}]}
        _attach_text_layer(shape, [PageText(index=0, width=W, height=842, fragments=[frag(words, 0.2, 0.2)])], {"replaced": 0, "kept": 0})
        assert shape["text"] == words


def test_a_slide_title_with_a_text_shadow_is_read_once():
    from app.parsers.hybrid_parser import _attach_text_layer
    from app.parsers.textlayer import PageText

    # The text layer drops the shadow; Docling read each line twice, line after line.
    page = PageText(index=0, width=W, height=842, fragments=[frag("LIBREOFFICE 6.2", 0.2, 0.2), frag("19 FEBRUARY 2019", 0.2, 0.23)])
    title = {"type": "section_header", "text": "LIBREOFFICE 6.2 LIBREOFFICE 6.2 19 FEBRUARY 2019 19 FEBRUARY 2019",
             "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.18, "x1": 0.9, "y1": 0.26}}]}
    stats = {"replaced": 0, "kept": 0}
    _attach_text_layer(title, [page], stats)
    assert title["text"] == "LIBREOFFICE 6.2 19 FEBRUARY 2019" and stats["replaced"] == 1


def test_captions_are_recognized_in_common_forms():
    from app.parsers.base import _mark_captions

    blocks = [
        {"type": "section_header", "text": "\u56f3\u8868Ⅱ-1-1-1 ICT を\u53d6り\u5dfbく\u5e02\u5834\u69cb\u9020", "level": 2},
        {"type": "picture", "text": ""},
        {"type": "paragraph", "text": "<표 2-1> 연도별 예산", "level": None},
        {"type": "table", "text": "| a |"},
        {"type": "section_header", "text": "표준화 동향", "level": 1},
    ]
    _mark_captions(blocks)
    assert [b["type"] for b in blocks] == ["caption", "picture", "caption", "table", "section_header"]
    assert blocks[0]["level"] is None


def _region(page: int) -> list[dict]:
    return [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.5}}]


def test_picture_text_preserves_reference_numerals_and_removes_repeated_captions():
    from app.parsers.base import _clean_picture_text

    blocks = [
        # A patent drawing: reference numerals and the figure label only.
        {"type": "picture", "text": "68\n24\n44 56\n-82\n∞8\nFIG. 3", "regions": _region(0)},
        # A figure whose text repeats the caption next to it, plus its own diagram labels.
        {"type": "picture", "text": "Figure 1: Overview of the pipeline.\nPanorama Generation\nSound Agent\n1374",
         "regions": _region(1)},
        {"type": "caption", "text": "Figure 1: Overview of the pipeline.", "regions": _region(1)},
        # Labels that also occur in the body text stay: they are short.
        {"type": "picture", "text": "encoder\ndecoder\nattention", "regions": _region(2)},
        {"type": "paragraph", "text": "The encoder and decoder use attention.", "regions": _region(2)},
    ]
    blocks.append({"type": "picture", "text": "The end!", "regions": _region(3)})
    _clean_picture_text(blocks)
    assert blocks[0]["text"] == "68\n24\n44 56\n-82\n∞8\nFIG. 3" and blocks[5]["text"] == "The end!"
    assert blocks[1]["text"] == "Panorama Generation\nSound Agent\n1374"
    assert blocks[3]["text"] == "encoder\ndecoder\nattention"


def test_a_title_in_angle_brackets_next_to_a_figure_is_its_caption():
    from app.parsers.base import _mark_captions

    def blk(kind, text, page=0):
        return {"type": kind, "text": text, "level": 2 if kind == "section_header" else None, "regions": _region(page)}

    blocks = [blk("section_header", "< 추진 방향 >"), blk("picture", ""), blk("paragraph", "<기술개발 개념도>"),
              blk("section_header", "<참고 문헌>"), blk("paragraph", "본문"), blk("section_header", "< 다음 장 >"), blk("table", "| a |", 1)]
    _mark_captions(blocks)
    assert [b["type"] for b in blocks] == ["caption", "picture", "caption", "section_header", "paragraph", "section_header", "table"]


def test_mathematical_letters_mapped_into_hangul_are_read_as_letters():
    from app.parsers.base import _truncated_math_letters

    # U+1D446 (italic S) and U+1D719 (italic phi) less their top bit, in an English paper.
    paper = [{"type": "paragraph", "text": "the field \ud446\u00b3 couples to \ud719\u2074 here"}]
    _truncated_math_letters(paper)
    assert paper[0]["text"] == "the field S\u00b3 couples to \u03c6\u2074 here"
    # Korean text has syllables before U+D400 too: its syllables after it stay.
    korean = [{"type": "paragraph", "text": "\ud55c\uad6d \ud559\ud68c"}]
    _truncated_math_letters(korean)
    assert korean[0]["text"] == "\ud55c\uad6d \ud559\ud68c"


def test_a_figure_is_read_with_its_pieces_and_beside_the_figure_next_to_it():
    from app.parsers.base import _figures_in_place

    def blk(name, kind, x0, y0, x1, y1, text=None):
        return {"type": kind, "text": text or name, "name": name,
                "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    long = "The text under both charts. " * 8
    # Two charts side by side; the layout model read the left column, the text under both, then the right chart.
    page = [blk("cap1", "caption", 0.12, 0.10, 0.43, 0.11), blk("pic1", "picture", 0.11, 0.11, 0.48, 0.36),
            blk("src1", "caption", 0.15, 0.37, 0.49, 0.38), blk("head", "section_header", 0.12, 0.42, 0.30, 0.44),
            blk("para1", "paragraph", 0.10, 0.46, 0.89, 0.56, long), blk("para2", "paragraph", 0.11, 0.56, 0.89, 0.64, long),
            blk("note", "footnote", 0.11, 0.92, 0.89, 0.94), blk("cap2", "caption", 0.52, 0.10, 0.86, 0.11),
            blk("axis", "paragraph", 0.86, 0.31, 0.89, 0.32), blk("pic2", "picture", 0.51, 0.12, 0.89, 0.38),
            blk("src2", "caption", 0.56, 0.38, 0.90, 0.39)]
    assert [b["name"] for b in _figures_in_place(page)] == [
        "cap1", "pic1", "src1", "cap2", "axis", "pic2", "src2", "head", "para1", "para2", "note"]
    # Two columns of text, a table in each: each column goes on under its own table, read in order.
    cols = [blk("t11cap", "caption", 0.10, 0.59, 0.32, 0.60), blk("t11", "table", 0.10, 0.63, 0.47, 0.75),
            blk("left", "paragraph", 0.10, 0.79, 0.48, 0.83, long), blk("t12cap", "caption", 0.52, 0.13, 0.77, 0.14),
            blk("t12", "table", 0.52, 0.16, 0.89, 0.28), blk("right", "paragraph", 0.52, 0.32, 0.90, 0.42, long),
            blk("t13cap", "caption", 0.52, 0.45, 0.80, 0.46), blk("t13", "table", 0.52, 0.48, 0.90, 0.69)]
    assert [b["name"] for b in _figures_in_place(cols)] == [b["name"] for b in cols]
    # The source line under a chart, read after the rest of the page, goes right after the chart.
    page = [blk("cap", "caption", 0.11, 0.10, 0.34, 0.11), blk("pic", "picture", 0.15, 0.11, 0.84, 0.32),
            blk("head", "section_header", 0.11, 0.37, 0.47, 0.39), blk("para", "paragraph", 0.11, 0.41, 0.89, 0.51, long),
            blk("note", "footnote", 0.11, 0.92, 0.39, 0.93), blk("src", "paragraph", 0.61, 0.33, 0.89, 0.34)]
    assert [b["name"] for b in _figures_in_place(page)] == ["cap", "pic", "src", "head", "para", "note"]
    # Another figure read between the two (an icon by a heading, a QR code), even between the right
    # chart and its caption, does not keep them apart.
    page = [blk("cap1", "caption", 0.12, 0.27, 0.48, 0.28), blk("pic1", "picture", 0.11, 0.29, 0.48, 0.61),
            blk("src1", "paragraph", 0.19, 0.615, 0.49, 0.62), blk("icon", "picture", 0.10, 0.64, 0.15, 0.67),
            blk("para", "paragraph", 0.10, 0.65, 0.89, 0.75, long), blk("cap2", "caption", 0.52, 0.27, 0.86, 0.29),
            blk("qr", "picture", 0.81, 0.77, 0.88, 0.82), blk("pic2", "picture", 0.51, 0.30, 0.89, 0.56),
            blk("src2", "caption", 0.51, 0.57, 0.89, 0.59)]
    assert [b["name"] for b in _figures_in_place(page)] == [
        "cap1", "pic1", "src1", "cap2", "pic2", "src2", "icon", "para", "qr"]
    # A chart's title read after the chart (and after the text under both charts) goes in front of it.
    page = [blk("cap1", "caption", 0.12, 0.10, 0.43, 0.11), blk("pic1", "picture", 0.11, 0.11, 0.48, 0.36),
            blk("axis", "paragraph", 0.86, 0.31, 0.89, 0.32), blk("pic2", "picture", 0.51, 0.12, 0.89, 0.38),
            blk("src2", "caption", 0.56, 0.38, 0.90, 0.39), blk("para", "paragraph", 0.10, 0.46, 0.89, 0.56, long),
            blk("note", "footnote", 0.11, 0.92, 0.89, 0.94), blk("cap2", "caption", 0.52, 0.10, 0.86, 0.11)]
    assert [b["name"] for b in _figures_in_place(page)] == [
        "cap1", "pic1", "cap2", "axis", "pic2", "src2", "para", "note"]
    # So does a title not yet known as one ("Figure 2 ..." read as a paragraph or heading).
    page[-1]["type"], page[-1]["text"] = "section_header", "Figure 2: Revenue by segment"
    assert [b["name"] for b in _figures_in_place(page)] == [
        "cap1", "pic1", "cap2", "axis", "pic2", "src2", "para", "note"]


def test_formula_latex_is_joined_and_checked():
    from app.parsers.formulas import _sound, agrees, compact_latex

    assert compact_latex(r"\mathrm { H e s s } _ { x } \in g U") == r"\mathrm{Hess}_{x}\in gU"
    assert compact_latex(r"a \ = \ b \hfill") == r"a\ =\ b"
    assert not _sound("~ " * 40)
    assert not _sound(r"\frac { 1 } { 2")
    assert _sound(r"c ( g ) ( i ) : = \begin{cases} { 1 } \\ { 0 } \end{cases}")
    assert _sound(r"e _ { t } = \left\{ \begin{aligned} { } & { \epsilon _ { t } } \end{aligned} \right.")
    # Same letters and digits as the text layer, in any order: the read agrees.
    assert agrees(r"U(f)=(\nabla f)^{T}\mathrm{adj}(\mathrm{Hess}f)\nabla f", "U(f) = (∇f)T adj(Hess f) ∇f")
    # A sentence the layout model called a formula reads as something else entirely.
    assert not agrees(r"\sum_{i=1}^{N}x_{i}", "Step 5: descent to f, which is (ii).")


def test_mathabx_symbols_read_as_math():
    from app.parsers.textlayer import math_glyph

    # pdfTeX maps mathabx glyphs through the T1 text encoding: "∈" arrives as "P", "=" as "“".
    assert [math_glyph("VPIAKC+TeX-matha10", c) for c in "Pp“ďô"] == ["∈", "(", "=", "≤", "⇔"]
    assert math_glyph("AZTGVB+TeX-mathx10", "\x13") == ""  # a piece of a tall delimiter
    assert math_glyph("LMRoman10-Regular", "P") == "P"


def test_text_with_remapped_math_wins_over_doclings_letters():
    from app.parsers.hybrid_parser import _attach_text_layer
    from app.parsers.textlayer import PageText

    line = frag("for all g ∈ G and q ∈ S(A), the map is continuous", 0.1, 0.2)
    for ch in line.chars:
        ch.remapped = ch.c in "∈()"
    block = {"type": "paragraph", "text": "for all g P G and q P SpAq, the map is continuous",
             "regions": [{"pageIndex": 0, "bbox": {"x0": 0.05, "y0": 0.15, "x1": 0.95, "y1": 0.25}}]}
    _attach_text_layer(block, [PageText(index=0, width=W, height=842, fragments=[line])], {"replaced": 0, "kept": 0})
    assert block["text"] == "for all g ∈ G and q ∈ S(A), the map is continuous"


def test_a_formula_read_ahead_of_the_sentence_above_it_moves_after_it():
    from app.parsers.base import _place_formulas_in_flow

    def block(name, kind, x0, y0, x1, y1):
        return {"id": name, "type": kind, "text": name,
                "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    blocks = [
        block("eq2.2", "formula", 0.34, 0.45, 0.66, 0.50),
        block("eq2.3", "formula", 0.42, 0.56, 0.89, 0.61),   # drawn below the next two lines
        block("can be written", "paragraph", 0.12, 0.51, 0.28, 0.52),
        block("the predictor is", "paragraph", 0.12, 0.53, 0.89, 0.56),
        block("which gives", "paragraph", 0.12, 0.61, 0.89, 0.71),
    ]
    _place_formulas_in_flow(blocks)
    assert [b["id"] for b in blocks] == ["eq2.2", "can be written", "the predictor is", "eq2.3", "which gives"]
    # Two columns: a formula at the bottom of the left column stays before the right column.
    two = [
        block("left text", "paragraph", 0.08, 0.10, 0.47, 0.60),
        block("left formula", "formula", 0.10, 0.62, 0.45, 0.66),
        block("right text", "paragraph", 0.53, 0.10, 0.92, 0.90),
    ]
    _place_formulas_in_flow(two)
    assert [b["id"] for b in two] == ["left text", "left formula", "right text"]



def test_pieces_left_at_the_end_of_a_single_column_page_go_back_in_place():
    from app.parsers.base import _order_simple_pages, _side_by_side_pages

    def block(name, page, y0, y1, x0=0.1, x1=0.9, kind="paragraph"):
        return {"id": name, "type": kind, "text": name,
                "regions": [{"pageIndex": page, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    blocks = [block("top", 0, 0.1, 0.2), block("bottom", 0, 0.7, 0.8), block("middle piece", 0, 0.4, 0.45),
              block("next page", 1, 0.1, 0.2),
              block("left col", 2, 0.1, 0.9, 0.05, 0.45), block("right col", 2, 0.1, 0.5, 0.55, 0.95),
              block("right col 2", 2, 0.05, 0.08, 0.55, 0.95)]
    out = _order_simple_pages(blocks, _side_by_side_pages(blocks))
    # Page 3 has two columns: the layout model's order stays.
    assert [b["id"] for b in out] == ["top", "middle piece", "bottom", "next page", "left col", "right col", "right col 2"]


def test_text_beside_a_picture_is_put_in_order_around_it():
    from app.parsers.base import _order_simple_pages, _side_by_side_pages

    def block(name, y0, y1, x0=0.3, x1=0.95, kind="paragraph"):
        return {"id": name, "type": kind, "text": name,
                "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    # A slide with a decoration down the left edge; the model read the title after a subheading.
    blocks = [block("edge picture", 0.1, 0.3, 0.0, 0.2, "picture"), block("Our Vision", 0.18, 0.25, kind="section_header"),
              block("About", 0.05, 0.14, kind="section_header"), block("vision text", 0.25, 0.54, kind="list_item"),
              block("figure 1", 0.6, 0.8, 0.3, 0.6, "picture"), block("Fig. 1", 0.8, 0.85, 0.3, 0.6, "caption"),
              block("figure 2", 0.55, 0.7, 0.65, 0.95, "picture"), block("Fig. 2", 0.71, 0.75, 0.65, 0.95, "caption")]
    columns = _side_by_side_pages(blocks, pictures=False)
    assert not columns
    out = _order_simple_pages(blocks, columns, frozenset(_side_by_side_pages(blocks) - columns))
    # Pictures and captions keep their places; figures side by side keep their own captions.
    assert [b["id"] for b in out] == ["edge picture", "About", "Our Vision", "vision text", "figure 1", "Fig. 1", "figure 2", "Fig. 2"]


def test_formula_pieces_on_one_line_become_one_formula():
    from app.parsers.formulas import merge_formula_pieces

    def formula(text, x0, y0, x1, y1):
        return {"type": "formula", "text": text, "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    blocks = [
        {"type": "paragraph", "text": "the mean is", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}}]},
        formula("{0, t ≤ τ", 0.43, 0.555, 0.64, 0.61),   # the cases
        formula("E(e) =", 0.34, 0.567, 0.42, 0.597),    # its left-hand side, touching
        formula("x = 1", 0.40, 0.70, 0.60, 0.73),       # the next formula, another line
    ]
    assert merge_formula_pieces(blocks) == 1
    assert [b["text"] for b in blocks] == ["the mean is", "E(e) = {0, t ≤ τ", "x = 1"]
    assert blocks[1]["regions"][0]["bbox"] == {"x0": 0.34, "y0": 0.555, "x1": 0.64, "y1": 0.61}


def test_text_two_overlapping_ocr_regions_read_twice_stays_once():
    from docling_core.types.doc import BoundingBox, CoordOrigin
    from docling_core.types.doc.page import BoundingRectangle, TextCell

    from app.parsers.docling_parser import _distinct_reads

    def cell(text, l, t, r, b, confidence):
        box = BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT)
        return TextCell(rect=BoundingRectangle.from_bounding_box(box), text=text, orig=text, confidence=confidence, from_ocr=True)

    cells = [cell("ATP", 63, 428, 86, 437, 1.0), cell("Modulation and Goding Schome", 109, 482, 236, 487, 0.9),
             cell("ATP", 64, 429, 85, 437, 0.97), cell("Modulation and Coding Scheme", 110, 483, 236, 490, 1.0),
             cell("Association Timeout Period", 110, 429, 222, 437, 0.98), cell("CES", 64, 440, 84, 448, 1.0)]
    # What is left is in the order of the page (two regions' reads came one after the other).
    assert [c.text for c in _distinct_reads(cells)] == ["ATP", "Association Timeout Period", "CES", "Modulation and Coding Scheme"]
    # A paragraph read by two overlapping regions: the lines kept from each go back in page order.
    para = [cell("Large language model inference has become", 100, 100, 500, 110, 0.99),
            cell("put. Modern production systems use disaggre", 100, 112, 500, 122, 0.90),
            cell("gated inference: prefill and decode", 100, 124, 500, 134, 0.99),
            cell("Large language model inference has become", 101, 100, 500, 110, 0.90),
            cell("put. Modern production systems use disaggre", 101, 112, 500, 122, 0.99),
            cell("gated inference: prefill and decode", 101, 124, 500, 134, 0.90)]
    assert [c.text[:5] for c in _distinct_reads(para)] == ["Large", "put. ", "gated"]
    # Lines of a scan turned a little: each line's box takes in its neighbours, which read otherwise.
    turned = [cell("Its had resolving otherwise she contented", 100, 100, 500, 160, 0.95),
              cell("Afford relied warmth out sir hearts sister", 100, 110, 500, 170, 0.97),
              cell("Men day warmth formed admire former simple", 100, 120, 500, 180, 0.96)]
    assert len(_distinct_reads(turned)) == 3

def test_a_sentence_runs_on_past_footnotes_and_figures_into_the_next_column_or_page():
    from app.parsers.base import _merge_continuations

    def block(kind, text, page, x0, y0, x1, y1):
        return {"type": kind, "text": text, "regions": [{"pageIndex": page, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    blocks = [
        block("paragraph", "융합을 통해 지능화, 자", 0, 0.05, 0.6, 0.47, 0.8),
        block("paragraph", "동화, 그리고 연결성이 극대화된 환경을 의미한다", 0, 0.53, 0.55, 0.95, 0.8),  # next column
        block("footnote", "* DOI: 10.22648/ETRI", 0, 0.05, 0.85, 0.95, 0.9),
        block("picture", "", 0, 0.05, 0.92, 0.2, 0.97),
        block("page_header", "100", 1, 0.05, 0.02, 0.1, 0.04),
        block("paragraph", "지능은 가장 핵심적인 기술로 꼽힌다.", 1, 0.05, 0.1, 0.47, 0.3),  # next page
        block("paragraph", "새 문단입니다.", 1, 0.05, 0.32, 0.47, 0.4),
    ]
    out = _merge_continuations(blocks)
    assert [b["type"] for b in out] == ["paragraph", "footnote", "picture", "page_header", "paragraph"]
    assert out[0]["text"] == "융합을 통해 지능화, 자동화, 그리고 연결성이 극대화된 환경을 의미한다 지능은 가장 핵심적인 기술로 꼽힌다."
    assert len(out[0]["regions"]) == 3


def test_a_list_item_runs_on_into_the_next_page():
    from app.parsers.base import _merge_continuations

    def block(kind, text, page, y0, **extra):
        return {"type": kind, "text": text, "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": y0, "x1": 0.9, "y1": y0 + 0.1}}],
                **extra}

    blocks = [
        block("list_item", "If 433 MHz SC front end is selected, the design", 0, 0.8, _bullet=True),
        block("page_footer", "1", 0, 0.95),
        block("list_item", "of the spoke resonator is not trivial.", 1, 0.05, _bullet=False),
        block("list_item", "apples and pears", 1, 0.3, _bullet=True),
        block("list_item", "bananas", 2, 0.05, _bullet=True),  # a new item: it has its bullet
        block("list_item", "4.2.9. Немедленно письменно информировать", 2, 0.8, _bullet=True),
        block("list_item", "Государственного заказчика об обнаружении.", 3, 0.05, _bullet=False),
        block("list_item", "[0026] 예컨대, 터치 센서 패널은 손가락이나 펜 같은 객체가 패널에 직접 닿지 않은 경", 3, 0.8, _bullet=True),
        block("paragraph", "우에 객체의 존재가 호버링으로 인식될 수 있다.", 4, 0.05),
        block("list_item", "o 추진 배경", 4, 0.9, _bullet=True),
        block("paragraph", "본 사업은 도로 위험을 줄이기 위해 추진한다.", 5, 0.05),
    ]
    out = _merge_continuations(blocks)
    assert [b["text"] for b in out] == ["If 433 MHz SC front end is selected, the design of the spoke resonator is not trivial.",
                                        "1", "apples and pears", "bananas",
                                        "4.2.9. Немедленно письменно информировать Государственного заказчика об обнаружении.",
                                        "[0026] 예컨대, 터치 센서 패널은 손가락이나 펜 같은 객체가 패널에 직접 닿지 않은 경우에 객체의 존재가 호버링으로 인식될 수 있다.",
                                        "o 추진 배경", "본 사업은 도로 위험을 줄이기 위해 추진한다."]


def test_korean_lines_broken_inside_a_word_join_without_a_space():
    from app.parsers.textlayer import PageText

    page = PageText(index=0, width=W, height=842, fragments=[frag("기술로 일반적인 최", 0.1, 0.2), frag("신의 방법을 쓰고 이", 0.1, 0.22),
                                                              frag("논문에서는 다룬다.", 0.1, 0.24)])
    text, lines, _ = build_text(page.fragments, 0, W)
    assert text == "기술로 일반적인 최신의 방법을 쓰고 이 논문에서는 다룬다."
    assert [text[ln["start"]:ln["end"]] for ln in lines] == ["기술로 일반적인 최", "신의 방법을 쓰고 이", "논문에서는 다룬다."]


def test_lines_of_one_paragraph_split_into_blocks_are_joined():
    from app.parsers.base import _merge_continuations

    def block(text, y0, x1=0.95, x0=0.05):
        return {"type": "paragraph", "text": text, "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y0 + 0.015}}]}

    blocks = [
        block("◈ 최근 10년간 자연재해는 총 186건으로 재산피해 발생함. 그중 12", 0.10),
        block("월은 16건으로 6,056억원의 재산피해 발생", 0.12, x1=0.5),
        block("• 가스사고 예방 안전행동요령", 0.15, x1=0.4),
        block("• 승강기사고 예방 안전행동요령", 0.17, x1=0.42),
    ]
    out = _merge_continuations(blocks)
    assert [b["text"] for b in out] == [
        "◈ 최근 10년간 자연재해는 총 186건으로 재산피해 발생함. 그중 12월은 16건으로 6,056억원의 재산피해 발생",
        "• 가스사고 예방 안전행동요령",
        "• 승강기사고 예방 안전행동요령",
    ]


def test_symbol_font_and_hancom_private_use_glyphs():
    from app.parsers.textlayer import private_glyph

    assert [private_glyph("SymbolMT", chr(c)) for c in (0xF061, 0xF0A3, 0xF0E5)] == ["α", "≤", "∑"]
    assert private_glyph("Wingdings-Regular", "\uf0fc") == "✓"
    # LibreOffice draws Wingdings and Symbol alike with OpenSymbol: a lone glyph is a bullet.
    assert private_glyph("OpenSymbol", "\uf076", alone=True) == "❖"
    assert private_glyph("OpenSymbol", "\uf061") == "α"
    assert private_glyph("HCRBatang", "\uf53a") == "한"
    assert private_glyph("SomeFont", "\ue28b") == "\ue28b"  # unknown: left alone


def test_bullets_of_symbol_fonts_are_taken_off_list_items():
    from app.parsers.pymupdf_parser import list_marker

    assert list_marker("❖ Leaders must be vocal") == (2, None)
    assert list_marker("➢ Next step") == (2, None)
    assert list_marker("(a) first") == (4, "(a)")
    # Check marks and boxes say something.
    assert list_marker("✓ done") is None
    assert list_marker("☐ open") is None


def test_a_picture_repeated_on_every_page_is_described_once(tmp_path):
    import json

    import pymupdf

    from app.models import ParsedDocument
    from app.parsers.figures import write_figures

    doc = pymupdf.open()
    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 80), 0)
    logo.set_rect(logo.irect, (200, 30, 30))
    for n in range(4):
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(20, 20, 140, 140), pixmap=logo)
        chart = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 80, 80), 0)
        chart.set_rect(chart.irect, (40, 60 * n, 200))
        page.insert_image(pymupdf.Rect(150, 300, 450, 600), pixmap=chart)
    pdf = tmp_path / "deck.pdf"
    doc.save(pdf)
    W, H = doc[0].rect.width, doc[0].rect.height

    def picture(id_, page, x0, y0, x1, y1):
        return {"id": id_, "type": "picture", "text": "", "readingOrder": 0, "source": {"parser": "pymupdf"},
                "regions": [{"pageIndex": page, "bbox": {"x0": x0 / W, "y0": y0 / H, "x1": x1 / W, "y1": y1 / H}}]}

    blocks = [b for n in range(4) for b in (picture(f"logo{n}", n, 20, 20, 140, 140), picture(f"chart{n}", n, 150, 300, 450, 600))]
    for i, b in enumerate(blocks):
        b["readingOrder"] = i
    parsed = ParsedDocument.model_validate({
        "document": {"id": "d", "filename": "deck.pdf", "fileHash": "h", "pageCount": 4, "parser": "pymupdf", "parserVersion": "1"},
        "pages": [{"index": n, "width": W, "height": H} for n in range(4)], "blocks": blocks, "blocksFingerprint": "x"})
    assert write_figures(pdf, parsed, tmp_path / "figures", 80) == 5
    sent = [f["blockId"] for f in json.loads((tmp_path / "figures" / "figures.json").read_text())["figures"]]
    # The logo once, on its first page; each chart (same spot, different picture) every time.
    assert sent == ["logo0", "chart0", "chart1", "chart2", "chart3"]


def test_text_drawn_twice_in_the_same_place_is_read_once():
    import pymupdf

    from app.parsers.textlayer import read_page

    doc = pymupdf.open()
    page = doc.new_page()
    for dx, color in ((0, (0, 0, 0)), (0.2, (1, 1, 0))):  # a copy highlighted for an animation step
        page.insert_text((72 + dx, 100), "3 of 9 still on orbit", fontsize=18, color=color)
    page.insert_text((72, 124), "3 of 9 still on orbit", fontsize=18)  # the next line says it again
    lines = ["".join(c.c for c in f.chars).strip() for f in read_page(page, 0).fragments]
    assert lines == ["3 of 9 still on orbit", "3 of 9 still on orbit"]
    from app.parsers.textlayer import similarity

    # Extractions differ in the kind of dash; Docling reads the doubled line twice.
    assert similarity("02/78 – 10/85", "02/78 - 10/85") == 1.0
    assert similarity("02/78 – 10/85 02/78 – 10/85", "02/78 - 10/85 - 02/78 - 10/85") > 0.95


def test_a_chart_keeps_repeated_panel_labels_and_tick_values():
    from app.parsers.base import _chart_labels

    panels = " ".join(f"QM9 C-C-{x} 0.030 Test Set 0.025 Ours 0.020 0.015 40 60 80 100 Bond Angle (Degree)" for x in "NOH")
    assert _chart_labels(panels) == panels
    # A sentence with a few numbers is not a chart.
    sentence = "Thus the Wick contraction by the Algorithm 1 is the pairing of 0, 1 up to 7 in the order of the sites."
    assert _chart_labels(sentence) == sentence


def test_a_table_cell_spanning_columns_is_said_once():
    from types import SimpleNamespace

    from app.parsers.docling_parser import _table_text

    def cell(text, col):
        return SimpleNamespace(text=text, start_col_offset_idx=col)

    title = cell("5 Measures of Complexity", 0)
    grid = [[title, title, title], [cell("5.1", 0), cell("Entropy", 1), cell("12", 2)]]
    item = SimpleNamespace(data=SimpleNamespace(grid=grid))
    assert _table_text(item).splitlines() == ["| 5 Measures of Complexity |  |  |", "| --- | --- | --- |", "| 5.1 | Entropy | 12 |"]


def test_a_table_of_contents_is_a_list_not_headings():
    from app.parsers.base import _mark_toc

    def block(text, kind="section_header", page=1):
        return {"type": kind, "text": text, "level": 2, "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.12}}]}

    toc = [block("1. 개요 5"), block("1.1 수식 편집기 7"), block("2. 수식 작성 예제 (Examples) 13", "paragraph"), block("3. 변경 사항 이력 15")]
    body = [block("1. 개요", page=2), block("Results for 2019", page=2)]
    blocks = toc + body
    _mark_toc(blocks)
    assert [b["type"] for b in blocks] == ["list_item"] * 4 + ["section_header", "section_header"]
    # Numbers that do not go up the page are not page numbers.
    shuffled = [block("A 40"), block("B 3"), block("C 77"), block("D 12"), block("E 5")]
    _mark_toc(shuffled)
    assert {b["type"] for b in shuffled} == {"section_header"}


def test_raised_small_digits_are_superscripts():
    import pymupdf

    from app.parsers.textlayer import build_text, read_page

    doc = pymupdf.open()
    page = doc.new_page()
    font = pymupdf.Font("helv")
    writer = pymupdf.TextWriter(page.rect)
    for y, parts in ((100, [("T = 86.8036 P", 11, 0), ("0.3570", 7, -5)]), (130, [("area in cm", 11, 0), ("2", 7, -5), (" and H2O", 11, 0)])):
        x = 72
        for text, size, rise in parts:
            _, end = writer.append((x, y + rise), text, font=font, fontsize=size)
            x = end.x
    writer.write_text(page)
    pt = read_page(page, 0)
    text, lines, _ = build_text(pt.fragments, 0, pt.width)
    assert "86.8036 P⁰.³⁵⁷⁰" in text and "cm² and H2O" in text
    assert all(len(ln["xs"]) == ln["end"] - ln["start"] for ln in lines)


def test_a_line_cut_in_two_by_the_layout_model_is_one_line():
    from app.parsers.base import _join_line_pieces

    def block(text, x0, x1, y0):
        return {"type": "paragraph", "text": text, "lines": None,
                "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y0 + 0.011}}]}

    # Column order put both left pieces first.
    blocks = [block("하한: T1a", 0.108, 0.17, 0.591), block("상한: T2a", 0.108, 0.17, 0.626),
              block("= 86.8036×P⁰.³⁵⁷⁰", 0.178, 0.299, 0.588), block("= 96.8276×P⁰.³²⁰¹", 0.178, 0.299, 0.623),
              block("Balance Sheet", 0.5, 0.6, 0.8), block("Balance Sheet", 0.605, 0.7, 0.8),
              block("Left column text", 0.1, 0.45, 0.9), block("right column", 0.52, 0.9, 0.9)]
    out = _join_line_pieces(blocks)
    assert [b["text"] for b in out] == ["하한: T1a = 86.8036×P⁰.³⁵⁷⁰", "상한: T2a = 96.8276×P⁰.³²⁰¹", "Balance Sheet Balance Sheet",
                                        "Left column text", "right column"]


def test_text_of_a_font_with_a_broken_unicode_map_is_found_for_ocr():
    from app.parsers.garbled import garbled_blocks

    def block(text, kind="paragraph"):
        return {"type": kind, "text": text, "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.12}}]}

    body = [block("국내 시스템 반도체 산업은 창업 이후 정착 단계에서 어려움을 겪는다. " * 20),
            block("국제특허분류(Int. Cl.) G06F 17/30"), block("𝜸𝑚𝑖𝑛 ← min(X, axis=1)"), block("الرابطة الولائية لكرة القدم")]
    broken = [block("�əฝ �� ➚ญᜅ ʑᨦ ᳦ᨦᬱ ᙹ ᇥඹ", "caption"), block("�ᯱഭ�� ,*4\" %# %BUB�")]
    assert garbled_blocks(body + broken) == broken


def test_a_heading_numbered_as_a_figure_is_a_caption():
    from app.parsers.base import _mark_captions

    def block(text, kind):
        return {"type": kind, "text": text, "level": 3}

    blocks = [block("\u56f3\u8868Ⅱ-1-1-6 \u6211が\u56fdの\u60c5\u5831\u5316\u6295\u8cc7の\u63a8\u79fb", "section_header"), block("（\u51fa\u5178）\u7dcf\u52d9\u7701", "paragraph"),
              block("Figure 3 shows the results of the second run in detail.", "paragraph"), block("3. Results", "section_header")]
    _mark_captions(blocks)
    assert [b["type"] for b in blocks] == ["caption", "paragraph", "paragraph", "section_header"]


def test_a_list_number_the_text_starts_with_is_not_written_twice():
    from app.parsers.base import block_markdown

    assert block_markdown({"type": "list_item", "text": "[12] Pick-it N.V.", "_marker": "[12]"}) == "- [12] Pick-it N.V."
    assert block_markdown({"type": "list_item", "text": "Overview", "_marker": "1."}) == "1. Overview"
    # A sheet prints "###" for a number too wide for its column: text, not a heading.
    assert block_markdown({"type": "paragraph", "text": "### MYOCPOC0JOA0"}) == "\\### MYOCPOC0JOA0"
    assert block_markdown({"type": "paragraph", "text": "#hashtag"}) is None


def test_an_exponent_boxed_on_its_own_joins_its_line_as_a_superscript():
    from app.parsers.base import _join_line_pieces

    line = {"type": "paragraph", "text": "하한: T1b = 139.100×P⁰", "lines": None,
            "regions": [{"pageIndex": 3, "bbox": {"x0": 0.1079, "y0": 0.4543, "x1": 0.2695, "y1": 0.4647}}]}
    power = {"type": "paragraph", "text": ".1438", "lines": None,
             "regions": [{"pageIndex": 3, "bbox": {"x0": 0.2696, "y0": 0.4503, "x1": 0.2985, "y1": 0.4552}}]}
    out = _join_line_pieces([power, line])
    assert [b["text"] for b in out] == ["하한: T1b = 139.100×P⁰.¹⁴³⁸"]


def test_continuation_marks_are_running_marks_not_text():
    from app.parsers.base import _CONTINUED

    for mark in ("(Continued)", "(뒷면에 계속)", "(다음 쪽에 계속)", "(continued on next page)"):
        assert _CONTINUED.fullmatch(mark)
    assert not _CONTINUED.fullmatch("(이 사업은 계속 추진된다)")
    from app.parsers.base import _DASH_CONTINUED

    # A patent's "-continued" over a table's rest (both columns' read as one block, too).
    assert _DASH_CONTINUED.fullmatch("-continued") and _DASH_CONTINUED.fullmatch("-continued -continued")
    assert not _DASH_CONTINUED.fullmatch("The method continued")


def test_a_title_drawn_twice_word_by_word_is_read_once_in_order():
    import pymupdf

    from app.parsers.textlayer import build_text, read_page

    doc = pymupdf.open()
    page = doc.new_page(width=600, height=400)
    for dx in (0, -1.6):  # WordArt with a shadow: every word placed on its own, twice
        for x, word in ((48, "LIGUE"), (160, "  DE"), (230, "   FOOTBALL")):
            page.insert_text((x + dx, 80 + dx), word, fontsize=30)
    pt = read_page(page, 0)
    text, _, _ = build_text(pt.fragments, 0, pt.width)
    assert text == "LIGUE DE FOOTBALL"


def test_single_letters_without_picture_evidence_are_preserved():
    from app.parsers.base import _without_letter_labels

    def block(text, page=0):
        return {"type": "paragraph", "text": text, "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.12}}]}

    blocks = [block("Do Programmers Refactor Often?"), block("R"), block("R"), block("R"), block("A", page=1), block("versus")]
    assert [b["text"] for b in _without_letter_labels(blocks)] == ["Do Programmers Refactor Often?", "R", "R", "R", "A", "versus"]


def test_opensymbol_glyphs_are_bullets_at_a_line_start_and_greek_in_equations():
    from types import SimpleNamespace
    import pymupdf

    from app.parsers.textlayer import read_page

    def char(c, x):
        return {"c": c, "bbox": (x, 100, x + 8, 112), "origin": (x, 110)}

    def page(lines):
        raw = {"blocks": [{"type": 0, "bbox": (0, 0, 500, 500), "lines": [
            {"dir": (1, 0), "wmode": 0, "spans": [{"font": font, "size": 12, "flags": 0, "chars": [char(c, x) for c, x in chars]}
                                                  for font, chars in spans]} for spans in lines]}]}
        return SimpleNamespace(rotation=0, rect=pymupdf.Rect(0, 0, 500, 500),
                               derotation_matrix=pymupdf.Matrix(1, 1), get_text=lambda *a, **k: raw,
                               get_fonts=lambda: [], get_drawings=lambda **k: [])

    bullet = page([[("OpenSymbol", [("", 10)]), ("Arial", [("D", 30), ("e", 38), ("a", 46), ("l", 54)])]])
    assert "".join(c.c for f in read_page(bullet, 0).fragments for c in f.chars) == "◆Deal"
    equation = page([[("Times", [("c", 10), ("o", 18), ("s", 26), ("2", 40)]), ("OpenSymbol", [("", 50)]),
                      ("OpenSymbol", [("", 60)])],
                     [("OpenSymbol", [("", 10)])]])
    assert "".join(c.c for f in read_page(equation, 0).fragments for c in f.chars) == "cos2π+π"


def test_page_numbers_at_the_page_edge_are_running_marks():
    from app.parsers.base import _mark_page_numbers

    def block(text, y0):
        return {"type": "paragraph", "text": text, "level": None,
                "regions": [{"pageIndex": 0, "bbox": {"x0": 0.88, "y0": y0, "x1": 0.9, "y1": y0 + 0.015}}]}

    blocks = [block("2", 0.938), block("- 14 -", 0.95), block("Page 3 of 20", 0.02), block("2", 0.5), block("2019", 0.6),
              # A workbook prints "Page 2" further from the edge; a bare number there may be a cell.
              block("Page 2", 0.905), block("17", 0.905)]
    _mark_page_numbers(blocks)
    assert [b["type"] for b in blocks] == ["page_footer", "page_footer", "page_header", "paragraph", "paragraph",
                                           "page_footer", "paragraph"]


@pytest.mark.skipif(get_settings().soffice_path is None, reason="LibreOffice not installed")
def test_symbols_libreoffice_draws_with_opensymbol_are_read_from_their_outlines(tmp_path):
    import subprocess

    import pymupdf

    from app.parsers import opensymbol
    from app.parsers.textlayer import build_text, read_page

    soffice = get_settings().soffice_path
    opensymbol.reference.cache_clear()
    # The PDF keeps U+F070 for both "π" (Symbol) and a box (Wingdings); only the glyph drawn differs.
    fonts = "".join(f'<style:font-face style:name="{f}" svg:font-family="&apos;{f}&apos;" style:font-charset="x-symbol"/>'
                    for f in ("Symbol", "Wingdings"))
    styles = "".join(f'<style:style style:name="{f}" style:family="text"><style:text-properties style:font-name="{f}"/>'
                     "</style:style>" for f in ("Symbol", "Wingdings"))

    def span(font, code):
        return f'<text:span text:style-name="{font}">{chr(code)}</text:span>'

    body = (f"<text:p>{span('Symbol', 0xF061)} = 2{span('Symbol', 0xF070)}r</text:p>"
            f"<text:p>{span('Wingdings', 0xF078)} Yes {span('Wingdings', 0xF070)} No</text:p>")
    (tmp_path / "s.fodt").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><office:document '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.3" '
        'office:mimetype="application/vnd.oasis.opendocument.text">'
        f"<office:font-face-decls>{fonts}</office:font-face-decls><office:automatic-styles>{styles}"
        f"</office:automatic-styles><office:body><office:text>{body}</office:text></office:body></office:document>",
        encoding="utf-8")
    subprocess.run([str(soffice), f"-env:UserInstallation=file://{tmp_path}/profile", "--headless", "--convert-to", "pdf",
                    "--outdir", str(tmp_path), str(tmp_path / "s.fodt")], check=True, capture_output=True, timeout=240)
    page = pymupdf.open(tmp_path / "s.pdf")[0]
    pt = read_page(page, 0)
    text, _, _ = build_text(pt.fragments, 0, pt.width)
    assert text == "α = 2πr ☒ Yes ◻ No"  # without the outlines: "☒ Yes π No"


def test_a_heading_with_only_pictures_below_it_on_a_slide_is_a_label():
    from app.parsers.base import _mark_slide_labels

    def block(kind, text, page=0):
        return {"type": kind, "text": text, "level": 2 if kind == "section_header" else None,
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.2}}]}

    blocks = [block("section_header", "T. gondii and S. mansoni"), block("section_header", "polarized Th1 response"),
              block("picture", ""), block("section_header", "polarized Th2 response"),
              block("section_header", "Conclusions", page=1), block("section_header", "In vivo", page=1),
              block("paragraph", "IL-4 is not required...", page=1)]
    _mark_slide_labels(blocks)
    assert [b["type"][:4] for b in blocks] == ["sect", "para", "pict", "para", "sect", "sect", "para"]


def test_an_end_of_proof_box_at_a_line_end_is_not_a_block():
    from app.parsers.base import _without_proof_marks

    def block(text, x0, y0, x1, y1):
        return {"type": "paragraph", "text": text, "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    blocks = [block("The lemma then follows from the definitions.", 0.1, 0.768, 0.63, 0.781),
              block("□", 0.88, 0.772, 0.894, 0.778),  # the proof's end mark
              block("□", 0.1, 0.80, 0.12, 0.81)]  # a box alone on its line (a form's check box)
    assert [b["text"] for b in _without_proof_marks(blocks)] == ["The lemma then follows from the definitions.", "□"]


def test_glyphs_without_unicode_are_read_from_type1_glyph_names():
    import binascii

    from app.parsers.glyph_names import meaning, type1_names

    assert [meaning(n) for n in ("summationtext.1", "braceleftbig", "parenleftBigg", "uni222B.dsp", "hatwide")] == \
        ["∑", "{", "(", "∫", "ˆ"]
    assert [meaning(n) for n in ("barex", "radicalbt", "parenlefttp", "github")] == ["|", "", "(", None]
    # A Type 1 program's private part is eexec-encrypted; its CharStrings order is the glyph id.
    plain = (b"\0\0\0\0/Private 1 dict dup begin end\n/CharStrings 3 dict dup begin\n"
             b"/summationtext 2 RD xx ND\n/.notdef 1 RD x ND\n/braceleftbig 3 RD xyz ND\nend\n")
    key, secret = 55665, bytearray()
    for p in plain:
        c = p ^ (key >> 8)
        secret.append(c)
        key = ((c + key) * 52845 + 22719) & 0xFFFF
    font = b"%!PS-AdobeFont-1.0: txexs\n/Encoding StandardEncoding def\ncurrentfile eexec\n" + binascii.hexlify(bytes(secret))
    assert type1_names(font) == [".notdef", "summationtext", "braceleftbig"]


def test_line_pieces_overlapping_by_a_repeated_character_join():
    from app.parsers.textlayer import _visual_lines, _without_seams

    # "셋째" and "째는 칩" drawn as two pieces sharing the "째" at the same place, then the next line.
    a, b = frag("셋째", 0.10, 0.1, advance=0.02), frag("째는 칩", 0.12, 0.1, advance=0.02)
    c, d = frag("적하고", 0.10, 0.13, advance=0.02), frag("고 코어", 0.14, 0.13, advance=0.02)
    lines = _visual_lines(_without_seams([a, c, b, d]))
    assert ["".join(ch.c for ch in line) for line in lines] == ["셋째는 칩", "적하고 코어"]
    # Boxes overlapping with different text stay apart.
    x, y = frag("ABCDEF", 0.10, 0.2), frag("uvwxyz", 0.13, 0.2)
    assert len(_visual_lines(_without_seams([x, y]))) == 2


def test_an_index_under_an_exponent_is_not_a_repeated_character():
    from app.parsers.textlayer import Char, Fragment, _without_seams

    # r with ² above and ₂ below at the same x: same middle, different lines.
    r = Fragment([Char(c="r", x0=0.10, y0=0.100, x1=0.11, y1=0.115, size=10, bold=False),
                  Char(c="2", x0=0.11, y0=0.096, x1=0.116, y1=0.106, size=7, bold=False)])
    low = Fragment([Char(c="2", x0=0.11, y0=0.110, x1=0.116, y1=0.120, size=7, bold=False),
                    Char(c="+", x0=0.12, y0=0.100, x1=0.13, y1=0.115, size=10, bold=False)])
    assert ["".join(c.c for c in f.chars) for f in _without_seams([r, low])] == ["r2", "2+"]


def test_check_boxes_are_items_that_say_whether_they_are_ticked():
    from app.parsers.base import block_markdown
    from app.parsers.docling_parser import LABEL_TO_TYPE

    assert LABEL_TO_TYPE["checkbox_selected"] == LABEL_TO_TYPE["checkbox_unselected"] == "list_item"
    item = {"type": "list_item", "text": "female", "level": None, "_marker": "☑", "_bullet": True}
    assert block_markdown(item) == "☑ female"


def test_slides_keep_their_own_text_and_a_logo_name_is_no_column():
    from app.parsers.base import _merge_continuations, _side_by_side_pages

    def block(kind, text, page, x0, y0, x1, y1):
        return {"type": kind, "text": text, "level": None,
                "regions": [{"pageIndex": page, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    logo = block("picture", "", 1, 0.02, 0.86, 0.19, 0.91)
    name = block("paragraph", "IHEP", 1, 0.07, 0.85, 0.2, 0.92)
    item = block("list_item", "IHEP is committed to improving college access", 1, 0.29, 0.65, 0.92, 0.89)
    assert 1 not in _side_by_side_pages([logo, name, item], pictures=False)
    end = block("paragraph", "Leadership Summit June 2012", 0, 0.1, 0.8, 0.5, 0.85)
    assert len(_merge_continuations([end, name])) == 1  # a document page may go on
    assert len(_merge_continuations([end, name], slides=True)) == 2  # a slide does not


def test_prose_read_as_code_is_a_paragraph():
    from app.parsers.base import _prose

    prose = " ".join(f"Sentence number {i} is plain prose about a garden. " for i in range(6))
    assert _prose(prose)
    assert not _prose("def f(x):\n    return x + 1  # Done. Next. Then. More. Last. End.")
    assert not _prose("Input: Graph G. Question: Does G have a proper coloring?")


def test_a_bullet_the_item_text_starts_with_is_not_doubled():
    from app.parsers.base import block_markdown

    assert block_markdown({"type": "list_item", "text": "□ 지원 배경", "_marker": None}) == "□ 지원 배경"
    assert block_markdown({"type": "list_item", "text": "Plain item", "_marker": None}) == "- Plain item"


def test_a_contents_line_at_the_page_edge_stays_in_the_contents():
    from app.parsers.base import _mark_toc

    def block(kind, text, y):
        return {"type": kind, "text": text, "level": None,
                "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": y, "x1": 0.5, "y1": y + 0.02}}]}

    blocks = [block("section_header", "저작권 1", 0.5), block("section_header", "본 문서에 대하여... 4", 0.55),
              block("section_header", "1. 수식 5", 0.6), block("page_footer", "1.1. 수식 편집기의 기본 개념 5", 0.94),
              block("page_footer", "- 1 -", 0.97)]
    _mark_toc(blocks)
    assert [b["type"] for b in blocks] == ["list_item"] * 4 + ["page_footer"]


def test_a_contents_page_read_as_one_paragraph_becomes_items():
    from app.parsers.base import _split_toc_paragraphs

    entries = ["1.1.1. 수식에 쓰이는 글씨체 5", "1.1.1.1. 영문 글꼴 명령 5", "1.1.1.2. 한글 글꼴 명령 6", "1.1.2. 수식 편집기 고유 규칙 6"]
    text, lines = "", []
    for i, e in enumerate(entries):
        start = len(text) + (1 if text else 0)
        text = f"{text} {e}" if text else e
        lines.append({"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1 + 0.03 * i, "x1": 0.5, "y1": 0.12 + 0.03 * i},
                      "start": start, "end": start + len(e), "xs": [0.1] * len(e)})
    block = {"type": "paragraph", "text": text, "level": None, "lines": lines,
             "regions": [{"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.22}}]}
    items = _split_toc_paragraphs([block])
    assert [(b["type"], b["text"]) for b in items] == [("list_item", e) for e in entries]
    assert items[2]["lines"][0]["start"] == 0 and items[2]["regions"][0]["bbox"]["y0"] == 0.16


def test_inventors_on_a_patent_front_page_are_not_headings():
    from app.parsers.base import _mark_patent_parties

    def block(kind, text, page=0):
        return {"type": kind, "text": text, "level": 2 if kind == "section_header" else None,
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}}]}

    front = [block("paragraph", "(21) 출원번호 10-2008-0008998"), block("list_item", "발명자"),
             block("section_header", "정미혜"), block("section_header", "삼성디스플레이 주식회사"),
             block("section_header", "(54) 발명의 명칭 액정 표시 장치")]
    other = [block("section_header", "서론", page=3), block("section_header", "결론", page=3)]
    _mark_patent_parties(front + other)
    assert [b["type"][:4] for b in front + other] == ["para", "list", "para", "para", "sect", "sect", "sect"]


def test_reference_numerals_on_drawing_pages_do_not_run_on():
    from app.parsers.base import _merge_continuations

    def block(text, page):
        return {"type": "paragraph", "text": text, "level": None,
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": 0.5, "x1": 0.3, "y1": 0.52}}]}

    assert len(_merge_continuations([block("205 300", 2), block("1", 3)])) == 2
    assert len(_merge_continuations([block("the supply line is connected to the", 2), block("pump and the valve.", 3)])) == 1


def test_a_patent_drawing_label_is_a_caption():
    from app.parsers.base import _mark_captions

    blocks = [{"type": "section_header", "text": "도면3", "level": 2,
               "regions": [{"pageIndex": 4, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.12}}]}]
    _mark_captions(blocks)
    assert blocks[0]["type"] == "caption"


def test_a_formula_read_after_the_text_below_it_goes_back_up():
    from app.parsers.base import _place_formulas_in_flow

    def block(kind, text, x0, y0, x1, y1):
        return {"type": kind, "text": text, "level": None,
                "regions": [{"pageIndex": 0, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]}

    # One column: "... Claim 2." follows the formula on the page but was read before it.
    page = [block("paragraph", "Take Σ finite and put", 0.12, 0.30, 0.87, 0.34), block("paragraph", "Claim 2. For all A", 0.12, 0.83, 0.68, 0.85),
            block("formula", "g U_i", 0.32, 0.35, 0.72, 0.37)]
    _place_formulas_in_flow(page)
    assert [b["text"] for b in page] == ["Take Σ finite and put", "g U_i", "Claim 2. For all A"]
    # Two columns: right-column formula read after "Therefore" below it.
    cols = [block("paragraph", "left text", 0.08, 0.1, 0.48, 0.9), block("paragraph", "Using we obtain", 0.52, 0.70, 0.56, 0.72),
            block("formula", "<r(0) x r(t)>", 0.56, 0.77, 0.92, 0.79), block("paragraph", "Therefore", 0.52, 0.86, 0.59, 0.88),
            block("formula", "[n(θ1) x n(θ2)]", 0.60, 0.73, 0.92, 0.74)]
    _place_formulas_in_flow(cols)
    assert [b["text"] for b in cols] == ["left text", "Using we obtain", "[n(θ1) x n(θ2)]", "<r(0) x r(t)>", "Therefore"]


def test_chart_markers_drawn_as_glyphs_are_not_picture_text():
    from app.parsers.base import _clean_picture_text

    pic = {"type": "picture", "text": "◆ 0.0370 o ◆◆◆◆◆ oooooo □□□□ Energy (eV)\nTime (s) ◆ o", "level": None,
           "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.5}}]}
    _clean_picture_text([pic])
    assert "◆" not in pic["text"] and "oooooo" in pic["text"] and "Energy (eV)" in pic["text"]


@pytest.mark.skipif(not Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists(), reason="DejaVu Sans not installed")
def test_a_whole_truetype_font_gives_glyphs_their_characters_through_its_cmap():
    import pymupdf

    from app.parsers.glyph_names import truetype_chars

    path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    chars = truetype_chars(Path(path).read_bytes(), "DejaVuSans")
    font = pymupdf.Font(fontfile=path)
    assert chars[font.has_glyph(ord("∑"))] == "∑" and chars[font.has_glyph(ord("A"))] == "A"


def test_a_caption_above_its_table_is_read_before_it():
    from app.parsers.base import _captions_before_figures

    def block(kind, text, y0, y1):
        return {"type": kind, "text": text, "level": None,
                "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": y0, "x1": 0.9, "y1": y1}}]}

    blocks = [block("table", "| a | b |", 0.2, 0.4), block("caption", "Table 1: Results", 0.15, 0.19),
              block("picture", "", 0.5, 0.7), block("caption", "Figure 1: Model", 0.71, 0.73)]
    assert [b["text"] for b in _captions_before_figures(blocks)] == ["Table 1: Results", "| a | b |", "", "Figure 1: Model"]


def test_tex_accents_drawn_over_their_letters_go_onto_them():
    from app.parsers.textlayer import _with_accents

    def line(*glyphs, size=10.0):
        # (character, x0, x1) in points, as PyMuPDF reports them (the accent's box over its letter's)
        return [Char(c=c, x0=x0 / W, y0=0.5, x1=x1 / W, y1=0.52, size=size, bold=False) for c, x0, x1 in glyphs]

    def text(chars):
        return "".join(c.c for c in _with_accents(chars, W))

    # "Fr´echet": the acute comes before the e it is drawn on.
    assert text(line(("r", 184.93, 188.32), ("´", 188.87, 192.19), ("e", 188.32, 192.83), ("c", 192.83, 197.34))) == "réc"
    # "D ˛EBIEC": the space before the ogonek lies on the E: no word break.
    assert text(line(("D", 135.96, 143.67), (" ", 143.67, 147.28), ("˛", 147.28, 150.40), ("E", 143.67, 149.75),
                     ("B", 149.75, 155.84))) == "DĘB"
    # "Gönc¸ et al.": a cedilla after its c.
    assert text(line(("c", 342.06, 345.60), ("¸", 342.51, 345.16), (" ", 345.16, 348.39), ("e", 348.39, 351.93))) == "ç e"
    # Math: no precomposed letter, so the letter and a combining mark, one character each.
    rho = _with_accents(line(("(", 206.27, 210.15), ("˜", 211.06, 216.05), ("ρ", 210.15, 215.30)), W)
    assert [c.c for c in rho] == ["(", "ρ", "̃"]
    # \not = : the slash of no width negates the relation after it.
    assert text(line(("∗", 498.10, 502.33), ("̸", 502.33, 502.33), (" ", 502.33, 506.25), ("=", 506.25, 515.36),
                     size=12.0)) == "∗ ≠"
    # In math a space before the accent stays when there is a gap: "𝔽p ˆG" is not "𝔽pĜ".
    assert text(line(("p", 264.72, 268.83), (" ", 268.83, 271.58), ("ˆ", 271.58, 276.56), ("G", 269.33, 277.16))) == "p Ĝ"
    # An accent over a dotless i: "na¨ıve".
    assert text(line(("a", 100.0, 105.0), ("¨", 105.2, 107.0), ("ı", 105.0, 107.8), ("v", 107.8, 112.0))) == "aïv"
    # A typed acute beside a letter is an apostrophe, not an accent.
    assert text(line(("n", 100.0, 105.0), ("´", 105.0, 107.5), ("t", 107.5, 110.0))) == "n´t"


def test_tex_accents_in_text_without_positions():
    from app.parsers.textlayer import tex_accents

    assert tex_accents("Fr´echet and Erd˝os–R´enyi") == "Fréchet and Erdős–Rényi"
    assert tex_accents("Educa¸c˜ao, G¨onc¸ et al.") == "Educação, Gönç et al."
    assert tex_accents("Jo˜ao B. R. Silva") == "João B. R. Silva"
    assert tex_accents("na¨ıve, Sab´ın") == "naïve, Sabín"
    # Math letters without a precomposed form get a combining mark.
    assert tex_accents("˜ρ") == "ρ\u0303"
    assert tex_accents("b\u0338 = p and x\u0338∈A") == "b ≠ p and x∉A"
    # Quotes and code keep their marks; an accent before a digit is no letter's.
    assert tex_accents("``must'' and `x` and [H¨07]") == "``must'' and `x` and [H¨07]"


def test_a_workbook_block_over_several_regions_is_one_block_per_region():
    from app.parsers.base import _one_region_each

    def line(page, y, start, end):
        return {"pageIndex": page, "bbox": {"x0": 0.1, "y0": y, "x1": 0.3, "y1": y + 0.01}, "start": start, "end": end,
                "xs": [0.1] * (end - start)}

    text = "Comment Page 4"
    block = {"type": "paragraph", "text": text, "level": None,
             "regions": [{"pageIndex": 3, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.21}},
                         {"pageIndex": 3, "bbox": {"x0": 0.1, "y0": 0.91, "x1": 0.3, "y1": 0.92}}],
             "lines": [line(3, 0.2, 0, 7), line(3, 0.91, 8, 14)]}
    first, second = _one_region_each([block])
    assert (first["text"], second["text"]) == ("Comment", "Page 4")
    assert second["lines"][0]["start"] == 0 and second["regions"][0]["bbox"]["y0"] == 0.91
    # Without lines saying which text is where, the block stays as it is.
    assert _one_region_each([{**block, "lines": None}]) == [{**block, "lines": None}]


def test_a_running_head_repeats_and_text_at_the_page_edge_that_does_not_is_text():
    from app.parsers.base import _furniture_that_repeats

    def block(kind, text, page, y0):
        return {"type": kind, "text": text, "level": None,
                "regions": [{"pageIndex": page, "bbox": {"x0": 0.1, "y0": y0, "x1": 0.9, "y1": y0 + 0.02}}]}

    blocks = [block("page_header", "Project plan 2026", 0, 0.02), block("paragraph", "Body", 0, 0.3),
              block("page_header", "Project plan 2026", 1, 0.02), block("page_header", "데이터 구축으로 정량적 평가와 신뢰성 확보", 1, 0.04),
              block("page_footer", "- 2 -", 1, 0.95), block("page_footer", "연구유형 기초연구 ( )", 1, 0.93)]
    _furniture_that_repeats(blocks)
    assert [b["type"] for b in blocks] == ["page_header", "paragraph", "page_header", "paragraph", "page_footer", "paragraph"]
    # The foot stays a piece of its own; the head goes on from the page before.
    from app.parsers.base import _merge_continuations

    pages = [block("paragraph", "데이터를 모아", 0, 0.5), block("page_header", "추진 일정에 따라 구축한다", 1, 0.02),
             block("paragraph", "Body text.", 1, 0.3), block("page_footer", "Odd page footer", 1, 0.95),
             block("paragraph", "The quick brown fox jumped over the lazy dog.", 2, 0.1)]
    _furniture_that_repeats(pages)
    assert [b["text"] for b in _merge_continuations(pages)] == [
        "데이터를 모아 추진 일정에 따라 구축한다", "Body text.", "Odd page footer", "The quick brown fox jumped over the lazy dog."]


def _at(name, kind, page, y0, y1, text=None, x0=0.1, x1=0.9, more=()):
    regions = [{"pageIndex": page, "bbox": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]
    regions += [{"pageIndex": p, "bbox": {"x0": x0, "y0": a, "x1": x1, "y1": b}} for p, a, b in more]
    return {"type": kind, "text": name if text is None else text, "name": name, "level": None, "regions": regions}


def test_a_table_read_after_the_list_around_it_goes_back_to_its_place():
    from app.parsers.base import _late_blocks_in_place

    # The layout model read a list running over pages as one group, and the tables between its items after it.
    blocks = [_at("i1", "list_item", 16, 0.09, 0.11), _at("i2", "list_item", 16, 0.24, 0.27),
              _at("i3", "list_item", 17, 0.80, 0.82), _at("head18", "page_header", 18, 0.01, 0.02),
              _at("i4", "list_item", 18, 0.08, 0.11), _at("t1", "table", 16, 0.14, 0.24),
              _at("t2", "table", 17, 0.85, 0.95, more=[(18, 0.03, 0.08)])]
    assert [b["name"] for b in _late_blocks_in_place(blocks, set())] == ["i1", "t1", "i2", "i3", "t2", "head18", "i4"]
    # On a page with columns a block read after the next page goes back into its column.
    cols = [_at("l1", "paragraph", 4, 0.1, 0.5, x0=0.1, x1=0.45), _at("r1", "paragraph", 4, 0.5, 0.9, x0=0.55, x1=0.9),
            _at("next", "paragraph", 5, 0.1, 0.9), _at("fig", "picture", 4, 0.1, 0.45, x0=0.55, x1=0.9)]
    assert [b["name"] for b in _late_blocks_in_place(cols, {4})] == ["l1", "fig", "r1", "next"]
    # A word under a drawing three pages on, read after the references of page 2, goes to its page.
    early = [_at("refs", "paragraph", 1, 0.1, 0.3), _at("home", "paragraph", 6, 0.48, 0.49, "Home"),
             _at("fig1", "picture", 2, 0.1, 0.9), _at("fig2", "picture", 3, 0.1, 0.9),
             _at("fig5", "picture", 6, 0.1, 0.47), _at("fig6", "picture", 7, 0.1, 0.9)]
    assert [b["name"] for b in _late_blocks_in_place(early, set())] == ["refs", "fig1", "fig2", "fig5", "home", "fig6"]


def test_nothing_runs_on_past_a_table_of_a_word_or_hangul_document():
    from app.parsers.base import _merge_continuations

    blocks = [_at("note", "paragraph", 15, 0.57, 0.63, "※ cold air from the north west meets warm air and"),
              _at("t1", "table", 15, 0.65, 0.93), _at("t2", "table", 16, 0.03, 0.57),
              _at("next", "paragraph", 16, 0.57, 0.62, "the biggest damage of the last ten years was in 2005")]
    assert len(_merge_continuations([dict(b) for b in blocks], flow=True)) == 4
    # In a PDF a figure set between two pages' text may sit inside a paragraph...
    assert len(_merge_continuations([dict(b) for b in blocks])) == 3
    # ...but not one that itself runs over the page.
    over = [blocks[0], _at("t", "table", 15, 0.65, 0.93, more=[(16, 0.03, 0.57)]), blocks[3]]
    assert len(_merge_continuations([dict(b) for b in over])) == 3


def test_a_note_taken_for_the_running_foot_is_a_footnote():
    from app.parsers.base import _notes_taken_for_footers

    blocks = [_at("n1", "page_footer", 8, 0.93, 0.94, "＊16 Alphabet (Google), Amazon, Meta, Apple, Microsoft"),
              _at("f8", "page_footer", 8, 0.96, 0.97, "112 White Paper 2024 Part II"),
              _at("d8", "page_footer", 8, 0.95, 0.96, "※ Figures are provisional"),
              _at("d9", "page_footer", 9, 0.95, 0.96, "※ Figures are provisional"),
              _at("f9", "page_footer", 9, 0.96, 0.97, "113 White Paper 2024 Part II")]
    _notes_taken_for_footers(blocks)
    assert [b["type"] for b in blocks] == ["footnote", "page_footer", "page_footer", "page_footer", "page_footer"]


def test_an_empty_ornament_in_the_same_corner_of_pages_is_furniture():
    from app.parsers.base import _decorations_that_repeat

    blocks = [_at("arc2", "picture", 2, 0.0, 0.06, "", 0.0, 0.104), _at("arc8", "picture", 8, 0.0, 0.06, "", 0.0, 0.105),
              _at("logo", "picture", 3, 0.0, 0.06, "", 0.4, 0.6), _at("chart", "picture", 2, 0.11, 0.36, "", 0.1, 0.48)]
    _decorations_that_repeat(blocks)
    assert [b["type"] for b in blocks] == ["page_header", "page_header", "picture", "picture"]


def test_template_pictures_and_boxes_repeated_on_slides_are_furniture():
    from app.parsers.base import _decorations_that_repeat

    logo = lambda p: _at(f"logo{p}", "picture", p, 0.01, 0.11, "A seal with a tree", 0.0, 0.09)  # noqa: E731
    photo = lambda p: _at(f"photo{p}", "picture", p, 0.2, 0.8, "", 0.1, 0.9)  # noqa: E731
    box = lambda p: _at(f"box{p}", "paragraph", p, 0.8, 0.83, "MASTERTEXTBOX", 0.5, 0.6)  # noqa: E731
    title = lambda p: _at(f"title{p}", "section_header", p, 0.02, 0.08, "Introduction", 0.2, 0.8)  # noqa: E731
    blocks = [f(p) for p in (1, 2, 3) for f in (logo, photo, box, title)]
    _decorations_that_repeat(blocks, slides=True)
    assert {b["name"][:-1]: b["type"] for b in blocks} == {
        "logo": "page_header", "photo": "picture", "box": "page_footer", "title": "section_header"}
    # On two slides only, or in a document, they stay.
    blocks = [f(p) for p in (1, 2) for f in (logo, box)]
    _decorations_that_repeat(blocks, slides=True)
    assert [b["type"] for b in blocks] == ["picture", "paragraph"] * 2


def test_a_footnote_read_as_paragraphs_is_a_footnote():
    from app.parsers.base import _notes_at_the_foot

    blocks = [_at("body", "paragraph", 2, 0.5, 0.86, "The contract is concluded with"),
              _at("n1", "paragraph", 2, 0.87, 0.88, "¹ In accordance with article 34 of the federal law"),
              _at("n1b", "paragraph", 2, 0.884, 0.92, "the contract is concluded with a natural person"),
              _at("top", "paragraph", 3, 0.07, 0.2, "an individual entrepreneur, as follows."),
              _at("star", "paragraph", 3, 0.6, 0.62, "* rows marked are optional"),
              _at("after", "table", 3, 0.63, 0.8)]
    _notes_at_the_foot(blocks)
    assert [b["type"] for b in blocks] == ["paragraph", "footnote", "footnote", "paragraph", "paragraph", "table"]


def test_running_text_repeated_at_the_page_edge_is_furniture():
    from app.parsers.base import _repeated_running_text

    blocks = [_at("f2", "paragraph", 1, 0.938, 0.955, "Even page footer"), _at("f4", "paragraph", 3, 0.938, 0.955, "Even page footer"),
              _at("f6", "paragraph", 5, 0.938, 0.955, "Even page footer"), _at("first", "paragraph", 0, 0.938, 0.955, "First page footer"),
              _at("h", "paragraph", 1, 0.03, 0.05, "Chapter 2 - Results"), _at("h2", "paragraph", 2, 0.03, 0.05, "Chapter 3 - Results"),
              _at("h3", "paragraph", 3, 0.03, 0.05, "Chapter 4 - Results"), _at("body", "paragraph", 1, 0.4, 0.5, "Even page footer"),
              _at("row11", "paragraph", 11, 0.93, 0.95, "Field / team  Name"), _at("row16", "paragraph", 16, 0.92, 0.94, "Field / team  Name")]
    _repeated_running_text(blocks)
    assert [b["type"] for b in blocks] == ["page_footer"] * 3 + ["paragraph"] + ["page_header"] * 3 + ["paragraph"] * 3
    # Copies the layout model already took for running feet count.
    mixed = [_at("f2", "page_footer", 1, 0.938, 0.955, "Even page footer"), _at("f4", "page_footer", 3, 0.938, 0.955, "Even page footer"),
             _at("f6", "paragraph", 5, 0.938, 0.955, "Even page footer")]
    _repeated_running_text(mixed)
    assert [b["type"] for b in mixed] == ["page_footer"] * 3


def test_a_short_title_right_above_a_figure_is_its_caption_and_never_runs_on():
    from app.parsers.base import _mark_captions, _merge_continuations

    blocks = [_at("end", "paragraph", 6, 0.9, 0.93, "Result example: si n, c os, a n d"),
              _at("title", "paragraph", 7, 0.034, 0.053, "Basic commands"), _at("table", "table", 7, 0.072, 0.5),
              _at("t2", "paragraph", 7, 0.6, 0.62, "Greek letters()"), _at("one", "paragraph", 7, 0.64, 0.66, "1", 0.2, 0.25),
              _at("pic", "picture", 7, 0.635, 0.95), _at("sentence", "paragraph", 8, 0.1, 0.12, "See the table below."),
              _at("t3", "table", 8, 0.13, 0.3)]
    _mark_captions(blocks)
    assert [b["type"] for b in blocks] == ["paragraph", "caption", "table", "caption", "paragraph", "picture", "paragraph", "table"]
    # A table whose part on the page is a row of cells: their span is under the title.
    cells = _at("cells", "table", 1, 0.556, 0.574, x0=0.1, x1=0.137, more=[(1, 0.556, 0.574), (2, 0.03, 0.96)])
    cells["regions"][1]["bbox"].update(x0=0.6, x1=0.68)
    title = _at("title", "paragraph", 1, 0.496, 0.524, "Annex: launcher compared", x0=0.1, x1=0.68)
    _mark_captions([title, cells])
    assert title["type"] == "caption"
    assert len(_merge_continuations(blocks)) == len(blocks)


def test_a_slide_title_shown_on_a_few_slides_is_a_heading():
    from app.parsers.base import _slide_titles_that_repeat

    blocks = [_at(f"body{p}", "paragraph", p, 0.3, 0.6) for p in range(10)]
    blocks += [_at("t3", "page_header", 3, 0.05, 0.12, "3 Research Questions"), _at("t7", "page_header", 7, 0.05, 0.12, "3 Research Questions")]
    blocks += [_at(f"deck{p}", "page_header", p, 0.02, 0.04, "Acme Corp Strategy") for p in range(10)]
    blocks += [_at(f"big{p}", "page_header", p, 0.02, 0.09, "Annual Report 2024") for p in range(10)]
    _slide_titles_that_repeat(blocks)
    kinds = {b["name"]: b["type"] for b in blocks}
    assert kinds["t3"] == kinds["t7"] == "section_header"
    assert kinds["deck2"] == "page_header" and kinds["big2"] == "page_header"


def test_images_no_picture_was_found_for_become_pictures():
    import pymupdf

    from app.parsers.hybrid_parser import _images_as_pictures

    logo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
    logo.clear_with(30)
    pdf = pymupdf.open()
    for n in range(3):
        page = pdf.new_page(width=600, height=800)
        page.insert_image(pymupdf.Rect(20, 20, 140, 100), pixmap=logo)  # the same logo on every page
    def image(shade: int) -> "pymupdf.Pixmap":
        one = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        one.clear_with(shade)
        return one

    pdf[0].insert_image(pymupdf.Rect(60, 300, 400, 600), pixmap=image(120), keep_proportion=False)  # a drawing alone on its page
    pdf[1].insert_image(pymupdf.Rect(60, 300, 400, 600), pixmap=image(140), keep_proportion=False)  # one the layout model found
    for k in range(8):  # a scanned page in strips
        pdf[2].insert_image(pymupdf.Rect(0, k * 100, 600, (k + 1) * 100), pixmap=image(150 + k), keep_proportion=False)
    blocks = [{"type": "paragraph", "text": "Drawings", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.1, "y0": 0.2, "x1": 0.5, "y1": 0.25}}]},
              {"type": "picture", "text": "", "regions": [{"pageIndex": 1, "bbox": {"x0": 0.1, "y0": 0.37, "x1": 0.67, "y1": 0.75}}]}]
    stats = {"images": 0}
    out = _images_as_pictures(blocks, pdf, stats)
    assert stats["images"] == 1
    assert [b["type"] for b in out] == ["paragraph", "picture", "picture"]
    assert out[1]["regions"][0]["pageIndex"] == 0 and round(out[1]["regions"][0]["bbox"]["y0"], 3) == 0.375
    # A picture found inside a larger image (its margin left out) is that image.
    framed = [{"type": "picture", "text": "", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.2, "y0": 0.42, "x1": 0.55, "y1": 0.7}}]}]
    assert [b["regions"][0]["pageIndex"] for b in _images_as_pictures(framed, pdf, {"images": 0})] == [0, 1]
    # An image with words set on it is their background (a diagram's shapes, a banner).
    worded = [{"type": "paragraph", "text": "Reading Strand Key Ideas", "regions": [{"pageIndex": 0, "bbox": {"x0": 0.2, "y0": 0.5, "x1": 0.4, "y1": 0.55}}]},
              {"type": "paragraph", "text": "12 14", "regions": [{"pageIndex": 1, "bbox": {"x0": 0.2, "y0": 0.5, "x1": 0.3, "y1": 0.52}}]}]
    assert [b["regions"][0]["pageIndex"] for b in _images_as_pictures(worded, pdf, {"images": 0}) if b["type"] == "picture"] == [1]
    # A logo big enough to count, in the same place on every page, stays out.
    big = pymupdf.open()
    for n in range(3):
        big.new_page(width=600, height=800).insert_image(pymupdf.Rect(0, 0, 200, 120), pixmap=logo, keep_proportion=False)
    assert _images_as_pictures([], big, {"images": 0}) == []


def test_a_picture_of_an_image_drawn_on_every_slide_is_decoration():
    import pymupdf

    from app.parsers.hybrid_parser import _pictures_of_repeated_images

    band = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 10), False)
    band.clear_with(200)
    pdf = pymupdf.open()
    blocks = []
    for n in range(3):
        page = pdf.new_page(width=800, height=600)
        page.insert_image(pymupdf.Rect(56, 36, 744, 138), pixmap=band, keep_proportion=False)
        photo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 30), False)
        photo.clear_with(40 + n)
        page.insert_image(pymupdf.Rect(80, 200, 720, 560), pixmap=photo, keep_proportion=False)
        blocks += [{"type": "picture", "text": "", "regions": [{"pageIndex": n, "bbox": {"x0": 0.07, "y0": 0.06, "x1": 0.93, "y1": 0.23}}]},
                   {"type": "picture", "text": "", "regions": [{"pageIndex": n, "bbox": {"x0": 0.1, "y0": 0.33, "x1": 0.9, "y1": 0.93}}]}]
    _pictures_of_repeated_images(blocks, pdf)
    assert [b["type"] for b in blocks] == ["page_header", "picture"] * 3


@pytest.mark.parametrize("moving", [False, True])
def test_reused_body_photos_are_kept_even_on_three_pages(moving):
    import copy
    import pymupdf
    from app.parsers.hybrid_parser import _images_as_pictures, _pictures_of_repeated_images

    photo = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 40), False)
    photo.clear_with(120)
    with pymupdf.open() as pdf:
        blocks = []
        for page_index in range(3):
            # Repeated illustrations in the body are content. Moving photos near
            # the top are not a running logo either.
            y = 100 + 80 * page_index if moving else 300
            page = pdf.new_page(width=600, height=800)
            page.insert_image(pymupdf.Rect(60, y, 240, y + 120), pixmap=photo, keep_proportion=False)
            blocks.append({"type": "picture", "text": "", "regions": [{"pageIndex": page_index,
                "bbox": {"x0": .1, "y0": y / 800, "x1": .4, "y1": (y + 120) / 800}}]})
        classified = copy.deepcopy(blocks)
        _pictures_of_repeated_images(classified, pdf)
        assert [b["type"] for b in classified] == ["picture"] * 3
        recovered = _images_as_pictures([], pdf, {"images": 0})
        assert [b["regions"][0]["pageIndex"] for b in recovered] == [0, 1, 2]


def test_fake_bold_partial_text_runs_do_not_repeat_their_containing_line():
    import pymupdf
    from app.parsers.textlayer import read_page, build_text

    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        line = 'Morgan Lee Member'
        page.insert_text((50, 100), line, fontsize=12)
        # One fake-bold pass was emitted as shorter PDF runs instead of one line.
        page.insert_text((50.5, 100.5), 'Morgan Lee', fontsize=12)
        x = 50 + pymupdf.get_text_length('Morgan Lee ', fontsize=12)
        page.insert_text((x + .5, 100.5), 'Member', fontsize=12)
        # The same name genuinely appears in the next row and another column.
        page.insert_text((50, 125), 'Morgan Lee', fontsize=12)
        page.insert_text((300, 100), 'Morgan Lee', fontsize=12)
        native = read_page(page, 0)
        text, _, _ = build_text(native.fragments, 0, native.width)
        assert text.count('Morgan Lee') == 3
        assert text.count('Member') == 1


def test_partial_overprint_match_requires_matching_glyph_positions_and_size():
    import pymupdf
    from app.parsers.textlayer import read_page, build_text

    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_text((50, 100), 'Status active', fontsize=12)
        # Similar words with different glyph advances are a separate overlaid annotation.
        page.insert_text((50.5, 100.5), 'Status', fontsize=10)
        native = read_page(page, 0)
        text, _, _ = build_text(native.fragments, 0, native.width)
        assert text.count('Status') == 2
