"""Synthetic heading trees; no document-specific titles or corpus snippets."""
import copy

import pymupdf
import pytest

from app.models import BlockRegion, ParsedDocument
from app.parsers.heading_structure import (
    Evidence, Style, apply_heading_evidence, assign_heading_levels, printed_marker,
)


def document(spec, fmt="pdf"):
    blocks = [dict(id=f"b{i}", readingOrder=i, type=kind, text=text, level=level,
                   regions=[], source=dict(parser="pymupdf")) for i, (kind, text, level) in enumerate(spec)]
    return ParsedDocument.model_validate(dict(
        document=dict(id="synthetic", filename="synthetic.pdf", fileHash="synthetic", pageCount=1,
                      sourceFormat=fmt, parser="pymupdf", parserVersion="test"),
        pages=[dict(index=0, width=600, height=800)], blocks=blocks, blocksFingerprint="unchanged"))


def parents(parsed):
    result, stack = {}, []
    for b in parsed.blocks:
        if b.type not in {"title", "section_header"}:
            continue
        while stack and stack[-1].level >= b.level:
            stack.pop()
        result[b.text] = stack[-1].text if stack else None
        assert b.level == (stack[-1].level + 1 if stack else 0 if b.type == "title" else 1)
        stack.append(b)
    return result


def apply(spec, sizes, outline=(), fmt="pdf"):
    parsed = document(spec, fmt)
    before = copy.deepcopy(parsed.model_dump())
    apply_heading_evidence(parsed, Evidence({f"b{i}": Style(s, 1) for i, s in enumerate(sizes)}, list(outline)))
    after = parsed.model_dump()
    for left, right in zip(before["blocks"], after["blocks"]):
        if left["type"] in {"title", "section_header"}:
            for k in ("level", "markdown"):
                left.pop(k); right.pop(k)
    assert before == after
    parents(parsed)
    return parsed


def h(text, level=4):
    return "section_header", text, level


def test_numbered_same_font_headings_have_consecutive_tree_depths():
    p = apply([("title", "Manual", 0), h("I. Scope"), h("1. Inputs"), h("1.1 Fields"),
               h("1.2 Values"), h("2. Outputs"), h("II. Execution"), h("1. Steps")], [20] + [12] * 7)
    assert [b.level for b in p.blocks] == [0, 1, 2, 3, 3, 2, 1, 2]


def test_reused_counters_and_non_number_digits_respect_distinct_font_styles():
    p = apply([h("1. Large"), h("1. Small"), h("2. Small"), h("2. Large"),
               h("2027 plan"), h("3.5inch devices"), h("24hour operation")], [18, 12, 12, 18, 14, 12, 14])
    assert parents(p) == {"1. Large": None, "1. Small": "1. Large", "2. Small": "1. Large",
                          "2. Large": None, "2027 plan": "2. Large", "3.5inch devices": "2027 plan",
                          "24hour operation": "2. Large"}


def test_numbered_siblings_resume_after_a_quoted_outline_and_cover():
    p = apply([h("I. First"), h("1. A"), h("\uac00. Detail"),
               h("\uc81c1\uc7a5 Quoted"), h("\uc81c2\uc7a5 Quoted"), h("\ub098. More"),
               h("Sample cover"), h("Organisation"), h("II. Second")], [16, 13, 11.5, 12, 12, 11.5, 20, 14, 16])
    assert parents(p)["\ub098. More"] == "1. A"
    assert parents(p)["II. Second"] is None


def test_counter_cannot_resurrect_a_parent_from_an_earlier_chapter():
    p = apply([h("Chapter A"), h("1. Old"), h("Chapter B"), h("2. New")], [18, 12, 18, 12])
    assert parents(p)["2. New"] == "Chapter B"


def test_missing_numbered_parent_never_uses_the_previous_chapter():
    p = apply([h("2. Prior"), h("2.1 Old"), h("3.1 Missing parent"), h("3.2 Peer")], [16, 12, 12, 12])
    assert parents(p)["3.1 Missing parent"] is None
    assert parents(p)["3.2 Peer"] is None


