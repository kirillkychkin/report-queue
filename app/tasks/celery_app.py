"""Приложение Celery: брокер, result backend, политика доставки, роутинг, beat.

Запуск воркера:  celery -A app.tasks.celery_app worker -Q reports,notifications --concurrency=1
Запуск beat:     celery -A app.tasks.celery_app beat
Мониторинг:      celery -A app.tasks.celery_app flower

Брокер задаётся BROKER_URL (redis:// или amqp://) — код задач от него не зависит,
это и позволяет сравнить Redis и RabbitMQ на одном приложении.
"""

from celery import Celery
from kombu import Exchange, Queue

from app.core.config import get_settings

settings = get_settings()

celery = Celery(
    "report_queue",
    broker=settings.broker_url,
    backend=settings.redis_url,
    include=["app.tasks.celery_tasks"],
)

celery.conf.update(
    # --- Сериализация: только JSON. pickle небезопасен и привязывает к версии кода ---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Yakutsk",
    enable_utc=True,
    # --- Надёжность: at-least-once ---
    # ack отправляется ПОСЛЕ выполнения: если воркер упал посреди задачи, брокер отдаст её другому.
    # Цена — возможный повтор, поэтому задачи должны быть идемпотентными (см. reports.service).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Воркер берёт по одной задаче: длинные CPU-задачи не «захватывают» очередь про запас,
    # и нагрузка честно распределяется между воркерами (важно для масштабирования --scale).
    worker_prefetch_multiplier=1,
    # --- Наблюдаемость ---
    task_track_started=True,   # состояние STARTED, а не только PENDING/SUCCESS
    task_send_sent_event=True,  # события для Flower
    worker_send_task_events=True,
    result_extended=True,
    result_expires=3600,        # результаты в Redis живут час — брокер/бэкенд не хранилище
    # --- Ограничения времени: зависшая задача не должна блокировать воркер вечно ---
    task_soft_time_limit=120,
    task_time_limit=180,
    # --- Роутинг: отдельные очереди под разную нагрузку ---
    # CPU-тяжёлые отчёты не задерживают быстрые уведомления; воркеры можно
    # масштабировать независимо (-Q reports vs -Q notifications).
    task_queues=(
        Queue("reports", Exchange("reports"), routing_key="reports"),
        Queue("notifications", Exchange("notifications"), routing_key="notifications"),
    ),
    task_default_queue="reports",
    task_routes={
        "app.tasks.celery_tasks.generate_report_task": {"queue": "reports"},
        "app.tasks.celery_tasks.summarize_batch_task": {"queue": "reports"},
        "app.tasks.celery_tasks.cleanup_artifacts_task": {"queue": "reports"},
        "app.tasks.celery_tasks.notify_task": {"queue": "notifications"},
    },
    # --- Периодические задачи (beat) ---
    beat_schedule={
        "cleanup-old-artifacts": {
            "task": "app.tasks.celery_tasks.cleanup_artifacts_task",
            "schedule": 600.0,  # каждые 10 минут
            "kwargs": {"max_age_hours": 6},
        },
    },
    broker_connection_retry_on_startup=True,
)
