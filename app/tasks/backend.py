"""Единый интерфейс к фоновым задачам для API: Celery и RQ за одним протоколом.

Почему: API-слой не должен знать, какой фреймворк очередей под ним. Это даёт
(1) честное сравнение на одном коде, (2) возможность заменить фреймворк без
переписывания API. Цена: из API недоступны уникальные фичи (canvas Celery,
depends_on RQ) — они живут в celery_tasks / rq_app и используются точечно.
"""

from typing import Protocol

from app.core.config import get_settings
from app.core.models import ReportParams, ReportState, ReportStatus


class UnknownReport(LookupError):
    """Бэкенд точно знает, что такого отчёта нет (RQ). Celery этого сказать не может."""


class TaskBackend(Protocol):
    name: str

    def enqueue_report(self, report_id: str, params: ReportParams) -> str:
        """Поставить отчёт в очередь, вернуть id задачи (== report_id)."""

    def get_status(self, report_id: str) -> ReportStatus: ...

    def ping(self) -> bool:
        """Доступен ли брокер (для /health)."""


# --- Celery ---


class CeleryBackend:
    name = "celery"

    def __init__(self) -> None:
        from app.tasks import celery_tasks  # импорт лениво: не тянуть Celery в RQ-режиме
        from app.tasks.celery_app import celery

        self._tasks = celery_tasks
        self._app = celery

    def enqueue_report(self, report_id: str, params: ReportParams) -> str:
        return self._tasks.enqueue_report(report_id, params).id

    def get_status(self, report_id: str) -> ReportStatus:
        from celery.result import AsyncResult

        result = AsyncResult(report_id, app=self._app)
        return status_from_celery(report_id, result.state, result.info)

    def ping(self) -> bool:
        try:
            with self._app.connection_for_write() as conn:
                conn.ensure_connection(max_retries=1)
            return True
        except Exception:  # noqa: BLE001 — любой сбой = недоступен
            return False


def status_from_celery(report_id: str, state: str, info) -> ReportStatus:
    """Нормализация состояния Celery. PENDING в Celery — «неизвестно», а не «в очереди»:
    result backend не хранит записи о задачах, которые ещё не начались."""
    if state == "SUCCESS" and isinstance(info, dict):
        return ReportStatus(id=report_id, state=ReportState.SUCCESS, progress=100, artifacts=info.get("artifacts", {}))
    if state == "FAILURE":
        return ReportStatus(id=report_id, state=ReportState.FAILURE, error=repr(info))
    if state == "PROGRESS" and isinstance(info, dict):
        return ReportStatus(id=report_id, state=ReportState.PROGRESS, progress=info.get("progress", 0), step=info.get("step"))
    if state in ("STARTED", "RETRY"):
        return ReportStatus(id=report_id, state=ReportState.STARTED)
    return ReportStatus(id=report_id, state=ReportState.PENDING)


# --- RQ ---


class RQBackend:
    name = "rq"

    def __init__(self) -> None:
        from app.tasks import rq_app

        self._rq = rq_app
        self._conn = rq_app.get_redis()

    def enqueue_report(self, report_id: str, params: ReportParams) -> str:
        return self._rq.enqueue_report(report_id, params, self._conn).id

    def get_status(self, report_id: str) -> ReportStatus:
        from rq.exceptions import NoSuchJobError
        from rq.job import Job

        try:
            job = Job.fetch(report_id, connection=self._conn)
        except NoSuchJobError as exc:
            raise UnknownReport(report_id) from exc
        return status_from_rq(report_id, job.get_status(refresh=False), job.meta, job.return_value(), job.exc_info)

    def ping(self) -> bool:
        try:
            return bool(self._conn.ping())
        except Exception:  # noqa: BLE001
            return False


def status_from_rq(report_id: str, status, meta: dict | None, return_value, exc_info: str | None) -> ReportStatus:
    """Нормализация состояния RQ. У RQ есть явный статус queued — «в очереди», в отличие от Celery."""
    status = str(getattr(status, "value", status))
    meta = meta or {}
    if status == "finished":
        artifacts = return_value.get("artifacts", {}) if isinstance(return_value, dict) else {}
        return ReportStatus(id=report_id, state=ReportState.SUCCESS, progress=100, artifacts=artifacts)
    if status in ("failed", "stopped", "canceled"):
        error = (exc_info or status).strip().splitlines()[-1]
        return ReportStatus(id=report_id, state=ReportState.FAILURE, error=error)
    if status == "started":
        if meta.get("progress"):
            return ReportStatus(id=report_id, state=ReportState.PROGRESS, progress=meta["progress"], step=meta.get("step"))
        return ReportStatus(id=report_id, state=ReportState.STARTED)
    return ReportStatus(id=report_id, state=ReportState.PENDING)  # queued / deferred / scheduled


# --- фабрика ---

_BACKENDS: dict[str, type] = {"celery": CeleryBackend, "rq": RQBackend}


def get_backend(name: str | None = None) -> TaskBackend:
    """Бэкенд по TASK_BACKEND (или явному имени). Экземпляр лёгкий — можно создавать на запрос."""
    name = name or get_settings().task_backend
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(f"unknown TASK_BACKEND={name!r}, expected one of {sorted(_BACKENDS)}") from None
