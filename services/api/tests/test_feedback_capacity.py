import asyncio
import threading
import time

import pytest
from starlette.requests import Request

from app import main
from app.config import Settings
from app.db import Database, DocRow, QueueFull


def test_upload_reservations_do_not_consume_parser_queue_slots(tmp_path):
    db = Database(tmp_path / "state.db")
    for doc_id, client in [("a", "one"), ("b", "two")]:
        assert db.reserve(doc_id, client, sample=False, pending_limit=1, client_limit=5, stored_limit=20) is None
    assert db.count_pending() == 0
    # A ready sample must remain available while uploads are in flight.
    assert db.reserve("sample", "three", sample=True, pending_limit=1, client_limit=5, stored_limit=20) is None


def test_unlock_stops_reading_an_oversized_body(tmp_path, monkeypatch):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent"))
    monkeypatch.setattr(main, "state", state)
    calls = []
    async def receive():
        calls.append(True)
        return {"type": "http.request", "body": b" " * 8192, "more_body": True}
    request = Request({"type": "http", "method": "POST", "path": "/api/vlm/unlock", "headers": [],
                       "client": ("127.0.0.1", 1)}, receive)
    async def run():
        with pytest.raises(main.HTTPException) as error:
            await main.vlm_unlock(request)
        assert error.value.status_code == 413
    asyncio.run(run())
    assert len(calls) <= 2


def test_ipv6_rotation_within_a_subnet_does_not_reset_limits(tmp_path):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent"))
    def key(ip):
        return state.client_key(Request({"type": "http", "client": (ip, 1), "headers": []}))
    assert key("2001:db8:1:2::1") == key("2001:db8:1:2::ffff")
    assert key("2001:db8:1:2::1") == key("2001:db8:1:3::1")
    assert key("2001:db8:1:2::1") != key("2001:db8:1:100::1")
    assert key("::ffff:192.0.2.1") == key("192.0.2.1")


def test_upload_limits_are_independent_from_samples_and_parser_capacity(tmp_path):
    db = Database(tmp_path / "state.db")
    def reserve(doc, client, sample=False):
        return db.reserve(doc, client, sample=sample, pending_limit=10, client_limit=5, stored_limit=10,
                          upload_limit=2, client_upload_limit=1)
    assert reserve("a", "one") is None
    assert reserve("b", "one") == "upload_busy"
    assert reserve("c", "two") is None
    assert reserve("d", "three") == "upload_busy"
    assert reserve("ready-sample", "three", True) is None
    db.release("a")
    assert reserve("e", "three") is None


def test_queue_admission_is_atomic_after_concurrent_uploads_finish(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    a, b = Database(tmp_path / "state.db"), Database(tmp_path / "state.db")
    barrier = threading.Barrier(2)
    def insert(pair):
        db, key = pair
        row = DocRow(id=key, token_hash="h", filename="test.pdf", file_hash="h", page_count=1,
                     parser="pymupdf", status="queued", error_code=None, error_message=None,
                     client_key=key, sample_key=None, created_at=time.time(), expires_at=time.time() + 60,
                     started_at=None, finished_at=None, revision=0)
        barrier.wait()
        try:
            db.insert(row, pending_limit=1)
            return "accepted"
        except QueueFull:
            return "full"
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(insert, [(a, "one"), (b, "two")]))
    assert sorted(results) == ["accepted", "full"]
    assert a.count_pending() == 1


def test_stalled_upload_releases_its_directory_and_reservation(tmp_path, monkeypatch):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent",
                              upload_idle_seconds=0.02))
    monkeypatch.setattr(main, "state", state)
    async def receive():
        await asyncio.sleep(60)
        return {"type": "http.request", "body": b"", "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/api/documents", "headers": [],
                       "client": ("127.0.0.1", 1)}, receive)
    async def run():
        with pytest.raises(main.HTTPException) as error:
            await main.upload(request, "pymupdf")
        assert error.value.status_code == 408
    asyncio.run(run())
    assert list(state.storage.root.iterdir()) == []
    assert state.db._conn.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 0


def test_sample_creation_has_its_own_hourly_limit(tmp_path, monkeypatch):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / "absent",
                              samples_per_client_per_hour=1))
    state.samples = {"example": {}}
    monkeypatch.setattr(main, "state", state)
    request = Request({"type": "http", "client": ("127.0.0.1", 1), "headers": []})
    state.sample_creations.allow(state.client_key(request))
    async def run():
        with pytest.raises(main.HTTPException) as error:
            await main.create_from_sample("example", request, "pymupdf")
        assert error.value.status_code == 429
        assert error.value.detail["code"] == "sample_rate_limited"
    asyncio.run(run())


def test_sample_pool_cannot_take_capacity_reserved_for_uploads(tmp_path):
    db = Database(tmp_path/'state.db')
    def reserve(doc, sample):
        return db.reserve(doc,doc,sample=sample,pending_limit=10,client_limit=20,
                          stored_limit=6,sample_stored_limit=2)
    assert reserve('sample-v6-a',True) is None
    assert reserve('sample-v6-b',True) is None
    assert reserve('sample-v6-c',True) == 'sample_storage_full'
    assert reserve('upload-v4',False) is None
    assert reserve('upload-v6',False) is None
    db.release('sample-v6-a')
    assert reserve('sample-v6-c',True) is None


