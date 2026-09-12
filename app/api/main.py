"""HTTP API: заказ отчёта, статус, скачивание артефактов.

Паттерн «асинхронный запрос»: POST возвращает 202 и идентификатор сразу,
работа выполняется воркером, клиент опрашивает статус (polling). API не знает,
Celery это или RQ — общается через TaskBackend.
"""

import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import FileResponse, HTMLResponse

from app.core.config import get_settings
from app.core.models import ReportParams, ReportState, ReportStatus
from app.core.storage import artifact_path
from app.tasks.backend import TaskBackend, UnknownReport, get_backend

app = FastAPI(
    title="report-queue",
    description="Фоновая генерация отчётов через Celery/RQ поверх Redis/RabbitMQ",
    version="0.1.0",
)

INDEX_HTML = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


@lru_cache
def backend() -> TaskBackend:
    """Один экземпляр бэкенда на процесс: держит соединение/приложение Celery."""
    return get_backend()


def _with_links(report: ReportStatus) -> dict:
    payload = report.model_dump()
    payload["links"] = {
        fmt: f"/reports/{report.id}/download?format={fmt}"
        for fmt in report.artifacts
    } if report.state == ReportState.SUCCESS else {}
    return payload


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return INDEX_HTML


@app.get("/health")
def health(be: TaskBackend = Depends(backend)) -> dict:
    settings = get_settings()
    broker = settings.broker_url if be.name == "celery" else settings.redis_url
    return {
        "status": "ok" if be.ping() else "degraded",
        "backend": be.name,
        "broker": broker.split("@")[-1],  # без учётных данных
    }


@app.post("/reports", status_code=status.HTTP_202_ACCEPTED)
def create_report(params: ReportParams | None = None, be: TaskBackend = Depends(backend)) -> dict:
    """Принять заказ. Тяжёлая работа — в очереди; отвечаем сразу идентификатором."""
    report_id = f"rep-{uuid.uuid4().hex[:10]}"
    be.enqueue_report(report_id, params or ReportParams())
    return {"id": report_id, "status_url": f"/reports/{report_id}", "backend": be.name}


@app.get("/reports/{report_id}")
def get_report(report_id: str, be: TaskBackend = Depends(backend)) -> dict:
    """Статус отчёта. Для Celery неизвестный id выглядит как PENDING — это ограничение result backend."""
    try:
        return _with_links(be.get_status(report_id))
    except UnknownReport:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"report {report_id} not found") from None


@app.get("/reports/{report_id}/download")
def download(report_id: str, format: Literal["csv", "png", "pdf"] = Query("pdf"), be: TaskBackend = Depends(backend)):
    try:
        report = be.get_status(report_id)
    except UnknownReport:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"report {report_id} not found") from None
    if report.state != ReportState.SUCCESS:
        raise HTTPException(status.HTTP_409_CONFLICT, f"report is {report.state}, not ready")
    filename = report.artifacts.get(format)
    path = artifact_path(report_id, filename) if filename else None
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"artifact {format} not found")
    return FileResponse(path, filename=f"{report_id}.{format}")
