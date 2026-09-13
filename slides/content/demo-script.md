# Пошаговый сценарий демонстрации проекта

> Демонстрация рассчитана на 5 минут. Все команды проверены на чистом стенде
> (`docker compose down -v` → `up --build` → полный прогон). Курсив — пометки себе; текст в кавычках — что говорить.

## Подготовка (до выступления, в хронометраж не входит)

1. Проверить, что запущен Docker Desktop: `docker info` отвечает без ошибок.
2. Поднять стек заранее, чтобы не ждать сборку при зрителях:

```
cd report-queue
cp .env.example .env
docker compose up -d --build
docker compose ps
```

3. Дождаться, пока `api`, `redis`, `redis-backend`, `rabbitmq` покажут `healthy` (всего 8 контейнеров).
4. Прогреть страницу: открыть `http://localhost:8000` и один раз заказать отчёт — тогда на выступлении
   первый запрос не будет ждать инициализации.
5. Открыть вкладки браузера в таком порядке: **приложение** (`localhost:8000`), **Flower** (`localhost:5555`),
   **RabbitMQ Management** (`localhost:15672`, guest / guest).
6. Терминал — в каталоге проекта, шрифт крупный (Ctrl + «+»), окно на половину экрана рядом с браузером.
7. Открыть в редакторе три файла на случай вопросов: `app/tasks/backend.py`, `app/tasks/celery_tasks.py`,
   `app/tasks/raw_amqp.py`.
8. Команды ниже — для bash (Git Bash). В PowerShell переменная задаётся иначе:
   `$env:BROKER_URL="amqp://guest:guest@rabbitmq:5672//"; docker compose up -d worker-celery api flower`,
   после — `Remove-Item Env:BROKER_URL`.

**Проверка готовности одной командой:**

```
curl -s localhost:8000/health
```

Ожидаемый ответ: `{"status":"ok","backend":"celery","broker":"redis://redis:6379/0"}`.

## Шаг 0 (0:00–0:20). Что это за проект

*Показать слайд «Задача и домен» из `project.pptx`, терминал и браузер уже открыты.*

> «Это сервис генерации отчётов. Пользователь заказывает отчёт, API отвечает мгновенно кодом 202 и
> идентификатором, а сам отчёт — CSV, график и PDF — строит фоновый воркер. Под одним и тем же кодом
> может работать Celery или RQ, а под Celery — Redis или RabbitMQ. Сейчас покажу поток задачи,
> отказоустойчивость, масштабирование, переключение брокера и то, как это выглядит без фреймворка».

## Шаг 1 (0:20–1:20). Поток задачи: заказ → прогресс → файл

**Что делать:** в браузере на `localhost:8000` задать 200 000 строк, группировку «по регионам», нажать «Заказать».

**Что показывать:** как в списке появляется запись со статусом, как прогресс идёт 20 → 55 → 100 %,
как появляются ссылки на CSV, PNG и PDF. Открыть PDF — там таблица и график.

> «Обратите внимание: ответ пришёл мгновенно — API не ждал генерации. Страница опрашивает статус
> раз в 400 миллисекунд: сначала PENDING, потом PROGRESS с процентом и названием шага, потом SUCCESS
> со ссылками. Прогресс воркер пишет в result backend, а файлы кладёт на общий том — в сообщении
> передаются только идентификатор и параметры, сами данные через брокер не ходят».

**Затем в терминале:**

```
docker compose logs worker-celery --tail 5
```

> «Со стороны воркера это две строки: `received` и `succeeded` с временем выполнения».

**Если не сработало:** статус завис в PENDING → `docker compose logs worker-celery --tail 20`;
скорее всего, воркер не поднялся, лечится `docker compose up -d worker-celery`.

## Шаг 2 (1:20–2:20). Очередь и горизонтальное масштабирование

**Что делать:** открыть `http://localhost:8000/?demo=8` — страница сразу заказывает восемь отчётов.

