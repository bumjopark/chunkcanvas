"""Regressions from the independent code review; synthetic inputs contain no user data."""
from types import SimpleNamespace
import json
import threading
import asyncio
import pytest

from app import main


def test_csv_table_continuation_does_not_repeat_first_data_row():
    from app.parsers import source_tables as s
    table = [["Danish", "A first record"], ["German", "A second record"], ["Greek", "A third record"]]
    text = "German A second record Greek A third record"
    rows = s._rows(table, [s._grams(" ".join(r)) for r in table], s._grams(text),
                   text, repeat_header=False)
    assert rows == table[1:]


def test_csv_table_does_not_restore_a_similar_record_from_the_next_page():
    from app.parsers.source_tables import apply_source_tables
    from app.parsers.base import table_markdown
    rows = [[f"samples/basic/{name}.hwp", "2", "2", "0"]
            for name in ("bookreview", "english", "interview")]
    visible = table_markdown(rows[:2])
    doc = document(visible, "table")
    apply_source_tables(doc, [rows], columns_overflow=False, repeat_header=False)
    assert doc.blocks[0].text == visible


def test_sheet_page_does_not_grow_into_a_row_whose_cells_occur_separately():
    from app.parsers import source_tables as s
    table = [
        ["Region", "Seller", "Product", "Amount"],
        ["North", "Kurt", "Books", "200"],  # all values occur, but this row does not
        ["North", "Hans", "Books", "100"],
        ["West", "Kurt", "Computer", "200"],
    ]
    visible = "North Hans Books 100 West Kurt Computer 200"
    keys = [[s._cell_key(cell) for cell in row] for row in table]
    norm = [[" ".join(s._words(cell)) for cell in row] for row in table]
    rows = s._sheet_part(table, keys, norm, " " + visible.lower() + " ", s._grams(visible))
    assert rows == [table[0], *table[2:]]


def test_sheet_page_requires_boundary_row_amounts_to_be_visible():
    from app.parsers import source_tables as s
    table = [["Seller", "Product", "Amount"], ["Alex", "Books", "999.99"], ["Alex", "Books", "100.00"]]
    visible = "Alex Books 100.00"
    keys = [[s._cell_key(cell) for cell in row] for row in table]
    norm = [[" ".join(s._words(cell)) for cell in row] for row in table]
    # Matching words are punctuation-free; the production caller separately
    # supplies printed text so signed amounts and decimal points remain evidence.
    rows = s._sheet_part(table, keys, norm, " " + " ".join(s._words(visible)) + " ", s._grams(visible),
                         printed_text=visible)
    assert rows == [table[0], table[2]]


def document(text, kind="paragraph"):
    from app.models import ParsedDocument
    return ParsedDocument.model_validate({
        "document": {"id": "review", "filename": "review.pdf", "fileHash": "h", "pageCount": 1,
                     "parser": "hybrid", "parserVersion": "test"},
        "pages": [{"index": 0, "width": 595, "height": 842}],
        "blocks": [{"id": "b0000", "readingOrder": 0, "type": kind, "text": text,
                    "regions": [{"pageIndex": 0, "bbox": {"x0": .1, "y0": .1, "x1": .9, "y1": .9}}],
                    "source": {"parser": "test"}}], "blocksFingerprint": "f"})


def test_page_notes_may_follow_the_body_without_duplicating_core_order_rules():
    blocks = [
        {"id": "body1", "type": "paragraph", "text": "First page."},
        {"id": "note", "type": "footnote", "text": "First note. Second note.", "lines": [{}]},
        {"id": "body2", "type": "paragraph", "text": "Second page."},
    ]
    def cut(block, offset=None):
        return SimpleNamespace(block_id=block, offset=offset)

    # Core moves the note after body2. A cut inside it must remain saveable.
    cuts = [cut("body1"), cut("body2"), cut("note", 12)]
    assert main.boundaries_valid(blocks, cuts)
    assert not main.boundaries_valid(blocks, [*cuts, cut("body1")])
    assert not main.boundaries_valid(blocks, [cut("note", 12), cut("body2"), cut("note", 5)])
    assert not main.boundaries_valid(blocks, [cut("unknown")])
    assert not main.boundaries_valid(blocks, [cut("note", 999)])


