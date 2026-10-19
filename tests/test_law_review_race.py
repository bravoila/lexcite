import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException

from app import db, main


def test_review_does_not_succeed_if_law_is_deleted_after_precheck(client, monkeypatch):
    monkeypatch.setattr(main.indexing, "start", lambda: None)
    law = json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())
    response = client.post("/api/laws", json=law)
    assert response.status_code == 200, response.text
    law_id = response.json()["id"]

    checked = Event()
    continue_review = Event()
    original_law_detail = main.law_detail

    def pause_after_precheck(lid):
        result = original_law_detail(lid)
        checked.set()
        assert continue_review.wait(timeout=5)
        return result

    monkeypatch.setattr(main, "law_detail", pause_after_precheck)
    value = main.ReviewInput(confirmed=True, valid_from="2021-01-01")

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(main.review, law_id, value)
        try:
            assert checked.wait(timeout=5), "review did not complete its existence check"
            with db.connect() as connection:
                connection.execute("DELETE FROM laws WHERE id=?", (law_id,))
            continue_review.set()
            with pytest.raises(HTTPException) as exc_info:
                future.result(timeout=5)
            assert exc_info.value.status_code == 404
        finally:
            continue_review.set()
