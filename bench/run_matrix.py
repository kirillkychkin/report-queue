"""Оркестратор эксперимента (запускается на ХОСТЕ, управляет docker compose).

    .venv/Scripts/python -m bench.run_matrix              # полная матрица (~1.5–2 ч)
    .venv/Scripts/python -m bench.run_matrix --quick      # 1 повтор, N=300, только 1 и 2 воркера

Для каждой конфигурации: переключить брокер/класс воркера (переменные окружения
для compose), отмасштабировать воркеры, запустить bench.runner в контейнере продьюсера,
записать строку в results/raw.csv. В конце — results/summary.csv (медианы по повторам).

Методика v2 (после аудита v1, см. docs/experiment.md):
- метрики и result backend — на отдельном Redis, брокер не делит сервер с хранилищем;
- события Flower выключены (`CELERY_EVENTS=false`) — иначе Redis-конфигурация получает лишнюю
  работу, которой нет у RabbitMQ-конфигурации;
- добавлена конфигурация `celery-rabbitmq-confirm` — RabbitMQ при равных с Redis гарантиях публикации;
- продьюсер живёт в отдельном контейнере (`api`), а не внутри воркера;
- beat и flower на время матрицы гасятся;
- сам прогон разделён на фазы (см. bench/runner.py): постановка в остановленную очередь,
  разбор заведомо полной очереди, отдельный замер задержки на пустой очереди.
"""

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

RESULTS = Path(__file__).parent / "results"
RAW = RESULTS / "raw.csv"
SUMMARY = RESULTS / "summary.csv"

# Конфигурация → (сервис воркера, переменные окружения для compose, аргументы runner).
# CELERY_EVENTS=false во всех конфигурациях Celery: события Flower — это лишнее сообщение в брокер
# на каждый переход задачи, и в v1 они нагружали Redis-конфигурацию сильнее, чем RabbitMQ-конфигурацию.
CELERY_ENV = {"CELERY_EVENTS": "false"}
CONFIGS = {
    "celery-redis": ("worker-celery", {**CELERY_ENV, "BROKER_URL": "redis://redis:6379/0"}, ["--backend", "celery", "--broker", "redis"]),
    "celery-rabbitmq": ("worker-celery", {**CELERY_ENV, "BROKER_URL": "amqp://guest:guest@rabbitmq:5672//"}, ["--backend", "celery", "--broker", "rabbitmq"]),
    # Те же гарантии на публикации, что у Redis (команда подтверждена сервером): publisher confirms
    "celery-rabbitmq-confirm": ("worker-celery", {**CELERY_ENV, "BROKER_URL": "amqp://guest:guest@rabbitmq:5672//", "BROKER_CONFIRM_PUBLISH": "true"}, ["--backend", "celery", "--broker", "rabbitmq-confirm"]),
    "rq-redis": ("worker-rq", {"RQ_WORKER_CLASS": "fork"}, ["--backend", "rq", "--broker", "redis"]),
    "rq-simple-redis": ("worker-rq", {"RQ_WORKER_CLASS": "simple"}, ["--backend", "rq", "--broker", "redis-simple"]),
}
# Вид задачи → размеры N. noop берётся большим: очередь наполняется заранее, и на 5000 задач
# разбор длится секунды, а не доли секунды (меньше влияние ramp-up воркеров).
KINDS = {"noop": [5000], "cpu_small": [1000], "io_sleep": [1000]}
WORKERS = [1, 2, 4]
# RQ с fork тратит ~70 мс на задачу, поэтому ему даются меньшие N: пропускная способность и задержка —
# это скорости, от N они не зависят, а время прогона матрицы иначе вырастает в разы.
SIZES_OVERRIDE = {"rq-redis": {"noop": [1000], "cpu_small": [500], "io_sleep": [500]}}


def sizes(config: str, kind: str) -> list[int]:
    return SIZES_OVERRIDE.get(config, {}).get(kind, KINDS[kind])
# Продьюсер работает не в контейнере воркера, а отдельно (как настоящий API):
# иначе постановка конкурирует с исполнением за CPU.
PRODUCER_SERVICE = "api"
# На время матрицы гасятся: beat (каждые 10 минут публикует cleanup в ту же очередь reports)
# и flower (лишний потребитель событий и соединение с брокером).
IDLE_SERVICES = ("beat", "flower")
FIELDS = [
    "config", "backend", "broker", "kind", "n", "workers", "repeat",
    "enqueue_sec", "enqueue_rate", "drain_sec", "throughput",
    "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "wait_p50_ms", "workers_seen",
]
ALL_WORKER_SERVICES = ("worker-celery", "worker-rq")


def compose(*args: str, env: dict | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        ["docker", "compose", *args], env=full_env, check=True,
        capture_output=capture, text=True, encoding="utf-8", errors="replace",
    )


def cleanup_rq_registry() -> None:
    """Убрать из Redis записи RQ-воркеров, убитых без graceful shutdown (иначе они «живут» 420 с)."""
    compose("exec", "-T", "redis", "sh", "-c",
            "redis-cli -n 1 --scan --pattern 'rq:worker*' | xargs -r redis-cli -n 1 del >/dev/null; "
            "redis-cli -n 1 del rq:workers >/dev/null", capture=True)


