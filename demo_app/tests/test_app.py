from fastapi.testclient import TestClient

from demo_app.app import app

client = TestClient(app)


def test_lists_seed_notes() -> None:
    texts = [n["text"] for n in client.get("/api/notes").json()]
    assert "Buy milk and eggs" in texts


def test_add_note_rejects_empty_text() -> None:
    assert client.post("/api/notes", json={"text": ""}).status_code == 422


def test_index_serves_ui() -> None:
    assert "Notes board" in client.get("/").text
