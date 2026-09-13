# Сценарий демонстрации (5 минут)

Подготовка **до** выступления (не входит в 5 минут):

```bash
cd report-queue
cp .env.example .env            # TASK_BACKEND=celery, BROKER_URL=redis://redis:6379/0
docker compose up -d --build    # ~1 мин при собранном образе
docker compose ps               # все 8 контейнеров Up, api и брокеры — healthy
```

Открыть вкладки: `http://localhost:8000`, `http://localhost:5555` (Flower), `http://localhost:15672` (RabbitMQ, guest/guest).
Терминал — в каталоге проекта, шрифт крупный. Команды ниже — для bash; в PowerShell переменные задаются так:
`$env:BROKER_URL="amqp://guest:guest@rabbitmq:5672//"; docker compose up -d worker-celery api flower` (и `Remove-Item Env:BROKER_URL` после).

---

## 0:00 — Что показываем (20 с)

> «Приложение: заказ отчёта → 202 сразу → отчёт строит воркер → скачиваем PDF. Один код, под ним Celery или RQ, Redis или RabbitMQ.
> Покажу поток задачи, отказоустойчивость, масштабирование и переключение брокера».

## 0:20 — Поток задачи через UI (1 мин)

1. В браузере `http://localhost:8000` — заказать отчёт на 200 000 строк.
   > «POST вернул 202 и id за миллисекунды. Страница опрашивает `/reports/{id}`: PENDING → PROGRESS 20…100 → SUCCESS».
2. Скачать PDF, показать график.
3. `docker compose logs worker-celery --tail 5` — видно `received` → `succeeded`.
   > «Прогресс — это `update_state(PROGRESS)` в result backend; сам файл — на общем volume, в сообщении только id и параметры».

## 1:20 — Очередь и распределение (1 мин)

1. Открыть `http://localhost:8000/?demo=8` — 8 заказов сразу.
   > «Один воркер с `prefetch_multiplier=1` берёт по одной. Остальные ждут в очереди — это и есть буфер между API и обработкой».
2. В терминале:
   ```bash
   docker compose up -d --scale worker-celery=4 --no-recreate
   ```
   Снова `/?demo=8`. Во Flower — 4 воркера, задачи разбираются параллельно.
   > «Горизонтальное масштабирование — просто ещё контейнеры. Никакой координации: брокер сам раздаёт».

## 2:20 — Отказоустойчивость: retry и chain (1 мин)

```bash
NOTIFY_FAILURE_RATE=0.9 docker compose up -d --scale worker-celery=1 worker-celery   # чтобы retry был виден наверняка
docker compose exec -T worker-celery python scripts/produce.py pipeline
```

> «`chain`: отчёт → уведомление. Канал уведомлений «падает» — в выводе `retries: 3`, в логах `Retry in 1s… 7s`:
> `autoretry_for` + `retry_backoff` + jitter (поэтому интервалы не ровно 2, 4, 8 — jitter их размазывает).
> Теперь `acks_late`: убиваю воркер посреди задачи —»

```bash
curl -s -X POST localhost:8000/reports -H 'Content-Type: application/json' -d '{"rows":1000000}'
docker compose kill -s SIGQUIT worker-celery    # холодная остановка: Celery закрывает соединение
docker compose logs worker-celery --tail 3      # «Cold shutdown» + «Restoring 1 unacknowledged message(s)»
sleep 5 && docker compose up -d worker-celery   # sleep обязателен: контейнер должен успеть выйти
```

> «`Restoring 1 unacknowledged message(s)` — задача вернулась в очередь. Новый воркер получает её снова,
> с тем же `report_id`, и делает заново. Поэтому задачи обязаны быть идемпотентными: тот же id, тот же seed,
> файлы перезаписываются тем же содержимым».

Лог смотреть **до** перезапуска: `docker compose up -d` пересоздаёт контейнер, и вывод убитого воркера пропадает.