def test_busy_upload_does_not_consume_hourly_allowance(tmp_path,monkeypatch):
    state = main.App(Settings(data_dir=tmp_path,basic_samples=False,gallery_dir=tmp_path/'absent',
                              max_uploads_in_progress=1,uploads_per_client_per_hour=1))
    monkeypatch.setattr(main,'state',state)
    main.reserve_capacity('other-client','occupied')
    request = Request({'type':'http','method':'POST','path':'/api/documents','headers':[], 'client':('127.0.0.1',1)})
    async def run():
        for _ in range(3):
            with pytest.raises(main.HTTPException) as error: await main.upload(request,'pymupdf')
            assert error.value.detail['code'] == 'upload_busy'
        assert not state.uploads.full(state.client_key(request))
    asyncio.run(run())


def test_trickling_bytes_cannot_hold_upload_slot_until_total_timeout(tmp_path,monkeypatch):
    state = main.App(Settings(data_dir=tmp_path,basic_samples=False,gallery_dir=tmp_path/'absent',
                              upload_idle_seconds=.1,upload_grace_seconds=.05,upload_min_bytes_per_second=1000))
    monkeypatch.setattr(main,'state',state)
    reads=[]
    async def receive():
        await asyncio.sleep(.01)
        reads.append(True)
        return {'type':'http.request','body':b'x','more_body':True}
    request = Request({'type':'http','method':'POST','path':'/api/documents','headers':[], 'client':('127.0.0.1',1)},receive)
    async def run():
        with pytest.raises(main.HTTPException) as error: await main.upload(request,'pymupdf')
        assert error.value.status_code == 408
    asyncio.run(run())
    assert len(reads) < 10
    assert list(state.storage.root.iterdir()) == []
    assert state.db._conn.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 0


def test_sample_copy_does_not_block_the_request_event_loop(tmp_path,monkeypatch):
    from types import SimpleNamespace
    state=main.App(Settings(data_dir=tmp_path,basic_samples=False,gallery_dir=tmp_path/'absent'))
    state.samples={'example':{}}
    monkeypatch.setattr(main,'state',state)
    started,release=threading.Event(),threading.Event()
    def copying(sample,parser,key,doc_id):
        started.set()
        assert release.wait(2), 'event loop could not release the copy'
        return SimpleNamespace(id=doc_id),'token'
    monkeypatch.setattr(main,'create_document_from_sample',copying)
    monkeypatch.setattr(main,'status_payload',lambda row:{'id':row.id})
    request=Request({'type':'http','client':('127.0.0.1',1),'headers':[]})
    async def run():
        task=asyncio.create_task(main.create_from_sample('example',request,'pymupdf'))
        while not started.is_set(): await asyncio.sleep(.001)
        release.set()
        assert (await task)['accessToken'] == 'token'
    asyncio.run(run())


def test_ipv6_64_rotation_cannot_fill_all_upload_slots(tmp_path, monkeypatch):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / 'absent'))
    monkeypatch.setattr(main, 'state', state)
    def key(ip):
        return state.client_key(Request({'type': 'http', 'client': (ip, 1), 'headers': []}))
    main.reserve_capacity(key('2001:db8:1:1::1'), 'first')
    main.reserve_capacity(key('2001:db8:1:2::1'), 'second')
    with pytest.raises(main.HTTPException) as failure:
        main.reserve_capacity(key('2001:db8:1:3::1'), 'third')
    assert failure.value.detail['code'] == 'upload_busy'
    main.reserve_capacity(key('2001:db8:2:1::1'), 'unrelated')
    main.reserve_capacity(key('192.0.2.1'), 'ipv4')
    assert state.db._conn.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 4


def test_large_declared_upload_must_make_progress_toward_its_deadline(tmp_path, monkeypatch):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / 'absent',
                             upload_timeout_seconds=1, upload_grace_seconds=.04,
                             upload_idle_seconds=.3, upload_min_bytes_per_second=1))
    monkeypatch.setattr(main, 'state', state)
    reads = []
    async def receive():
        await asyncio.sleep(.01)
        reads.append(True)
        return {'type': 'http.request', 'body': b'x' * 100, 'more_body': True}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/documents',
                       'headers': [(b'content-length', b'100000')], 'client': ('192.0.2.1', 1)}, receive)
    async def run():
        with pytest.raises(main.HTTPException) as failure:
            await main.upload(request, 'pymupdf')
        assert failure.value.status_code == 408
    asyncio.run(run())
    assert len(reads) < 10
    assert list(state.storage.root.iterdir()) == []
    assert state.db._conn.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 0


