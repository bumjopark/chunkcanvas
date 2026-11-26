"""Whole column selection checked against exhaustive, scalar evidence paths."""
import itertools
import random

from app.parsers import header_projection as hp
from app.parsers.carried_headers import carry_source_headers
from app.parsers.source_tables import table_markdown
from test_carried_headers import _doc, _numbered


def _enumerate(tables, printed, missing, require_witnesses=True):
    """Small alphabet: scalar equality/empty compatibility, no bitmap helpers."""
    explanations = []
    width = len(printed[0])
    if require_witnesses and len({tuple(r) for r in printed if sum(bool(v) for v in r) >= 2}) < 2:
        return []
    for ti, table in enumerate(tables):
        for columns in itertools.combinations(range(len(table[0])), width):
            owners = {j: set() for j in range(len(printed))}
            complete, strict_complete = False, False
            for start in range(len(table) - len(printed) + 1):
                for swapped in [None, *range(len(printed) - 1)]:
                    positions = list(range(start, start + len(printed)))
                    if swapped is not None:
                        positions[swapped], positions[swapped + 1] = positions[swapped + 1], positions[swapped]
                    errors, witnesses, possible_witnesses = [], set(), set()
                    fully_evidenced = True
                    has_anchors = True
                    for j, row in enumerate(printed):
                        strict, possible, anchors, candidate_anchors = 0, 0, 0, 0
                        for m, c in enumerate(columns):
                            p, s = row[m], table[positions[j]][c]
                            numeric = p.isdigit()
                            supported = bool(p) and p == s
                            compatible = (not p or supported or (not s and (missing or all(not r[c] for r in table[1:]))))
                            if require_witnesses and not any(row):
                                compatible = not s
                            fully_evidenced &= not p or supported
                            strict += supported
                            possible += supported or (numeric and s.isdigit())
                            anchors += supported
                            candidate_anchors += supported or (numeric and s.isdigit())
                            if not compatible:
                                errors.append(numeric)
                        if strict >= 2:
                            witnesses.add(tuple(row))
                        if possible >= 2:
                            possible_witnesses.add(tuple(row))
                        if any(row) and not (anchors if swapped is not None else candidate_anchors):
                            has_anchors = False
                    full = not errors and (len(witnesses) >= 2 or not require_witnesses) and has_anchors
                    near = len(errors) <= 1 and all(errors) and (len(possible_witnesses) >= 2 or not require_witnesses) and has_anchors
                    if (full if swapped is not None else near):
                        complete |= swapped is None and full
                        strict_complete |= swapped is None and full and fully_evidenced
                        for j, ri in enumerate(positions):
                            owners[j].add(ri)
            if not any(owners.values()):
                continue
            support = {m for m, c in enumerate(columns) if any(
                printed[j][m] and any(printed[j][m] == table[ri][c] for ri in ris)
                for j, ris in owners.items()) and not any(
                printed[j][m] and any(not table[ri][c] for ri in ris)
                for j, ris in owners.items())}
            if strict_complete:
                support |= {m for m in range(width) if any(row[m] for row in printed)}
            explanations.append((ti, columns, complete, owners, support, strict_complete))
    return explanations


def _oracle(tables, printed, missing):
    width = len(printed[0])
    explanations = []
    for ti, columns, complete, owners, support, strict_complete in _enumerate(tables, printed, missing):
        if complete and len(tables) > 1 and not strict_complete:
            reduced = [[row[m] for m in sorted(support)] for row in printed]
            other_tables = [table for i, table in enumerate(tables) if i != ti]
            complete = len(support) >= 2 and not _enumerate(other_tables, reduced, missing, require_witnesses=False)
        explanations.append((ti, columns, complete, owners, support))
    supported_anywhere = set().union(*(p[4] for p in explanations))
    kept = [p for p in explanations if p[4] >= supported_anywhere]
    if not kept or not any(p[2] for p in kept) or len({p[0] for p in kept}) != 1:
        return None
    ti = kept[0][0]
    common = {}
    for m in range(width):
        choices = {p[1][m] for p in kept}
        if len(choices) == 1 and (m in supported_anywhere or width == len(tables[ti][0])):
            common[m] = next(iter(choices))
    if len(common) < 2:
        return None
    owners = {j: set().union(*(p[3][j] for p in kept)) for j in range(len(printed))}
    return ti, common, owners