> «Восемь заказов ушли одновременно. Воркер один и берёт по одной задаче — `prefetch_multiplier=1`.
> Остальные ждут в очереди: вот она, работа буфера между API и обработкой».

**Дальше — добавить воркеров:**

```
docker compose up -d --scale worker-celery=4 --no-recreate
```

*Подождать 5–8 секунд, обновить `/?demo=8`, переключиться на вкладку Flower.*

> «Мы не меняли ни строки кода — просто запустили ещё три контейнера. Во Flower видно четыре воркера,
> и задачи разбираются параллельно. Координации между воркерами нет: брокер сам раздаёт. В эксперименте
> переход с одного воркера на четыре давал ускорение в три–четыре раза».

**Если не сработало:** Flower показывает старых воркеров → `docker compose restart flower`
(это кэш событий, на работу задач не влияет).

## Шаг 3 (2:20–3:20). Надёжность: повторы и потеря воркера

**Часть А — повторы.**

```
NOTIFY_FAILURE_RATE=0.9 docker compose up -d --scale worker-celery=1 worker-celery
docker compose exec -T worker-celery python scripts/produce.py pipeline
```

> «Здесь цепочка из двух задач: сначала отчёт, потом уведомление. Канал уведомлений я специально
> сделал ненадёжным — он падает с вероятностью 90 %. Смотрите вывод: `retries: 3` — задача трижды
> упала и на четвёртый раз прошла».

*Показать лог:*

```
docker compose logs worker-celery --tail 15
```

> «В логах видно `Retry in 1s`, `Retry in 7s` — экспоненциальный backoff с джиттером. Джиттер нужен,
> чтобы повторы от разных воркеров не пришли одной стеной в момент, когда внешний сервис только поднялся».

**Часть Б — потеря воркера.**

```
curl -s -X POST localhost:8000/reports -H 'Content-Type: application/json' -d '{"rows":1000000}'
docker compose kill -s SIGQUIT worker-celery
docker compose logs worker-celery --tail 3
```

> «Я заказал большой отчёт и убил воркер прямо во время выполнения. В логах — `Cold shutdown`
> и `Restoring 1 unacknowledged message(s)`: задача не подтверждена, поэтому вернулась в очередь».

```
sleep 5 && docker compose up -d worker-celery
```

*Обновить страницу в браузере — отчёт готов.*

> «Воркер поднялся, получил ту же задачу с тем же идентификатором и выполнил её заново. Именно поэтому
> задачи обязаны быть идемпотентными: тот же `report_id`, тот же seed, файлы перезаписываются тем же
> содержимым. Это цена гарантии at-least-once».

**Важная оговорка, которую стоит проговорить самому** (это же и заготовка под вопрос):

> «Так работает холодная остановка, когда процесс успевает закрыть соединение. Если убить процесс
> жёстко — SIGKILL, выдернутый шнур, — соединение остаётся висеть, и задача вернётся только когда это
> заметит брокер: на RabbitMQ по heartbeat, до нескольких минут, а на Redis по visibility timeout,
> который по умолчанию равен часу. Я это проверял отдельно».

**Если не сработало:** после `kill` воркер не поднялся — контейнер не успел выйти;
подождать 5 секунд и повторить `docker compose up -d worker-celery`.

## Шаг 4 (3:20–4:20). Переключение брокера и фреймворка

**Часть А — Celery на RabbitMQ:**

```
BROKER_URL=amqp://guest:guest@rabbitmq:5672// docker compose up -d worker-celery api flower
curl -s localhost:8000/health
```

*Дождаться `"broker":"rabbitmq:5672//"`, заказать отчёт через `/?demo=3`, переключиться на вкладку
RabbitMQ Management → Queues.*

> «Код задач не изменился ни на строку — поменялась одна переменная окружения. В Management UI видны
> очереди `reports` и `notifications`, число потребителей и скорость. Разница с Redis не в скорости,
> а в гарантиях: настоящие ack и nack, durable-очереди, dead-letter, prefetch на стороне брокера».

**Часть Б — переключение на RQ:**

