"""Ручной продьюсер для демонстрации очередей без API.

Запуск внутри работающего контейнера (нужен доступ к брокеру по имени сервиса):
    docker compose exec -T worker-celery python scripts/produce.py single
    docker compose exec -T worker-celery python scripts/produce.py pipeline   # chain: отчёт → notify с retry
    docker compose exec -T worker-celery python scripts/produce.py batch 4    # chord: 4 отчёта → сводка
    docker compose exec -T worker-rq     python scripts/produce.py single   --backend rq
    docker compose exec -T worker-rq     python scripts/produce.py pipeline --backend rq   # depends_on + Retry

Статус отслеживается через единый TaskBackend — так же, как это делает API.
"""

import argparse
import time
import uuid

from app.core.models import ReportParams, ReportState
from app.tasks.backend import get_backend

TERMINAL = {ReportState.SUCCESS, ReportState.FAILURE}


def wait(backend, report_id: str, timeout: float = 120.0) -> None:
    """Опрос статуса с печатью изменений — то, что в UI делает polling."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status = backend.get_status(report_id)
        snapshot = (status.state, status.progress, status.step)
        if snapshot != last:
            print(f"  [{report_id}] {status.state} {status.progress}% {status.step or ''} {status.error or ''}")
            last = snapshot
        if status.state in TERMINAL:
            return
        time.sleep(0.2)
    raise TimeoutError(report_id)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["single", "pipeline", "batch"], nargs="?", default="single")
    parser.add_argument("n", type=int, nargs="?", default=3)
    parser.add_argument("--backend", choices=["celery", "rq"], default=None, help="по умолчанию TASK_BACKEND")
    args = parser.parse_args()

    backend = get_backend(args.backend)
    params = ReportParams(rows=20_000, group_by="region")
    print(f"backend={backend.name}")

    if args.mode == "single":
        report_id = f"single-{uuid.uuid4().hex[:8]}"
        backend.enqueue_report(report_id, params)
        wait(backend, report_id)

    elif args.mode == "pipeline":
        report_id = f"pipe-{uuid.uuid4().hex[:8]}"
        if backend.name == "celery":
            from app.tasks import celery_tasks as t

            notify = t.enqueue_report_with_notification(report_id, params)
            wait(backend, report_id)
            print("notify result:", t.wait_result(notify))
        else:
            from app.tasks import rq_app

            notify = rq_app.enqueue_report_with_notification(report_id, params)
            wait(backend, report_id)
            while notify.get_status() not in ("finished", "failed"):
                time.sleep(0.5)
            print("notify result:", notify.get_status(), notify.return_value())

    elif args.mode == "batch":
        if backend.name != "celery":
            parser.exit(message="batch (chord) доступен только в Celery — у RQ нет canvas; аналог — N отдельных job + depends_on на всех\n")
        from app.tasks import celery_tasks as t

        items = [(f"batch-{uuid.uuid4().hex[:6]}-{i}", params.model_copy(update={"seed": i})) for i in range(args.n)]
        print("summary:", t.wait_result(t.enqueue_batch(items)))


if __name__ == "__main__":
    main()
