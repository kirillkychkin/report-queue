"""Доменные модели, общие для API, воркеров и бенчмарка.

Модели сериализуются в JSON и передаются через брокер, поэтому в них нет
ничего, кроме простых типов: сообщение в очереди должно быть маленьким и
независимым от кода воркера.
"""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

GroupBy = Literal["region", "category", "month"]


class ReportParams(BaseModel):
    """Параметры заказа отчёта — то, что пользователь передаёт в API."""

    rows: int = Field(default=10_000, ge=100, le=1_000_000, description="Размер синтетического набора продаж")
    group_by: GroupBy = Field(default="region", description="Измерение для агрегации")
    date_from: date = Field(default=date(2025, 1, 1))
    date_to: date = Field(default=date(2025, 12, 31))
    seed: int = Field(default=42, description="Seed генератора — отчёт воспроизводим (идемпотентность)")
    title: str = Field(default="Отчёт по продажам", max_length=100)


class ReportState(StrEnum):
    """Состояния отчёта, единые для Celery и RQ (API отдаёт именно их)."""

    PENDING = "PENDING"    # в очереди, воркер ещё не взял
    STARTED = "STARTED"    # выполняется
    PROGRESS = "PROGRESS"  # выполняется, есть процент
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


class ReportStatus(BaseModel):
    """Ответ API о состоянии отчёта — нормализованный поверх AsyncResult/Job."""

    id: str
    state: ReportState
    progress: int = Field(default=0, ge=0, le=100)
    step: str | None = None
    error: str | None = None
    artifacts: dict[str, str] = Field(default_factory=dict, description="формат → имя файла")


class ReportResult(BaseModel):
    """Результат работы воркера — хранится в result backend, а не в сообщении."""

    report_id: str
    artifacts: dict[str, str]
    rows: int
    total_amount: float
    duration_sec: float