def test_utf16_wire_geometry_and_saved_offsets():
    from app.offsets import wire_document

    block = {"id": "b", "type": "paragraph", "text": "😀𝑋. B.",
             "lines": [{"start": 0, "end": 6, "xs": [0, .1, .2, .3, .4, .5]}]}
    original = {"blocks": [block]}
    wire = wire_document(original)
    assert wire["offsetEncoding"] == "utf16"
    assert wire["blocks"][0]["lines"][0] == {"start": 0, "end": 8, "xs": [0, 0, .1, .1, .2, .3, .4, .5]}
    assert block["lines"][0]["end"] == 6  # old disk data is not mutated
    assert wire_document(wire) is wire
    assert main.boundaries_valid([block], [SimpleNamespace(block_id="b", offset=6)])
    assert not main.boundaries_valid([block], [SimpleNamespace(block_id="b", offset=1)])
    assert main.excluded_valid([block], [SimpleNamespace(block_id="b", start=0, end=8)])
    assert not main.excluded_valid([block], [SimpleNamespace(block_id="b", start=1, end=8)])
    table = "| h |\n| --- |\n| 😀 |\n| 𝑋 |"
    assert main.table_row_starts(table) == {len(table[:table.index('| 𝑋')]) + 1}


def test_independent_equal_rows_survive_but_spanning_source_cells_are_not_duplicated():
    from app.office_tables import fill_grid
    from app.parsers.source_tables import apply_source_tables
    rows = [["Item", "Quantity"], ["Widget", "100"], ["Widget", "100"], ["Gadget", "200"]]
    shown = "| Item | Quantity |\n| --- | --- |\n| Widget | 100 |\n| Widget | 100 |\n| Gadget | 200 |"
    doc = document(shown, "table")
    apply_source_tables(doc, [rows], columns_overflow=False)
    assert doc.blocks[0].text.count("| Widget | 100 |") == 2
    assert fill_grid([[(1, 1, "a")], [(1, 1, "a")]]) == [["a"], ["a"]]
    assert fill_grid([[(1, 2, "a")], []]) == [["a"]]


def test_one_letter_sheet_preserves_all_cells():
    from app.parsers.source_tables import apply_source_tables
    doc = document("A B C D E F G H I")
    apply_source_tables(doc, [[list("ABC"), list("DEF"), list("GHI")]], sheets=True)
    from collections import Counter
    from app.parsers.source_tables import _words
    assert Counter(_words(" ".join(b.text for b in doc.blocks))) == Counter("abcdefghi")


def test_source_table_header_match_cannot_erase_unmatched_body():
    from app.parsers.source_tables import apply_source_tables
    text = "| Item description | Value amount |\n| --- | --- |\n| Unique continuation text | 987654 |"
    doc = document(text, "table")
    apply_source_tables(doc, [[["Item description", "Value amount"], ["Different first row", "123456"]]])
    assert "Unique continuation text" in " ".join(b.text for b in doc.blocks)
    assert "987654" in " ".join(b.text for b in doc.blocks)


def test_source_table_cannot_absorb_fragment_with_different_body():
    from app.parsers.source_tables import _absorb_fragments
    doc = document("| Common header description | Cost total |\n| --- | --- |\n| NEW ROW | 987654 |", "table")
    other = doc.blocks[0].model_copy(update={"id": "other", "text":
        "| Common header description | Cost total |\n| --- | --- |\n| OLD ROW | 123456 |"})
    doc.blocks.append(other)
    _absorb_fragments(doc, {"other": 0})
    assert len(doc.blocks) == 2
    assert "987654" in doc.blocks[0].text


def test_source_table_does_not_copy_an_unprinted_long_cell_tail():
    from app.parsers.source_tables import apply_source_tables
    words = [f"sentence{n:03d}" for n in range(100)]
    visible = " ".join(words[:75])
    text = f"| Description |\n| --- |\n| {visible} |"
    doc = document(text, "table")
    apply_source_tables(doc, [[["Description"], [" ".join(words)]]], columns_overflow=False)
    assert "sentence074" in doc.blocks[0].text
    assert "sentence099" not in doc.blocks[0].text


@pytest.mark.parametrize("picture_text", ["A", "A\n10\n20\nUseful diagram"])
def test_picture_cleanup_cannot_delete_the_last_copy_of_a_letter(picture_text):
    from app.parsers.base import finish
    original = document("A")
    paragraph = original.blocks[0].model_dump(by_alias=True)
    paragraph["regions"][0]["bbox"] = {"x0": .3, "y0": .3, "x1": .32, "y1": .32}
    picture = document(picture_text, "picture").blocks[0].model_dump(by_alias=True)
    parsed = finish(original.document.model_dump(by_alias=True),
                    [p.model_dump(by_alias=True) for p in original.pages], [picture, paragraph])
    assert any("A" in b.text.split() for b in parsed.blocks)


def test_html_preserves_body_text_and_inline_tails():
    from app.convert import sanitize_html
    assert sanitize_html("<body>Hello <b>world</b>!</body>") == "Hello <b>world</b>!"
    assert sanitize_html("<html><body>Only text &amp; more</body></html>") == "Only text &amp; more"


