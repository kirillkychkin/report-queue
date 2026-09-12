"""Задачи Celery: генерация отчёта, уведомление с retry, пакет через chord, очистка по beat.

Задачи — тонкие адаптеры над доменом (app.reports): сериализация аргументов,
прогресс через update_state, политика повторов. Бизнес-логики здесь нет —
она общая с RQ-версией.
"""

import logging
import random
import shutil
import time
from datetime import datetime, timedelta

from celery import chain, chord, group
from celery.result import AsyncResult

from app.core.config import get_settings
from app.core.models import ReportParams, ReportState
from app.core.storage import report_dir
from app.reports.service import generate_report
from app.tasks.celery_app import celery

log = logging.getLogger(__name__)


class NotificationError(RuntimeError):
    """Временный сбой канала уведомлений (сеть, 5xx) — имеет смысл повторить."""


@celery.task(bind=True, name="app.tasks.celery_tasks.generate_report_task")
def generate_report_task(self, report_id: str, params: dict) -> dict:
    """Сгенерировать отчёт. Прогресс публикуется в result backend (state=PROGRESS)."""

    def on_progress(pct: int, step: str) -> None:
        self.update_state(state=ReportState.PROGRESS, meta={"progress": pct, "step": step})

    result = generate_report(report_id, ReportParams.model_validate(params), on_progress)
    return result.model_dump()


@celery.task(
    bind=True,
    name="app.tasks.celery_tasks.notify_task",
    autoretry_for=(NotificationError,),
    retry_backoff=2,          # 2, 4, 8, 16... секунд
    retry_backoff_max=30,
    retry_jitter=True,        # разброс, чтобы повторы не приходили «стеной»
    max_retries=5,
    acks_late=True,
)
def notify_task(self, report: dict, channel: str = "email") -> dict:
    """Уведомить о готовности отчёта. Канал «ненадёжный»: падает с вероятностью NOTIFY_FAILURE_RATE.

    Первый аргумент — результат предыдущей задачи в chain (dict ReportResult).
    """
    failure_rate = get_settings().notify_failure_rate
    if random.random() < failure_rate:
        log.warning("notify %s via %s failed (attempt %d)", report["report_id"], channel, self.request.retries + 1)
        raise NotificationError(f"{channel} gateway unavailable")

    log.info("notify %s via %s: OK after %d retries", report["report_id"], channel, self.request.retries)
    return {"report_id": report["report_id"], "channel": channel, "retries": self.request.retries}


@celery.task(name="app.tasks.celery_tasks.summarize_batch_task")
def summarize_batch_task(results: list[dict]) -> dict:
    """Callback chord: сводка по пакету отчётов (результаты group приходят списком)."""
    return {
        "reports": [r["report_id"] for r in results],
        "count": len(results),
        "total_rows": sum(r["rows"] for r in results),
        "total_amount": round(sum(r["total_amount"] for r in results), 2),
        "max_duration_sec": max(r["duration_sec"] for r in results),
    }


@celery.task(name="app.tasks.celery_tasks.cleanup_artifacts_task")
def cleanup_artifacts_task(max_age_hours: int = 6) -> dict:
    """Периодическая (beat) очистка старых каталогов отчётов на общем volume."""
    base = get_settings().artifacts_dir
    if not base.exists():
        return {"removed": 0}
    threshold = datetime.now() - timedelta(hours=max_age_hours)
    removed = 0
    for path in base.iterdir():
        if path.is_dir() and datetime.fromtimestamp(path.stat().st_mtime) < threshold:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    log.info("cleanup: removed %d report dirs older than %dh", removed, max_age_hours)
    return {"removed": removed}


# --- Composition helpers (canvas): используются API и scripts/produce.py ---


def enqueue_report(report_id: str, params: ReportParams) -> AsyncResult:
    """Одиночный отчёт — базовый сценарий API."""
    return generate_report_task.apply_async(args=[report_id, params.model_dump(mode="json")], task_id=report_id)


def enqueue_report_with_notification(report_id: str, params: ReportParams, channel: str = "email") -> AsyncResult:
    """chain: отчёт → уведомление. Результат первой задачи передаётся во вторую первым аргументом."""
    workflow = chain(
        generate_report_task.s(report_id, params.model_dump(mode="json")).set(task_id=report_id),
        notify_task.s(channel=channel),
    )
    return workflow.apply_async()


def enqueue_batch(items: list[tuple[str, ReportParams]]) -> AsyncResult:
    """chord: group параллельных отчётов → summarize. Возвращает AsyncResult callback'а."""
    header = group(
        generate_report_task.s(report_id, params.model_dump(mode="json")).set(task_id=report_id)
        for report_id, params in items
    )
    return chord(header)(summarize_batch_task.s())


def wait_result(result: AsyncResult, timeout: float = 120.0, poll: float = 0.2):
    """Ожидание с печатью прогресса — для скриптов, не для API (API опрашивает статус)."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        state, info = result.state, result.info
        snapshot = (state, info.get("step") if isinstance(info, dict) else None)
        if snapshot != last:
            print(f"  [{result.id}] {state} {info if isinstance(info, dict) and state == 'PROGRESS' else ''}")
            last = snapshot
        if result.ready():
            return result.get(propagate=True)
        time.sleep(poll)
    raise TimeoutError(f"task {result.id} did not finish in {timeout}s")
