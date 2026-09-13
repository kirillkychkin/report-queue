"""Диагностика: почему Celery на Redis медленнее, чем Celery на RabbitMQ.

Матрица (`run_matrix.py`) меряет конфигурацию целиком. Этот скрипт разбирает её на части
и отвечает на вопрос «а Redis-то тут при чём»: отдельно цена round-trip к Redis, цена
сборки сообщения в Celery, цена событий Flower и цена подтверждения публикации в AMQP.

Запуск ВНУТРИ контейнера (сеть Docker, как у реального продьюсера):

    docker compose exec -T worker-celery python -m bench.diagnose publish
    docker compose exec -T redis redis-cli config resetstat
    docker compose exec -T worker-celery python -m bench.runner --backend celery --kind noop --n 1000 --workers 1
    docker compose exec -T worker-celery python -m bench.diagnose commands   # сколько команд Redis на задачу
"""

import argparse
import time

from celery import Celery
from redis import Redis

from app.core.config import get_settings

N = 2000
PROBE_QUEUE = "probe"  # эту очередь никто не слушает: меряем только публикацию
PROBE_KEY = "bench:probe:list"


def _rate(label: str, seconds: float, n: int = N) -> None:
    print(f"{label:<48} {n / seconds:8.0f} оп/с   ({seconds * 1e6 / n:7.1f} мкс/оп)")


def raw_redis() -> None:
    """Потолок Redis из контейнера: одиночные команды против конвейера."""
    print("--- сырой Redis (без Celery) ---")
    redis = Redis.from_url(get_settings().redis_url)
    redis.delete(PROBE_KEY)

    start = time.perf_counter()
    for _ in range(N):
        redis.ping()
    _rate("PING — чистый round-trip", time.perf_counter() - start)

    start = time.perf_counter()
    for _ in range(N):
        redis.lpush(PROBE_KEY, "x" * 400)
    _rate("LPUSH по одной (так делает транспорт kombu)", time.perf_counter() - start)
    redis.delete(PROBE_KEY)

    start = time.perf_counter()
    pipe = redis.pipeline(transaction=False)
    for i in range(N):
        pipe.lpush(PROBE_KEY, "x" * 400)
        if i % 100 == 99:
            pipe.execute()
    pipe.execute()
    _rate("LPUSH пачками по 100 (pipeline)", time.perf_counter() - start)
    redis.delete(PROBE_KEY)


def publish(label: str, broker: str, sent_event: bool, confirm: bool = False) -> None:
    """Скорость постановки задачи Celery при разных настройках публикации."""
    app = Celery(f"probe-{label}", broker=broker, backend=get_settings().redis_url)
    app.conf.update(
        task_serializer="json",
        accept_content=["json"],
        task_send_sent_event=sent_event,
        task_create_missing_queues=True,
        broker_transport_options={"confirm_publish": True} if confirm else {},
    )
    for _ in range(20):  # прогрев: соединение и объявление очереди
        app.send_task("probe.noop", args=[1], queue=PROBE_QUEUE)

    start = time.perf_counter()
    for _ in range(N):
        app.send_task("probe.noop", args=[1], queue=PROBE_QUEUE)
    _rate(label, time.perf_counter() - start)

    app.control.purge()
    app.close()


def publish_matrix() -> None:
    settings = get_settings()
    redis_broker = "redis://redis:6379/0"
    amqp_broker = "amqp://guest:guest@rabbitmq:5672//"
    print(f"\n--- публикация задачи Celery (backend результатов: {settings.redis_url}) ---")
    publish("Redis,    события Flower вкл (как в матрице)", redis_broker, True)
    publish("Redis,    события выкл", redis_broker, False)
    publish("RabbitMQ, события Flower вкл (как в матрице)", amqp_broker, True)
    publish("RabbitMQ, события выкл", amqp_broker, False)
    publish("RabbitMQ, события выкл + confirm_publish", amqp_broker, False, confirm=True)


def commands() -> None:
    """Сколько команд Redis обслужил с момента `redis-cli config resetstat`."""
    redis = Redis.from_url(get_settings().redis_url)
    stats = redis.info("commandstats")
    rows = sorted(
        ((name.removeprefix("cmdstat_"), int(data["calls"])) for name, data in stats.items()),
        key=lambda item: -item[1],
    )
    total = sum(calls for _, calls in rows)
    print(f"--- команды Redis с последнего resetstat: всего {total} ---")
    for name, calls in rows[:15]:
        print(f"{name:<16} {calls:>8}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("what", choices=["publish", "commands", "all"])
    args = parser.parse_args()
    if args.what in ("publish", "all"):
        raw_redis()
        publish_matrix()
    if args.what in ("commands", "all"):
        commands()


if __name__ == "__main__":
    main()