```
TASK_BACKEND=rq docker compose up -d api
curl -s localhost:8000/health
```

*Дождаться `"backend":"rq"`, заказать отчёт через `/?demo=3`.*

> «Теперь под тем же API работает RQ — другой фреймворк, другой воркер, тот же результат.
> API обращается к очереди через протокол `TaskBackend` из двух методов и не знает, что под ним.
> Именно это позволило честно сравнить фреймворки в эксперименте — сравнивались две очереди,
> а не два разных приложения».

**Вернуться к исходной конфигурации:**

```
docker compose up -d
```

## Шаг 5 (4:20–4:50). Под капотом: producer/consumer на голом pika

```
docker compose exec -T -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery python -m app.tasks.raw_amqp produce 6
docker compose exec -T -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery timeout 8 python -m app.tasks.raw_amqp consume
```

*Показать вывод: часть сообщений обрабатывается, часть помечена `poison=True` и после трёх попыток
уходит в dead-letter.*

> «Это то же самое, но без фреймворка — сто строк на pika. Здесь руками сделано всё, что Celery делает
> за нас: объявлена durable-очередь, сообщения persistent, prefetch равен единице, подтверждение
> отправляется вручную после обработки. И обработка ядовитых сообщений: в заголовке счётчик попыток,
> после третьей сообщение уходит в dead-letter очередь `raw.reports.dead`, а не крутится вечно,
> занимая воркер».

*Если останется время — показать в Management UI очередь `raw.reports.dead` с накопленными сообщениями.*

## Шаг 6 (4:50–5:00). Итог

> «Итого: очередь задач — это буфер, повторы и горизонтальное масштабирование, купленные ценой
> идемпотентности и гарантии at-least-once. Celery берут, когда нужны маршрутизация, пайплайны и
> RabbitMQ; RQ — когда достаточно Redis и хочется простоты. Всё, что я показал, измерено — цифры
> в эксперименте, а проект поднимается одной командой».

## План Б

| Что случилось | Что делать |
|---|---|
| Docker не стартовал или контейнеры не поднимаются | Показать заранее записанные скриншоты/видео прогона, рассказывать по слайдам `project.pptx`; код открыть в редакторе |
| Нет сети (не скачались образы) | Образы уже собраны локально — проверить заранее `docker images \| grep report-queue` |
| Порт занят (8000, 5555, 15672) | Остановить конкурента или поменять порт в `docker-compose.yml`, пересобрать не нужно |
| Отчёт долго генерируется на слабой машине | Уменьшить число строк: 50 000 вместо 200 000 |
| Совсем мало времени (2 минуты вместо 5) | Оставить шаги 1, 2 и часть Б шага 3 — заказ, масштабирование, восстановление после падения |

## Шпаргалка: все команды подряд

```
# подготовка
docker compose up -d --build
curl -s localhost:8000/health

# шаг 1 — поток задачи
docker compose logs worker-celery --tail 5

# шаг 2 — масштабирование
docker compose up -d --scale worker-celery=4 --no-recreate

# шаг 3 — повторы и потеря воркера
NOTIFY_FAILURE_RATE=0.9 docker compose up -d --scale worker-celery=1 worker-celery
docker compose exec -T worker-celery python scripts/produce.py pipeline
curl -s -X POST localhost:8000/reports -H 'Content-Type: application/json' -d '{"rows":1000000}'
docker compose kill -s SIGQUIT worker-celery
docker compose logs worker-celery --tail 3
sleep 5 && docker compose up -d worker-celery

# шаг 4 — переключение
BROKER_URL=amqp://guest:guest@rabbitmq:5672// docker compose up -d worker-celery api flower
TASK_BACKEND=rq docker compose up -d api
docker compose up -d

# шаг 5 — pika
docker compose exec -T -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery python -m app.tasks.raw_amqp produce 6
docker compose exec -T -e BROKER_URL=amqp://guest:guest@rabbitmq:5672// worker-celery timeout 8 python -m app.tasks.raw_amqp consume

# после выступления
docker compose down
```
