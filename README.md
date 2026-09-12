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

- API и веб-страница: http://localhost:8000
- Flower (мониторинг Celery): http://localhost:5555
- RabbitMQ Management: http://localhost:15672 (guest / guest)

Переключение бэкенда задач и брокера — переменные `TASK_BACKEND` и `BROKER_URL` в `.env`.

## Документы

- `docs/architecture.md` — архитектура и обоснование решений
- `docs/experiment.md` — эксперимент: Celery vs RQ, Redis vs RabbitMQ
- `docs/conclusions.md` — выводы: когда применять, плюсы, ограничения
- `docs/demo.md` — сценарий 5-минутной демонстрации
- `slides/` — презентации (доклад и проект)

_Проект в разработке; разделы будут дополняться по этапам._