def test_whole_match_agrees_with_independent_exhaustive_column_and_row_paths():
    rng = random.Random(831304)
    for _ in range(1500):
        n, size = rng.randint(2, 6), rng.randint(3, 7)
        tables = [[[f'H{c}' for c in range(n)], *[[rng.choice(['', '0', '1', '2', 'a', 'b']) for _ in range(n)] for _ in range(size)]]]
        if rng.random() < .25:
            tables.append([[f'Other{c}' for c in range(n)], *[list(row) for row in tables[0][1:]]])
        if rng.random() < .25:
            tables.append([[f'Other{c}' for c in range(n)], *[[rng.choice(['', '0', '1', '2', 'a', 'b']) for _ in range(n)] for _ in range(size)]])
        columns = sorted(rng.sample(range(n), rng.randint(2, n)))
        start, length = 1, rng.randint(2, size)
        printed = [[r[c] for c in columns] for r in tables[0][start:start + length]]
        if rng.random() < .4:
            printed[rng.randrange(length)][rng.randrange(len(columns))] = rng.choice(['', '0', '1', 'a'])
        missing = rng.choice([True, False])
        expected = _oracle(tables, printed, missing)
        actual = hp.match(printed, hp.SourceIndex(tables, lambda s: s, keep_empty=True, allow_missing=missing), lambda s: s)
        assert (actual[:3] if actual else None) == expected, (tables, printed, missing)


def test_blank_neighbour_never_names_a_differently_printed_column():
    variants = [('1234.5', '1,234.50', False), ('2026-0001-17', '2026-0001', False),
                ('Steel 10mm', 'Steel 10 mm (KS)', True), ('Daesung', '*Daesung', True)]
    for actual, displayed, missing in variants:
        source = [['No', 'Item', 'Value', 'Remarks', 'Zone'],
                  ['1', 'Alpha pump', actual, '', 'North'], ['2', 'Beta valve', actual, '', 'South']]
        rows = [['1', 'Alpha pump', displayed], ['2', 'Beta valve', displayed]]
        doc = _doc(_numbered(rows))
        assert carry_source_headers(doc, [source], allow_missing_cells=missing) == 1
        assert doc.blocks[0].text == table_markdown([['No', 'Item', 'Column 3'], *rows])
        rows = [r[:3] for r in source[1:]]
        doc = _doc(_numbered(rows))
        assert carry_source_headers(doc, [source], allow_missing_cells=missing) == 1
        assert doc.blocks[0].text == table_markdown([source[0][:3], *rows])


def test_constant_column_paths_do_not_exhaust_or_starve_later_pages():
    source = [['Name', 'Value', *[f'Zero {c}' for c in range(38)]],
              *[[f'Row {r}', str(100 + r), *['0'] * 38] for r in range(400)]]
    easy = [['Name', 'Value', 'Code'], *[[f'Easy {r}', str(200 + r), f'C{r}'] for r in range(800)]]
    hard_pages = [[r[:7] for r in source[1 + p * 40:41 + p * 40]] for p in range(5)]
    easy_pages = [easy[1 + p * 40:41 + p * 40] for p in range(20)]
    for pages in [hard_pages + easy_pages, easy_pages + hard_pages]:
        index = hp.SourceIndex([source, easy], lambda s: s)
        answers = [hp.match(page, index, lambda s: s) for page in pages]
        assert all(answers[pages.index(page)] is not None for page in easy_pages)
        assert not index.exhausted and not index.limited_tables
        assert 20_000_000 - index.remaining < 2_000_000


def test_actual_table_work_limit_is_logged_and_leaves_budget_for_an_easy_table(monkeypatch, caplog):
    monkeypatch.setattr(hp, '_TABLE_BUDGET', 5000)
    source = [['Name', 'Value', *[f'Zero {c}' for c in range(38)]],
              *[[f'Row {r}', str(100 + r), *['0'] * 38] for r in range(200)]]
    index = hp.SourceIndex([source], lambda s: s)
    assert hp.match([r[:7] for r in source[1:41]], index, lambda s: s) is None
    assert index.limited_tables == 1 and not index.exhausted
    assert 'table heading alignment budget' in caplog.text
    easy = [[f'Row {r}', str(100 + r), *['0'] * 38] for r in range(2)]
    assert hp.match(easy, index, lambda s: s) is not None


