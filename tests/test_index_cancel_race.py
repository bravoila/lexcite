"""Barrier-controlled regression tests for index cancellation races."""
import threading

from app import db, indexing, retrieval_services as rs


def _configure():
    settings = rs.RetrievalSettings().model_dump()
    settings["auto_index"] = False
    settings["embedding"].update(
        provider="custom",
        base_url="http://localhost:9998/v1",
        model="synthetic-embedding",
        api_key="test-only-key",
    )
    rs.save_settings(settings)


def _add_article(client):
    response = client.post(
        "/api/laws",
        json={
            "name": "索引取消竞态测试规则",
            "version": "仅测试",
            "valid_from": "2021-01-01",
            "text": "第一条 测试义务人应当依法履行相关职责。",
        },
    )
    assert response.status_code == 200, response.text
    law_id = response.json()["id"]
    response = client.post(
        f"/api/laws/{law_id}/review",
        json={"confirmed": True, "valid_from": "2021-01-01"},
    )
    assert response.status_code == 200, response.text
    return client.get(f"/api/laws/{law_id}").json()["articles"][0]["id"]


def _stub_embed(texts, **kwargs):
    return [[1.0, 0.0] for _ in texts]


def _counts(article_id):
    state = indexing.status()
    with db.connect() as connection:
        row = connection.execute(
            "SELECT status FROM embedding_states WHERE article_id=?",
            (article_id,),
        ).fetchone()
        chunks = connection.execute(
            "SELECT COUNT(*) FROM embedding_chunks WHERE article_id=?",
            (article_id,),
        ).fetchone()[0]
    return state, row["status"] if row else None, chunks


def test_cancel_winning_before_publication_keeps_rows_and_job_interrupted(
    client, monkeypatch
):
    article_id = _add_article(client)
    _configure()
    monkeypatch.setattr(rs, "embed", _stub_embed)
    indexing._stop.clear()
    entered_fingerprint = threading.Event()
    release_fingerprint = threading.Event()
    original_fingerprint = rs.fingerprint
    calls = 0

    def paused_fingerprint(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered_fingerprint.set()
            assert release_fingerprint.wait(3)
        return original_fingerprint(*args, **kwargs)

    monkeypatch.setattr(rs, "fingerprint", paused_fingerprint)
    errors = []

    def check_cancelled():
        if indexing._stop.is_set():
            raise InterruptedError("索引已停止")

    def build():
        try:
            indexing.build(check=check_cancelled)
        except InterruptedError as exc:
            errors.append(exc)

    worker = threading.Thread(target=build)
    worker.start()
    try:
        assert entered_fingerprint.wait(3)
        indexing.cancel()
    finally:
        release_fingerprint.set()
        worker.join(3)
        indexing._stop.clear()

    assert not worker.is_alive()
    state, row_status, chunk_count = _counts(article_id)
    assert state["job"]["status"] == "interrupted"
    assert state["ready"] == 0
    assert row_status is None
    assert chunk_count == 0
    assert errors


def test_completion_wins_finalization_race_without_terminal_overwrite(
    client, monkeypatch
):
    article_id = _add_article(client)
    _configure()
    monkeypatch.setattr(rs, "embed", _stub_embed)
    indexing._stop.clear()
    entered_finalizer = threading.Event()
    release_finalizer = threading.Event()
    cancel_finished = threading.Event()
    cancel_waiting_for_state = threading.Event()
    original_write = indexing._write_job
    original_guard = indexing._state_guard

    class ObservedGuard:
        def __enter__(self):
            if threading.current_thread().name == "cancel-finalizer-race":
                cancel_waiting_for_state.set()
            original_guard.acquire()

        def __exit__(self, exc_type, exc_value, traceback):
            original_guard.release()

    def paused_write(connection, value):
        if value.get("status") == "completed":
            entered_finalizer.set()
            assert release_finalizer.wait(3)
        return original_write(connection, value)

    monkeypatch.setattr(indexing, "_write_job", paused_write)
    monkeypatch.setattr(indexing, "_state_guard", ObservedGuard())
    worker = threading.Thread(target=indexing.build)
    worker.start()
    try:
        assert entered_finalizer.wait(3)
        cancel_thread = threading.Thread(
            target=lambda: (indexing.cancel(), cancel_finished.set()),
            name="cancel-finalizer-race",
        )
        cancel_thread.start()
        assert cancel_waiting_for_state.wait(3)
        assert not cancel_finished.is_set()
        release_finalizer.set()
        worker.join(3)
        cancel_thread.join(3)
    finally:
        release_finalizer.set()
        indexing._stop.clear()

    assert not worker.is_alive()
    assert not cancel_thread.is_alive()
    state, row_status, chunk_count = _counts(article_id)
    assert state["job"]["status"] == "completed"
    assert state["ready"] == 1
    assert row_status == "ready"
    assert chunk_count > 0
