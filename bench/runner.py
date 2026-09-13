"""Один прогон бенчмарка. Методика v2 — разделены три разные величины.

Почему не «поставить N задач и засечь время»: при живых воркерах продьюсер и воркеры
конкурируют, а на коротких задачах очередь всё время пуста — тогда «пропускная способность»
измеряет скорость продьюсера, а не воркеров (это и вскрыл аудит v1, см. docs/experiment.md).

Поэтому прогон состоит из трёх фаз:

1. **Постановка.** Потребление приостанавливается (Celery — `cancel_consumer`, RQ — задачи
   копятся в отдельном списке), N задач публикуются в пустую очередь → чистая цена публикации.
2. **Разбор очереди.** Потребление возобновляется; очередь заведомо полна, воркеры не простаивают.
   Пропускная способность считается по timestamp'ам самих задач: N / (последний finish − первый start).
3. **Задержка.** M задач по одной, с ожиданием каждой, на ПУСТОЙ очереди → настоящие накладные
   расходы на задачу (в v1 «задержка» была временем ожидания в очереди, то есть законом Литтла).

Запускается ВНУТРИ контейнера, который не является воркером (сеть Docker, как у реального API):
    docker compose exec -T api python -m bench.runner --backend celery --kind noop --n 1000 --workers 1

Печатает одну строку `BENCH_RESULT {json}` — её парсит bench/run_matrix.py на хосте.
"""

import argparse
import json
import statistics
import sys
import time
import uuid

from redis import Redis

from app.core.config import get_settings
from bench.tasks import KINDS, celery_bench_task, results_key, rq_bench_job

HOLD_QUEUE = "bench_hold"  # список RQ, куда копятся задачи, пока воркеры не должны их видеть
LATENCY_SAMPLES = 30


def backend_redis() -> Redis:
    """Redis с метриками (отдельный от брокера)."""
    return Redis.from_url(get_settings().result_backend)


def wait_workers(backend: str, expected: int, timeout: float = 90.0) -> int:
    """Ждём, пока число живых воркеров совпадёт с ожидаемым (после --scale)."""
    deadline = time.monotonic() + timeout
    count = -1
    while time.monotonic() < deadline:
        if backend == "celery":
            from app.tasks.celery_app import celery

            replies = celery.control.inspect(timeout=2.0).ping() or {}
            count = len(replies)
        else:
            from rq import Worker

            from app.tasks.rq_app import get_redis

            count = Worker.count(connection=get_redis())
        if count == expected:
            return count
        time.sleep(1.0)
    raise TimeoutError(f"expected {expected} {backend} workers, saw {count}")


def purge(backend: str) -> None:
    """Очистить очередь задач перед прогоном — хвосты прошлых прогонов исказят замер."""
    if backend == "celery":
        from app.tasks.celery_app import celery

        celery.control.purge()
    else:
        from app.tasks.rq_app import QUEUE_REPORTS, get_queue, get_redis

        get_queue(QUEUE_REPORTS).empty()
        get_queue(HOLD_QUEUE, get_redis()).empty()


def pause_consumption(backend: str, workers: int) -> None:
    """Остановить потребление, чтобы очередь наполнялась, а не разбиралась на лету.

    Celery: `cancel_consumer` — воркер отписывается от очереди (для RabbitMQ это basic_cancel).
    RQ: аналога нет, поэтому задачи копятся в отдельном списке `rq:queue:bench_hold`,
    который потом атомарно переименовывается в рабочую очередь (очередь RQ — обычный список Redis).
    """
    if backend != "celery":
        return
    from app.tasks.celery_app import celery

    celery.control.cancel_consumer("reports")
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        active = celery.control.inspect(timeout=2.0).active_queues() or {}
        if len(active) == workers and not any(
            q["name"] == "reports" for queues in active.values() for q in queues
        ):
            return
        time.sleep(0.5)
    raise TimeoutError("воркеры не отписались от очереди reports")


def resume_consumption(backend: str) -> None:
    """Вернуть потребление: подписать воркеров обратно / отдать накопленный список воркерам."""
    if backend == "celery":
        from app.tasks.celery_app import celery

        celery.control.add_consumer("reports")
    else:
        from app.tasks.rq_app import QUEUE_REPORTS, get_redis

        redis = get_redis()
        # RENAME атомарен и будит клиентов, заблокированных на BLPOP рабочей очереди
        redis.rename(f"rq:queue:{HOLD_QUEUE}", f"rq:queue:{QUEUE_REPORTS}")