def test_a_partially_matching_blank_neighbour_cannot_name_an_unknown_column():
    source = [['No', 'Item', 'Maker', 'Remarks', 'Zone'],
              ['1', 'Alpha', 'Hanil', 'Hanil', 'North'],
              ['2', 'Beta', 'Daesung', '', 'South'],
              ['3', 'Gamma', 'Hanil', '', 'East']]
    rows = [['1', 'Alpha', 'Hanil'], ['2', 'Beta', '*Daesung'], ['3', 'Gamma', 'Hanil']]
    doc = _doc(_numbered(rows))
    carry_source_headers(doc, [source], allow_missing_cells=True)
    assert doc.blocks[0].text == table_markdown([['No', 'Item', 'Column 3'], *rows])


def test_blank_cells_cannot_make_another_form_look_like_the_true_source():
    actual = [['No', 'Item', 'Maker 2020', 'Units 2020'],
              ['1', 'Alpha', 'First maker', 'ea'], ['2', 'Beta', 'Second maker', 'set'],
              ['3', 'Gamma', 'Third maker', 'box']]
    other = [['No', 'Item', 'Maker 2021', 'Units 2021'],
             ['1', 'Alpha', 'First maker', 'ea'], ['2', 'Beta', '', 'set'],
             ['3', 'Gamma', 'Third maker', 'box']]
    rows = [r[:] for r in actual[1:]]
    rows[1][2] += ' (rev)'
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [actual, other], allow_missing_cells=True) == 0
    assert doc.blocks[0].text == _numbered(rows)


def test_formula_results_keep_the_fixed_columns_of_a_uniquely_identified_table():
    source = [['Command', 'Description', 'Example', 'Result'],
              ['TIMES', 'Multiply values', 'times a b', ''], ['SUM', 'Add values', 'sum a b', ''],
              ['MAX', 'Largest value', 'max a b', '']]
    other = [['Letter', 'Name'], ['A', 'Alpha'], ['B', 'Beta']]
    rows = [r[:3] + ['unextracted formula'] for r in source[1:]]
    doc = _doc(_numbered(rows))
    assert carry_source_headers(doc, [source, other], allow_missing_cells=True) == 1
    assert doc.blocks[0].text == table_markdown([source[0], *rows])


def test_source_identity_cannot_discard_a_sparse_competing_form():
    source = [['Item', 'Spec 2020', 'Unit 2020', 'Note 2020'],
              ['First', '', '', ''], ['Second', 'Steel 37mm', 'set', 'see drawing'], ['Third', 'Rubber 23mm', '', '']]
    other = [['Item', 'Spec 2021', 'Unit 2021', 'Price 2021', 'Note 2021'],
             ['First', '', '', '', ''], ['Second', '', 'set', '', 'see drawing'], ['Third', 'Rubber 23mm', '', '98', '']]
    printed = [r[:] for r in source[1:]]
    printed[1][-1] += '!'
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [source, other], allow_missing_cells=True) == 0
    assert doc.blocks[0].text == _numbered(printed)


def test_an_explicit_form_assignment_cannot_become_the_table_heading():
    source = [['Location =', 'North', '', ''],
              ['Power Cost =', '$0.12', 'Per kWh', ''],
              ['Argon Cost =', '$26.00', 'Per 1000 cu ft', ''],
              ['Helium Cost =', '$106.00', 'Per 1000 cu ft', 'Operational Time'],
              ['Hydrogen Cost =', '$16.57', 'Per 1000 cu ft', 'Hours Per Day']]
    doc = _doc(_numbered(source[2:]))
    assert carry_source_headers(doc, [source]) == 0
    assert doc.blocks[0].text == _numbered(source[2:])


def test_equivalent_paths_distinguish_blanks_from_printed_overflow_values():
    for reverse in [False, True]:
        source = [['Name', 'Count', 'Missing', 'Overflow'],
                  ['Alpha', '1', '', '123'], ['Beta', '2', '###', '###'], ['Gamma', '3', '###', '###']]
        if reverse:
            source = [r[:2] + r[2:][::-1] for r in source]
        printed = [['Alpha', '1', '###'], ['Beta', '2', '###'], ['Gamma', '3', '###']]
        doc = _doc(_numbered(printed))
        assert carry_source_headers(doc, [source], allow_missing_cells=True) == 1
        assert doc.blocks[0].text == table_markdown([['Name', 'Count', 'Overflow'], *printed])


