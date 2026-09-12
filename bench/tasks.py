"""Бенч-задачи: одна и та же полезная нагрузка в Celery и RQ.

Результат каждой задачи (timestamps) пишется напрямую в Redis-список
`bench:results:<run_id>` — независимо от фреймворка, чтобы сборщик метрик
был один и тот же и не зависел от особенностей result backend.
Все контейнеры работают на одном хосте, поэтому time.time() сравним.
"""

import io
import json
import socket
import time
from datetime import date
from functools import lru_cache

from redis import Redis

from app.core.config import get_settings
from app.reports.aggregate import aggregate_sales
from app.reports.data import generate_sales
from app.tasks.celery_app import celery

KINDS = ("noop", "cpu_small", "io_sleep")
IO_SLEEP_SEC = 0.05
CPU_ROWS = 10_000


def results_key(run_id: str) -> str:
    return f"bench:results:{run_id}"


@lru_cache
def _redis() -> Redis:
    return Redis.from_url(get_settings().redis_url)


def work(kind: str) -> None:
    """Полезная нагрузка. noop — измеряет чистые накладные расходы очереди."""
    if kind == "noop":
        return
    if kind == "io_sleep":
        time.sleep(IO_SLEEP_SEC)
        return
    if kind == "cpu_small":
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        df = generate_sales(CPU_ROWS, date(2025, 1, 1), date(2025, 12, 31), seed=1)
        summary = aggregate_sales(df, "region")
        fig, ax = plt.subplots(figsize=(6, 3))
        ax.bar(summary["region"], summary["amount"])
        fig.savefig(io.BytesIO(), format="png", dpi=72)  # рендер без записи на диск
        plt.close(fig)
        return
    raise ValueError(f"unknown kind {kind!r}")


def execute(kind: str, run_id: str, enqueued_at: float) -> dict:
    """Общее тело задачи: замер, работа, запись результата."""
    started_at = time.time()
    work(kind)
    finished_at = time.time()
    payload = {
        "enqueued_at": enqueued_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "worker": socket.gethostname(),
    }
    _redis().rpush(results_key(run_id), json.dumps(payload))
    return payload


# --- Celery ---


@celery.task(name="bench.task")
def celery_bench_task(kind: str, run_id: str, enqueued_at: float) -> dict:
    return execute(kind, run_id, enqueued_at)


# --- RQ (обычная функция, импортируется воркером по пути bench.tasks.rq_bench_job) ---


def rq_bench_job(kind: str, run_id: str, enqueued_at: float) -> dict:
    return execute(kind, run_id, enqueued_at)
