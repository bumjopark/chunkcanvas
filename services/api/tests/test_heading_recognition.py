"""Printed synthetic documents, including counterexamples to heading heuristics."""
import copy

import pymupdf
import pytest

from app.models import BlockRegion, ParsedDocument, TextLine
from app.offsets import wire_document
from app.parsers.base import add_text_lines, normalize_line_geometry
from app.parsers.heading_recognition import recognize_headings, _slice_block
from app.parsers.heading_structure import assign_heading_levels


def row(text, kind="paragraph", size=10, bold=False, page=0, y=None, **extra):
    return dict(text=text, kind=kind, size=size, bold=bold, page=page, y=y, **extra)


def fixture(tmp_path, rows, groups=(), outline=(), fmt="pdf"):
    pdf = pymupdf.open()
    pages = [pdf.new_page(width=600, height=800) for _ in range(max(r["page"] for r in rows) + 1)]
    boxes = []
    for i, item in enumerate(rows):
        page = pdf[item["page"]]
        y = item["y"] if item["y"] is not None else 70 + 35 * i
        page.insert_text((item.get("x", 60), y), item["text"], fontsize=item["size"], fontname="hebo" if item["bold"] else "helv", rotate=item.get("rotate", 0))
        found = page.search_for(item["text"])
        r = tuple(found[-1]) if found else page.get_text("dict")["blocks"][-1]["bbox"]
        boxes.append(dict(pageIndex=item["page"], bbox=dict(x0=max(0, r[0]/600), y0=max(0, r[1]/800),
                                                          x1=min(1, r[2]/600), y1=min(1, r[3]/800))))
    if outline:
        pdf.set_toc(list(outline))
    path = tmp_path / "source.pdf"; pdf.save(path); pdf.close()
    groups = list(groups) or [(i,) for i in range(len(rows))]
    blocks = []
    for i, indices in enumerate(groups):
        first = rows[indices[0]]
        blocks.append(dict(id=f"b{i}", readingOrder=i, type=first["kind"], level=0 if first["kind"] == "title" else None,
                           text=" ".join(rows[k].get("plain", rows[k]["text"]) for k in indices),
                           markdown=first.get("markdown"), regions=[boxes[k] for k in indices], source=dict(parser="pymupdf")))
    parsed = ParsedDocument.model_validate(dict(document=dict(id="test", filename="test.pdf", fileHash="test",
        pageCount=len(pages), parser="pymupdf", parserVersion="test", sourceFormat=fmt),
        pages=[dict(index=i, width=600, height=800) for i in range(len(pages))], blocks=blocks, blocksFingerprint="before"))
    return parsed, path


def process(parsed, path):
    stats = recognize_headings(parsed, path)
    assign_heading_levels(parsed, path)
    add_text_lines(parsed)
    normalize_line_geometry(parsed)
    return stats