def test_complete_printed_records_keep_all_indistinguishable_columns():
    # The printed pair is EXACTLY Plan and EXACTLY Actual. Later missing
    # records cannot break that tie, regardless of table/column/row order.
    for missing in [False, True]:
        for reverse in [False, True]:
            for blanks in [1, 2]:
                source = [['Item', 'Code', 'Plan', 'Actual'],
                          ['Alpha pump', 'K-100', '10', '10'],
                          ['Beta valve', 'K-200', '20', '20'],
                          ['Gamma seal', 'K-300', '35', '31']]
                source += [row[:] for row in source[1:3]]
                for row in source[-blanks:]:
                    row[2] = ''
                if reverse:
                    source = [row[:2] + row[2:][::-1] for row in source]
                printed = [row[:3] for row in source[1:3]]
                doc = _doc(_numbered(printed))
                carry_source_headers(doc, [source], allow_missing_cells=missing)
                assert doc.blocks[0].text == table_markdown([['Item', 'Code', 'Column 3'], *printed])


def test_complete_printed_form_cannot_be_outvoted_by_a_different_form():
    a = [['Item', 'Spec old', 'Unit old', 'Qty old'],
         ['Pump', 'steel', 'ea', '10'], ['Valve', 'brass', 'set', '25'],
         ['Pump', 'iron', 'ea', '10'], ['Valve', 'brass', 'set', '40']]
    b = [['Item', 'Spec new', 'Unit new', 'Qty new'],
         ['Pump', 'steel', 'ea', '10'], ['Valve', 'brass', '', '25'],
         ['Pump', '', 'ea', '10'], ['Valve', 'brass', 'set', '']]
    for tables in [[a, b], [b, a]]:
        doc = _doc(_numbered(b[1:3]))
        assert carry_source_headers(doc, tables, allow_missing_cells=True) == 0
        assert doc.blocks[0].text == _numbered(b[1:3])


def test_two_column_faithful_tie_with_missing_later_copies_is_withheld():
    rows = [['Alpha', 'K-100', '2', '2'], ['Beta', 'K-300', '7', '7'],
            ['Gamma', 'K-200', '7', '7'], ['Delta', 'K-200', '6', '6'], ['Omega', 'K-100', '8', '8']]
    later = [r[:] for r in rows]
    later[0][2] = later[0][3] = later[1][3] = ''
    table = [['Item', 'Code', 'Plan', 'Actual'], *rows, *later]
    printed = [r[1:3] for r in rows]
    doc = _doc(_numbered(printed))
    assert carry_source_headers(doc, [table]) == 0
    assert doc.blocks[0].text == _numbered(printed)


def test_shared_decimal_index_does_not_spend_the_first_tables_search_budget():
    source = [['Name', 'Code', *[f'Amount {c}' for c in range(18)]],
              *[[f'Row {r}', f'Key {r}', *[f'{r * 20 + c}.1234' for c in range(18)]] for r in range(5000)]]
    pages = [[r[:6] for r in source[1 + start:41 + start]] for start in [0, 40, 80]]
    for order in [[0, 1, 2, 0], [2, 1, 0, 2]]:
        index = hp.SourceIndex([source], lambda s: s)
        for page in order:
            found = hp.match(pages[page], index, lambda s: s)
            assert found is not None and found[1] == dict(enumerate(range(6)))
        assert not index.exhausted and not index.limited_tables


def test_assignment_metadata_cannot_reuse_the_previous_sections_heading():
    first = [['Item', 'Jan', 'Feb', 'Mar', 'Apr'],
             ['Bolts', '11', '12', '13', '14'], ['Nuts', '21', '22', '23', '24']]
    second = [['Part', 'Plan', 'Actual', 'Gap', 'Rate'],
              ['Frames', '41', '42', '43', '44'], ['Panels', '51', '52', '53', '54']]
    # The assignment row ends the first section's heading. The real heading right
    # below it is not looked for: there the first all-text row is as likely a
    # record as a label (next test), so a page of three or four columns stays
    # unnamed. A known loss. On five columns the two-cell assignment row is too
    # sparse to be the section's label row, and the heading below it is found.
    for width in [3, 4, 5]:
        for title in [['Second half', '', '', '', ''], ['', '', '', '', 'Second half']]:
            table = [*first, [''] * 5, title, ['Location =', 'North', '', '', ''], *second]
            printed = [r[:width] for r in second[1:]]
            doc = _doc(_numbered(printed))
            if width == 5:
                assert carry_source_headers(doc, [table]) == 1
                assert doc.blocks[0].text == table_markdown([second[0], *printed])
            else:
                assert carry_source_headers(doc, [table]) == 0
                assert doc.blocks[0].text == _numbered(printed)