def test_outlines_disambiguate_equal_fonts_beyond_markdown_depth():
    specs = [("title", "Root", 0)] + [h(f"Depth {i}") for i in range(1, 9)] + [h("Sibling")]
    toc = [(i + 1, f"b{i}") for i in range(9)] + [(2, "b9")]
    p = apply(specs, [20] + [11] * 9, toc)
    assert [b.level for b in p.blocks] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 1]
    assert p.blocks[8].markdown == "###### Depth 8"
    assert parents(p)["Sibling"] == "Root"


def test_missing_outline_heading_is_not_filled_by_an_unrelated_sibling():
    p = apply([h("First"), h("Child A"), h("Child B")], [16, 12, 12],
              [(1, "b0"), (2, "b1"), (1, None), (2, "b2")])
    assert parents(p)["Child B"] is None


@pytest.mark.parametrize("fmt", ["pptx", "odp", "xlsx", "csv", "numbers"])
def test_page_oriented_formats_only_compress_existing_levels(fmt):
    p = apply([("title", "Deck", 0), h("1. Slide", 1), h("1.1 Sub", 1), h("Note", 4)],
              [20, 20, 12, 10], fmt=fmt)
    assert [b.level for b in p.blocks] == [0, 1, 1, 2]


def test_scanned_page_without_font_evidence_keeps_existing_parent_relations():
    p = apply([h("First", 2), h("Child", 38), h("Second", 2)], [0, 0, 0])
    assert [b.level for b in p.blocks] == [1, 2, 1]


@pytest.mark.parametrize("text,kind,depth", [
    ("\u2163. Summary", "roman", 0), (". \u2161 Summary", "roman", 0),
    ("1.2.3. Detail", "dotted", 3), ("(4) Detail", "paren", 4),
    ("\ub098. Detail", "hangul", 3), ("\uc81c2\uc808 Detail", "legal", 1),
])
def test_printed_number_forms(text, kind, depth):
    m = printed_marker(text)
    assert m and (m.kind, m.depth) == (kind, depth)


@pytest.mark.parametrize("text", ["2027 planning", "3.5inch drive", "24hour service", "MIXED text", "ordinary text"])
def test_text_that_only_looks_numeric_is_not_a_numbered_heading(text):
    assert printed_marker(text) is None


def test_pdf_bookmarks_and_font_evidence_use_the_converted_page(tmp_path):
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    texts = ["Main", "Child", "Deep"]
    for i, text in enumerate(texts):
        page.insert_text((60, 80 + 60 * i), text, fontsize=12, fontname="hebo")
    pdf.set_toc([[1, "Main", 1], [2, "Child", 1], [3, "Deep", 1]])
    path = tmp_path / "source.pdf"; pdf.save(path); pdf.close()
    p = document([h(t, 1) for t in texts])
    with pymupdf.open(path) as pdf:
        for b in p.blocks:
            r = pdf[0].search_for(b.text)[0]
            b.regions = [BlockRegion.model_validate(dict(pageIndex=0, bbox=dict(x0=r.x0/600, y0=r.y0/800, x1=r.x1/600, y1=r.y1/800)))]
        p = ParsedDocument.model_validate(p.model_dump())
    assign_heading_levels(p, path)
    assert [b.level for b in p.blocks] == [1, 2, 3]


def test_counter_sequence_can_resume_after_a_missing_or_differently_sized_number():
    p = apply([h("I. Chapter"), h("1. One"), h("2. Two"), h("3. Three"),
               h("4. Four")], [21, 17, 17, 18, 17])
    assert parents(p)["4. Four"] == "I. Chapter"


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_fonts_use_display_coordinates(tmp_path, rotation):
    pdf = pymupdf.open(); page = pdf.new_page(width=600, height=800)
    page.insert_text((60, 80), "Large", fontsize=18, fontname="hebo")
    page.insert_text((60, 140), "Small", fontsize=12, fontname="hebo")
    page.set_rotation(rotation)
    path = tmp_path / "source.pdf"; pdf.save(path); pdf.close()
    p = document([h("Large", 1), h("Small", 1)])
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        for b in p.blocks:
            r = page.search_for(b.text)[0] * page.rotation_matrix
            b.regions = [BlockRegion.model_validate(dict(pageIndex=0, bbox=dict(
                x0=r.x0/page.rect.width, y0=r.y0/page.rect.height,
                x1=r.x1/page.rect.width, y1=r.y1/page.rect.height)))]
    assign_heading_levels(p, path)
    assert [b.level for b in p.blocks] == [1, 2]


