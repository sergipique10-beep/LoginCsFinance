"""PERF-07: el pinger externo que mantiene Render despierto solo puede hacer HEAD.
AOS-02: la raiz expone el commit desplegado para que el smoke espere al deploy nuevo."""


def test_root_answers_head_and_get(client):
    assert client.head("/").status_code == 200
    body = client.get("/").json()
    assert body["status"] == "ok"
    assert "commit" in body


def test_root_exposes_deployed_commit(client, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    assert client.get("/").json()["commit"] == "abc123"


def test_root_commit_is_null_outside_render(client, monkeypatch):
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    assert client.get("/").json()["commit"] is None