def test_bold_numbered_list_heading_recovers_the_existing_printed_marker(tmp_path):
    rows = [row("1. Setup", "list_item", 12, True, plain="Setup", markdown="1. Setup"),
            row("Body text. " * 5), row("1.1 Checks", "section_header", 12, True), row("Body details. " * 4)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert [b.type for b in p.blocks] == ["section_header", "paragraph", "section_header", "paragraph"]
    assert p.blocks[0].text == "1. Setup"
    assert p.blocks[0].level == 1 and p.blocks[2].level == 2
    assert p.blocks_fingerprint != "before"


def test_bold_numbered_procedure_and_figure_caption_are_not_sections(tmp_path):
    p, path = fixture(tmp_path, [row("1. Open the application.", "list_item", 14, True),
                               row("Figure 2. System diagram", "caption", 18, True), row("Body text. " * 5)])
    before = [(b.id, b.type, b.text) for b in p.blocks]
    process(p, path)
    assert [(b.id, b.type, b.text) for b in p.blocks] == before


def test_cover_metadata_is_not_a_parent_of_body_chapters(tmp_path):
    p, path = fixture(tmp_path, [row("Upgrade Plan", "title", 28, True),
        row("Implementation for 2028", "section_header", 20, y=130),
        row("Information Office", "section_header", 16, True, y=600),
        row("2028. 11. 14.", "section_header", 14, y=650),
        row("1. Scope", "section_header", 16, True, page=1, y=80),
        row("Body text. " * 5, page=1, y=120)])
    process(p, path)
    assert [b.type for b in p.blocks[:4]] == ["title", "paragraph", "paragraph", "paragraph"]
    assert p.blocks[4].level == 1


def test_short_first_page_paragraphs_and_numbered_sections_are_not_a_cover(tmp_path):
    p, path = fixture(tmp_path, [row("Manual", "title", 22, True), row("I. Chapter", "section_header", 16, True),
                               row("Short prose."), row("1. Section", "section_header", 13, True), row("More short prose.")])
    process(p, path)
    assert [b.type for b in p.blocks] == ["title", "section_header", "paragraph", "section_header", "paragraph"]


def test_first_chapter_with_peer_styles_is_not_document_title(tmp_path):
    p, path = fixture(tmp_path, [row("First chapter", "title", 18, True), row("Body text. " * 5),
                               row("Second chapter", "section_header", 18, True), row("Body details. " * 4)])
    process(p, path)
    assert [(b.type, b.level) for b in (p.blocks[0], p.blocks[2])] == [("section_header", 1), ("section_header", 1)]


def test_contents_navigation_stays_out_of_the_heading_tree(tmp_path):
    p, path = fixture(tmp_path, [row("Project", "section_header", 30), row("Agency", "section_header", 30),
        row("Contents", "section_header", 18, True), row("I. Scope 1", "list_item", 16, True),
        row("II. Work 3", "list_item", 16, True), row("I. Scope", "paragraph", 16),
        row("1. Details", "section_header", 13, True), row("Body text. " * 5)])
    process(p, path)
    assert p.blocks[0].type == "title" and p.blocks[1].type == "paragraph"
    assert p.blocks[2].type == "paragraph"
    assert [b.type for b in p.blocks[3:5]] == ["list_item", "list_item"]
    assert [(b.type, b.level) for b in p.blocks[5:7]] == [("section_header", 1), ("section_header", 2)]


def test_contents_as_a_real_chapter_name_is_not_demoted(tmp_path):
    p, path = fixture(tmp_path, [row("Contents", "section_header", 16, True), row("This chapter discusses package contents.")])
    process(p, path)
    assert p.blocks[0].type == "section_header"


def test_chronology_date_headings_are_not_treated_as_body_date_metadata(tmp_path):
    p, path = fixture(tmp_path, [row("2028. 11. 14.", "section_header", 16, True), row("Body text. " * 5),
                               row("2028. 11. 15.", "section_header", 16, True), row("Body details. " * 4)])
    process(p, path)
    assert p.blocks[0].type == p.blocks[2].type == "section_header"


def test_source_bookmarks_recover_unbold_headings_at_body_size(tmp_path):
    p, path = fixture(tmp_path, [row("Root"), row("Child"), row("Deep"), row("Body text. " * 5)],
                      outline=[(1, "Root", 1), (2, "Child", 1), (3, "Deep", 1)])
    process(p, path)
    assert [(b.type, b.level) for b in p.blocks[:3]] == [("section_header", 1), ("section_header", 2), ("section_header", 3)]


def test_merged_body_and_heading_are_split_without_losing_characters(tmp_path):
    p, path = fixture(tmp_path, [row("Leading paragraph."), row("1. Section", size=14, bold=True),
                               row("Trailing paragraph."), row("Table text", "table", 20, True)], groups=[(0, 1, 2), (3,)])
    before = copy.deepcopy(p.blocks[1].model_dump())
    text = p.blocks[0].text
    process(p, path)
    assert [b.type for b in p.blocks] == ["paragraph", "section_header", "paragraph", "table"]
    assert " ".join(b.text for b in p.blocks[:3]) == text
    assert [b.id for b in p.blocks] == ["b0", "b0_part1", "b0_part2", "b1"]
    assert p.blocks[0].regions[-1].bbox.y1 < p.blocks[1].regions[0].bbox.y0
    assert p.blocks[1].regions[-1].bbox.y1 < p.blocks[2].regions[0].bbox.y0
    assert p.blocks[0].line_geometry == p.blocks[2].line_geometry == "estimated"
    after = p.blocks[3].model_dump()
    before.pop("reading_order"); after.pop("reading_order")
    assert before == after


def test_adjacent_source_headings_at_one_size_are_not_a_wrapped_heading(tmp_path):
    p, path = fixture(tmp_path, [row("One", "section_header", 16, True), row("Two", "section_header", 16, True),
                               row("Body text. " * 5)], groups=[(0, 1), (2,)], outline=[(1, "One", 1), (1, "Two", 1)])
    process(p, path)
    assert [b.text for b in p.blocks[:2]] == ["One", "Two"]
    assert [b.level for b in p.blocks[:2]] == [1, 1]


def test_wrapped_title_in_one_block_stays_one_title(tmp_path):
    p, path = fixture(tmp_path, [row("Operations", "title", 28, True), row("Manual", "title", 28, True),
                               row("Body text. " * 5)], groups=[(0, 1), (2,)])
    process(p, path)
    assert p.blocks[0].type == "title" and p.blocks[0].text == "Operations Manual"
    assert len(p.blocks) == 2


def test_adjacent_wrapped_title_blocks_are_joined_but_distant_agency_is_not(tmp_path):
    p, path = fixture(tmp_path, [row("Operations", "title", 28, True, y=80), row("Manual", "title", 28, True, y=120),
                               row("Agency", "title", 28, True, y=600), row("Body text. " * 5, page=1, y=80)])
    stats = process(p, path)
    assert stats["merged"] == 1
    assert [b.text for b in p.blocks[:2]] == ["Operations Manual", "Agency"]


@pytest.mark.parametrize("kind", ["table", "picture", "page_header", "page_footer", "code", "formula", "footnote"])
def test_protected_blocks_never_become_headings(tmp_path, kind):
    p, path = fixture(tmp_path, [row("I. Bold words", kind, 18, True), row("Body text. " * 5)])
    before = p.blocks[0].model_dump()
    recognize_headings(p, path)
    assert p.blocks[0].model_dump() == before


@pytest.mark.parametrize("fmt", ["xlsx", "csv", "pptx"])
def test_sheet_and_slide_recognition_is_unchanged(tmp_path, fmt):
    p, path = fixture(tmp_path, [row("I. Bold words", "list_item", 18, True), row("Body text. " * 5)], fmt=fmt)
    before = p.model_dump()
    assert recognize_headings(p, path)["changed"] == 0
    assert p.model_dump() == before


def test_split_offsets_stay_in_codepoints_until_the_api_boundary(tmp_path):
    p, _ = fixture(tmp_path, [row("Heading Original payload.")])
    b = p.blocks[0]
    b.text = "Heading Original \U0001f680 payload."
    b.lines = [TextLine(page_index=0, bbox=b.regions[0].bbox, start=0, end=len(b.text),
                        xs=[.1 + i * .01 for i in range(len(b.text))])]
    b.lines[0].bbox.x0 = .1
    b.lines[0].bbox.x1 = .1 + len(b.text) * .01
    b.line_geometry = "exact"
    part = _slice_block(b, len("Heading "), len(b.text), None, "part", False)
    p.blocks = [part]
    assert part.text == "Original \U0001f680 payload."
    assert part.lines[0].end == len(part.text)
    assert part.lines[0].bbox.x0 == b.lines[0].xs[len("Heading ")]
    assert part.line_geometry == "exact"
    wire = wire_document(p.model_dump(by_alias=True, exclude_none=True))
    line = wire["blocks"][0]["lines"][0]
    assert line["end"] == len(part.text) + 1
    assert len(line["xs"]) == line["end"]


def test_many_small_chart_values_cannot_promote_numeric_body_prose(tmp_path):
    rows = [row(f"Count {i}.9 57.4", size=6, y=40 + i * 15) for i in range(14)]
    rows += [row("I. Results", size=16, y=300),
             row("13.9 million units were counted in the current reporting period", size=10.6, y=340),
             row("70.5 percent of the observations were collected during the second period", size=10.6, y=380)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert p.blocks[-3].type == "section_header"
    assert [b.type for b in p.blocks[-2:]] == ["paragraph", "paragraph"]


def test_splitting_a_list_cannot_orphan_its_preserved_list_marker(tmp_path):
    rows = [row("1. Preliminary text", "list_item", plain="Preliminary text", markdown="1. Preliminary text Inner heading Ending text."),
            row("Inner heading", size=16, bold=True), row("Ending text.")]
    p, path = fixture(tmp_path, rows, groups=[(0, 1, 2)])
    before = [(b.id, b.type, b.text, b.markdown) for b in p.blocks]
    process(p, path)
    assert [(b.id, b.type, b.text, b.markdown) for b in p.blocks] == before


@pytest.mark.parametrize("label", ["Contents", "\ucc28 \ub840", "\uc21c \uc11c"])
def test_grouped_contents_entries_and_cover_metadata_are_not_promoted(tmp_path, label):
    rows = [row("Operator guide", "title", 24, True),
            row("Service organisation", "paragraph", 15, True),
            row(label, "paragraph", 16, True),
            row("I. Overview", "list_item", 14, True),
            row("1. Setup 1", "list_item", 12, True),
            row("2. Checks 2", "list_item", 12, True),
            row("II. Operation", "list_item", 14, True),
            row("1. Start 3", "list_item", 12, True),
            row("2. Stop 4", "list_item", 12, True),
            row("I. Overview", "paragraph", 18, True),
            row("Ordinary explanatory prose describes the equipment and its operating conditions.")]
    p, path = fixture(tmp_path, rows)
    recognize_headings(p, path)
    assert p.blocks[0].type == "title"
    assert all(b.type not in {"title", "section_header"} for b in p.blocks[1:9])
    assert p.blocks[9].type == "section_header"


def test_later_navigation_does_not_reclassify_preceding_chapters_as_a_cover(tmp_path):
    p, path = fixture(tmp_path, [row("First chapter", "section_header", 24, True),
        row("Short introduction."), row("Second chapter", "section_header", 18, True),
        row("Short summary."), row("Contents", "paragraph", 16, True, page=3),
        row("1. Entry 4", "list_item", 12, True, page=3),
        row("2. Entry 5", "list_item", 12, True, page=3)])
    recognize_headings(p, path)
    assert p.blocks[0].type == p.blocks[2].type == "section_header"


def test_a_malformed_original_line_does_not_break_heading_splitting(tmp_path):
    p, _ = fixture(tmp_path, [row("Heading Original payload.")])
    b = p.blocks[0]
    b.lines = [TextLine(page_index=0, bbox=b.regions[0].bbox, start=0, end=len(b.text), xs=[.1])]
    b.line_geometry = "exact"
    part = _slice_block(b, len("Heading "), len(b.text), None, "part", False)
    assert part.text == "Original payload."
    assert part.lines is None and part.line_geometry == "unavailable"


def test_a_large_vertical_stamp_is_not_newly_promoted_to_a_heading(tmp_path):
    p, path = fixture(tmp_path, [row("APPROVED", size=18, bold=True, y=200, rotate=90),
        row("Ordinary explanatory prose describes the conditions and the operating procedure.", y=300)])
    recognize_headings(p, path)
    assert p.blocks[0].type == "paragraph"


def test_source_outline_preserves_a_real_document_title_despite_equal_chapter_fonts(tmp_path):
    p, path = fixture(tmp_path, [row("Manual", "title", 16, True),
        row("First chapter", "section_header", 16, True), row("Body text. " * 5),
        row("Second chapter", "section_header", 16, True), row("Body details. " * 4)],
        outline=[(1, "Manual", 1), (2, "First chapter", 1), (2, "Second chapter", 1)])
    process(p, path)
    assert p.blocks[0].type == "title" and p.blocks[0].level == 0
    assert p.blocks[1].level == p.blocks[3].level == 1


def test_source_chapter_with_children_and_peer_chapters_is_not_a_document_title(tmp_path):
    p, path = fixture(tmp_path, [row("First chapter", "title", 18, True), row("Body text. " * 5),
        row("Detail", "section_header", 14, True), row("Body details. " * 4),
        row("Second chapter", "section_header", 18, True), row("Further details. " * 4)],
        outline=[(1, "First chapter", 1), (2, "Detail", 1), (1, "Second chapter", 1)])
    process(p, path)
    assert p.blocks[0].type == "section_header"
    assert p.blocks[0].level == p.blocks[4].level == 1


@pytest.mark.parametrize("same_row", [True, False])
def test_a_printed_chapter_counter_stays_with_its_heading_on_the_same_row(tmp_path, same_row):
    p, path = fixture(tmp_path, [row("IV", size=11, bold=True, y=80, x=60),
        row("Overview", size=18, bold=True, y=80 if same_row else 180, x=300),
        row("Ordinary body text describes the subject and its operating conditions.", y=250)],
        groups=[(0, 1), (2,)])
    process(p, path)
    if same_row:
        assert len(p.blocks) == 2
        assert p.blocks[0].type == "section_header" and p.blocks[0].text == "IV Overview"
        assert p.blocks[1].type == "paragraph"
    else:
        assert [(b.type, b.text) for b in p.blocks[:2]] == [("paragraph", "IV"), ("section_header", "Overview")]


def test_repeated_body_lines_do_not_borrow_each_others_fragment_location(tmp_path):
    p, path = fixture(tmp_path, [row("Repeated body line.", y=70), row("1. Method", size=16, bold=True, y=140),
        row("Repeated body line.", y=210)], groups=[(0, 1, 2)])
    process(p, path)
    assert [b.type for b in p.blocks] == ["paragraph", "section_header", "paragraph"]
    assert p.blocks[0].text == p.blocks[2].text == "Repeated body line."
    assert p.blocks[0].line_geometry == p.blocks[2].line_geometry == "unavailable"
    assert p.blocks[0].lines is None and p.blocks[2].lines is None


def test_separate_cover_without_bookmarks_survives_equal_chapter_fonts(tmp_path):
    p, path = fixture(tmp_path, [row("Operations manual", "title", 18, True, y=180),
        row("1. Setup", "section_header", 18, True, page=1, y=80),
        row("Ordinary explanatory prose describes the required preparation.", page=1, y=130),
        row("2. Operation", "section_header", 18, True, page=2, y=80),
        row("Ordinary explanatory prose describes the running procedure.", page=2, y=130)])
    process(p, path)
    assert p.blocks[0].type == "title" and p.blocks[0].level == 0
    assert p.blocks[1].level == p.blocks[3].level == 1


@pytest.mark.parametrize("printed_horizontal", [True, False])
def test_heading_orientation_uses_the_displayed_page_rotation(tmp_path, printed_horizontal):
    p, path = fixture(tmp_path, [row("1. Setup", size=18, bold=True, y=250,
        rotate=90 if printed_horizontal else 0),
        row("Ordinary body prose describes the required setup.", y=600, x=300, rotate=90)])
    with pymupdf.open(path) as pdf:
        page = pdf[0]
        page.set_rotation(90)
        for block in p.blocks:
            for region in block.regions:
                b = region.bbox
                rect = pymupdf.Rect(b.x0 * 600, b.y0 * 800, b.x1 * 600, b.y1 * 800) * page.rotation_matrix
                region.bbox = region.bbox.model_copy(update=dict(x0=rect.x0/page.rect.width,
                    y0=rect.y0/page.rect.height, x1=rect.x1/page.rect.width, y1=rect.y1/page.rect.height))
        p.pages[0].width, p.pages[0].height = page.rect.width, page.rect.height
        pdf.saveIncr()
    recognize_headings(p, path)
    assert p.blocks[0].type == ("section_header" if printed_horizontal else "paragraph")


@pytest.mark.parametrize("kind", ["table", "code", "formula"])
def test_a_first_chapter_with_structured_body_is_not_an_empty_cover(tmp_path, kind):
    p, path = fixture(tmp_path, [row("First chapter", "title", 18, True, y=80),
        row("Detailed structured data " * 8, kind, y=140),
        row("Second chapter", "section_header", 18, True, page=1, y=80),
        row("Ordinary explanatory prose describes the next chapter.", page=1, y=130)])
    before = p.blocks[1].model_dump()
    recognize_headings(p, path)
    assign_heading_levels(p, path)
    assert p.blocks[0].type == "section_header"
    assert p.blocks[0].level == p.blocks[2].level == 1
    assert p.blocks[1].model_dump() == before


@pytest.mark.parametrize("kind", ["table", "code", "formula", "list_item"])
@pytest.mark.parametrize("first_kind", ["title", "section_header"])
@pytest.mark.parametrize("later_kind", ["section_header", "paragraph", "list_item"])
def test_contents_after_structured_body_does_not_turn_the_previous_chapter_into_a_cover(tmp_path, kind, first_kind, later_kind):
    p, path = fixture(tmp_path, [row("First chapter", first_kind, 20, True, y=80),
        row("Detailed structured content " * 8, kind, y=140),
        row("Contents", "section_header", 18, True, page=1, y=80),
        row("1. Setup 3", "list_item", 12, page=1, y=120),
        row("2. Results 4", "list_item", 12, page=1, y=160),
        row("1. Setup", later_kind, 20, True, page=2, y=80),
        row("Ordinary explanatory prose describes the required preparation.", page=2, y=130)])
    before = p.blocks[1].model_dump()
    recognize_headings(p, path)
    assert p.blocks[0].type == "section_header"
    assert p.blocks[1].model_dump() == before


def test_a_distinct_document_title_can_have_table_metadata_before_contents(tmp_path):
    p, path = fixture(tmp_path, [row("Operations manual", "title", 26, True, y=80),
        row("Contact details and document metadata " * 5, "table", y=140),
        row("2028. 11.", "section_header", 16, y=220),
        row("Contents", "section_header", 18, True, page=1, y=80),
        row("1. Setup 3", "list_item", 12, page=1, y=120),
        row("2. Results 4", "list_item", 12, page=1, y=160),
        row("1. Setup", "section_header", 20, True, page=2, y=80),
        row("Ordinary explanatory prose describes the required preparation.", page=2, y=130)])
    before = p.blocks[1].model_dump()
    recognize_headings(p, path)
    assert p.blocks[0].type == "title"
    assert p.blocks[1].model_dump() == before
    assert p.blocks[2].type == "paragraph"


@pytest.mark.parametrize("marker", ["A.", "a.", "i.", "ii."])
def test_source_outline_matches_preserved_alphabetic_list_markers(tmp_path, marker):
    printed = marker + " Setup"
    p, path = fixture(tmp_path, [row("Overview", size=10),
        row(printed, "list_item", 10, plain="Setup", markdown=printed),
        row("Ordinary explanatory prose describes the required preparation.")],
        outline=[(1, "Overview", 1), (2, printed, 1)])
    process(p, path)
    assert [(b.type, b.level, b.text) for b in p.blocks[:2]] == [
        ("section_header", 1, "Overview"), ("section_header", 2, printed)]


def test_alphabetic_procedure_marker_is_preserved_without_promoting_body(tmp_path):
    p, path = fixture(tmp_path, [row("A. Open the application.", "list_item", 10, True,
        plain="Open the application.", markdown="A. Open the application."),
        row("Ordinary explanatory prose describes the required preparation.")])
    before = p.blocks[0].model_dump()
    recognize_headings(p, path)
    assert p.blocks[0].model_dump() == before


@pytest.mark.parametrize("variant", ["inline", "new_page", "source_outline", "left_aligned",
                                     "reset", "changed_style", "single_heading", "numbered_frame"])
def test_inline_display_panel_between_numbered_peers_is_not_a_new_parent(tmp_path, variant):
    panel = "III. New chapter" if variant == "numbered_frame" else "Sample form"
    peer = "1. Next" if variant == "reset" else "2. Next"
    def centered(text, size):
        return (600 - pymupdf.get_text_length(text, fontname="hebo", fontsize=size)) / 2
    panel_page, next_page = (1, 2) if variant == "new_page" else (0, 0)
    rows = [row("I. Main", "section_header", 18, True, y=60),
        row("1. First", "section_header", 12, True, y=100),
        row("Ordinary explanatory prose describes the required preparation.", y=140),
        row(panel, "section_header", 24, True, y=220, page=panel_page,
            x=60 if variant == "left_aligned" else centered(panel, 24))]
    if variant != "single_heading":
        rows.append(row("Agency field", "section_header", 14, True, y=270, page=panel_page,
                        x=60 if variant == "left_aligned" else centered("Agency field", 14)))
    rows += [row(peer, "section_header", 15 if variant == "changed_style" else 12,
                 True, y=360, page=next_page),
             row("Ordinary explanatory prose describes the later section.", y=410, page=next_page)]
    outline = [(1, "I. Main", 1), (2, "1. First", 1), (1, panel, 1),
               (2, "Agency field", 1), (2, peer, 1)] if variant == "source_outline" else []
    p, path = fixture(tmp_path, rows, outline=outline)
    process(p, path)
    by_text = {b.text: b for b in p.blocks}
    expected = "paragraph" if variant == "inline" else "section_header"
    assert by_text[panel].type == expected
    if variant != "single_heading":
        assert by_text["Agency field"].type == expected
    if variant == "inline":
        assert by_text[peer].level == by_text["1. First"].level == 2


# P2b' narrowing rules (2b-N1..N5) and their opposite inputs.
BODY = "Body text keeps going here. " * 3


def kinds(p):
    return [(b.type, b.text) for b in p.blocks]


def test_a_bold_counter_alone_is_not_cut_from_its_title(tmp_path):
    rows = [row(BODY), row("3", "section_header", 20, True, y=150, x=60),
            row("Selection plan and criteria", "section_header", 20, False, y=150, x=250), row(BODY, y=200)]
    p, path = fixture(tmp_path, rows, groups=[(0,), (1, 2), (3,)])
    process(p, path)
    assert ("section_header", "3 Selection plan and criteria") in kinds(p)
    assert not any("_part" in b.id for b in p.blocks)


def test_one_wrapped_bold_sentence_is_not_cut_into_headings(tmp_path):
    rows = [row(BODY), row("Strong winds call for checks of every", "paragraph", 13, True, y=150),
            row("fish farm and closed harbours until the", "paragraph", 13, True, y=167),
            row("warning ends", "paragraph", 13, True, y=184), row(BODY, y=230)]
    p, path = fixture(tmp_path, rows, groups=[(0,), (1, 2, 3), (4,)])
    process(p, path)
    # Not cut into three headings; a short large bold block may stay one (as before).
    assert len(p.blocks) == 3 and not any("_part" in b.id for b in p.blocks)
    assert p.blocks[1].text.startswith("Strong winds") and p.blocks[1].text.endswith("warning ends")


def test_merged_chapter_section_and_bodies_are_still_separated(tmp_path):
    # Opposite input: numbered headings with their own bodies in one block.
    rows = [row(BODY), row("2. Scope", "paragraph", 16, True, y=150), row(BODY, y=180),
            row("2.1 Inputs", "paragraph", 13, True, y=215), row(BODY, y=245)]
    p, path = fixture(tmp_path, rows, groups=[(0,), (1, 2, 3, 4)])
    process(p, path)
    assert [b.text for b in p.blocks if b.type == "section_header"] == ["2. Scope", "2.1 Inputs"]


def test_contents_lines_between_paged_entries_stay_body(tmp_path):
    rows = [row(BODY), row("I. Outlook and conditions", "list_item", 12, True), row("Weather outlook 1", "list_item"),
            row("Social conditions 6", "list_item"), row("II. Incidents of the month", "list_item", 12, True),
            row("Natural hazards 7", "list_item"), row("Social hazards 13", "list_item"),
            row("Outlook and conditions", "section_header", 16, True, page=1, y=80), row(BODY, page=1, y=120)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert p.blocks[1].type == "list_item" and p.blocks[4].type == "list_item"


def test_numbered_list_items_with_bodies_are_still_promoted(tmp_path):
    # Opposite input: the same labels as chapters with bodies, no page numbers around.
    rows = [row("I. Outlook and conditions", "list_item", 12, True), row(BODY),
            row("II. Incidents of the month", "list_item", 12, True), row(BODY),
            row("1. Overview", "list_item", 12, True), row(BODY), row("III. Measures", "list_item", 12, True), row(BODY),
            row("1. Overview", "list_item", 12, True), row(BODY)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert [b.type for b in p.blocks[::2]] == ["section_header"] * 5


def test_a_one_page_notice_keeps_its_item_headings(tmp_path):
    p, path = fixture(tmp_path, [row("Residents meeting", "title", 20, True), row("Date", "section_header", 13, True),
                                 row("Thursday evening"), row("Place", "section_header", 13, True), row("Town hall"),
                                 row("Contact", "section_header", 13, True), row("Civic office")])
    process(p, path)
    assert [b.type for b in p.blocks[1::2]] == ["section_header"] * 3


def test_consecutive_bold_numbered_steps_are_a_list(tmp_path):
    rows = [row("1. Overview", "section_header", 14, True), row(BODY),
            row("1. Fill in the form", "list_item", 11, True), row("2. Attach the records", "list_item", 11, True),
            row("3. Hand in at the desk", "list_item", 11, True), row(BODY)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert [b.type for b in p.blocks[2:5]] == ["list_item"] * 3


def test_first_line_of_a_wrapped_bold_numbered_sentence_is_not_a_heading(tmp_path):
    rows = [row(BODY), row("1. Every agency keeps the records it collects and", "list_item", 11, True, y=150),
            row("hands them in when the office asks for them.", "paragraph", 11, True, y=165), row(BODY, y=200)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert p.blocks[1].type == "list_item"


def test_roman_looking_words_are_not_counters(tmp_path):
    rows = [row(BODY), row("I agree to the terms set out above", "paragraph", 10, True), row(BODY),
            row("IV. Results", "paragraph", 10, True), row(BODY)]
    p, path = fixture(tmp_path, rows)
    process(p, path)
    assert p.blocks[1].type == "paragraph" and p.blocks[3].type == "section_header"