def test_missing_numbered_parent_cannot_hide_behind_an_unnumbered_heading():
    p = apply([h("2. Prior"), h("Unnumbered subsection"), h("3.1 Missing parent")], [18, 14, 12])
    assert parents(p)["3.1 Missing parent"] is None


def test_decimal_measurements_do_not_break_their_numbered_chapter():
    p = apply([h("1. Equipment"), h("1.1 Settings"), h("3.5 inch drive"),
               h("24 hour support"), h("2. Load"), h("0.5 kg payload")], [18, 14, 14, 14, 18, 14])
    assert parents(p)["3.5 inch drive"] == "1. Equipment"
    assert parents(p)["0.5 kg payload"] == "2. Load"
    assert printed_marker("2.4 GHz operation") is None
    assert printed_marker("3.5. inch equipment").parts == (3, 5)
    assert printed_marker("3.5 In depth").parts == (3, 5)


def test_number_marker_can_touch_its_label_without_becoming_a_decimal_prefix():
    p = apply([h("2.Prior"), h("2.1 Detail"), h("3.1 Missing parent")], [18, 14, 14])
    assert parents(p)["2.1 Detail"] == "2.Prior"
    assert parents(p)["3.1 Missing parent"] is None
    assert printed_marker("3.5inch drive") is None


def test_ambiguous_numeric_ancestor_can_withhold_but_cannot_establish_numbered_depth():
    p = apply([h("9 8 Overview"), h("2.Prior"), h("3.1 Missing parent")], [20, 18, 14])
    assert parents(p)["3.1 Missing parent"] is None
    assert printed_marker("9 8 Overview") is None
    p = apply([("title", "24 hour guide", 0), h("3.1 Details")], [20, 14])
    assert parents(p)["3.1 Details"] == "24 hour guide"


def test_a_document_titles_number_does_not_override_its_root_role():
    p = apply([("title", "1. Operator guide", 0), h("2.1 Details")], [20, 14])
    assert parents(p)["2.1 Details"] == "1. Operator guide"


def test_source_roots_do_not_inherit_an_unbookmarked_heading_from_the_previous_chapter():
    p = apply([("title", "Manual", 0), h("First"), h("Inserted example"), h("Second")],
              [30, 16, 20, 16], [(1, "b1"), (1, "b3")])
    assert parents(p)["First"] == parents(p)["Second"] == "Manual"
    p = apply([h("First"), h("Inserted example"), h("Orphan")], [16, 20, 12],
              [(1, "b0"), (1, None), (2, "b2")])
    assert parents(p)["Orphan"] is None


@pytest.mark.parametrize("delimiter", [".", ")"])
@pytest.mark.parametrize("size", [0, 12])
def test_unspaced_dotted_headings_retain_numbered_parents_without_font_evidence(delimiter, size):
    first = "4.1" + delimiter + "Detail"
    deep = "4.1.1" + delimiter + "Subdetail"
    peer = "4.2" + delimiter + "Other"
    p = apply([h("Summary", 1), h("4.Main", 1), h(first, 1), h(deep, 1),
               h(peer, 1), h("5.Next", 1)], [size] * 6)
    tree = parents(p)
    assert tree[first] == tree[peer] == "4.Main"
    assert tree[deep] == first
    assert p.blocks[1].level == p.blocks[-1].level


@pytest.mark.parametrize("text", ["4.1kg payload", "4.1 inch drive", "4.1.2kg payload",
                                  "4.12.2027", "4.1.2Title"])
def test_unspaced_decimal_values_and_dates_do_not_become_dotted_headings(text):
    assert printed_marker(text) is None


