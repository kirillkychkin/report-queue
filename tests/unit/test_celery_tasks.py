"""Задачи Celery в eager-режиме: без брокера и воркера, result backend — в памяти.

Проверяем адаптеры (сериализация, прогресс, chord-callback), а не сам Celery.
"""

import pytest

from app.core.models import ReportParams
from app.tasks import celery_tasks as t
from app.tasks.celery_app import celery


@pytest.fixture(autouse=True)
def eager_celery():
    """Выполнять задачи синхронно в том же процессе, результаты хранить в памяти."""
    celery.conf.update(task_always_eager=True, task_store_eager_result=True, result_backend="cache+memory://")
    # backend кэшируется в app — сбросить, чтобы подхватился memory
    celery._local.__dict__.pop("backend", None)
    yield
    celery.conf.update(task_always_eager=False, task_store_eager_result=False)
    celery._local.__dict__.pop("backend", None)


def test_generate_report_task_returns_serializable_result():
    result = t.generate_report_task.apply(args=["t-1", ReportParams(rows=300).model_dump(mode="json")])
    assert result.successful()
    payload = result.get()
    assert payload["report_id"] == "t-1"
    assert payload["rows"] == 300
    assert set(payload["artifacts"]) == {"csv", "png", "pdf"}


def test_summarize_batch_task_aggregates_group_results():
    results = [
        {"report_id": "a", "rows": 100, "total_amount": 10.5, "duration_sec": 0.2},
        {"report_id": "b", "rows": 200, "total_amount": 20.0, "duration_sec": 0.5},
    ]
    summary = t.summarize_batch_task.apply(args=[results]).get()
    assert summary == {
        "reports": ["a", "b"],
        "count": 2,
        "total_rows": 300,
        "total_amount": 30.5,
        "max_duration_sec": 0.5,
    }


def test_notify_task_succeeds_when_channel_is_reliable(monkeypatch):
    monkeypatch.setattr(t, "get_settings", lambda: type("S", (), {"notify_failure_rate": 0.0})())
    result = t.notify_task.apply(args=[{"report_id": "x"}], kwargs={"channel": "sms"}).get()
    assert result == {"report_id": "x", "channel": "sms", "retries": 0}


def test_notify_task_raises_after_exhausting_retries(monkeypatch):
    monkeypatch.setattr(t, "get_settings", lambda: type("S", (), {"notify_failure_rate": 1.0})())
    result = t.notify_task.apply(args=[{"report_id": "x"}])
    assert result.failed()
    assert isinstance(result.result, t.NotificationError)


def test_cleanup_removes_only_old_dirs(artifacts_tmp):
    import os
    import time

    old = artifacts_tmp / "old"
    fresh = artifacts_tmp / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    stale = time.time() - 10 * 3600
    os.utime(old, (stale, stale))

    assert t.cleanup_artifacts_task.apply(kwargs={"max_age_hours": 6}).get() == {"removed": 1}
    assert not old.exists() and fresh.exists()


def test_task_routing_separates_queues():
    routes = celery.conf.task_routes
    assert routes["app.tasks.celery_tasks.generate_report_task"]["queue"] == "reports"
    assert routes["app.tasks.celery_tasks.notify_task"]["queue"] == "notifications"
    assert celery.conf.task_acks_late is True
    assert celery.conf.worker_prefetch_multiplier == 1
