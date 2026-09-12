"""Интеграционные тесты API против запущенного `docker compose up`.

    API_URL=http://localhost:8000 pytest tests/integration

Пропускаются, если API недоступен. Проходят с любым TASK_BACKEND — это и есть
проверка, что абстракция TaskBackend даёт одинаковое поведение.
"""

import os
import time

import httpx
import pytest

API_URL = os.environ.get("API_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def client():
    c = httpx.Client(base_url=API_URL, timeout=10.0)
    try:
        health = c.get("/health").json()
    except httpx.HTTPError:
        pytest.skip(f"API недоступен по {API_URL}")
    if health["status"] != "ok":
        pytest.skip(f"брокер недоступен: {health}")
    c.backend = health["backend"]
    yield c
    c.close()


def wait_success(client, report_id: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    seen_states = set()
    while time.monotonic() < deadline:
        status = client.get(f"/reports/{report_id}").json()
        seen_states.add(status["state"])
        if status["state"] == "SUCCESS":
            status["_seen"] = seen_states
            return status
        assert status["state"] != "FAILURE", status
        time.sleep(0.3)
    pytest.fail(f"{report_id} не завершился за {timeout}s, состояния: {seen_states}")


def test_health_reports_backend(client):
    health = client.get("/health").json()
    assert health["backend"] in ("celery", "rq")
    assert "@" not in health["broker"]  # без учётных данных


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "report-queue" in r.text


def test_full_report_lifecycle(client):
    r = client.post("/reports", json={"rows": 5000, "group_by": "category", "title": "Интеграция"})
    assert r.status_code == 202
    body = r.json()
    report_id = body["id"]
    assert body["status_url"] == f"/reports/{report_id}"
    assert body["backend"] == client.backend

    status = wait_success(client, report_id)
    assert status["progress"] == 100
    assert set(status["artifacts"]) == {"csv", "png", "pdf"}
    assert set(status["links"]) == {"csv", "png", "pdf"}

    for fmt, expected in (("csv", b"orders"), ("png", b"\x89PNG"), ("pdf", b"%PDF")):
        d = client.get(f"/reports/{report_id}/download", params={"format": fmt})
        assert d.status_code == 200, d.text
        assert expected in d.content[:200]
        assert f"{report_id}.{fmt}" in d.headers["content-disposition"]


def test_default_params_accepted(client):
    r = client.post("/reports")
    assert r.status_code == 202
    wait_success(client, r.json()["id"])


def test_invalid_params_rejected(client):
    r = client.post("/reports", json={"rows": 1})
    assert r.status_code == 422


def test_unknown_report(client):
    r = client.get("/reports/does-not-exist")
    if client.backend == "rq":
        assert r.status_code == 404  # RQ знает, что job нет
    else:
        assert r.status_code == 200 and r.json()["state"] == "PENDING"  # Celery: PENDING == неизвестно


def test_download_before_ready_is_409_or_404(client):
    r = client.get("/reports/does-not-exist/download", params={"format": "csv"})
    assert r.status_code in (404, 409)


def test_parallel_batch_completes(client):
    ids = [client.post("/reports", json={"rows": 3000, "seed": i}).json()["id"] for i in range(6)]
    for report_id in ids:
        wait_success(client, report_id)