def test_rotated_text_is_retained_without_inventing_horizontal_character_positions(tmp_path):
    import pymupdf
    from app.parsers import pymupdf_parser
    from app.parsers.base import add_text_lines
    pdf = pymupdf.open()
    pdf.new_page().insert_text((150, 250), "VERTICAL CONTENT", rotate=90)
    path = tmp_path / "vertical.pdf"
    pdf.save(path)
    doc = pymupdf_parser.parse(path, file_meta={"id": "x", "filename": "v.pdf", "fileHash": "h"}, progress=lambda *a: None)
    assert any(b.text == "VERTICAL CONTENT" for b in doc.blocks)
    add_text_lines(doc)
    assert all(b.lines is None for b in doc.blocks)


def test_estimated_table_positions_do_not_allocate_a_row_to_markdown_separator():
    from app.parsers.base import add_table_lines
    doc = document("| H |\n| --- |\n| A |\n| B |", "table")
    add_table_lines(doc)
    block = doc.blocks[0]
    assert block.line_geometry == "estimated"
    assert len(block.lines) == 3
    assert [block.text[line.start:line.end] for line in block.lines] == ["| H |", "| A |", "| B |"]


def test_stale_figure_manifest_is_cleared_and_wrong_types_cannot_be_overwritten(tmp_path, monkeypatch):
    from app import vlm
    from app.config import Settings
    from app.parsers.figures import write_figures
    doc = document("IMPORTANT PARAGRAPH")
    folder = tmp_path / "figures"
    folder.mkdir()
    manifest = folder / "figures.json"
    spec = {"blocksFingerprint": "f", "figures": [{"blockId": "b0000", "file": "old.png"}]}
    manifest.write_text(json.dumps(spec))
    assert write_figures(tmp_path / "unused.pdf", doc, folder, 10) == 0
    assert not manifest.exists()
    manifest.write_text(json.dumps(spec))
    (tmp_path / "parsed.json").write_text(doc.model_dump_json(by_alias=True))
    monkeypatch.setattr(vlm, "_ask", lambda *a: (_ for _ in ()).throw(AssertionError("unexpected image request")))
    vlm.describe_figures(tmp_path, Settings(data_dir=tmp_path))
    assert json.loads((tmp_path / "parsed.json").read_text())["blocks"][0]["text"] == "IMPORTANT PARAGRAPH"


def test_cancelled_vision_job_does_not_start_queued_requests(tmp_path, monkeypatch):
    from app import vlm
    from app.config import Settings
    from app.jobs import JobRunner
    doc = document("picture", "picture")
    doc.blocks = [doc.blocks[0].model_copy(update={"id": str(i)}) for i in range(3)]
    (tmp_path / "parsed.json").write_text(doc.model_dump_json(by_alias=True))
    folder = tmp_path / "figures"
    folder.mkdir()
    for i in range(3):
        (folder / f"{i}.png").write_bytes(b"mock")
    (folder / "figures.json").write_text(json.dumps({"blocksFingerprint": "f", "figures": [
        {"blockId": str(i), "file": f"{i}.png"} for i in range(3)]}))
    queued, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    calls = []
    async def ask(_settings, _image, figure, *_args):
        calls.append(figure["blockId"])
        queued.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        return None
    monkeypatch.setattr(vlm, "_ask", ask)
    settings = Settings(data_dir=tmp_path, vlm_concurrency=1)
    runner = JobRunner(settings, None, None)
    runner._cancel_events["review"] = cancelled
    thread = threading.Thread(target=vlm.describe_figures, args=(tmp_path, settings, cancelled))
    thread.start()
    try:
        assert queued.wait(5)
        runner.cancel("review")
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert calls == ["0"]


