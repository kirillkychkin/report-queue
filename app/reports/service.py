"""Единая точка входа генерации отчёта.

Эту функцию вызывают и задача Celery, и задача RQ, и бенчмарк — доменная
логика не знает о фреймворке очередей. Прогресс сообщается через callback,
который каждый фреймворк реализует по-своему (update_state / job.meta).
"""

import time
from collections.abc import Callable

from app.core.models import ReportParams, ReportResult
from app.core.storage import report_dir
from app.reports.aggregate import aggregate_sales, monthly_trend
from app.reports.data import generate_sales
from app.reports.render import FILENAMES, render_csv, render_pdf, render_png

ProgressCallback = Callable[[int, str], None]

# Шаги пайплайна и доля прогресса после каждого — чтобы UI показывал осмысленный процент
STEPS: list[tuple[str, int]] = [
    ("generate_data", 20),
    ("aggregate", 40),
    ("render_csv", 55),
    ("render_png", 75),
    ("render_pdf", 100),
]


def generate_report(
    report_id: str,
    params: ReportParams,
    progress_cb: ProgressCallback | None = None,
) -> ReportResult:
    """Сгенерировать данные → агрегировать → отрисовать CSV/PNG/PDF.

    Идемпотентна: повторный вызов с тем же report_id и params перезаписывает
    те же файлы тем же содержимым (фиксированный seed), поэтому дубликат
    сообщения при at-least-once доставке безопасен.
    """
    started = time.perf_counter()
    report = lambda pct, step: progress_cb(pct, step) if progress_cb else None  # noqa: E731

    out_dir = report_dir(report_id, create=True)

    df = generate_sales(params.rows, params.date_from, params.date_to, params.seed)
    report(*_after("generate_data"))

    summary = aggregate_sales(df, params.group_by)
    trend = monthly_trend(df)
    report(*_after("aggregate"))

    render_csv(summary, out_dir)
    report(*_after("render_csv"))

    render_png(summary, params.group_by, params.title, out_dir)
    report(*_after("render_png"))

    render_pdf(summary, trend, params.group_by, params.title, out_dir)
    report(*_after("render_pdf"))

    return ReportResult(
        report_id=report_id,
        artifacts=dict(FILENAMES),
        rows=len(df),
        total_amount=float(df["amount"].sum()),
        duration_sec=round(time.perf_counter() - started, 3),
    )


def _after(step: str) -> tuple[int, str]:
    """(процент, имя шага) после завершения шага."""
    return dict(STEPS)[step], step
