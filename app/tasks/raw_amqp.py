"""Producer/consumer на «голом» AMQP (pika) — что Celery делает за нас под капотом.

    python -m app.tasks.raw_amqp produce 5     # опубликовать 5 заказов отчётов
    python -m app.tasks.raw_amqp consume       # обрабатывать до Ctrl+C

Демонстрирует базовые примитивы надёжной очереди задач:
- durable queue + persistent message: переживают рестарт брокера;
- ручной ack ПОСЛЕ обработки (at-least-once): упал consumer — сообщение вернётся в очередь;
- prefetch_count=1: брокер не отдаёт следующее сообщение, пока текущее не подтверждено
  (честное распределение между consumer'ами);
- dead-letter exchange: «ядовитое» сообщение после N неудач уходит в отдельную очередь,
  а не крутится в бесконечном цикле.
Всё это в Celery — настройки task_acks_late, worker_prefetch_multiplier, delivery_mode и очередь с DLX.
"""

import json
import logging
import sys
import uuid

import pika

from app.core.config import get_settings
from app.core.models import ReportParams
from app.reports.service import generate_report

log = logging.getLogger("raw_amqp")

# Отдельные имена, чтобы не конфликтовать с очередями Celery (у них свои аргументы объявления)
QUEUE = "raw.reports"
DLX = "raw.reports.dlx"
DEAD_QUEUE = "raw.reports.dead"
MAX_ATTEMPTS = 3


def connect() -> tuple[pika.BlockingConnection, pika.adapters.blocking_connection.BlockingChannel]:
    url = get_settings().broker_url
    if not url.startswith("amqp"):
        sys.exit(f"raw_amqp требует BROKER_URL=amqp://..., сейчас {url}")
    # kombu (Celery) трактует хвост «//» как vhost «/», pika — как пустой vhost: приводим к виду pika
    if url.endswith("//"):
        url = url[:-1]
    connection = pika.BlockingConnection(pika.URLParameters(url))
    channel = connection.channel()
    declare_topology(channel)
    return connection, channel


def declare_topology(channel) -> None:
    """Объявления идемпотентны — их делают и producer, и consumer (кто первый поднялся)."""
    channel.exchange_declare(exchange=DLX, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=DEAD_QUEUE, durable=True)
    channel.queue_bind(queue=DEAD_QUEUE, exchange=DLX)
    channel.queue_declare(
        queue=QUEUE,
        durable=True,
        arguments={"x-dead-letter-exchange": DLX},  # отклонённые (nack, requeue=False) → DLX
    )


# --- producer ---


def produce(count: int) -> None:
    connection, channel = connect()
    for i in range(count):
        report_id = f"raw-{uuid.uuid4().hex[:8]}"
        params = ReportParams(rows=5_000, group_by="category", seed=i)
        # Каждое третье сообщение — «ядовитое»: воспроизводит poison message
        body = {"report_id": report_id, "params": params.model_dump(mode="json"), "poison": i % 3 == 2}
        channel.basic_publish(
            exchange="",  # default exchange: routing_key == имя очереди
            routing_key=QUEUE,
            body=json.dumps(body).encode(),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=pika.DeliveryMode.Persistent,  # сообщение на диск
                message_id=report_id,
                headers={"x-attempt": 1},
            ),
        )
        log.info("published %s poison=%s", report_id, body["poison"])
    connection.close()


# --- consumer ---


def consume() -> None:
    connection, channel = connect()
    channel.basic_qos(prefetch_count=1)

    def on_message(ch, method, properties, body: bytes) -> None:
        msg = json.loads(body)
        attempt = (properties.headers or {}).get("x-attempt", 1)
        log.info("received %s attempt=%d", msg["report_id"], attempt)
        try:
            if msg.get("poison"):
                raise ValueError("poison message: cannot be processed")
            result = generate_report(msg["report_id"], ReportParams.model_validate(msg["params"]))
            ch.basic_ack(delivery_tag=method.delivery_tag)  # ack только после успешной обработки
            log.info("done %s rows=%d in %.2fs", result.report_id, result.rows, result.duration_sec)
        except Exception as exc:  # noqa: BLE001
            if attempt >= MAX_ATTEMPTS:
                # В DLX: requeue=False + x-dead-letter-exchange у очереди
                ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
                log.error("dead-lettered %s after %d attempts: %s", msg["report_id"], attempt, exc)
            else:
                # Повтор: переопубликовать с увеличенным счётчиком и подтвердить оригинал.
                # (basic_nack(requeue=True) вернул бы сообщение без счётчика — бесконечный цикл.)
                headers = {**(properties.headers or {}), "x-attempt": attempt + 1}
                ch.basic_publish(
                    exchange="",
                    routing_key=QUEUE,
                    body=body,
                    properties=pika.BasicProperties(
                        content_type=properties.content_type,
                        delivery_mode=pika.DeliveryMode.Persistent,
                        message_id=properties.message_id,
                        headers=headers,
                    ),
                )
                ch.basic_ack(delivery_tag=method.delivery_tag)
                log.warning("retry %s -> attempt %d: %s", msg["report_id"], attempt + 1, exc)

    channel.basic_consume(queue=QUEUE, on_message_callback=on_message, auto_ack=False)
    log.info("consuming %s (prefetch=1, manual ack, DLX=%s). Ctrl+C to stop", QUEUE, DLX)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
    finally:
        connection.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    mode = sys.argv[1] if len(sys.argv) > 1 else "consume"
    if mode == "produce":
        produce(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif mode == "consume":
        consume()
    else:
        sys.exit(__doc__)
