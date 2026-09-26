"""PERF-07: el pinger externo que mantiene Render despierto solo puede hacer HEAD."""


def test_root_answers_head_and_get(client):
    assert client.head("/").status_code == 200
    assert client.get("/").json() == {"status": "ok"}