После шага можно ничего не восстанавливать вручную: следующая команда демо (`BROKER_URL=… docker compose up -d …`)
пересоздаёт воркер с обычным `NOTIFY_FAILURE_RATE=0.5` из `.env`.

**Важно (и это хороший ответ на вопрос «а если воркер умер внезапно?»):** так работает только *холодная остановка*,
когда процесс успевает закрыть соединение с брокером. Если процесс убить «жёстко» (`docker compose kill`, SIGKILL,
паника ядра), соединение остаётся висеть, и задача вернётся в очередь только когда брокер это заметит:
на **RabbitMQ** — по heartbeat (`broker_heartbeat=120` с, то есть до ~4 минут), на **Redis** — по `visibility_timeout`,
а он по умолчанию **1 час**. Проверено: после `docker compose kill` задача на Redis 45 секунд оставалась в `STARTED`.
Это одна из главных причин выбирать RabbitMQ для продакшена и уменьшать `visibility_timeout` на Redis.

## 3:20 — Переключение: RabbitMQ и RQ (1 мин)

1. Брокер — RabbitMQ:
   ```bash
   BROKER_URL=amqp://guest:guest@rabbitmq:5672// docker compose up -d worker-celery api flower
   ```
   `/?demo=3` → в RabbitMQ Management видны очереди `reports`, `notifications`, ack-rate.
   > «Код задач не изменился. Разница — в гарантиях: настоящие ack/nack, durable-очереди, DLX».
2. Фреймворк — RQ:
   ```bash
   TASK_BACKEND=rq docker compose up -d api
   ```
   `/?demo=3` → страница показывает `backend: rq`, всё работает так же.
   > «API общается через `TaskBackend` — ему всё равно. Но RQ форкает процесс на каждую задачу: это видно в эксперименте».

## 4:20 — Под капотом: pika + dead-letter (40 с)

```bash
docker compose exec -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery python -m app.tasks.raw_amqp produce 6
docker compose exec -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery timeout 8 python -m app.tasks.raw_amqp consume
```

> «Это то, что Celery делает за нас: durable queue, persistent-сообщения, ручной ack после обработки, prefetch=1.
> Каждое третье сообщение „ядовитое“ — после 3 попыток уходит в dead-letter очередь `raw.reports.dead`, а не крутится вечно».

## 5:00 — Итог (одна фраза)

> «Очередь задач — это буфер + retry + масштабирование за счёт идемпотентности и at-least-once. Celery — когда нужны
> маршрутизация, canvas и RabbitMQ; RQ — когда достаточно Redis и хочется простоты; цифры — в эксперименте».

---

## Если что-то пошло не так

| Симптом | Действие |
|---|---|
| Страница показывает PENDING бесконечно | `docker compose logs worker-celery --tail 20`; воркер не поднялся или слушает другой брокер → `docker compose up -d worker-celery` |
| `/health` → `degraded` | брокер не готов: `docker compose ps`, дождаться healthy |
| После `--scale` Flower показывает старые воркеры | это кэш событий; `docker compose restart flower` |
| RQ: 404 на статус сразу после заказа | нормально для RQ только если job уже удалён по TTL; иначе проверить `TASK_BACKEND` у `api` и `worker-rq` |
| После `kill` воркер не поднялся | контейнер ещё не успел выйти, когда пришла `up -d`: подождать 5 с и повторить `docker compose up -d worker-celery` |
| Задача «висит» в `STARTED` после жёсткого `kill` | так и должно быть: соединение не закрыто, Redis вернёт задачу только через `visibility_timeout` (1 ч), RabbitMQ — по heartbeat (до ~4 мин). Для демо используется `kill -s SIGQUIT` |
| Вернуть всё в исходное | `docker compose up -d` (перечитает `.env`); полностью — `docker compose down && docker compose up -d`, с очисткой данных — `down -v` |
