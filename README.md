# report-queue

Демо-проект к докладу **«Очереди сообщений и фоновые задачи в Python»**
(дисциплина «Разработка приложений на Python»).

Пользователь заказывает отчёт через HTTP API, отчёт генерируется в фоне воркером
(CSV + график PNG + PDF), API отдаёт статус и файлы. Одно и то же приложение работает
поверх **Celery** или **RQ**, а Celery — поверх **Redis** или **RabbitMQ**; это позволяет
сравнить подходы на одном коде.

## Быстрый старт

```bash
cp .env.example .env
docker compose up --build
```

| Что | Где |
|---|---|
| Веб-страница (заказ отчётов, прогресс, скачивание) | http://localhost:8000 — `/?demo=5` сразу заказывает 5 отчётов |
| OpenAPI | http://localhost:8000/docs |
| Flower (мониторинг Celery) | http://localhost:5555 |
| RabbitMQ Management | http://localhost:15672 (guest / guest) |

Масштабирование воркеров: `docker compose up -d --scale worker-celery=4`.

## Переключение бэкенда и брокера

В `.env`:

```dotenv
TASK_BACKEND=celery          # или rq
BROKER_URL=redis://redis:6379/0                    # Celery на Redis
# BROKER_URL=amqp://guest:guest@rabbitmq:5672//    # Celery на RabbitMQ
RESULT_BACKEND_URL=redis://redis-backend:6379/0    # результаты — на отдельном Redis
CELERY_EVENTS=true           # события для Flower (в бенчмарке выключаются)
BROKER_CONFIRM_PUBLISH=false # publisher confirms для RabbitMQ
```

затем `docker compose up -d api worker-celery beat flower` (RQ всегда работает через `REDIS_URL`).

## Структура

```
app/api/        FastAPI: POST /reports → 202, GET /reports/{id}, GET /reports/{id}/download, /health, HTML-страница
app/core/       конфиг (pydantic-settings), модели, файловое хранилище артефактов
app/reports/    домен: синтетические продажи → агрегаты → CSV/PNG/PDF (не знает про очереди)
app/tasks/
  backend.py    TaskBackend — единый интерфейс к Celery и RQ для API
  celery_app.py / celery_tasks.py   Celery: acks_late, prefetch=1, две очереди, retry+backoff, chain/chord, beat
  rq_app.py     RQ: те же задачи, depends_on, Retry (нужен scheduler), прогресс через job.meta
  raw_amqp.py   producer/consumer на pika: durable, manual ack, prefetch, DLX — «как это работает под капотом»
scripts/produce.py   ручной продьюсер: single | pipeline | batch, --backend celery|rq
bench/          эксперимент: runner.py (3 фазы замера), run_matrix.py (матрица), diagnose.py (разбор цены публикации),
                plot.py (графики), results/ (данные; v1/ — первая версия до аудита) — см. docs/experiment.md
docs/           архитектура, эксперимент, выводы, сценарий демо
slides/         презентации: content/*.md (тексты) + builder.py (рендер) → talk.pptx, project.pptx
tests/unit      домен, задачи (eager), нормализация статусов    →  .venv/Scripts/python -m pytest tests/unit
tests/integration   API против запущенного compose               →  .venv/Scripts/python -m pytest tests/integration
```

## Ручные сценарии

```bash
docker compose exec -T worker-celery python scripts/produce.py pipeline          # Celery chain + retry
docker compose exec -T worker-celery python scripts/produce.py batch 4           # Celery chord
docker compose exec -T worker-rq     python scripts/produce.py pipeline --backend rq   # RQ depends_on + Retry
# pika (нужен RabbitMQ): в одном терминале consume, в другом produce
docker compose exec -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery python -m app.tasks.raw_amqp consume
docker compose exec -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery python -m app.tasks.raw_amqp produce 6
```

## Эксперимент

Матрица: 5 конфигураций (`celery-redis`, `celery-rabbitmq`, `celery-rabbitmq-confirm`, `rq-redis`, `rq-simple-redis`)
× 3 вида задач (`noop`, `cpu_small`, `io_sleep`) × 1/2/4 воркера × 3 повтора = 135 прогонов (~90 минут).
Каждый прогон разбит на фазы: постановка в остановленную очередь → разбор заранее наполненной очереди →
замер задержки одиночных задач на пустой очереди.

```bash
.venv/Scripts/python -m bench.run_matrix              # полная матрица (фоном: таймаут терминала)
.venv/Scripts/python -m bench.run_matrix --quick      # дымовой прогон, ~3 минуты
.venv/Scripts/python -m bench.plot                    # графики и таблицы в bench/results/
docker compose exec -T api python -m bench.diagnose publish   # из чего складывается цена постановки
```

Ключевые цифры (медианы, 1 → 4 воркера): накладные расходы очереди **1,8 мс** на задачу у Celery,
3,5 мс у RQ `SimpleWorker`, **72 мс** у RQ с `fork`; `noop` 434 → 1497 (Celery+Redis) против 560 → 1744
(Celery+RabbitMQ); на задачах от 50 мс все конфигурации без fork неразличимы (20 → 67); масштабирование ×3,0–4,1.
Постановка: 1583 msg/s на Redis, 2333 на RabbitMQ — но с publisher confirms всего **210 msg/s**.
Подробности, оговорки и разбор ошибок первой версии методики — в `docs/experiment.md`.

## Документы

- `docs/architecture.md` — архитектура и обоснование решений
- `docs/experiment.md` — эксперимент: Celery vs RQ, Redis vs RabbitMQ
- `docs/conclusions.md` — выводы: когда применять, плюсы, ограничения
- `docs/demo.md` — сценарий 5-минутной демонстрации

## Презентации

`slides/talk.pptx` — доклад (10–15 мин, 21 слайд), `slides/project.pptx` — демонстрация проекта (5 мин, 9 слайдов).
Тексты слайдов и заметки докладчика лежат в `slides/content/*.md`, оформление — в `slides/builder.py`; графики
подтягиваются из `bench/results/`. Пересборка после правки текстов:

```bash
.venv/Scripts/python slides/build_talk.py
.venv/Scripts/python slides/build_project.py
```
