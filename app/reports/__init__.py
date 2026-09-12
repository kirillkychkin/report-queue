"""Домен: генерация отчётов о продажах (данные → агрегаты → CSV/PNG/PDF)."""

from app.reports.service import STEPS, generate_report

__all__ = ["STEPS", "generate_report"]