def test_records_below_an_assignment_row_never_become_its_heading():
    # Before this fix the first record under "Owner = | Kim" was taken out of the
    # body and used as the column names. The rows below an assignment row stay
    # unnamed, and the heading above it is not carried over it either.
    first = [['Item', 'Jan', 'Feb'], ['Pump', '11', '12'], ['Valve', '21', '22'], ['Seal', '31', '32']]
    texts = [['Ring b0', 'steel', 'left'], ['Gear b1', 'brass', 'right'], ['Hose b2', 'iron', 'wood'],
             ['Fan b3', 'wood', 'box']]
    numbers = [['Ring b0', '41', '42'], ['Gear b1', '51', '52'], ['Hose b2', '61', '62']]
    blank, title, owner = [''] * 3, ['Second half', '', ''], ['Owner =', 'Kim', '']
    parameters = [['Parameter =', 'Value'], ['Power cost', 'set'], ['Argon cost', '26.00'],
                  ['Helium cost', '106.00'], ['Neon cost', 'box']]
    grouped = [['Item', 'Jan', 'Feb'], *texts[:2], blank, ['Group 2', '', ''], ['Total =', 'open', ''],
               *texts[2:], blank, ['Group 3', '', ''], ['Cap c0', 'roll', 'left'], ['Pin c1', 'iron', 'set']]
    cases = [
        ([*first, blank, title, owner, *texts], texts),
        ([*first, blank, owner, *texts], texts[1:]),
        ([title, blank, owner, ['Site =', 'Plant B', ''], *texts], texts),   # no heading at all
        ([*first, blank, title, owner, *numbers], numbers),                   # the heading above is not carried
        (parameters, parameters[1:]),                                       # a two-column "Name = | Value" heading
        (grouped, [*grouped[6:8], *grouped[9:11]]),                          # "Total = | open" is a record
    ]
    for table, printed in cases:
        for missing in [False, True]:
            doc = _doc(_numbered(printed))
            assert carry_source_headers(doc, [table], allow_missing_cells=missing) == 0
            assert doc.blocks[0].text == _numbered(printed)


def test_generated_faithful_records_never_invent_a_unique_column_label():
    # Ground truth is equality of actual source cells. It knows nothing about
    # support filters, budgets, numeric near-matches or production bitmaps.
    rng = random.Random(11450583)
    for _ in range(400):
        width, length = rng.randint(3, 7), rng.randint(3, 8)
        rows = [[f'Item {r}', *[rng.choice(['', '1', '2', '3']) for _ in range(width - 1)]] for r in range(length)]
        if rng.random() < .7:
            for row in rows:
                row[-1] = row[-2]
        later = [[v if rng.random() > .35 else '' for v in row] for row in rows]
        tables = [[[f'Label {c}' for c in range(width)], *rows, *later]]
        if rng.random() < .5:
            tables.append([[f'Other {c}' for c in range(width)], *[r[:] for r in rows]])
        columns = sorted(rng.sample(range(width), rng.randint(2, width)))
        start = rng.randrange(length - 1)
        printed = [[r[c] for c in columns] for r in rows[start:start + rng.randint(2, length - start)]]
        if any(not any(r) for r in printed):
            continue
        truths = [set() for _ in columns]
        for table in tables:
            body = [r for r in table if any(r)]
            for cols in itertools.combinations(range(width), len(columns)):
                view = [[r[c] for c in cols] for r in body]
                if any(view[s:s + len(printed)] == printed for s in range(len(view) - len(printed) + 1)):
                    for m, c in enumerate(cols):
                        truths[m].add(table[0][c])
        doc = _doc(_numbered(printed))
        carry_source_headers(doc, tables, allow_missing_cells=rng.choice([False, True]))
        from app.parsers.carried_headers import _cells
        head = _cells(doc.blocks[0].text.splitlines()[0])
        assert doc.blocks[0].text.splitlines()[2:] == _numbered(printed).splitlines()[2:]
        for label, truth in zip(head, truths):
            if label and not label.startswith('Column '):
                assert truth == {label}