def scale(service: str, count: int, env: dict) -> None:
    """Нужный сервис — в count реплик (с пересозданием при смене env), остальные воркеры — в 0."""
    for other in ALL_WORKER_SERVICES:
        if other != service:
            compose("up", "-d", "--no-deps", "--scale", f"{other}=0", other, env=env)
    compose("up", "-d", "--no-deps", "--scale", f"{service}=0", service, env=env)  # чистый рестарт
    cleanup_rq_registry()  # все RQ-контейнеры остановлены — любая запись в реестре устарела
    compose("up", "-d", "--no-deps", "--scale", f"{service}={count}", service, env=env)
    # Продьюсер должен работать с тем же брокером, что и воркеры, — пересоздаём при смене env
    compose("up", "-d", "--no-deps", PRODUCER_SERVICE, env=env)


def quiet_background(env: dict) -> None:
    """Погасить beat и flower: они шлют задачи и держат соединения с брокером во время замеров."""
    for service in IDLE_SERVICES:
        compose("up", "-d", "--no-deps", "--scale", f"{service}=0", service, env=env)


def restore_background() -> None:
    for service in IDLE_SERVICES:
        compose("up", "-d", "--no-deps", "--scale", f"{service}=1", service, env={})


def run_once(config: str, kind: str, n: int, workers: int, repeat: int) -> dict:
    _service, env, runner_args = CONFIGS[config]
    cmd = ["exec", "-T", PRODUCER_SERVICE, "python", "-m", "bench.runner", *runner_args,
           "--kind", kind, "--n", str(n), "--workers", str(workers)]
    proc = compose(*cmd, env=env, capture=True)
    for line in proc.stdout.splitlines():
        if line.startswith("BENCH_RESULT "):
            data = json.loads(line[len("BENCH_RESULT "):])
            return {"config": config, "repeat": repeat, **data}
    raise RuntimeError(f"no BENCH_RESULT in output:\n{proc.stdout}\n{proc.stderr}")


def append_raw(row: dict) -> None:
    new = not RAW.exists()
    with RAW.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in FIELDS})


def summarize() -> None:
    """Медианы по повторам для каждой ячейки матрицы."""
    with RAW.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["config"], r["kind"], r["n"], r["workers"]), []).append(r)
    metric_fields = FIELDS[FIELDS.index("enqueue_sec"):]
    with SUMMARY.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["config", "backend", "broker", "kind", "n", "workers", "repeats", *metric_fields])
        for (config, kind, n, workers), items in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], int(kv[0][2]), int(kv[0][3]))):
            medians = [round(statistics.median(float(i[m]) for i in items), 1) for m in metric_fields]
            writer.writerow([config, items[0]["backend"], items[0]["broker"], kind, n, workers, len(items), *medians])
    print(f"summary -> {SUMMARY} ({len(groups)} cells)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configs", nargs="*", default=list(CONFIGS), choices=list(CONFIGS))
    parser.add_argument("--kinds", nargs="*", default=list(KINDS), choices=list(KINDS))
    parser.add_argument("--workers", nargs="*", type=int, default=WORKERS)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--quick", action="store_true", help="дымовой прогон: N=300, 1 повтор, воркеров 1 и 2")
    parser.add_argument("--fresh", action="store_true", help="удалить results/raw.csv перед запуском")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()

    RESULTS.mkdir(exist_ok=True)
    if args.summary_only:
        summarize()
        return
    if args.fresh and RAW.exists():
        RAW.unlink()

    kinds = list(args.kinds)
    workers = [1, 2] if args.quick else args.workers
    repeats = 1 if args.quick else args.repeats

    plan = [
        (c, k, n, w, r)
        for c in args.configs
        for k in kinds
        for n in ([300] if args.quick else sizes(c, k))
        for w in workers
        for r in range(1, repeats + 1)
    ]
    print(f"{len(plan)} runs; results -> {RAW}")
    started = time.monotonic()
    quiet_background(CONFIGS[args.configs[0]][1])
    current = None
    for i, (config, kind, n, w, r) in enumerate(plan, 1):
        service, env, _ = CONFIGS[config]
        if current != (config, w):
            scale(service, w, env)
            current = (config, w)
        try:
            row = run_once(config, kind, n, w, r)
        except (subprocess.CalledProcessError, RuntimeError) as exc:
            print(f"[{i}/{len(plan)}] {config} {kind} n={n} w={w} #{r}: FAILED {exc}", file=sys.stderr)
            # Воркер мог остаться отписанным от очереди (упавший прогон между cancel/add consumer) —
            # поднимаем заново, иначе посыплются все следующие прогоны.
            scale(service, w, env)
            continue
        append_raw(row)
        print(f"[{i}/{len(plan)}] {config:16} {kind:9} n={n:<5} w={w} #{r}: "
              f"thr={row['throughput']:>7} t/s  enq={row['enqueue_rate']:>7} msg/s  "
              f"p50={row['latency_p50_ms']:>7} ms  drain={row['drain_sec']:>7}s  "
              f"({(time.monotonic() - started) / 60:.1f} min)", flush=True)

    # вернуть стек в обычное состояние: по одному воркеру каждого типа, beat и flower обратно
    scale("worker-celery", 1, {})
    compose("up", "-d", "--no-deps", "--scale", "worker-rq=1", "worker-rq")
    restore_background()
    summarize()


if __name__ == "__main__":
    main()
