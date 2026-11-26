"""Printed column labels survive pictures and partial rows across a page break."""
import pytest
import pymupdf
from app.parsers import source_tables as s
from tests.test_feedback_tables import document


@pytest.mark.parametrize('case', ['picture', 'partial', 'projected', 'missing', 'shifted', 'section', 'ambiguous'])
def test_continuation_uses_only_observed_aligned_column_labels(tmp_path, case):
    header = ['Metric', 'Rocket', 'Prototype']
    records = [['Purpose', 'Payload launch', 'Engine test'], ['Length', '47.2 m', '25.8 m'],
               ['Weight', '200 tons', '52.1 tons']]
    source = [header, ['Appearance', '', ''], *records]
    if case == 'projected':
        source = [[*row, 'Remarks' if i == 0 else ''] for i, row in enumerate(source)]
        header = source[0]
    if case == 'section':
        source[1] = ['Other section', '', '']
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    def grid(page, rows, top, xs=None):
        xs = xs or ((40, 180, 360, 500, 560) if case == 'projected' else (40, 180, 360, 560))
        for x in xs:
            page.draw_line((x, top), (x, top + 40 * len(rows)))
        for y in range(top, top + 40 * len(rows) + 1, 40):
            page.draw_line((xs[0], y), (xs[-1], y))
        for ri, row in enumerate(rows):
            for x, text in zip(xs, row):
                page.insert_text((x + 5, top + 25 + 40 * ri), text, fontsize=9)
    if case != 'missing':
        grid(page, [header], 600, (40, 200, 400, 560) if case == 'shifted' else None)
    page = pdf.new_page(width=600, height=800)
    if case == 'section':
        grid(page, [['Other section']], 40, (40, 560))
    else:
        grid(page, [['Appearance', '', '', ''] if case == 'projected' else ['Appearance', '', '']], 40)
    printed = records if case not in ('partial', 'projected') else [['', 'Payload launch', 'Engine test'], *records[1:]]
    grid(page, printed, 80)
    path = tmp_path / 'continued.pdf'
    pdf.save(path)
    pdf.close()
    # A layout parser can combine the preceding labels into one paragraph and
    # promote the first actual data row to a Markdown heading.
    doc = document([('paragraph', ' '.join(header), 0),
                    ('table', s.table_markdown(printed), 1)], pages=2, fmt='hwp')
    doc.blocks[1].regions[0].bbox.y0 = .10
    doc.blocks[1].regions[0].bbox.y1 = .25
    sources = [source]
    if case == 'ambiguous':
        sources.append([header, ['Appearance', '', ''], *records, ['Hidden', 'Elsewhere', 'Absent']])
    s.apply_source_tables(doc, sources, pdf=path, columns_overflow=False)
    actual = doc.blocks[-1].text
    if case in ('picture', 'partial', 'projected'):
        assert actual.splitlines()[0] == s.table_markdown([header[:3]]).splitlines()[0]
        assert actual.count('Payload launch') == 1
        assert actual.count('Length') == 1
        if case in ('partial', 'projected'):
            assert 'Purpose' not in actual
    else:
        assert 'Metric' not in actual
        assert 'Payload launch' in actual


def test_partial_grid_keeps_headers_over_two_pages_without_copying_body(tmp_path):
    header = ['Topic', 'Details']
    source = [header, ['First', 'start end'], ['Second', 'alpha'], ['Third', 'beta'],
              ['Fourth', 'gamma'], ['Fifth', 'delta']]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    def grid(page, rows, top):
        for x in (40, 200, 560):
            page.draw_line((x, top), (x, top + len(rows) * 40))
        for y in range(top, top + len(rows) * 40 + 1, 40):
            page.draw_line((40, y), (560, y))
        for ri, row in enumerate(rows):
            for x, text in zip((45, 205), row):
                page.insert_text((x, top + 25 + 40 * ri), text, fontsize=9)
    grid(page, [header, ['First', 'start']], 600)
    page = pdf.new_page(width=600, height=800)
    part = [['', 'end'], ['Second', 'alpha'], ['Third', 'beta']]
    grid(page, part, 40)
    page = pdf.new_page(width=600, height=800)
    grid(page, source[-2:], 40)
    path = tmp_path / 'three-pages.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', 'Topic Details First start', 0),
                    ('table', s.table_markdown(part), 1),
                    ('table', s.table_markdown(source[-2:]), 2)], pages=3, fmt='hwp')
    for block in doc.blocks[1:]:
        block.regions[0].bbox.y0 = .04
        block.regions[0].bbox.y1 = .25
    s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False)
    for block in doc.blocks[1:]:
        assert block.text.splitlines()[0] == '| Topic | Details |'
        assert 'First' not in block.text and 'start' not in block.text
    assert 'end' in doc.blocks[1].text
    assert 'Second' not in doc.blocks[2].text


@pytest.mark.parametrize('matching_header', [True, False])
def test_merged_printed_heading_labels_only_its_retained_columns(tmp_path, matching_header):
    header = ['Category', '', 'Description', 'Score']
    source = [header, ['First', 'Sub', 'Alpha', '10'], ['Second', 'Sub', 'start tail', '20'],
              ['Total', '', 'Gamma', '30']]
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=800)
    # One heading spans the first two source columns. The following row proves
    # the internal column boundary; its data must never be copied to page two.
    for x in (40, 200, 400, 560):
        page.draw_line((x, 600), (x, 680))
    page.draw_line((110, 640), (110, 680))
    for y in (600, 640, 680):
        page.draw_line((40, y), (560, y))
    for x, value in zip((45, 205, 405), ['Category' if matching_header else 'Other', 'Description', 'Score']):
        page.insert_text((x, 625), value, fontsize=9)
    for x, value in zip((45, 115, 205, 405), source[1]):
        page.insert_text((x, 665), value, fontsize=9)
    page = pdf.new_page(width=600, height=800)
    for x in (40, 110, 200, 400, 560):
        page.draw_line((x, 40), (x, 120))
    for y in (40, 80, 120):
        page.draw_line((40, y), (560, y))
    physical = [['', '', 'tail', '20'], ['Total', '', 'Gamma', '30']]
    for ri, row in enumerate(physical):
        for x, value in zip((45, 115, 205, 405), row):
            page.insert_text((x, 65 + ri * 40), value, fontsize=9)
    path = tmp_path / 'merged-heading.pdf'
    pdf.save(path)
    pdf.close()
    doc = document([('paragraph', 'Category Description Score First Sub Alpha 10', 0),
                    ('table', s.table_markdown(physical), 1)], pages=2, fmt='hwp')
    doc.blocks[1].regions[0].bbox.y0 = .04
    doc.blocks[1].regions[0].bbox.y1 = .16
    s.apply_source_tables(doc, [source], pdf=path, columns_overflow=False)
    actual = doc.blocks[-1].text
    if matching_header:
        assert actual.splitlines()[0] == '| Category | Description | Score |'
    else:
        assert 'Category' not in actual
    assert 'tail' in actual and 'Gamma' in actual
    assert 'Second' not in actual and 'start' not in actual and 'Alpha' not in actual
