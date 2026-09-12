"""RQ-версия фоновых задач: те же доменные функции, другая модель очереди.

Запуск воркера:  python -m app.tasks.rq_app            (внутри Docker — RQ использует fork)
Очереди:         reports, notifications (как в Celery — для сравнимости)

Ключевые отличия от Celery, которые видны в этом файле:
- нет «приложения»: очередь = имя + соединение с Redis, задача = обычная функция;
- прогресс — через job.meta, а не состояние в result backend;
- нет canvas: пайплайн собирается через depends_on, результат зависимости читается вручную;
- Retry с интервалами требует планировщик в воркере (with_scheduler=True);
- брокер только Redis.
"""

import logging
import random
import sys

from redis import Redis
from rq import Queue, Retry, SimpleWorker, Worker, get_current_job
from rq.job import Job

from app.core.config import get_settings
from app.core.models import ReportParams
from app.reports.service import generate_report

log = logging.getLogger(__name__)

QUEUE_REPORTS = "reports"
QUEUE_NOTIFICATIONS = "notifications"

JOB_TIMEOUT = 180       # аналог task_time_limit
RESULT_TTL = 3600       # аналог result_expires
NOTIFY_RETRY = Retry(max=5, interval=[2, 4, 8, 16, 30])  # аналог retry_backoff (без jitter)


class NotificationError(RuntimeError):
    """Временный сбой канала уведомлений — имеет смысл повторить."""


# --- соединение и очереди ---


def get_redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def get_queue(name: str = QUEUE_REPORTS, connection: Redis | None = None) -> Queue:
    return Queue(name, connection=connection or get_redis())


# --- задачи (обычные функции; RQ импортирует их по строковому пути) ---


def generate_report_job(report_id: str, params: dict) -> dict:
    """Сгенерировать отчёт; прогресс — в job.meta (читает API через Job.fetch)."""
    job = get_current_job()

    def on_progress(pct: int, step: str) -> None:
        if job is not None:
            job.meta.update(progress=pct, step=step)
            job.save_meta()

    result = generate_report(report_id, ReportParams.model_validate(params), on_progress)
    return result.model_dump()


def notify_job(report_id: str, channel: str = "email") -> dict:
    """Уведомить о готовности. Результат отчёта берём из зависимости (depends_on), а не из аргументов."""
    job = get_current_job()
    attempt = 1
    report: dict | None = None
    if job is not None:
        retries_left = job.retries_left if job.retries_left is not None else NOTIFY_RETRY.max
        attempt = NOTIFY_RETRY.max - retries_left + 1
        if job.dependency is not None:
            report = job.dependency.return_value()

    if random.random() < get_settings().notify_failure_rate:
        log.warning("notify %s via %s failed (attempt %d)", report_id, channel, attempt)
        raise NotificationError(f"{channel} gateway unavailable")

    log.info("notify %s via %s: OK on attempt %d", report_id, channel, attempt)
    return {"report_id": report_id, "channel": channel, "attempt": attempt, "rows": report and report.get("rows")}


# --- постановка задач (аналог helpers в celery_tasks) ---


def enqueue_report(report_id: str, params: ReportParams, connection: Redis | None = None) -> Job:
    """Одиночный отчёт. job_id = report_id — API находит задачу по своему идентификатору."""
    return get_queue(QUEUE_REPORTS, connection).enqueue(
        generate_report_job,
        report_id,
        params.model_dump(mode="json"),
        job_id=report_id,
        job_timeout=JOB_TIMEOUT,
        result_ttl=RESULT_TTL,
        meta={"progress": 0, "step": None},
    )


def enqueue_report_with_notification(
    report_id: str, params: ReportParams, channel: str = "email", connection: Redis | None = None
) -> Job:
    """Пайплайн через depends_on: notify стартует только после успешного отчёта."""
    connection = connection or get_redis()
    report_job = enqueue_report(report_id, params, connection)
    return get_queue(QUEUE_NOTIFICATIONS, connection).enqueue(
        notify_job,
        report_id,
        channel,
        depends_on=report_job,
        retry=NOTIFY_RETRY,
        result_ttl=RESULT_TTL,
    )


# --- воркер ---


def run_worker(queues: list[str] | None = None) -> None:
    """Воркер слушает обе очереди; планировщик нужен для отложенных повторов Retry.

    Worker (по умолчанию) форкает процесс на каждую задачу: утечки памяти и падения
    изолированы, но fork стоит десятки миллисекунд. SimpleWorker выполняет задачи
    в своём процессе — аналог Celery с --pool=solo. Выбор — RQ_WORKER_CLASS.
    """
    connection = get_redis()
    names = queues or [QUEUE_REPORTS, QUEUE_NOTIFICATIONS]
    worker_cls = SimpleWorker if get_settings().rq_worker_class == "simple" else Worker
    log.info("starting %s on queues %s", worker_cls.__name__, names)
    worker = worker_cls([Queue(n, connection=connection) for n in names], connection=connection)
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run_worker(sys.argv[1:] or None)