def test_optional_ocr_retry_uses_only_the_remaining_budget(tmp_path, monkeypatch):
    from app.config import Settings
    from app.jobs import JobRunner
    from app.storage import Storage
    from app import jobs
    elapsed = [0]
    timeouts = []
    db = SimpleNamespace(get=lambda _: SimpleNamespace(notice=""), update=lambda *a, **kw: None)
    settings = Settings(data_dir=tmp_path, job_timeout_seconds=60)
    storage = Storage(settings)
    doc_id = "r" * 24
    folder = storage.doc_dir(doc_id)
    folder.mkdir()
    storage.write_json(doc_id, "job.json", {"id": doc_id})
    (folder / "source.pdf").write_bytes(b"%PDF-test")
    runner = JobRunner(settings, db, storage)
    monkeypatch.setattr(runner, "_prepare", lambda *a: "hybrid")
    monkeypatch.setattr(jobs.capabilities, "ocr_available", lambda _: True)
    monkeypatch.setattr(jobs.time, "monotonic", lambda: elapsed[0])
    def parse(_id, folder, args, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            elapsed[0] += 36
            return -1, True
        parsed = document("A visible paragraph")
        parsed.document.id = doc_id
        (folder / "parsed.json").write_text(parsed.model_dump_json(by_alias=True))
        return 0, False
    monkeypatch.setattr(runner, "_parse_once", parse)
    runner._run(doc_id, "hybrid", None, "h")
    assert timeouts == [36, 24]
    assert storage.read_json(doc_id, "parsed.json")["blocks"][0]["text"] == "A visible paragraph"


def test_concurrent_uploads_reserve_capacity_before_pdf_inspection(tmp_path, monkeypatch):
    from starlette.requests import Request
    from app.config import Settings
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent",
                              max_active_documents_per_client=1, max_queued_jobs=1))
    monkeypatch.setattr(main, "state", state)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def inspect(*args):
            entered.set()
            await release.wait()
            return {"ok": True, "pageCount": 1, "hasText": True}
        monkeypatch.setattr(main, "inspect_upload", inspect)
        def request():
            async def receive():
                return {"type": "http.request", "body": b"%PDF-1.7\nreview", "more_body": False}
            return Request({"type": "http", "method": "POST", "path": "/api/documents", "headers": [],
                            "client": ("127.0.0.1", 1)}, receive)
        first = asyncio.create_task(main.upload(request(), "pymupdf"))
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with pytest.raises(main.HTTPException) as rejected:
                await main.upload(request(), "pymupdf")
            assert rejected.value.status_code == 429
            assert rejected.value.detail["code"] == "too_many_documents"
        finally:
            release.set()
            await first
        assert state.db.count_pending() == 1
    asyncio.run(run())


def test_capacity_is_atomic_across_database_connections_and_covers_samples(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from app.db import Database
    a, b = Database(tmp_path / "db"), Database(tmp_path / "db")
    barrier = threading.Barrier(2)
    def reserve(pair):
        db, key = pair
        barrier.wait()
        return db.reserve(key, "client", sample=True, pending_limit=10, client_limit=1, stored_limit=10)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(reserve, [(a, "one"), (b, "two")]))
    assert results.count(None) == 1
    assert results.count("too_many_documents") == 1
    a.release("one"); a.release("two")
    assert a.reserve("three", "client", sample=True, pending_limit=10, client_limit=1, stored_limit=10) is None


def test_proxy_uses_only_the_selected_header(tmp_path):
    from starlette.requests import Request
    from app.config import Settings
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent",
                              trust_proxy_headers=True, proxy_ip_header="x-forwarded-for"))
    def request(cf):
        return Request({"type": "http", "client": ("127.0.0.1", 1), "headers": [
            (b"x-forwarded-for", b"192.0.2.1"), (b"cf-connecting-ip", cf.encode())]})
    assert state.client_key(request("198.51.100.1")) == state.client_key(request("198.51.100.2"))
    state.settings.proxy_ip_header = "cf-connecting-ip"
    assert state.client_key(request("198.51.100.1")) != state.client_key(request("198.51.100.2"))


def test_body_limit_stops_consuming_the_stream():
    from starlette.requests import Request
    calls = []
    async def receive():
        calls.append(1)
        return {"type": "http.request", "body": b"xxxx", "more_body": True}
    async def run():
        request = Request({"type": "http", "headers": []}, receive)
        with pytest.raises(main.HTTPException) as rejected:
            await main.limited_body(request, 5)
        assert rejected.value.status_code == 413
    asyncio.run(run())
    assert len(calls) == 2


def test_zip_sniffing_reads_only_a_bounded_prefix(tmp_path, monkeypatch):
    import zipfile
    from app.formats import detect
    path = tmp_path / "mime.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", b"A" * (16 * 1024 * 1024))
    reads = []
    original = zipfile.ZipExtFile.read
    def read(self, n=-1):
        reads.append(n)
        return original(self, n)
    monkeypatch.setattr(zipfile.ZipExtFile, "read", read)
    detect(path, "mime.zip")
    assert reads == [200]


def test_inspections_are_bounded_and_cancellation_releases_the_slot(monkeypatch):
    async def run():
        state = SimpleNamespace(inspections=asyncio.Semaphore(1))
        monkeypatch.setattr(main, "state", state)
        entered = asyncio.Queue()
        release = asyncio.Event()
        active = peak = 0
        async def inspect(path, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await entered.put(path)
            try:
                await release.wait()
                return {"ok": True}
            finally:
                active -= 1
        monkeypatch.setattr(main, "_inspect_upload", inspect)
        first = asyncio.create_task(main.inspect_upload("first"))
        assert await asyncio.wait_for(entered.get(), 2) == "first"
        second = asyncio.create_task(main.inspect_upload("second"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await asyncio.wait_for(entered.get(), 2) == "second"
        release.set()
        assert await second == {"ok": True}
        assert peak == 1 and active == 0
    asyncio.run(run())