def test_declared_upload_at_required_rate_is_consumed_completely():
    received = 0
    async def receive():
        nonlocal received
        await asyncio.sleep(.005)
        received += 1000
        return {'type': 'http.request', 'body': b'x' * 1000, 'more_body': received < 10000}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/', 'headers': []}, receive)
    async def run():
        result = b''.join([part async for part in main.idle_limited_stream(
            request, .5, minimum_rate=100000, grace_seconds=.05)])
        assert result == b'x' * 10000
    asyncio.run(run())


def test_one_ipv6_site_cannot_hold_every_upload_slot(tmp_path, monkeypatch):
    # Two /56 customers inside one /48 used to take all four global slots.
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / 'absent'))
    monkeypatch.setattr(main, 'state', state)
    def keys(ip):
        request = Request({'type': 'http', 'client': (ip, 1), 'headers': []})
        return state.client_key(request), state.network_key(request)
    for i, ip in enumerate(['2001:db8:4800:100::1', '2001:db8:4800:100::2',
                            '2001:db8:4800:200::1', '2001:db8:4800:200::2']):
        key, network = keys(ip)
        main.reserve_capacity(key, f'site-{i}', network=network)
    key, network = keys('2001:db8:4800:300::1')
    with pytest.raises(main.HTTPException) as failure:
        main.reserve_capacity(key, 'same-site', network=network)
    assert failure.value.detail['code'] == 'upload_busy'
    for i, ip in enumerate(['2001:db8:4900::1', '198.51.100.7', '203.0.113.9']):
        key, network = keys(ip)
        main.reserve_capacity(key, f'other-{i}', network=network)
    assert state.db._conn.execute('SELECT COUNT(*) FROM admissions').fetchone()[0] == 7


def test_network_key_groups_a_48_and_keeps_ipv4_addresses_apart(tmp_path):
    state = main.App(Settings(data_dir=tmp_path, basic_samples=False, gallery_dir=tmp_path / 'absent'))
    def network(ip):
        return state.network_key(Request({'type': 'http', 'client': (ip, 1), 'headers': []}))
    assert network('2001:db8:4800:100::1') == network('2001:db8:4800:ff00::1')
    assert network('2001:db8:4800::1') != network('2001:db8:4801::1')
    assert network('198.51.100.7') != network('198.51.100.8')
    assert network('::ffff:198.51.100.7') == network('198.51.100.7')


def test_existing_admissions_table_gains_the_network_column(tmp_path):
    import sqlite3
    path = tmp_path / 'state.db'
    old = sqlite3.connect(path)
    old.execute('CREATE TABLE admissions (id TEXT PRIMARY KEY, client_key TEXT NOT NULL, '
                'sample INTEGER NOT NULL, expires_at REAL NOT NULL)')
    old.execute("INSERT INTO admissions VALUES ('old', 'client', 0, ?)", (time.time() + 60,))
    old.commit()
    old.close()
    db = Database(path)
    assert 'network_key' in {r[1] for r in db._conn.execute('PRAGMA table_info(admissions)')}
    assert db.reserve('new', 'client', sample=False, pending_limit=10, client_limit=5, stored_limit=10,
                      upload_limit=4, client_upload_limit=2, network_key='site', network_upload_limit=1) is None
    assert db.reserve('again', 'other', sample=False, pending_limit=10, client_limit=5, stored_limit=10,
                      upload_limit=4, client_upload_limit=2, network_key='site', network_upload_limit=1) == 'upload_busy'


def test_interruption_budget_survives_restarts_and_legacy_rows(tmp_path):
    import sqlite3
    from app.db import Database, DocRow, MAX_INTERRUPTED_RETRIES, SCHEMA

    path = tmp_path / "state.db"
    with sqlite3.connect(path) as old:
        old.executescript(SCHEMA.replace(",\n    interrupted_retries INTEGER NOT NULL DEFAULT 0", ""))
    db = Database(path)
    now = time.time()
    for key in ("first", "second"):
        db.insert(DocRow(id=key, token_hash="t", filename="sample.pdf", file_hash="h", page_count=1,
                         parser="pymupdf", status="queued", error_code=None, error_message=None, client_key="c",
                         sample_key=None, created_at=now + (key == "second"), expires_at=now + 900,
                         started_at=None, finished_at=None, revision=0))
    for retry in range(MAX_INTERRUPTED_RETRIES):
        assert db.claim_next_job().id == "first"
        db = Database(path)
        assert db.requeue_interrupted() == 1
        assert db.get("first").interrupted_retries == retry + 1
        assert db.requeue_interrupted() == 0  # reopening before the next claim costs no attempt
    assert db.claim_next_job().id == "first"
    assert Database(path).requeue_interrupted() == 0
    failed = db.get("first")
    assert failed.status == "failed" and failed.error_code == "processing_interrupted" and failed.finished_at
    assert db.claim_next_job().id == "second"


@pytest.mark.parametrize("code", [-9, 137])
def test_a_killed_parser_is_recognized_with_or_without_a_launcher(tmp_path, code):
    from app.jobs import JobRunner

    assert JobRunner._resource_failure(code, False, tmp_path)
    assert not JobRunner._resource_failure(1, False, tmp_path)
