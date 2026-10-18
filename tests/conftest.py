import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import db


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("LEXCITE_DATA", str(tmp_path))
    monkeypatch.setenv("LEXCITE_ADMIN_PASSWORD", "test-only-password-123")
    for key in (
        "MODEL_PROFILE", "MODEL_PROVIDER", "MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME",
        "MODEL_LOCAL_PROVIDER", "MODEL_LOCAL_BASE_URL", "MODEL_LOCAL_NAME", "MODEL_LOCAL_API_KEY",
        "MODEL_CLOUD_PROVIDER", "MODEL_CLOUD_BASE_URL", "MODEL_CLOUD_NAME", "MODEL_CLOUD_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MODEL_PROFILE", "local")
    monkeypatch.setenv("MODEL_LOCAL_PROVIDER", "none")
    with TestClient(app, headers={"X-Lexcite": "1"}) as c:
        assert c.post("/api/login", json={"password":"test-only-password-123"}).status_code == 200
        yield c


@pytest.fixture
def corpus(client):
    import json
    value = json.loads((db.ROOT / "examples/civil_excerpt.json").read_text())
    r = client.post("/api/laws", json=value)
    assert r.status_code == 200, r.text
    lid = r.json()["id"]
    assert client.post(f"/api/laws/{lid}/review", json={"confirmed":True, "valid_from":"2021-01-01"}).status_code == 200
    return client.get(f"/api/laws/{lid}").json()