def test_unnumbered_headings_in_the_same_style_do_not_own_numbered_chapters():
    # Front matter, a contents line and back matter in the chapters' own style
    # are their siblings; nothing printed says "1." belongs to "Summary".
    p = apply([("title", "Report", 0), h("Summary"), h("Notes"), h("1. Scope"), h("1.1 Inputs"),
               h("2. Method"), h("Contents"), h("3. Results"), h("References")], [20] + [12] * 8)
    assert parents(p) == {"Report": None, "Summary": "Report", "Notes": "Report", "1. Scope": "Report",
                          "1.1 Inputs": "1. Scope", "2. Method": "Report", "Contents": "Report",
                          "3. Results": "Report", "References": "Report"}
    p = apply([h("Overview"), h("\uac00. First"), h("\ub098. Second"), h("(1) Item")], [12] * 4)
    assert parents(p) == {"Overview": None, "\uac00. First": None, "\ub098. Second": None,
                          "(1) Item": "\ub098. Second"}


def test_sections_without_their_chapter_lines_do_not_hang_under_front_matter():
    p = apply([h("Notes"), h("1.1 A"), h("1.2 B"), h("2.1 C"), h("3.1 D")], [12] * 5)
    assert set(parents(p).values()) == {None}


def test_unnumbered_chapter_keeps_the_sections_its_number_implies():
    # Opposite input: "Chapter 2" is the missing "2." of 2.1 and 2.2.
    p = apply([h("Chapter 1 Intro"), h("1.1 A"), h("1.1.1 Deep"), h("1.2 B"), h("Chapter 2 Data"),
               h("2.1 C")], [12] * 6)
    assert parents(p) == {"Chapter 1 Intro": None, "1.1 A": "Chapter 1 Intro", "1.1.1 Deep": "1.1 A",
                          "1.2 B": "Chapter 1 Intro", "Chapter 2 Data": None, "2.1 C": "Chapter 2 Data"}


def test_restarting_numbers_under_same_style_chapters_stay_shallow():
    # Opposite input and its known cost: even a counter that restarts after
    # each unnumbered heading is not taken as proof of ownership (a list of
    # references read as headings restarts the same way). Shallow, not wrong.
    p = apply([h("Background"), h("1. A"), h("2. B"), h("Approach"), h("1. C")], [12] * 5)
    assert set(parents(p).values()) == {None}


def test_larger_style_or_outline_still_nests_numbered_headings_under_unnumbered_ones():
    p = apply([h("Part one"), h("1. A"), h("2. B"), h("Part two"), h("1. C")], [16, 12, 12, 16, 12])
    assert parents(p) == {"Part one": None, "1. A": "Part one", "2. B": "Part one",
                          "Part two": None, "1. C": "Part two"}
    p = apply([h("Annex"), h("1. Item"), h("2. Item")], [12, 12, 12], [(1, "b0"), (2, "b1"), (2, "b2")])
    assert parents(p) == {"Annex": None, "1. Item": "Annex", "2. Item": "Annex"}


def test_front_matter_does_not_stand_for_a_chapter_line_read_as_body():
    # "1." was not read as a heading, so 1.1 and 1.2 have one chapter number.
    # Only a heading that prints that number ("Chapter 1") may stand for it.
    p = apply([h("Summary"), h("1.1 A"), h("1.2 B"), h("2. C"), h("2.1 D")], [12] * 5)
    assert parents(p) == {"Summary": None, "1.1 A": None, "1.2 B": None, "2. C": None, "2.1 D": "2. C"}
    # Known cost: an unnumbered chapter title in its sections' own style.
    p = apply([h("Introduction"), h("1.1 A"), h("1.2 B")], [12] * 3)
    assert set(parents(p).values()) == {None}


def test_same_style_rule_holds_without_font_evidence():
    # Scanned pages carry no font sizes; the parser's own level is the style.
    p = apply([h("Summary"), h("1. A"), h("2. B"), h("References")], [0, 0, 0, 0])
    assert set(parents(p).values()) == {None}
    p = apply([h("Summary", 2), h("1.1 A", 2), h("1.2 B", 2), h("2. C", 2)], [0, 0, 0, 0])
    assert set(parents(p).values()) == {None}
    p = apply([h("Part", 1), h("1. A", 2), h("2. B", 2)], [0, 0, 0])
    assert parents(p) == {"Part": None, "1. A": "Part", "2. B": "Part"}
