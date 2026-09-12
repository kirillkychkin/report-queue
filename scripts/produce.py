"""Ручной продьюсер для демонстрации Celery без API.

Запуск внутри контейнера (нужен доступ к брокеру по имени сервиса):
    docker compose run --rm --no-deps api python scripts/produce.py single
    docker compose run --rm --no-deps api python scripts/produce.py pipeline   # chain: отчёт → notify с retry
    docker compose run --rm --no-deps api python scripts/produce.py batch 4    # chord: 4 отчёта → сводка
"""

import sys
import uuid

from app.core.models import ReportParams
from app.tasks import celery_tasks as t


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else "single"
    params = ReportParams(rows=20_000, group_by="region")

    if mode == "single":
        report_id = f"single-{uuid.uuid4().hex[:8]}"
        print(f"enqueue {report_id}")
        print("result:", t.wait_result(t.enqueue_report(report_id, params)))

    elif mode == "pipeline":
        report_id = f"pipe-{uuid.uuid4().hex[:8]}"
        print(f"enqueue chain {report_id} -> notify")
        print("notify result:", t.wait_result(t.enqueue_report_with_notification(report_id, params)))

    elif mode == "batch":
        n = int(argv[2]) if len(argv) > 2 else 3
        items = [(f"batch-{uuid.uuid4().hex[:6]}-{i}", params.model_copy(update={"seed": i})) for i in range(n)]
        print(f"enqueue chord of {n} reports")
        print("summary:", t.wait_result(t.enqueue_batch(items)))

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