def enqueue_all(backend: str, kind: str, n: int, run_id: str) -> float:
    """Поставить N задач подряд; вернуть длительность постановки (с)."""
    if backend == "celery":
        started = time.perf_counter()
        for _ in range(n):
            celery_bench_task.apply_async(args=[kind, run_id, time.time()], queue="reports")
        return time.perf_counter() - started

    from app.tasks.rq_app import get_queue, get_redis

    queue = get_queue(HOLD_QUEUE, get_redis())
    started = time.perf_counter()
    for _ in range(n):
        queue.enqueue(rq_bench_job, kind, run_id, time.time(), result_ttl=500, failure_ttl=500)
    return time.perf_counter() - started


def collect(run_id: str, n: int, timeout: float) -> list[dict]:
    redis = backend_redis()
    key = results_key(run_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if redis.llen(key) >= n:
            break
        time.sleep(0.1)
    else:
        raise TimeoutError(f"only {redis.llen(key)}/{n} results after {timeout}s")
    rows = [json.loads(x) for x in redis.lrange(key, 0, -1)]
    redis.delete(key)
    return rows


def measure_latency(backend: str, kind: str, samples: int, timeout: float = 60.0) -> list[dict]:
    """Задержка на пустой очереди: по одной задаче, с ожиданием каждой."""
    redis = backend_redis()
    rows: list[dict] = []
    for _ in range(samples):
        run_id = uuid.uuid4().hex[:8]
        key = results_key(run_id)
        redis.delete(key)
        enqueue_all(backend, kind, 1, run_id)
        if backend != "celery":
            resume_consumption(backend)  # отдать единственную задачу воркерам
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if redis.llen(key) >= 1:
                break
            time.sleep(0.002)
        else:
            raise TimeoutError("задача из замера задержки не выполнилась")
        rows.append(json.loads(redis.lrange(key, 0, -1)[0]))
        redis.delete(key)
    return rows


def percentile(values: list[float], pct: float) -> float:
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(pct / 100 * (len(values) - 1))))
    return values[idx]


def metrics(rows: list[dict], enqueue_sec: float, latency_rows: list[dict]) -> dict:
    """Пропускная — по timestamp'ам задач (очередь полна), задержка — из отдельной фазы."""
    drain_sec = max(r["finished_at"] for r in rows) - min(r["started_at"] for r in rows)
    latency_ms = [(r["finished_at"] - r["enqueued_at"]) * 1000 for r in latency_rows]
    wait_ms = [(r["started_at"] - r["enqueued_at"]) * 1000 for r in latency_rows]
    return {
        "enqueue_sec": round(enqueue_sec, 3),
        "enqueue_rate": round(len(rows) / enqueue_sec, 1),
        "drain_sec": round(drain_sec, 3),
        "throughput": round(len(rows) / drain_sec, 1),
        "latency_p50_ms": round(statistics.median(latency_ms), 2),
        "latency_p95_ms": round(percentile(latency_ms, 95), 2),
        "latency_p99_ms": round(percentile(latency_ms, 99), 2),
        "wait_p50_ms": round(statistics.median(wait_ms), 2),
        "workers_seen": len({r["worker"] for r in rows}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["celery", "rq"], required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True, help="ожидаемое число воркеров (для проверки готовности)")
    parser.add_argument("--broker", default="", help="метка брокера для отчёта")
    parser.add_argument("--latency-samples", type=int, default=LATENCY_SAMPLES)
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:8]
    wait_workers(args.backend, args.workers)
    purge(args.backend)

    # Фаза 1: наполнение остановленной очереди
    pause_consumption(args.backend, args.workers)
    try:
        time.sleep(0.5)
        enqueue_sec = enqueue_all(args.backend, args.kind, args.n, run_id)
    finally:
        # Воркеры обязаны вернуться к работе, даже если постановка упала,
        # иначе следующий прогон встанет на отписанных воркерах.
        resume_consumption(args.backend)

    # Фаза 2: разбор полной очереди
    rows = collect(run_id, args.n, args.timeout)

    # Фаза 3: задержка на пустой очереди
    latency_rows = measure_latency(args.backend, args.kind, args.latency_samples)

    result = {
        "backend": args.backend,
        "broker": args.broker or ("redis" if args.backend == "rq" else "?"),
        "kind": args.kind,
        "n": args.n,
        "workers": args.workers,
        **metrics(rows, enqueue_sec, latency_rows),
    }
    print("BENCH_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as exc:
        print(f"BENCH_ERROR {exc}", file=sys.stderr)
        sys.exit(2)
