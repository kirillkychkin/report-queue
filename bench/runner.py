"""Один прогон бенчмарка: дождаться воркеров → поставить N задач → дождаться результатов → метрики.

Запускается ВНУТРИ контейнера (сеть Docker, как у реального продьюсера):
    docker compose exec -T worker-celery python -m bench.runner --backend celery --kind noop --n 1000 --workers 1
    docker compose exec -T worker-rq     python -m bench.runner --backend rq     --kind noop --n 1000 --workers 1

Печатает одну строку `BENCH_RESULT {json}` — её парсит bench/run_matrix.py на хосте.
"""

import argparse
import json
import statistics
import sys
import time
import uuid

from bench.tasks import KINDS, celery_bench_task, results_key, rq_bench_job


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
        from app.tasks.rq_app import QUEUE_REPORTS, get_queue

        get_queue(QUEUE_REPORTS).empty()


def enqueue_all(backend: str, kind: str, n: int, run_id: str) -> float:
    """Поставить N задач подряд; вернуть длительность постановки (с)."""
    started = time.perf_counter()
    if backend == "celery":
        for _ in range(n):
            celery_bench_task.apply_async(args=[kind, run_id, time.time()], queue="reports")
    else:
        from app.tasks.rq_app import QUEUE_REPORTS, get_queue, get_redis

        queue = get_queue(QUEUE_REPORTS, get_redis())
        for _ in range(n):
            queue.enqueue(rq_bench_job, kind, run_id, time.time(), result_ttl=500, failure_ttl=500)
    return time.perf_counter() - started


def collect(run_id: str, n: int, timeout: float) -> list[dict]:
    from app.tasks.rq_app import get_redis

    redis = get_redis()
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


def percentile(values: list[float], pct: float) -> float:
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round(pct / 100 * (len(values) - 1))))
    return values[idx]


def metrics(rows: list[dict], enqueue_sec: float, first_enqueue: float) -> dict:
    latency_ms = [(r["finished_at"] - r["enqueued_at"]) * 1000 for r in rows]
    wait_ms = [(r["started_at"] - r["enqueued_at"]) * 1000 for r in rows]
    total_sec = max(r["finished_at"] for r in rows) - first_enqueue
    return {
        "enqueue_sec": round(enqueue_sec, 3),
        "enqueue_rate": round(len(rows) / enqueue_sec, 1),
        "total_sec": round(total_sec, 3),
        "throughput": round(len(rows) / total_sec, 1),
        "latency_p50_ms": round(statistics.median(latency_ms), 1),
        "latency_p95_ms": round(percentile(latency_ms, 95), 1),
        "latency_p99_ms": round(percentile(latency_ms, 99), 1),
        "wait_p50_ms": round(statistics.median(wait_ms), 1),
        "workers_seen": len({r["worker"] for r in rows}),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["celery", "rq"], required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--workers", type=int, required=True, help="ожидаемое число воркеров (для проверки готовности)")
    parser.add_argument("--broker", default="", help="метка брокера для отчёта (redis|rabbitmq)")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:8]
    wait_workers(args.backend, args.workers)
    purge(args.backend)
    time.sleep(1.0)

    first_enqueue = time.time()
    enqueue_sec = enqueue_all(args.backend, args.kind, args.n, run_id)
    rows = collect(run_id, args.n, args.timeout)

    result = {
        "backend": args.backend,
        "broker": args.broker or ("redis" if args.backend == "rq" else "?"),
        "kind": args.kind,
        "n": args.n,
        "workers": args.workers,
        **metrics(rows, enqueue_sec, first_enqueue),
    }
    print("BENCH_RESULT " + json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except TimeoutError as exc:
        print(f"BENCH_ERROR {exc}", file=sys.stderr)
        sys.exit(2)
